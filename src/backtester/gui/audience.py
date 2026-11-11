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
_ORDINAL = r"(?:st|nd|rd|th)"
# Longer spellings first. A bare year is last so ``2024-01`` and ``2024-1-4`` stay one token.
# A year glued to a decimal is a number. A bare year stays only when the whole
# preceding word is "day" (``today 2024`` and ``holiday 2024`` still lose the year).
_BARE_YEAR = re.compile(
    r"(?<![A-Za-z0-9.])(?<!\bday )(?:19|20)\d{2}(?![A-Za-z0-9.])",
    re.IGNORECASE,
)
_DATE_PATTERNS = (
    # ISO, padded or not, with an optional time: 2024-01-04, 2024-1-4.
    re.compile(
        r"(?<!\d)(?:19|20)\d{2}-\d{1,2}-\d{1,2}(?!\d)"
        r"(?:[T ][0-2]\d:[0-5]\d(?::[0-5]\d)?)?"
    ),
    re.compile(r"(?<!\d)(?:19|20)\d{2}[/.]\d{1,2}[/.]\d{1,2}(?!\d)"),
    # Dotted, slashed, or dashed day-month-year: 01.04.2024, 04.01.2024, 1.4.2024.
    re.compile(r"(?<!\d)\d{1,2}[./-]\d{1,2}[./-](?:19|20)\d{2}(?!\d)"),
    re.compile(
        rf"(?<!\d)\d{{1,2}}[\s.\-]+{_MONTH}(?![A-Za-z])\.?[\s,.\-]*(?:19|20)\d{{2}}(?!\d)",
        re.IGNORECASE,
    ),
    # Month and day. A period alone separates them: Jan.4, 2024 and January.4 2024.
    re.compile(
        rf"(?<![A-Za-z]){_MONTH}(?![A-Za-z])(?:\.\s*|\s+)\d{{1,2}}{_ORDINAL}?"
        rf"(?:(?:,\s*|\s+)(?:19|20)\d{{2}})(?!\d)",
        re.IGNORECASE,
    ),
    re.compile(r"(?<!\d)(?:19|20)\d{2}-\d{2}(?!\d)"),
    re.compile(
        rf"(?<![A-Za-z]){_MONTH}(?![A-Za-z])\.?[\s.\-]+(?:19|20)\d{{2}}(?!\d)",
        re.IGNORECASE,
    ),
    _BARE_YEAR,
)
# The allow-list keeps a bare year. A day index or a price such as 2024.50 is not a date.
_EXPORT_DATE_PATTERNS = tuple(pattern for pattern in _DATE_PATTERNS if pattern is not _BARE_YEAR)
_SECRET_TOKENS = ("share_scale", "share_factor", "price_factor", "volume_factor", "date_map")
_SECRET_WORDS = re.compile(
    r"(?i)(?<![A-Za-z0-9_])(?:share_scale|share_factor|price_factor|volume_factor|date_map)(?![A-Za-z0-9_])"
)
# A path may contain spaces (``Strategy Builder/QQQ/data.csv``). After the drive,
# UNC, or POSIX prefix, keep going until a quote, a comma, or the end of the line.
_ABS_PATH = re.compile(
    r"(?:[A-Za-z]:[\\/][^'\"<>|*?,\r\n]*)"
    r"|(?:\\\\[^\\/'\"<>|*?,\r\n]+[\\/][^'\"<>|*?,\r\n]*)"
    r"|(?:[/]{2}[^/'\"<>|*?,\r\n]+/[^'\"<>|*?,\r\n]*)"
    # A letter in the final segment keeps a slash date such as 2024/1/4 from
    # being read as a path. Spaces still belong to the path, through a comma.
    r"|(?:/(?:[^/'\"<>|*?,\r\n]+/)+[^/'\"<>|*?,\r\n]*[A-Za-z][^/'\"<>|*?,\r\n]*)"
)
# A scale value is its own number, not a digit inside ``long_2x`` or ``2.5``.
_NUMBER_TOKEN = re.compile(
    r"(?<![A-Za-z0-9_])[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?(?![A-Za-z0-9_])"
)
_MONTH_WORDS = frozenset(
    "jan january feb february mar march apr april may jun june jul july aug august "
    "sep sept september oct october nov november dec december".split()
)
_MONTH_WORD = re.compile(rf"(?<![A-Za-z]){_MONTH}(?![A-Za-z])", re.IGNORECASE)
_NEAR_YEAR = re.compile(r"(?<![A-Za-z0-9.])(?:19|20)\d{2}(?![A-Za-z0-9.])")
_FREE_TOKEN = re.compile(r"[A-Za-z0-9_.]+")
_FREE_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
_SESSION_ID = re.compile(r"[Ss]\d+")
_YEAR_TOKEN = re.compile(r"(?:19|20)\d{2}")
_PLAIN_NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")
_DOTTED_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_-]*(?:\.[A-Za-z0-9_-]+)+")

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


def _casefold_index(text: str) -> tuple[str, list[int]]:
    """Casefold ``text`` and remember which original character each fold came from.

    The same index the report token rule uses. ``ß`` becomes ``ss`` and ``ﬁ``
    becomes ``fi``, and a hit can be mapped back onto ``text``.
    """
    folded: list[str] = []
    origins: list[int] = []
    for index, char in enumerate(text):
        piece = char.casefold()
        folded.append(piece)
        origins.extend([index] * len(piece))
    return "".join(folded), origins


def _ticker_spans(text: str, symbol: str) -> list[tuple[int, int]]:
    """Original spans where ``symbol`` is its own token, matched on casefold.

    The boundary is a letter, not a digit: ``QQQ1`` matches ``QQQ`` and ``MA``
    does not match inside ``max_drawdown``. Matching uses ``str.casefold``, so
    ``ß`` matches ``SS`` and ``ﬁ`` matches ``FI``, including a fold that is
    longer than the original character.
    """
    if not text or not symbol:
        return []
    needle = symbol.casefold()
    if not needle:
        return []
    folded, origins = _casefold_index(text)
    spans: list[tuple[int, int]] = []
    start = 0
    limit = len(folded)
    size = len(needle)
    while start <= limit - size:
        found = folded.find(needle, start)
        if found < 0:
            break
        end = found + size
        before = found == 0 or not folded[found - 1].isalpha()
        after = end == limit or not folded[end].isalpha()
        if not before or not after:
            start = found + 1
            continue
        orig_start = origins[found]
        orig_end = origins[end - 1] + 1
        if orig_end > orig_start and (not spans or orig_start >= spans[-1][1]):
            spans.append((orig_start, orig_end))
        start = end
    return spans


def _replace_ticker(text: str, symbol: str) -> str:
    spans = _ticker_spans(text, symbol)
    if not spans:
        return text
    pieces: list[str] = []
    last = 0
    for start, end in spans:
        pieces.append(text[last:start])
        pieces.append("ASSET")
        last = end
    pieces.append(text[last:])
    return "".join(pieces)


def _symbol_order(symbols: tuple[str, ...]) -> tuple[str, ...]:
    """Longer casefolds first, so ``SS`` is considered before ``S``."""
    ranked = sorted((symbol for symbol in symbols if symbol), key=lambda item: len(item.casefold()), reverse=True)
    ordered: list[str] = []
    seen: set[str] = set()
    for symbol in ranked:
        if symbol in seen:
            continue
        seen.add(symbol)
        ordered.append(symbol)
    return tuple(ordered)


def scrub_text(value: str, symbols: tuple[str, ...], *, scale: float | None = None) -> str:
    """Remove calendar dates, scale words, the scale number, and ticker tokens.

    ISO dates, unpadded ISO, slash dates, dotted DMY and MDY, month names,
    dotted abbreviations, a period with no space (``Jan.4, 2024``), ordinal
    days, ``YYYY-MM`` keys, and a bare year all count. ``2024.50``, ``0.2024``,
    and ``day 2024`` stay. ``today 2024`` does not. Session ids such as ``S01``
    stay. A one-letter ticker is a ticker.
    A digit glued to a ticker is still that ticker. Ticker matching uses
    casefold, so ``ß`` and ``ﬁ`` count.
    """
    text = value
    for pattern in _DATE_PATTERNS:
        text = pattern.sub("", text)
    text = _SECRET_WORDS.sub("", text)
    text = _scrub_scale_number(text, scale)
    for symbol in _symbol_order(symbols):
        text = _replace_ticker(text, symbol)
    return text


def _scale_number(scale: float | None) -> float | None:
    """The invoice scale, when it is a finite number other than 1."""
    if scale is None:
        return None
    try:
        number = float(scale)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or abs(number - 1.0) <= 1e-9:
        return None
    return number


def _same_number(token: str, number: float) -> bool:
    try:
        value = float(token)
    except ValueError:
        return False
    if not math.isfinite(value):
        return False
    return abs(value - number) <= 1e-9 * max(1.0, abs(number))


def _scrub_scale_number(text: str, scale: float | None) -> str:
    """Remove the scale only when it is its own numeric token.

    ``2`` inside ``long_2x`` or ``2.5`` is a different token and stays.
    """
    number = _scale_number(scale)
    if number is None:
        return text

    def replace(match: re.Match[str]) -> str:
        return "" if _same_number(match.group(0), number) else match.group(0)

    return _NUMBER_TOKEN.sub(replace, text)


def _contains_scale(text: str, scale: float | None) -> bool:
    number = _scale_number(scale)
    if number is None:
        return False
    return any(_same_number(match.group(0), number) for match in _NUMBER_TOKEN.finditer(text))


def _month_near_year(text: str) -> bool:
    """A month word and a calendar year in the same short phrase."""
    years = [match.start() for match in _NEAR_YEAR.finditer(text)]
    if not years:
        return False
    return any(
        any(abs(month.start() - year) <= 32 for year in years) for month in _MONTH_WORD.finditer(text)
    )


def _scrub_obj(value, symbols: tuple[str, ...], *, drop_prices: bool, scale: float | None = None):
    if isinstance(value, dict):
        if _is_period_map(value):
            return _relabel_periods(value, symbols, drop_prices=drop_prices, scale=scale)
        clean = {}
        used: set[str] = set()
        for key, item in value.items():
            name = str(key)
            if name in DESIGNER_DROP_KEYS or (drop_prices and name in PRICE_COLUMNS):
                continue
            renamed = _unique_key(scrub_text(name, symbols, scale=scale), used)
            clean[renamed] = _scrub_obj(item, symbols, drop_prices=drop_prices, scale=scale)
        return clean
    if isinstance(value, list):
        return [_scrub_obj(item, symbols, drop_prices=drop_prices, scale=scale) for item in value]
    if isinstance(value, str):
        return _scrub_string(value, symbols, scale)
    return value


def _scrub_string(value: str, symbols: tuple[str, ...], scale: float | None) -> str:
    return designer_surface_text(value, symbols, scale=scale)


def _is_period_map(value: dict) -> bool:
    return any(str(key) in {"monthly", "yearly"} and isinstance(item, dict) for key, item in value.items())


def _relabel_periods(value: dict, symbols: tuple[str, ...], *, drop_prices: bool, scale: float | None):
    """Monthly and yearly keys become ``M1``, ``M2`` and ``Y1``, ``Y2``. Every value stays."""
    clean = {}
    used: set[str] = set()
    for key, item in value.items():
        name = str(key)
        if name in {"monthly", "yearly"} and isinstance(item, dict):
            prefix = "M" if name == "monthly" else "Y"
            nested = {}
            for index, child in enumerate(item.values(), start=1):
                nested[f"{prefix}{index}"] = _scrub_obj(child, symbols, drop_prices=drop_prices, scale=scale)
            clean[name] = nested
            used.add(name)
            continue
        renamed = _unique_key(scrub_text(name, symbols, scale=scale), used)
        clean[renamed] = _scrub_obj(item, symbols, drop_prices=drop_prices, scale=scale)
    return clean


def _unique_key(renamed: str, used: set[str]) -> str:
    """A date key that scrubs to nothing gets a stable stand-in instead of collapsing."""
    candidate = renamed.strip() or "field"
    if candidate in used:
        number = 2
        while f"{candidate}{number}" in used:
            number += 1
        candidate = f"{candidate}{number}"
    used.add(candidate)
    return candidate


def _replace_table(table, columns, rows):
    if table is None:
        return None
    return type(table)(columns=tuple(columns), rows=tuple(rows), source=table.source)


def _scrub_table(table, symbols: tuple[str, ...], *, drop_prices: bool, scale: float | None = None):
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
                value = _scrub_string(value, symbols, scale)
            cleaned[column] = value
        rows.append(cleaned)
    return _replace_table(table, columns, rows)


def apply_designer(report, symbols: tuple[str, ...], *, redacted: bool, scale: float | None = None) -> None:
    """Mutate ``report`` so a designer screen cannot render operator secrets."""
    report.audience = "designer"
    report.metrics = _scrub_obj(report.metrics, symbols, drop_prices=redacted, scale=scale)
    report.other_fields = _scrub_obj(report.other_fields, symbols, drop_prices=redacted, scale=scale)
    report.benchmarks = _scrub_obj(report.benchmarks, symbols, drop_prices=redacted, scale=scale)
    report.cost_basis = None
    if report.liquidation is not None:
        report.liquidation = _scrub_obj(report.liquidation, symbols, drop_prices=redacted, scale=scale)
    if report.config is not None:
        report.config = _scrub_obj(report.config, symbols, drop_prices=True, scale=scale)
    if report.walkforward is not None:
        report.walkforward = _scrub_obj(report.walkforward, symbols, drop_prices=True, scale=scale)
    if report.fees_breakdown is not None:
        report.fees_breakdown = _scrub_obj(report.fees_breakdown, symbols, drop_prices=False, scale=scale)
    if report.period_returns is not None:
        report.period_returns = _scrub_obj(report.period_returns, symbols, drop_prices=False, scale=scale)
    report.warnings = [designer_surface_text(str(item), symbols, scale=scale) for item in report.warnings]
    report.missing = [designer_surface_text(str(item), symbols, scale=scale) for item in report.missing]
    for curve in list(report.curves) + list(report.benchmark_curves):
        curve.sessions = [
            _designer_session_label(label, index, symbols, scale) for index, label in enumerate(curve.sessions)
        ]
        if curve.name not in _PROTECTED_CURVE_NAMES:
            curve.name = designer_surface_text(str(curve.name), symbols, scale=scale)
        curve.drawdown_note = designer_surface_text(str(curve.drawdown_note), symbols, scale=scale)
    report.fills = _scrub_table(report.fills, symbols, drop_prices=redacted, scale=scale)
    report.episodes = _scrub_table(report.episodes, symbols, drop_prices=redacted, scale=scale)
    if report.leaderboard is not None:
        report.leaderboard = _scrub_table(report.leaderboard, symbols, drop_prices=redacted, scale=scale)
    cleaned_children = []
    for child in report.children:
        if not isinstance(child, dict):
            cleaned_children.append(child)
            continue
        nested = child.get("report")
        if nested is not None and hasattr(nested, "metrics"):
            apply_designer(nested, symbols, redacted=redacted, scale=scale)
            outer = {key: value for key, value in child.items() if key not in {"report", "path"}}
            outer = _scrub_obj(outer, symbols, drop_prices=True, scale=scale)
            outer.pop("path", None)
            _strip_child_error(outer, symbols, scale)
            outer["report"] = nested
            cleaned_children.append(outer)
            continue
        cleaned_children.append(_designer_child(child, symbols, scale=scale))
    report.children = cleaned_children


_PROTECTED_CURVE_NAMES = frozenset(
    {
        "Strategy",
        "1× buy and hold",
        "2× long",
    }
)


def _designer_session_label(label: str, index: int, symbols: tuple[str, ...], scale: float | None) -> str:
    """A designer tick is the day or session id, else the integer index.

    A calendar date is not a session label. A pure index is kept even when it
    happens to equal the invoice scale.
    """
    raw = str(label).strip()
    if re.fullmatch(r"\d+", raw) or _SESSION_ID.fullmatch(raw):
        return raw
    cleaned = designer_surface_text(raw, symbols, scale=scale)
    if re.fullmatch(r"\d+", cleaned) or _SESSION_ID.fullmatch(cleaned):
        return cleaned
    return str(index)


def _allow_listed_text(text: str) -> str:
    """Keep numbers, session ids, and words. Drop everything else.

    A month name is not a word we show. A four-digit year stays only when the
    previous word is ``day``. This is the structural layer. The regexes have
    already run.
    """
    pieces: list[str] = []
    previous = ""
    for match in _FREE_TOKEN.finditer(text):
        token = match.group(0)
        head = token.split(".", 1)[0].casefold()
        if head in _MONTH_WORDS:
            previous = ""
            continue
        if _PLAIN_NUMBER.fullmatch(token):
            pieces.append(token)
            previous = ""
            continue
        if _SESSION_ID.fullmatch(token):
            pieces.append(token)
            previous = token
            continue
        if _YEAR_TOKEN.fullmatch(token):
            if previous.casefold() == "day":
                pieces.append(token)
            previous = ""
            continue
        if token.casefold() in _MONTH_WORDS:
            previous = ""
            continue
        if _FREE_WORD.fullmatch(token) or _DOTTED_WORD.fullmatch(token):
            pieces.append(token)
            previous = token if _FREE_WORD.fullmatch(token) else ""
            continue
        previous = ""
    return " ".join(pieces)


def designer_surface_text(value: str, symbols: tuple[str, ...] = (), *, scale: float | None = None) -> str:
    """Text a designer banner, warning, or free-text cell may show.

    Paths are consumed through spaces. The remainder is limited to numbers,
    session ids, and words, and the date, ticker, and scale regexes still run.
    """
    raw = str(value)
    if "Traceback" in raw or _ABS_PATH.search(raw):
        raw = designer_error(raw)
    raw = _ABS_PATH.sub(" ", raw)
    raw = scrub_text(raw, symbols, scale=scale)
    return _allow_listed_text(raw)


def designer_error(text: str) -> str:
    """Exception summary with filesystem paths removed.

    The operator view keeps the traceback. A designer cell and its tooltip
    show this summary instead.
    """
    cleaned = _ABS_PATH.sub("", text)
    lines = []
    for raw in cleaned.splitlines():
        line = re.sub(r"\s+", " ", raw).strip(" \"',")
        if not line or line.startswith("Traceback") or line in {"File"} or line.startswith("File "):
            continue
        lines.append(line)
    for line in reversed(lines):
        if re.search(r"\b(?:Error|Exception)\b", line):
            return line
    return lines[-1] if lines else ""


def _strip_child_error(cleaned: dict, symbols: tuple[str, ...], scale: float | None) -> None:
    error = cleaned.get("error")
    if isinstance(error, str):
        cleaned["error"] = designer_surface_text(error, symbols, scale=scale)


def _leaf_token(value, symbols: tuple[str, ...], *, scale: float | None = None) -> str:
    """Last path segment, with dates and tickers already removed. Not a path."""
    text = scrub_text(str(value or ""), symbols, scale=scale).replace("\\", "/").strip()
    if "/" in text:
        text = text.rstrip("/").split("/")[-1]
    if len(text) >= 2 and text[1] == ":":
        text = text[2:].lstrip("/")
    return text.strip()


def _designer_child(child: dict, symbols: tuple[str, ...], *, scale: float | None = None) -> dict:
    """A job row a designer may see. No filesystem path and no ticker."""
    cleaned = _scrub_obj(
        {key: value for key, value in child.items() if key != "path"},
        symbols,
        drop_prices=True,
        scale=scale,
    )
    cleaned.pop("path", None)
    _strip_child_error(cleaned, symbols, scale)
    strategy = _leaf_token(cleaned.get("strategy_id"), symbols, scale=scale)
    source = _leaf_token(cleaned.get("source_id"), symbols, scale=scale)
    if strategy or source:
        cleaned["name"] = " · ".join(part for part in (strategy, source) if part)
    elif cleaned.get("name"):
        cleaned["name"] = _leaf_token(cleaned.get("name"), symbols, scale=scale)
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
    except (OSError, ValueError):
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
        if key in seen or not symbol:
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
            parts.append(_itxt_text(chunk))
        elif kind == b"IEND":
            break
    return "\n".join(parts)


def _itxt_text(chunk: bytes) -> str:
    """PNG iTXt. Flag 1 means the text is zlib, the same rule as zTXt."""
    if b"\x00" not in chunk:
        return chunk.decode("latin-1", "replace")
    keyword, _, rest = chunk.partition(b"\x00")
    pieces = [keyword.decode("latin-1", "replace")]
    if len(rest) < 2:
        return "\n".join(pieces)
    flag = rest[0]
    tail = rest[2:]
    language, _, tail = tail.partition(b"\x00")
    translated, _, text = tail.partition(b"\x00")
    pieces.append(language.decode("latin-1", "replace"))
    pieces.append(translated.decode("latin-1", "replace"))
    if flag == 1:
        try:
            text = zlib.decompress(text)
        except zlib.error:
            pass
    pieces.append(text.decode("latin-1", "replace"))
    return "\n".join(pieces)


def export_allowed(text: str, *, symbols: tuple[str, ...], scale: float | None) -> bool:
    """False when a file still contains operator-only text.

    The date and path regexes are the second layer. A month word next to a
    year, a path, or the scale as its own number fails even when the spelling
    is one the date list has not named yet.
    """
    folded = text.casefold()
    # Same case-insensitive token check for every invoice word, including share_factor.
    if any(token in folded for token in _SECRET_TOKENS):
        return False
    if any(pattern.search(text) for pattern in _EXPORT_DATE_PATTERNS):
        return False
    if _month_near_year(text) or _ABS_PATH.search(text):
        return False
    for symbol in symbols:
        if symbol and _ticker_spans(text, symbol):
            return False
    if _contains_scale(text, scale):
        return False
    return True
