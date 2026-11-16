
from __future__ import annotations

import csv
import io
import json
import math
import re
import zlib
from decimal import Decimal
from pathlib import Path

from ..calendar_text import contains_calendar_date, strip_calendar_dates
from ..designer_allow import (
    CLOCK_BODY,
    CLOCK_TEXT,
    designer_source_id,
    redacted_key_allowed,
    with_cost_drag,
)
from ..designer_ids import fold, id_has_calendar

_SECRET_TOKENS = (
    "share_scale",
    "share_factor",
    "price_factor",
    "volume_factor",
    "date_map",
    "factor_key",
    "factor_mac",
    "borrow_rate_source",
)
_SECRET_WORDS = re.compile(
    r"(?i)(?<![A-Za-z0-9_])(?:share_scale|share_factor|price_factor|volume_factor|date_map|factor_key|factor_mac|borrow_rate_source)(?![A-Za-z0-9_])"
)
_PATH_TAIL = r"[^'\"\r\n]*"
_ADJACENT_WORD = r"[A-Za-z][A-Za-z0-9_.-]*"
_BEFORE_DEVICE = rf"(?:(?<![A-Za-z0-9_]){_ADJACENT_WORD}[ \t]+)?"
_AFTER_DEVICE = rf"(?:['\"]?[ \t]+{_ADJACENT_WORD})?"
_REL_KEYWORD = r"(?i:hidden|secret|graded|NOTES\.md)"
_ABS_PATH = re.compile(
    "|".join(
        (
            _BEFORE_DEVICE + r"\\\\\?\\UNC\\" + _PATH_TAIL + _AFTER_DEVICE,
            _BEFORE_DEVICE + r"//\?/UNC/" + _PATH_TAIL + _AFTER_DEVICE,
            _BEFORE_DEVICE + r"\\\\\?\\" + _PATH_TAIL + _AFTER_DEVICE,
            _BEFORE_DEVICE + r"//\?/" + _PATH_TAIL + _AFTER_DEVICE,
            r"(?<![A-Za-z0-9_])[A-Za-z]:(?:[\\/]|[^'\"\r\n\\/]*\\)" + _PATH_TAIL,
            r"\\\\[^\\/'\"<>|*?\r\n]+[\\/]" + _PATH_TAIL,
            r"//[^/'\"<>|*?\r\n]+/" + _PATH_TAIL,
            r"/(?:[^/'\"<>|*?\r\n]+/)+[^/'\"<>|*?\r\n]*[A-Za-z]" + _PATH_TAIL,
            rf"(?<![A-Za-z0-9_])(?:[^\\/'\"\r\n]+\\)+{_REL_KEYWORD}[^'\"\r\n]*",
            rf"(?<![A-Za-z0-9_]){_REL_KEYWORD}\\[^'\"\r\n]*",
        )
    )
)
_NUMBER_TOKEN = re.compile(
    r"(?<![A-Za-z0-9_])[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?(?:\d+(?:\.\d*)?|\.\d+))?(?![A-Za-z0-9_])"
)
_NUMBER_LITERAL = re.compile(
    r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?(?:\d+(?:\.\d*)?|\.\d+))?"
)
_MONTH_WORDS = frozenset(
    "jan january feb february mar march apr april may jun june jul july aug august "
    "sep sept september oct october nov november dec december".split()
)
_FREE_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
_SESSION_ID = re.compile(r"[Ss]\d+")
_YEAR_TOKEN = re.compile(r"(?:19|20)\d{2}")
_DOTTED_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_-]*(?:\.[A-Za-z0-9_-]+)+")
_NUMBER_CORE = r"(?:\d{1,3}(?:,\d{3})+(?:\.\d*)?|\d+(?:\.\d*)?|\.\d+)"
_NUMBER_EXP = r"(?:[eE][+-]?(?:\d+(?:\.\d*)?|\.\d+))?"
_NUMBER_SURFACE = re.compile(rf"[+-]?\$?{_NUMBER_CORE}{_NUMBER_EXP}%?")
_HYPHEN_WORD = re.compile(r"[A-Za-z]+(?:-[A-Za-z]+)+")
_WORD_WITH_EXP = (
    r"(?:(?![eE][+-])[A-Za-z0-9_.])+[eE][+-](?:\d+(?:\.\d*)?|\.\d+)"
    r"|[A-Za-z0-9_.]+"
)
_CLOCK_TOKEN = re.compile(rf"(?<![A-Za-z0-9_])(?:{CLOCK_BODY})(?!\d)")
_SURFACE_TOKEN = re.compile(
    rf"(?<![A-Za-z0-9_])(?:{CLOCK_BODY})(?!\d)"
    rf"|(?<![A-Za-z0-9_])[+-]?\$?{_NUMBER_CORE}{_NUMBER_EXP}%?(?![A-Za-z0-9_])"
    rf"|{_HYPHEN_WORD.pattern}"
    rf"|{_WORD_WITH_EXP}"
)
_ID_COLUMNS = frozenset({"strategy_id", "source_id"})
_IDENTIFIER = re.compile(
    r"(?i)(?:"
    r"[A-Za-z_][A-Za-z0-9_]*(?:[eE][+-](?:\d+(?:\.\d*)?|\.\d+))[A-Za-z0-9_]*"
    r"|\d+[A-Za-z_][A-Za-z0-9_]*(?:[eE][+-](?:\d+(?:\.\d*)?|\.\d+))?[A-Za-z0-9_]*"
    r")\Z"
)
TICKER_STAND_IN = "RANDOM"
JSON_READ_LIMIT = 16 * 1024 * 1024
CSV_READ_LIMIT = 256 * 1024 * 1024
PNG_READ_LIMIT = 32 * 1024 * 1024

def refuse_oversized(path: Path, limit: int) -> None:
    size = path.stat().st_size
    if size > limit:
        raise ValueError(f"{path.name} is {size} bytes, over the {limit} byte read limit")


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
        "factor_key",
        "factor_mac",
        "borrow_rate_source",
        "last_price",
        "raw_price",
    }
)

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
SHARE_COLUMNS = frozenset({"shares", "shares_delta", "shares_after", "position_shares"})

def normalize_surface(text: str) -> str:
    return fold(text)


def _hide_column(name: str, *, drop_prices: bool, allow_list: bool = False) -> bool:
    if allow_list:
        return not redacted_key_allowed(name)
    if name in DESIGNER_DROP_KEYS:
        return True
    return bool(drop_prices and (name in PRICE_COLUMNS or name in SHARE_COLUMNS))


def _consume_paths(text: str, replacement: str = " ") -> str:
    return _ABS_PATH.sub(replacement, text)


def _ticker_spans(text: str, symbol: str) -> list[tuple[int, int]]:
    from ..report import ticker_token_spans

    return ticker_token_spans(text, symbol)


def _replace_ticker(text: str, symbol: str) -> str:
    spans = _ticker_spans(text, symbol)
    if not spans:
        return text
    pieces: list[str] = []
    last = 0
    for start, end in spans:
        pieces.append(text[last:start])
        pieces.append(TICKER_STAND_IN)
        last = end
    pieces.append(text[last:])
    return "".join(pieces)


def _symbol_order(symbols: tuple[str, ...]) -> tuple[str, ...]:
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
    text = normalize_surface(value)
    text = strip_calendar_dates(text)
    text = _SECRET_WORDS.sub("", text)
    text = _scrub_scale_number(text, scale)
    for symbol in _symbol_order(symbols):
        text = _replace_ticker(text, symbol)
    return text


def _scale_number(scale: float | None) -> float | None:
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
    number = _scale_number(scale)
    if number is None:
        return text

    def replace(match: re.Match[str]) -> str:
        return "" if _same_number(match.group(0), number) else match.group(0)

    return _NUMBER_TOKEN.sub(replace, text)


def _contains_scale(text: str, scale: float | None) -> bool:
    if _scale_number(scale) is None:
        return False
    return any(_scale_in_token(match.group(0), scale) for match in _SURFACE_TOKEN.finditer(text))


def _scrub_obj(
    value,
    symbols: tuple[str, ...],
    *,
    drop_prices: bool,
    scale: float | None = None,
    allow_list: bool = False,
    key: str | None = None,
):
    if isinstance(value, dict):
        if _is_period_map(value):
            return _relabel_periods(
                value, symbols, drop_prices=drop_prices, scale=scale, allow_list=allow_list
            )
        clean = {}
        used: set[str] = set()
        for key, item in value.items():
            name = str(key)
            if _hide_column(name, drop_prices=drop_prices, allow_list=allow_list):
                continue
            renamed = _unique_key(designer_surface_text(name, symbols, scale=scale), used)
            clean[renamed] = _scrub_obj(
                item, symbols, drop_prices=drop_prices, scale=scale, allow_list=allow_list, key=name
            )
        return clean
    if isinstance(value, list):
        return [
            _scrub_obj(item, symbols, drop_prices=drop_prices, scale=scale, allow_list=allow_list, key=key)
            for item in value
        ]
    if isinstance(value, str):
        if key == "source_id":
            value = designer_source_id(value)
        if key in _ID_COLUMNS:
            return _accepted_id(value, symbols, scale)
        return _scrub_string(value, symbols, scale)
    return value


def _scrub_string(value: str, symbols: tuple[str, ...], scale: float | None) -> str:
    return designer_surface_text(value, symbols, scale=scale)


def _accepted_id(value: str, symbols: tuple[str, ...], scale: float | None) -> str:
    text = normalize_surface(value)
    return scrub_text(text, symbols, scale=scale).strip()


def _is_period_map(value: dict) -> bool:
    return any(str(key) in {"monthly", "yearly"} and isinstance(item, dict) for key, item in value.items())


def _relabel_periods(
    value: dict,
    symbols: tuple[str, ...],
    *,
    drop_prices: bool,
    scale: float | None,
    allow_list: bool = False,
):
    clean = {}
    used: set[str] = set()
    for key, item in value.items():
        name = str(key)
        if name in {"monthly", "yearly"} and isinstance(item, dict):
            prefix = "M" if name == "monthly" else "Y"
            nested = {}
            for index, child in enumerate(item.values(), start=1):
                nested[f"{prefix}{index}"] = _scrub_obj(
                    child, symbols, drop_prices=drop_prices, scale=scale, allow_list=allow_list, key=name
                )
            clean[name] = nested
            used.add(name)
            continue
        if _hide_column(name, drop_prices=drop_prices, allow_list=allow_list):
            continue
        renamed = _unique_key(designer_surface_text(name, symbols, scale=scale), used)
        clean[renamed] = _scrub_obj(
            item, symbols, drop_prices=drop_prices, scale=scale, allow_list=allow_list, key=name
        )
    return clean


def _unique_key(renamed: str, used: set[str]) -> str:
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


def _scrub_table(
    table,
    symbols: tuple[str, ...],
    *,
    drop_prices: bool,
    scale: float | None = None,
    allow_list: bool = False,
):
    if table is None:
        return None
    pairs = []
    used: set[str] = set()
    for column in table.columns:
        name = str(column)
        if _hide_column(name, drop_prices=drop_prices, allow_list=allow_list):
            continue
        renamed = _unique_key(designer_surface_text(name, symbols, scale=scale), used)
        pairs.append((name, renamed))
    rows = []
    for row in table.rows:
        cleaned = {}
        for original, renamed in pairs:
            value = row.get(original)
            if original == "source_id" and isinstance(value, str):
                value = designer_source_id(value)
            if isinstance(value, str):
                if original in _ID_COLUMNS:
                    value = _accepted_id(value, symbols, scale)
                else:
                    value = _scrub_string(value, symbols, scale)
            cleaned[renamed] = value
        rows.append(cleaned)
    return _replace_table(table, [renamed for _, renamed in pairs], rows)


def apply_designer(report, symbols: tuple[str, ...], *, redacted: bool, scale: float | None = None) -> None:
    report.audience = "designer"
    report.metrics = _scrub_obj(
        with_cost_drag(report.metrics), symbols, drop_prices=True, scale=scale, allow_list=True
    )
    report.other_fields = _scrub_obj(
        report.other_fields, symbols, drop_prices=True, scale=scale, allow_list=True
    )
    report.benchmarks = _scrub_obj(
        with_cost_drag(report.benchmarks), symbols, drop_prices=True, scale=scale, allow_list=True
    )
    report.cost_basis = None
    if report.liquidation is not None:
        report.liquidation = _scrub_obj(
            report.liquidation, symbols, drop_prices=redacted, scale=scale, allow_list=True
        )
    if report.config is not None:
        report.config = _scrub_obj(
            report.config, symbols, drop_prices=True, scale=scale, allow_list=True
        )
    if report.walkforward is not None:
        report.walkforward = _scrub_obj(
            report.walkforward, symbols, drop_prices=True, scale=scale, allow_list=True
        )
    if report.fees_breakdown is not None:
        report.fees_breakdown = _scrub_obj(
            report.fees_breakdown, symbols, drop_prices=redacted, scale=scale, allow_list=True
        )
    if report.period_returns is not None:
        report.period_returns = _scrub_obj(
            report.period_returns, symbols, drop_prices=redacted, scale=scale, allow_list=True
        )
    report.warnings = [designer_surface_text(str(item), symbols, scale=scale) for item in report.warnings]
    report.missing = [designer_surface_text(str(item), symbols, scale=scale) for item in report.missing]
    for curve in list(report.curves) + list(report.benchmark_curves):
        curve.sessions = [
            _designer_session_label(label, index, symbols, scale) for index, label in enumerate(curve.sessions)
        ]
        if curve.name not in _PROTECTED_CURVE_NAMES:
            curve.name = designer_surface_text(str(curve.name), symbols, scale=scale)
        curve.drawdown_note = designer_surface_text(str(curve.drawdown_note), symbols, scale=scale)
    report.fills = _scrub_table(
        report.fills, symbols, drop_prices=redacted, scale=scale, allow_list=True
    )
    report.episodes = _scrub_table(
        report.episodes, symbols, drop_prices=redacted, scale=scale, allow_list=True
    )
    if report.leaderboard is not None:
        report.leaderboard = _scrub_table(
            report.leaderboard, symbols, drop_prices=redacted, scale=scale, allow_list=True
        )
    cleaned_children = []
    for child in report.children:
        if not isinstance(child, dict):
            cleaned_children.append(child)
            continue
        nested = child.get("report")
        if nested is not None and hasattr(nested, "metrics"):
            apply_designer(nested, symbols, redacted=redacted, scale=scale)
            outer = {key: value for key, value in child.items() if key not in {"report", "path"}}
            outer = _scrub_obj(outer, symbols, drop_prices=True, scale=scale, allow_list=True)
            outer.pop("path", None)
            _strip_child_error(outer, symbols, scale)
            outer["report"] = nested
            cleaned_children.append(outer)
            continue
        cleaned_children.append(_designer_child(child, symbols, scale=scale, allow_list=True))
    report.children = cleaned_children


_PROTECTED_CURVE_NAMES = frozenset(
    {
        "Strategy",
        "1× buy and hold",
        "2× long",
    }
)


def _designer_session_label(label: str, index: int, symbols: tuple[str, ...], scale: float | None) -> str:
    raw = str(label).strip()
    if _SESSION_ID.fullmatch(raw):
        return raw
    if re.fullmatch(r"\d+", raw) and not contains_calendar_date(raw):
        return raw
    cleaned = designer_surface_text(raw, symbols, scale=scale)
    if re.fullmatch(r"\d+", cleaned) or _SESSION_ID.fullmatch(cleaned):
        return cleaned
    return str(index)


def _wordish(token: str) -> bool:
    return bool(_FREE_WORD.fullmatch(token) or _HYPHEN_WORD.fullmatch(token))


def _numeric_text(token: str) -> str:
    text = token.strip()
    sign = ""
    if text[:1] in "+-":
        sign, text = text[:1], text[1:]
    if text.startswith("$"):
        text = text[1:]
    if text.endswith("%"):
        text = text[:-1]
    return sign + text.replace(",", "")


def _scientific_parts(literal: str) -> tuple[str, str] | None:
    match = re.fullmatch(r"(.+?)[eE]([+-]?(?:\d+(?:\.\d*)?|\.\d+))", literal)
    if match is None:
        return None
    return match.group(1), match.group(2)


def _recurse_exponent(significand: str, exponent: str) -> bool:
    if exponent[:1] in "+-" or "." in exponent:
        return True
    return _significand_digits(significand) == "1"


def _significand_digits(literal: str) -> str:
    mantissa = re.split(r"[eE]", literal.strip(), maxsplit=1)[0]
    mantissa = mantissa.lstrip("+-").replace(".", "").replace(",", "")
    digits = mantissa.strip("0")
    return digits or "0"


def _scale_digits(number: float) -> str:
    text = format(Decimal(str(abs(number))), "f").rstrip("0").rstrip(".")
    return _significand_digits(text)


def _power_of_ten_multiple(value: float, base: float) -> bool:
    if value == 0 or base == 0 or not math.isfinite(value) or not math.isfinite(base):
        return False
    try:
        ratio = abs(value / base)
    except OverflowError:
        return False
    if not math.isfinite(ratio) or ratio == 0:
        return False
    try:
        log = math.log10(ratio)
    except (OverflowError, ValueError):
        return False
    if not math.isfinite(log):
        return False
    try:
        nearest = round(log)
    except (OverflowError, ValueError):
        return False
    if abs(nearest) > 24:
        return False
    try:
        target = 10.0**nearest
    except OverflowError:
        return False
    if not math.isfinite(target):
        return False
    return math.isclose(ratio, target, rel_tol=1e-6, abs_tol=0.0)


def _literal_is_scale(literal: str, scale: float) -> bool:
    parts = _scientific_parts(literal)
    if parts is not None and _recurse_exponent(*parts) and _literal_is_scale(parts[1], scale):
        return True
    try:
        value = abs(float(literal))
    except (ValueError, OverflowError):
        value = None
    if value is not None and math.isfinite(value):
        if scale != 0 and math.isclose(value, abs(1.0 / scale), rel_tol=1e-9, abs_tol=1e-12):
            return True
        if _power_of_ten_multiple(value, abs(scale)) or math.isclose(
            value, abs(scale), rel_tol=1e-9, abs_tol=1e-12
        ):
            return True
    digits = _significand_digits(literal)
    return len(digits) >= 3 and digits != "0" and digits == _scale_digits(scale)


def _scale_in_token(token: str, scale: float | None) -> bool:
    number = _scale_number(scale)
    if number is None or not token:
        return False
    if CLOCK_TEXT.fullmatch(token):
        return False
    probed = token.replace("$", "").replace("%", "").replace(",", "")
    for match in _NUMBER_LITERAL.finditer(probed):
        literal = match.group(0)
        bare_integer = re.fullmatch(r"[+-]?\d+", literal) is not None
        whole = match.start() == 0 and match.end() == len(probed)
        if bare_integer and not whole:
            continue
        if _literal_is_scale(literal, number):
            return True
    return False


def _keep_surface_token(token: str, *, previous: str, scale: float | None) -> bool:
    del previous
    if CLOCK_TEXT.fullmatch(token):
        return True
    if id_has_calendar(token):
        return False
    if _scale_in_token(token, scale):
        return False
    if token.casefold() in _MONTH_WORDS or token.split(".", 1)[0].casefold() in _MONTH_WORDS:
        return False
    if _YEAR_TOKEN.fullmatch(token):
        return False
    if _NUMBER_SURFACE.fullmatch(token):
        return True
    if _SESSION_ID.fullmatch(token):
        return True
    if _HYPHEN_WORD.fullmatch(token):
        return not any(part.casefold() in _MONTH_WORDS for part in token.split("-"))
    if _FREE_WORD.fullmatch(token) or _DOTTED_WORD.fullmatch(token):
        return True
    return False


def _surface_pieces(text: str, *, scale: float | None) -> tuple[list[str], bool]:
    pieces: list[str] = []
    allowed = True
    previous = ""
    for match in _SURFACE_TOKEN.finditer(text):
        token = match.group(0)
        if _keep_surface_token(token, previous=previous, scale=scale):
            pieces.append(token)
            previous = token if _wordish(token) else ""
        else:
            allowed = False
            previous = ""
    return pieces, allowed


def _allow_listed_text(text: str, *, scale: float | None = None) -> str:
    pieces, _allowed = _surface_pieces(text, scale=scale)
    return " ".join(pieces)


def designer_surface_text(value: str, symbols: tuple[str, ...] = (), *, scale: float | None = None) -> str:
    raw = normalize_surface(value)
    if "Traceback" in raw or _consume_paths(raw, "") != raw:
        raw = designer_error(raw)
    raw = _consume_paths(raw, " ")
    raw = scrub_text(raw, symbols, scale=scale)
    return _allow_listed_text(raw, scale=scale)


def designer_error(text: str) -> str:
    cleaned = _consume_paths(normalize_surface(text), "")
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
    text = scrub_text(str(value or ""), symbols, scale=scale).replace("\\", "/").strip()
    if "/" in text:
        text = text.rstrip("/").split("/")[-1]
    if len(text) >= 2 and text[1] == ":":
        text = text[2:].lstrip("/")
    return text.strip()


def _designer_child(
    child: dict,
    symbols: tuple[str, ...],
    *,
    scale: float | None = None,
    allow_list: bool = False,
) -> dict:
    cleaned = _scrub_obj(
        {key: value for key, value in child.items() if key != "path"},
        symbols,
        drop_prices=True,
        scale=scale,
        allow_list=allow_list,
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


def _scale_value(document) -> float | None:
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


def peek_scale(folder: Path) -> float | None:
    folder = Path(folder)
    found = _scale_value(_read_json(folder / "metrics.json"))
    if found is not None:
        return found
    roots: list[Path] = []
    jobs = folder / "jobs"
    if jobs.is_dir():
        roots.append(jobs)
    if folder.name == "designer":
        parent_jobs = folder.parent / "jobs"
        if parent_jobs.is_dir() and parent_jobs not in roots:
            roots.append(parent_jobs)
    for root in roots:
        try:
            strategies = sorted(path for path in root.iterdir() if path.is_dir())
        except OSError:
            continue
        for strategy in strategies:
            try:
                sources = sorted(path for path in strategy.iterdir() if path.is_dir())
            except OSError:
                continue
            for source in sources:
                found = _scale_value(_read_json(source / "metrics.json"))
                if found is not None:
                    return found
    return None


def designer_tree(folder: Path) -> bool:
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
    if designer_tree(folder / "designer"):
        return "designer"
    for name in ("metrics.json", "run.json"):
        document = _read_json(folder / name)
        if book_is_anonymized(document):
            return "designer"
    return "operator"


def resolve_view(folder: Path, audience: str) -> tuple[str, Path, bool]:
    chosen = (audience or "auto").strip().lower()
    if chosen == "auto":
        chosen = default_audience(folder)
    if chosen not in {"operator", "designer"}:
        chosen = "operator"
    if chosen == "designer":
        published = folder / "designer"
        if designer_tree(published):
            return "designer", published, True
        return "designer", folder, True
    return "operator", folder, False


def export_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".png":
        limit = PNG_READ_LIMIT
    elif suffix == ".csv":
        limit = CSV_READ_LIMIT
    else:
        limit = JSON_READ_LIMIT
    refuse_oversized(path, limit)
    data = path.read_bytes()
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return _png_text(data)
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
            if index < len(data):
                parts.append(data[index:].decode("utf-8", "replace"))
            break
    return "\n".join(parts)


def png_caption(data: bytes) -> str:
    return _png_text(data)


def _itxt_text(chunk: bytes) -> str:
    if b"\x00" not in chunk:
        return chunk.decode("utf-8", "replace")
    keyword, _, rest = chunk.partition(b"\x00")
    pieces = [keyword.decode("latin-1", "replace")]
    if len(rest) < 2:
        return "\n".join(pieces)
    flag = rest[0]
    tail = rest[2:]
    language, _, tail = tail.partition(b"\x00")
    translated, _, text = tail.partition(b"\x00")
    pieces.append(language.decode("latin-1", "replace"))
    pieces.append(translated.decode("utf-8", "replace"))
    if flag == 1:
        try:
            text = zlib.decompress(text)
        except zlib.error:
            pass
    pieces.append(text.decode("utf-8", "replace"))
    return "\n".join(pieces)


def _is_identifier(text: str) -> bool:
    token = text.strip()
    if not token or any(char.isspace() for char in token):
        return False
    return _IDENTIFIER.fullmatch(token) is not None


def _identifier_reason(text: str, scale: float | None) -> str | None:
    if contains_calendar_date(text):
        return "a date"
    if _contains_scale(text, scale):
        return "the scale"
    return None


def _identifier_in_value(value, scale: float | None) -> str | None:
    if isinstance(value, dict):
        for item in value.values():
            reason = _identifier_in_value(item, scale)
            if reason:
                return reason
        return None
    if isinstance(value, list):
        for item in value:
            reason = _identifier_in_value(item, scale)
            if reason:
                return reason
        return None
    if isinstance(value, str) and _is_identifier(value):
        return _identifier_reason(value.strip(), scale)
    return None


def _structured_identifier_reason(kind: str, payload, scale: float | None) -> str | None:
    if kind == "json":
        return _identifier_in_value(payload, scale)
    for row in payload:
        for cell in row:
            if _is_identifier(cell):
                reason = _identifier_reason(cell.strip(), scale)
                if reason:
                    return reason
    return None


def scan_publication(text: str, *, symbols: tuple[str, ...] = (), scale: float | None = None) -> str | None:
    del symbols
    normalized = normalize_surface(text)
    if contains_calendar_date(_mask_listed_numbers(normalized)):
        return "a date"
    structured = _structured_document(normalized)
    if structured is None:
        if _is_identifier(normalized):
            return _identifier_reason(normalized, scale)
    else:
        reason = _structured_identifier_reason(structured[0], structured[1], scale)
        if reason:
            return reason
    if _publication_has_scale(normalized, scale):
        return "the scale"
    if _publication_has_price(normalized):
        return "a price"
    return None


def _free_has_scale(text: str, scale: float | None) -> bool:
    if _scale_number(scale) is None:
        return False
    return _contains_scale(text, scale)


def _published_cost_label(key, value) -> bool:
    if str(key or "") != "cost_model" or not isinstance(value, str):
        return False
    from ..costs_designer import is_cost_label

    return is_cost_label(value)


def _publication_has_scale(text: str, scale: float | None) -> bool:
    if _scale_number(scale) is None:
        return False
    structured = _structured_document(text)
    if structured is None:
        return _free_has_scale(text, scale)
    kind, payload = structured
    if kind == "json":
        return _json_has_scale(payload, scale)
    return _csv_has_scale(payload, scale)


def _json_has_scale(value, scale: float | None, key: str | None = None) -> bool:
    if isinstance(value, dict):
        for child_key, item in value.items():
            name = str(child_key)
            if _free_has_scale(name, scale) or _json_has_scale(item, scale, name):
                return True
        return False
    if isinstance(value, list):
        return any(_json_has_scale(item, scale, key) for item in value)
    if isinstance(value, str):
        if _published_cost_label(key, value):
            return False
        return _free_has_scale(value, scale)
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, (int, float)):
        if key is not None and redacted_key_allowed(key):
            return False
        return _contains_scale(str(value), scale)
    return _free_has_scale(str(value), scale)


def _header_parts(name: str) -> tuple[str, str]:
    text = name.strip()
    if "|" not in text:
        return text, ""
    key, label = text.split("|", 1)
    key = key.strip()
    label = label.strip()
    if key and redacted_key_allowed(key):
        return key, label
    return text, ""


def _listed_number(cell: str) -> bool:
    return _numeric_cell(cell) or _formatted_number(cell)


def _listed_metric_key(key: str | None) -> bool:
    if not key or key in _ID_COLUMNS or key in {"name", "message", "error"}:
        return False
    return redacted_key_allowed(key)


def _mask_listed_numbers(text: str) -> str:
    structured = _structured_document(text)
    if structured is None:
        return text
    kind, payload = structured
    if kind == "json":
        return json.dumps(_masked_json(payload), ensure_ascii=False, separators=(",", ":"))
    return _masked_csv_text(payload)


def _masked_json(value, key: str | None = None):
    if isinstance(value, dict):
        return {str(child): _masked_json(item, str(child)) for child, item in value.items()}
    if isinstance(value, list):
        return [_masked_json(item, key) for item in value]
    if isinstance(value, bool) or value is None:
        return value
    if _listed_metric_key(key) and isinstance(value, (int, float)) and math.isfinite(value):
        return 0
    if isinstance(value, str) and _listed_metric_key(key) and _listed_number(value):
        return "0"
    return value


def _masked_csv_text(rows: list[list[str]]) -> str:
    header = [cell.strip() for cell in rows[0]]
    masked = [list(row) for row in rows]
    if header == ["key", "raw", "display"]:
        for row in masked[1:]:
            if not row:
                continue
            metric = row[0]
            if metric in _ID_COLUMNS or not _listed_metric_key(metric):
                continue
            for index in range(1, len(row)):
                if _listed_number(row[index]):
                    row[index] = "0"
    else:
        keys: list[str] = []
        for name in header:
            key, _label = _header_parts(name)
            keys.append(key)
        for row in masked[1:]:
            for index, cell in enumerate(row):
                key = keys[index] if index < len(keys) else ""
                if _listed_metric_key(key) and _listed_number(cell):
                    row[index] = "0"
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerows(masked)
    return buffer.getvalue()


def _csv_has_scale(rows: list[list[str]], scale: float | None) -> bool:
    header = [cell.strip() for cell in rows[0]]
    if header == ["key", "raw", "display"]:
        for name in header:
            if _free_has_scale(name, scale):
                return True
        for row in rows[1:]:
            if not row:
                continue
            if _free_has_scale(row[0], scale):
                return True
            listed = redacted_key_allowed(row[0])
            for cell in row[1:]:
                if listed and _listed_number(cell):
                    continue
                if _free_has_scale(cell, scale):
                    return True
        return False
    keys: list[str] = []
    for name in header:
        key, label = _header_parts(name)
        keys.append(key)
        if _free_has_scale(key, scale) or (label and _free_has_scale(label, scale)):
            return True
    for row in rows[1:]:
        for index, cell in enumerate(row):
            key = keys[index] if index < len(keys) else ""
            if key and redacted_key_allowed(key) and key not in _ID_COLUMNS and _listed_number(cell):
                continue
            if _published_cost_label(key, cell):
                continue
            if _free_has_scale(cell, scale):
                return True
    return False


def _publication_has_price(text: str) -> bool:
    structured = _structured_document(text)
    if structured is None:
        return _free_text_has_price(text)
    kind, payload = structured
    if kind == "json":
        return _json_has_price(payload)
    return _csv_has_price(payload)


def _is_price_literal(literal: str) -> bool:
    if "." not in literal:
        return False
    try:
        value = abs(float(literal))
    except (ValueError, OverflowError):
        return False
    return math.isfinite(value) and value >= 1.0


def _free_text_has_price(text: str) -> bool:
    masked = _CLOCK_TOKEN.sub(" ", normalize_surface(text))
    return any(_is_price_literal(match.group(0)) for match in _NUMBER_LITERAL.finditer(masked))


def _json_has_price(value, key: str | None = None) -> bool:
    if isinstance(value, dict):
        for child_key, item in value.items():
            name = str(child_key)
            if _free_text_has_price(name) or _json_has_price(item, name):
                return True
        return False
    if isinstance(value, list):
        return any(_json_has_price(item, key) for item in value)
    if isinstance(value, str):
        if key in _ID_COLUMNS:
            return False
        return _free_text_has_price(value)
    return False


def _csv_has_price(rows: list[list[str]]) -> bool:
    header = [cell.strip() for cell in rows[0]]
    if header == ["key", "raw", "display"]:
        for name in header:
            if _free_text_has_price(name):
                return True
        for row in rows[1:]:
            if not row:
                continue
            if _free_text_has_price(row[0]):
                return True
            listed = redacted_key_allowed(row[0])
            for index, cell in enumerate(row[1:], start=1):
                if listed and _listed_number(cell):
                    continue
                if row[0] in _ID_COLUMNS:
                    continue
                if _free_text_has_price(cell):
                    return True
        return False
    keys: list[str] = []
    for name in header:
        key, label = _header_parts(name)
        keys.append(key)
        if _free_text_has_price(key) or (label and _free_text_has_price(label)):
            return True
    for row in rows[1:]:
        for index, cell in enumerate(row):
            key = keys[index] if index < len(keys) else ""
            if key in _ID_COLUMNS:
                continue
            if key and redacted_key_allowed(key) and _listed_number(cell):
                continue
            if _free_text_has_price(cell):
                return True
    return False


def export_block_reason(text: str, *, symbols: tuple[str, ...], scale: float | None) -> str | None:
    text = normalize_surface(text)
    structured = _structured_document(text)
    if structured is None:
        return _free_text_reason(text, symbols, scale)
    reason = _preamble_reason(text, symbols)
    if reason:
        return reason
    kind, payload = structured
    if kind == "json":
        return _json_reason(payload, symbols, scale)
    return _csv_reason(payload, symbols, scale)


def export_allowed(text: str, *, symbols: tuple[str, ...], scale: float | None) -> bool:
    return export_block_reason(text, symbols=symbols, scale=scale) is None


def _preamble_reason(text: str, symbols: tuple[str, ...]) -> str | None:
    folded = text.casefold()
    if any(token in folded for token in _SECRET_TOKENS):
        return "a secret"
    if contains_calendar_date(_mask_listed_numbers(text)):
        return "a date"
    if _consume_paths(text, "") != text:
        return "a path"
    for symbol in symbols:
        if symbol and _ticker_spans(text, symbol):
            return "a ticker"
    return None


def _preamble_allowed(text: str, symbols: tuple[str, ...]) -> bool:
    return _preamble_reason(text, symbols) is None


def _free_text_reason(
    text: str,
    symbols: tuple[str, ...],
    scale: float | None,
    *,
    price: bool = True,
) -> str | None:
    reason = _preamble_reason(text, symbols)
    if reason:
        return reason
    token = text.strip()
    if _is_identifier(token):
        return _identifier_reason(token, scale)
    if _contains_scale(text, scale):
        return "the scale"
    if price and _free_text_has_price(text):
        return "a price"
    _pieces, allowed = _surface_pieces(text, scale=scale)
    if allowed:
        return None
    if contains_calendar_date(text):
        return "a date"
    return "a withheld token"


def _free_text_allowed(
    text: str,
    symbols: tuple[str, ...],
    scale: float | None,
    *,
    price: bool = True,
) -> bool:
    return _free_text_reason(text, symbols, scale, price=price) is None


def _structured_document(text: str):
    stripped = text.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            value = json.loads(stripped)
        except json.JSONDecodeError:
            return None
        if isinstance(value, (dict, list)):
            return ("json", value)
        return None
    first, _sep, _rest = text.partition("\n")
    if "\n" in text and "," in first:
        try:
            rows = list(csv.reader(io.StringIO(text)))
        except csv.Error:
            return None
        if rows and rows[0]:
            return ("csv", rows)
    return None


def _numeric_cell(cell: str) -> bool:
    token = cell.strip()
    if not token:
        return False
    try:
        number = float(token)
    except ValueError:
        return False
    return math.isfinite(number)


def _formatted_number(cell: str) -> bool:
    token = cell.strip().replace(",", "").replace("$", "").replace("%", "")
    if token.endswith(" bp"):
        token = token[: -len(" bp")]
    token = token.strip()
    if token in {"", "—", "-", "∞", "-∞"}:
        return True
    return _numeric_cell(token)


def _json_reason(value, symbols: tuple[str, ...], scale: float | None, *, key: str | None = None) -> str | None:
    if isinstance(value, dict):
        for child_key, item in value.items():
            name = str(child_key)
            reason = _free_text_reason(name, symbols, scale)
            if reason:
                return reason
            reason = _json_reason(item, symbols, scale, key=name)
            if reason:
                return reason
        return None
    if isinstance(value, list):
        for item in value:
            reason = _json_reason(item, symbols, scale, key=key)
            if reason:
                return reason
        return None
    if isinstance(value, str):
        if _listed_metric_key(key) and _listed_number(value):
            return None
        return _free_text_reason(value, symbols, scale, price=key not in _ID_COLUMNS)
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        if key is not None and redacted_key_allowed(key):
            return None
        if _contains_scale(str(value), scale):
            return "the scale"
        return None
    return _free_text_reason(str(value), symbols, scale)


def _json_allowed(value, symbols: tuple[str, ...], scale: float | None, *, key: str | None = None) -> bool:
    return _json_reason(value, symbols, scale, key=key) is None


def _csv_reason(rows: list[list[str]], symbols: tuple[str, ...], scale: float | None) -> str | None:
    header = [cell.strip() for cell in rows[0]]
    if header == ["key", "raw", "display"]:
        for name in header:
            reason = _free_text_reason(name, symbols, scale)
            if reason:
                return reason
        for row in rows[1:]:
            if not row:
                continue
            metric = row[0]
            reason = _free_text_reason(metric, symbols, scale)
            if reason:
                return reason
            listed = redacted_key_allowed(metric)
            for cell in row[1:]:
                if metric in _ID_COLUMNS:
                    reason = _free_text_reason(cell, symbols, scale, price=False)
                    if reason:
                        return reason
                    continue
                if listed and _listed_number(cell):
                    continue
                reason = _free_text_reason(cell, symbols, scale)
                if reason:
                    return reason
        return None
    keys: list[str] = []
    for name in header:
        key, label = _header_parts(name)
        keys.append(key)
        reason = _free_text_reason(key, symbols, scale)
        if reason:
            return reason
        if label:
            reason = _free_text_reason(label, symbols, scale)
            if reason:
                return reason
    for row in rows[1:]:
        for index, cell in enumerate(row):
            key = keys[index] if index < len(keys) else ""
            if key in _ID_COLUMNS:
                reason = _free_text_reason(cell, symbols, scale, price=False)
                if reason:
                    return reason
                continue
            if key and redacted_key_allowed(key) and _listed_number(cell):
                continue
            reason = _free_text_reason(cell, symbols, scale)
            if reason:
                return reason
    return None


def _csv_allowed(rows: list[list[str]], symbols: tuple[str, ...], scale: float | None) -> bool:
    return _csv_reason(rows, symbols, scale) is None
