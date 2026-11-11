"""Operator and designer views of one report directory.

``strategy-builder.report.v1`` has two audiences. ``operator`` reads the folder
the runner wrote. ``designer`` reads ``designer/`` when that tree was published,
and otherwise drops operator-only fields from the same files. Ticker, calendar
dates, share scale, and raw prices are not part of a designer view.

This module does not import the runner or ``report.py``. Phase 3 owns
``load_report``. The window follows the same rule so a designer screen cannot
drift ahead of that function.
"""

from __future__ import annotations

import json
import math
import re
import zlib
from pathlib import Path

_MONTH = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
)
# Longer spellings first. A bare year is last so ``2024-01`` is one token.
_DATE_PATTERNS = (
    re.compile(r"(?:19|20)\d{2}-\d{2}-\d{2}(?:[T ][0-2]\d:[0-5]\d(?::[0-5]\d)?)?"),
    re.compile(r"(?:19|20)\d{2}[/.]\d{1,2}[/.]\d{1,2}"),
    re.compile(r"\d{1,2}[/-]\d{1,2}[/-](?:19|20)\d{2}"),
    re.compile(rf"\d{{1,2}}[\s\-]+{_MONTH}[\s,\-]+(?:19|20)\d{{2}}", re.IGNORECASE),
    re.compile(rf"{_MONTH}[\s\-]+\d{{1,2}},?[\s\-]+(?:19|20)\d{{2}}", re.IGNORECASE),
    re.compile(r"(?:19|20)\d{2}-\d{2}(?!\d)"),
    re.compile(r"(?<![A-Za-z0-9])(?:19|20)\d{2}(?![A-Za-z0-9])"),
)

# Keys a designer tree is not allowed to carry. ``cost_basis`` holds the
# invoice scale. Named operator runs still show that object.
DESIGNER_DROP_KEYS = frozenset(
    {
        "symbol",
        "ticker",
        "share_factor",
        "share_scale",
        "price_factor",
        "volume_factor",
        "date_map",
        "source_root",
        "source_glob",
        "operator_note",
        "cost_basis",
        "inspection",
        "sidecar",
        "sidecar_path",
        "last_price",
        "raw_price",
    }
)

# Raw print columns. A published designer tree may contain the scaled pack
# prices the designer is meant to see. An operator folder opened as a designer
# view drops these instead of relabeling them.
PRICE_COLUMNS = frozenset(
    {
        "open",
        "high",
        "low",
        "close",
        "price",
        "fill_price",
        "wap",
        "vwap",
        "last_price",
        "raw_price",
    }
)


def scrub_text(value: str, symbols: tuple[str, ...]) -> str:
    """Remove calendar dates and ticker tokens from one string.

    ISO dates, slash dates, month names, ``YYYY-MM`` keys, and a bare year
    all count. Session ids such as ``S01`` do not.
    """
    text = value
    for pattern in _DATE_PATTERNS:
        text = pattern.sub("", text)
    for symbol in symbols:
        if len(symbol) < 2:
            continue
        text = re.sub(
            rf"(?i)(?<![A-Za-z0-9]){re.escape(symbol)}(?![A-Za-z0-9])",
            "ASSET",
            text,
        )
    return text


def _scrub_obj(value, symbols: tuple[str, ...], *, drop_prices: bool):
    if isinstance(value, dict):
        clean = {}
        for key, item in value.items():
            name = str(key)
            if name in DESIGNER_DROP_KEYS or (drop_prices and name in PRICE_COLUMNS):
                continue
            renamed = scrub_text(name, symbols)
            clean[renamed] = _scrub_obj(item, symbols, drop_prices=drop_prices)
        return clean
    if isinstance(value, list):
        return [_scrub_obj(item, symbols, drop_prices=drop_prices) for item in value]
    if isinstance(value, str):
        return scrub_text(value, symbols)
    return value


def _replace_table(table, columns, rows):
    if table is None:
        return None
    return type(table)(columns=tuple(columns), rows=tuple(rows), source=table.source)


def _scrub_table(table, symbols: tuple[str, ...], *, drop_prices: bool):
    if table is None:
        return None
    columns = []
    for column in table.columns:
        name = str(column)
        if name in DESIGNER_DROP_KEYS or (drop_prices and name in PRICE_COLUMNS):
            continue
        columns.append(name)
    rows = []
    for row in table.rows:
        cleaned = {}
        for column in columns:
            value = row.get(column)
            if isinstance(value, str):
                value = scrub_text(value, symbols)
            cleaned[column] = value
        rows.append(cleaned)
    return _replace_table(table, columns, rows)


def apply_designer(report, symbols: tuple[str, ...], *, redacted: bool) -> None:
    """Mutate ``report`` so a designer screen cannot render operator secrets."""
    report.audience = "designer"
    report.metrics = _scrub_obj(report.metrics, symbols, drop_prices=redacted)
    report.other_fields = _scrub_obj(report.other_fields, symbols, drop_prices=redacted)
    report.benchmarks = _scrub_obj(report.benchmarks, symbols, drop_prices=redacted)
    report.cost_basis = None
    if report.liquidation is not None:
        report.liquidation = _scrub_obj(report.liquidation, symbols, drop_prices=redacted)
    if report.config is not None:
        report.config = _scrub_obj(report.config, symbols, drop_prices=True)
    if report.walkforward is not None:
        report.walkforward = _scrub_obj(report.walkforward, symbols, drop_prices=True)
    if report.fees_breakdown is not None:
        report.fees_breakdown = _scrub_obj(report.fees_breakdown, symbols, drop_prices=False)
    if report.period_returns is not None:
        report.period_returns = _scrub_obj(report.period_returns, symbols, drop_prices=False)
    report.warnings = [scrub_text(str(item), symbols) for item in report.warnings]
    report.missing = [scrub_text(str(item), symbols) for item in report.missing]
    for curve in list(report.curves) + list(report.benchmark_curves):
        curve.sessions = [scrub_text(str(label), symbols) for label in curve.sessions]
        curve.name = scrub_text(str(curve.name), symbols)
        curve.drawdown_note = scrub_text(str(curve.drawdown_note), symbols)
    report.fills = _scrub_table(report.fills, symbols, drop_prices=redacted)
    report.episodes = _scrub_table(report.episodes, symbols, drop_prices=redacted)
    if report.leaderboard is not None:
        report.leaderboard = _scrub_table(report.leaderboard, symbols, drop_prices=redacted)
    cleaned_children = []
    for child in report.children:
        if not isinstance(child, dict):
            cleaned_children.append(child)
            continue
        nested = child.get("report")
        if nested is not None and hasattr(nested, "metrics"):
            apply_designer(nested, symbols, redacted=redacted)
            outer = {key: value for key, value in child.items() if key not in {"report", "path"}}
            outer = _scrub_obj(outer, symbols, drop_prices=True)
            outer.pop("path", None)
            outer["report"] = nested
            cleaned_children.append(outer)
            continue
        cleaned_children.append(_designer_child(child, symbols))
    report.children = cleaned_children


def _leaf_token(value, symbols: tuple[str, ...]) -> str:
    """Last path segment, with dates and tickers already removed. Not a path."""
    text = scrub_text(str(value or ""), symbols).replace("\\", "/").strip()
    if "/" in text:
        text = text.rstrip("/").split("/")[-1]
    if len(text) >= 2 and text[1] == ":":
        text = text[2:].lstrip("/")
    return text.strip()


def _designer_child(child: dict, symbols: tuple[str, ...]) -> dict:
    """A job row a designer may see. No filesystem path and no ticker."""
    cleaned = _scrub_obj({key: value for key, value in child.items() if key != "path"}, symbols, drop_prices=True)
    cleaned.pop("path", None)
    strategy = _leaf_token(cleaned.get("strategy_id"), symbols)
    source = _leaf_token(cleaned.get("source_id"), symbols)
    if strategy or source:
        cleaned["name"] = " · ".join(part for part in (strategy, source) if part)
    elif cleaned.get("name"):
        cleaned["name"] = _leaf_token(cleaned.get("name"), symbols)
    return cleaned


def book_is_anonymized(document) -> bool:
    """True when a metrics or run document says the book was anonymized."""
    if not isinstance(document, dict):
        return False
    if document.get("anonymized") is True or document.get("audience") == "designer":
        return True
    run = document.get("run")
    if isinstance(run, dict) and run.get("anonymized") is True:
        return True
    basis = document.get("cost_basis")
    if isinstance(basis, dict) and basis.get("anonymized") is True:
        return True
    jobs = document.get("jobs")
    if isinstance(jobs, list) and jobs and all(isinstance(job, dict) and job.get("anonymized") is True for job in jobs):
        return True
    return False


def _read_json(path: Path):
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return document if isinstance(document, dict) else None


def _collect_symbols(document, found: list[str]) -> None:
    if not isinstance(document, dict):
        return
    for key in ("symbol", "ticker"):
        value = document.get(key)
        if isinstance(value, str) and value.strip():
            found.append(value.strip())
    arguments = document.get("arguments")
    if isinstance(arguments, dict):
        ticker = arguments.get("ticker")
        if isinstance(ticker, str) and ticker.strip():
            found.append(ticker.strip())
    jobs = document.get("jobs")
    if isinstance(jobs, list):
        for job in jobs:
            if isinstance(job, dict):
                _collect_symbols(job, found)


def peek_symbols(folder: Path) -> tuple[str, ...]:
    """Ticker tokens written on the operator side of ``folder``.

    Used only as a redaction needle. The returned strings are not for display.
    """
    found: list[str] = []
    for name in ("metrics.json", "run_config.json", "run.json"):
        document = _read_json(folder / name)
        if document is not None:
            _collect_symbols(document, found)
    ordered = []
    seen = set()
    for symbol in found:
        key = symbol.casefold()
        if key in seen or len(symbol) < 2:
            continue
        seen.add(key)
        ordered.append(symbol)
    return tuple(ordered)


def peek_scale(folder: Path) -> float | None:
    """Invoice scale, if the operator metrics recorded one. Not for display."""
    document = _read_json(folder / "metrics.json")
    if not isinstance(document, dict):
        return None
    basis = document.get("cost_basis")
    if not isinstance(basis, dict):
        return None
    value = basis.get("share_scale", basis.get("share_factor"))
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def designer_tree(folder: Path) -> bool:
    """True when ``folder`` is a published designer directory."""
    if not folder.is_dir():
        return False
    if (folder / "metrics.json").is_file() or (folder / "leaderboard.csv").is_file():
        return True
    return _nested_jobs(folder)


def _nested_jobs(folder: Path) -> bool:
    try:
        strategies = [path for path in folder.iterdir() if path.is_dir() and path.name not in {"benchmarks", "jobs"}]
    except OSError:
        return False
    for strategy in strategies:
        try:
            sources = list(strategy.iterdir())
        except OSError:
            continue
        for source in sources:
            if not source.is_dir() or source.name in {"benchmarks", "designer"}:
                continue
            if (source / "metrics.json").is_file() or (source / "error.json").is_file():
                return True
    return False


def default_audience(folder: Path) -> str:
    """``designer`` for an anonymized book or a folder that published ``designer/``."""
    if designer_tree(folder / "designer"):
        return "designer"
    for name in ("metrics.json", "run.json"):
        document = _read_json(folder / name)
        if book_is_anonymized(document):
            return "designer"
    return "operator"


def resolve_view(folder: Path, audience: str) -> tuple[str, Path, bool]:
    """Return ``(audience, directory to read, redacted)``.

    ``redacted`` is true when the designer view has no published ``designer/``
    tree and the operator files themselves must be filtered.
    """
    chosen = (audience or "auto").strip().lower()
    if chosen == "auto":
        chosen = default_audience(folder)
    if chosen not in {"operator", "designer"}:
        chosen = "operator"
    if chosen == "designer":
        published = folder / "designer"
        if designer_tree(published):
            return "designer", published, False
        return "designer", folder, True
    return "operator", folder, False


def export_text(path: Path) -> str:
    """Text used to decide whether a designer may receive ``path``.

    Bytes are decoded as latin-1, so a binary file cannot skip the check by
    failing a UTF-8 decode. PNG text chunks are included even when compressed.
    """
    data = path.read_bytes()
    return data.decode("latin-1") + "\n" + _png_text(data)


def _png_text(data: bytes) -> str:
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ""
    parts: list[str] = []
    index = 8
    while index + 8 <= len(data):
        length = int.from_bytes(data[index : index + 4], "big")
        kind = data[index + 4 : index + 8]
        start = index + 8
        end = start + length
        if end + 4 > len(data):
            break
        chunk = data[start:end]
        index = end + 4
        if kind == b"tEXt":
            parts.append(chunk.decode("latin-1", "replace"))
        elif kind == b"zTXt" and b"\x00" in chunk:
            keyword, _, rest = chunk.partition(b"\x00")
            parts.append(keyword.decode("latin-1", "replace"))
            if len(rest) > 1:
                try:
                    parts.append(zlib.decompress(rest[1:]).decode("latin-1", "replace"))
                except zlib.error:
                    parts.append(rest.decode("latin-1", "replace"))
        elif kind == b"iTXt":
            parts.append(chunk.decode("latin-1", "replace"))
        elif kind == b"IEND":
            break
    return "\n".join(parts)


def export_allowed(text: str, *, symbols: tuple[str, ...], scale: float | None) -> bool:
    """False when a file still contains operator-only text."""
    folded = text.casefold()
    if "share_scale" in folded or "price_factor" in folded or "volume_factor" in folded or "date_map" in folded:
        return False
    if any(pattern.search(text) for pattern in _DATE_PATTERNS):
        return False
    for symbol in symbols:
        if len(symbol) >= 2 and symbol.casefold() in folded:
            return False
    if scale is not None and abs(scale - 1.0) > 1e-9:
        rendered = f"{scale:.10g}"
        if rendered in text or str(scale) in text:
            return False
    return True
