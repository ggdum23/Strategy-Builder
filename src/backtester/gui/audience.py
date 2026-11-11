"""Operator and designer views of one report directory.

``strategy-builder.report.v1`` has two audiences. ``operator`` reads the folder
the runner wrote. ``designer`` reads ``designer/`` when that tree was published,
and otherwise reads the operator files. Every designer surface then keeps only
the shared allow-list. There is no unredacted designer path.

This module does not import the runner or ``report.py``. The allow-list lives
in ``designer_allow``, which ``report.py`` imports as well.
"""

from __future__ import annotations

import csv
import io
import json
import math
import re
import unicodedata
import zlib
from decimal import Decimal
from pathlib import Path

from ..designer_allow import redacted_key_allowed, with_cost_drag

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
# A path may contain spaces and commas (``Strategy Builder, Inc\QQQ\data.csv``).
# ``\\?\UNC\``, ``\\?\``, and the slash forms ``//?/UNC/`` and ``//?/`` are paths.
# A drive-relative path (``C:Users\...``, ``C:secret\NOTES.md``) is a path too.
# So is a relative backslash path that contains ``hidden``, ``secret``, ``graded``,
# or ``NOTES.md``. After a path starts, the rest of the field belongs to it,
# including commas, and it stops at a quote or the end of the line. The word
# immediately before or after a ``\\?\`` / ``//?/`` path goes with it. A slash
# date such as 2024/1/4 is not a path: a POSIX path needs a letter in it.
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
# A scale value is its own number, not a digit inside ``long_2x`` or ``2.5``.
_NUMBER_TOKEN = re.compile(
    r"(?<![A-Za-z0-9_])[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?(?:\d+(?:\.\d*)?|\.\d+))?(?![A-Za-z0-9_])"
)
# Every numeric literal: optional sign, digits, optional decimal, optional exponent.
# It may be glued to letters or underscores. ``_scale_in_token`` is the one
# comparison the screen and the export both use.
_NUMBER_LITERAL = re.compile(
    r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?(?:\d+(?:\.\d*)?|\.\d+))?"
)
_MONTH_WORDS = frozenset(
    "jan january feb february mar march apr april may jun june jul july aug august "
    "sep sept september oct october nov november dec december".split()
)
_MONTH_WORD = re.compile(rf"(?<![A-Za-z]){_MONTH}(?![A-Za-z])", re.IGNORECASE)
_NEAR_YEAR = re.compile(r"(?<![A-Za-z0-9.])(?:19|20)\d{2}(?![A-Za-z0-9.])")
_FREE_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
_SESSION_ID = re.compile(r"[Ss]\d+")
_YEAR_TOKEN = re.compile(r"(?:19|20)\d{2}")
_DOTTED_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_-]*(?:\.[A-Za-z0-9_-]+)+")
# One signed number: optional $, thousands commas, a decimal, an exponent, a trailing %.
# The exponent may itself be a decimal (``1e7.777``) so it stays one token.
_NUMBER_CORE = r"(?:\d{1,3}(?:,\d{3})+(?:\.\d*)?|\d+(?:\.\d*)?|\.\d+)"
_NUMBER_EXP = r"(?:[eE][+-]?(?:\d+(?:\.\d*)?|\.\d+))?"
_NUMBER_SURFACE = re.compile(rf"[+-]?\$?{_NUMBER_CORE}{_NUMBER_EXP}%?")
_HYPHEN_WORD = re.compile(r"[A-Za-z]+(?:-[A-Za-z]+)+")
# A signed exponent stays inside the word token (``build2_7777e-3``). The
# letter class does not swallow the ``e`` of that exponent, or the following
# ``-3`` would be a separate token and the scale check would miss it.
_WORD_WITH_EXP = (
    r"(?:(?![eE][+-])[A-Za-z0-9_.])+[eE][+-](?:\d+(?:\.\d*)?|\.\d+)"
    r"|[A-Za-z0-9_.]+"
)
_SURFACE_TOKEN = re.compile(
    rf"(?<![A-Za-z0-9_])[+-]?\$?{_NUMBER_CORE}{_NUMBER_EXP}%?(?![A-Za-z0-9_])"
    rf"|{_HYPHEN_WORD.pattern}"
    rf"|{_WORD_WITH_EXP}"
)
_GLUED_YEAR = re.compile(r"(?i)(?:[A-Za-z](?:19|20)\d{2}|(?:19|20)\d{2}[A-Za-z])")
_MONTH_IN_TOKEN = re.compile(rf"(?i)(?<![A-Za-z]){_MONTH}(?![A-Za-z])")
_YEAR_IN_TOKEN = re.compile(r"(?:19|20)\d{2}")
# Designer surfaces say RANDOM where a ticker was. The operator view keeps the ticker.
TICKER_STAND_IN = "RANDOM"
# Whole-file reads stop at these caps so a huge report cannot exhaust memory.
JSON_READ_LIMIT = 16 * 1024 * 1024
CSV_READ_LIMIT = 256 * 1024 * 1024
PNG_READ_LIMIT = 32 * 1024 * 1024

def refuse_oversized(path: Path, limit: int) -> None:
    """Do not read a report file whole when it is over the cap."""
    size = path.stat().st_size
    if size > limit:
        raise ValueError(f"{path.name} is {size} bytes, over the {limit} byte read limit")


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

# Raw print columns. The allow-list drops these on every designer surface,
# including a published ``designer/`` tree.
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
# Share counts let equity and exposure be inverted back into a price.
SHARE_COLUMNS = frozenset({"shares", "shares_delta", "shares_after", "position_shares"})

# Unicode minus and dash characters that NFKC leaves unchanged, mapped to ASCII
# before a numeric literal is read. Fullwidth digits fold under NFKC itself.
_DASH_TO_HYPHEN = str.maketrans(
    {
        "\u2212": "-",
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\ufe63": "-",
        "\uff0d": "-",
    }
)


def normalize_surface(text: str) -> str:
    """NFKC, then unicode minus and dash characters become ASCII ``-``."""
    return unicodedata.normalize("NFKC", str(text)).translate(_DASH_TO_HYPHEN)


def _hide_column(name: str, *, drop_prices: bool, allow_list: bool = False) -> bool:
    if allow_list:
        return not redacted_key_allowed(name)
    if name in DESIGNER_DROP_KEYS:
        return True
    return bool(drop_prices and (name in PRICE_COLUMNS or name in SHARE_COLUMNS))


def _consume_paths(text: str, replacement: str = " ") -> str:
    """Remove absolute paths. The screen filter and ``export_allowed`` both call this."""
    return _ABS_PATH.sub(replacement, text)


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
        pieces.append(TICKER_STAND_IN)
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
    A digit glued to a ticker is still that ticker, and the ticker is shown as
    ``RANDOM``. The operator view never calls this. Ticker matching uses
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
    """True when a surface token is the invoice scale. Same decision as the screen."""
    if _scale_number(scale) is None:
        return False
    return any(_scale_in_token(match.group(0), scale) for match in _SURFACE_TOKEN.finditer(text))


def _month_near_year(text: str) -> bool:
    """A month word and a calendar year in the same short phrase."""
    years = [match.start() for match in _NEAR_YEAR.finditer(text)]
    if not years:
        return False
    return any(
        any(abs(month.start() - year) <= 32 for year in years) for month in _MONTH_WORD.finditer(text)
    )


def _scrub_obj(
    value,
    symbols: tuple[str, ...],
    *,
    drop_prices: bool,
    scale: float | None = None,
    allow_list: bool = False,
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
                item, symbols, drop_prices=drop_prices, scale=scale, allow_list=allow_list
            )
        return clean
    if isinstance(value, list):
        return [
            _scrub_obj(item, symbols, drop_prices=drop_prices, scale=scale, allow_list=allow_list)
            for item in value
        ]
    if isinstance(value, str):
        return _scrub_string(value, symbols, scale)
    return value


def _scrub_string(value: str, symbols: tuple[str, ...], scale: float | None) -> str:
    return designer_surface_text(value, symbols, scale=scale)


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
    """Monthly and yearly keys become ``M1``, ``M2`` and ``Y1``, ``Y2``. Every value stays."""
    clean = {}
    used: set[str] = set()
    for key, item in value.items():
        name = str(key)
        if name in {"monthly", "yearly"} and isinstance(item, dict):
            prefix = "M" if name == "monthly" else "Y"
            nested = {}
            for index, child in enumerate(item.values(), start=1):
                nested[f"{prefix}{index}"] = _scrub_obj(
                    child, symbols, drop_prices=drop_prices, scale=scale, allow_list=allow_list
                )
            clean[name] = nested
            used.add(name)
            continue
        if _hide_column(name, drop_prices=drop_prices, allow_list=allow_list):
            continue
        renamed = _unique_key(designer_surface_text(name, symbols, scale=scale), used)
        clean[renamed] = _scrub_obj(
            item, symbols, drop_prices=drop_prices, scale=scale, allow_list=allow_list
        )
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
        # Headers use the same stripper as values. A ticker-only header becomes RANDOM.
        renamed = _unique_key(designer_surface_text(name, symbols, scale=scale), used)
        pairs.append((name, renamed))
    rows = []
    for row in table.rows:
        cleaned = {}
        for original, renamed in pairs:
            value = row.get(original)
            if isinstance(value, str):
                value = _scrub_string(value, symbols, scale)
            cleaned[renamed] = value
        rows.append(cleaned)
    return _replace_table(table, [renamed for _, renamed in pairs], rows)


def apply_designer(report, symbols: tuple[str, ...], *, redacted: bool, scale: float | None = None) -> None:
    """Mutate ``report`` so a designer screen cannot render operator secrets.

    ``redacted`` is retained for callers. Every designer surface uses the
    allow-list, whether or not a published ``designer/`` tree is what was read.
    """
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


def _wordish(token: str) -> bool:
    return bool(_FREE_WORD.fullmatch(token) or _HYPHEN_WORD.fullmatch(token))


def _numeric_text(token: str) -> str:
    """The number inside a surface token, without $, thousands commas, or %."""
    text = token.strip()
    sign = ""
    if text[:1] in "+-":
        sign, text = text[:1], text[1:]
    if text.startswith("$"):
        text = text[1:]
    if text.endswith("%"):
        text = text[:-1]
    return sign + text.replace(",", "")


def _exponent_text(token: str) -> str | None:
    match = re.search(r"[eE]([+-]?(?:\d+(?:\.\d*)?|\.\d+))$", token)
    if match is None:
        return None
    return match.group(1)


def _significand_digits(literal: str) -> str:
    """Digits of the significand, with the decimal point and end zeros removed."""
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
    ratio = abs(value / base)
    log = math.log10(ratio)
    nearest = round(log)
    if abs(nearest) > 24:
        return False
    return math.isclose(ratio, 10.0**nearest, rel_tol=1e-6, abs_tol=0.0)


def _literal_is_scale(literal: str, scale: float) -> bool:
    """True when this literal is the scale, its reciprocal, or the scale times 10^k.

    The significand digit string is compared with the scale's digit string.
    The parsed value is compared with the scale, with the reciprocal, and
    with the scale scaled by powers of ten. The exponent is the same kind of literal.
    """
    if _significand_digits(literal) == _scale_digits(scale):
        return True
    exponent = _exponent_text(literal)
    if exponent is not None and exponent != literal and _literal_is_scale(exponent, scale):
        return True
    try:
        value = abs(float(literal))
    except ValueError:
        return False
    if not math.isfinite(value):
        return False
    if scale != 0 and math.isclose(value, abs(1.0 / scale), rel_tol=1e-9, abs_tol=1e-12):
        return True
    return _power_of_ten_multiple(value, abs(scale)) or math.isclose(
        value, abs(scale), rel_tol=1e-9, abs_tol=1e-12
    )


def _scale_in_token(token: str, scale: float | None) -> bool:
    """True when any numeric literal in ``token`` is the invoice scale.

    One regex pulls every literal (optional sign, digits, optional decimal,
    optional exponent), glued to letters or not. A bare integer counts only
    when it is the whole token, so ``long_2x`` stays and a standalone ``2``
    does not. ``2.5`` is a different significand. The screen and the export
    both call this.
    """
    number = _scale_number(scale)
    if number is None or not token:
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
    """One decision for the screen filter and for ``export_allowed``.

    A signed number stays whole. A hyphenated word stays whole. A month stem
    plus a year, a year glued to letters, and the invoice scale do not.
    ``2024.50`` and the pair ``day 2024`` stay.
    """
    if _GLUED_YEAR.search(token):
        return False
    if _MONTH_IN_TOKEN.search(token) and _YEAR_IN_TOKEN.search(token):
        return False
    if _scale_in_token(token, scale):
        return False
    if token.casefold() in _MONTH_WORDS or token.split(".", 1)[0].casefold() in _MONTH_WORDS:
        return False
    if _NUMBER_SURFACE.fullmatch(token):
        return True
    if _SESSION_ID.fullmatch(token):
        return True
    if _YEAR_TOKEN.fullmatch(token):
        return previous.casefold() == "day"
    if _HYPHEN_WORD.fullmatch(token):
        return not any(part.casefold() in _MONTH_WORDS for part in token.split("-"))
    if _FREE_WORD.fullmatch(token) or _DOTTED_WORD.fullmatch(token):
        return True
    return False


def _surface_pieces(text: str, *, scale: float | None) -> tuple[list[str], bool]:
    """Kept tokens, and whether every token was kept.

    The screen joins the kept tokens. Export fails when any token was dropped.
    Both call ``_keep_surface_token``.
    """
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
    """Keep numbers, session ids, hyphenated words, and words. Drop the rest.

    A signed number, including ``$``, thousands commas, an exponent, and a
    trailing ``%``, is one token. ``pre-market`` stays hyphenated. A month name
    is not a word we show. A four-digit year stays only when the previous word
    is ``day``. This is the structural layer. The regexes have already run.
    """
    pieces, _allowed = _surface_pieces(text, scale=scale)
    return " ".join(pieces)


def designer_surface_text(value: str, symbols: tuple[str, ...] = (), *, scale: float | None = None) -> str:
    """Text a designer banner, warning, header, key, or free-text cell may show.

    Paths are consumed through spaces. A ticker is ``RANDOM``. The remainder is
    limited to numbers, session ids, and words, and the date, ticker, and scale
    regexes still run. Text is NFKC-normalized first, and unicode minus and
    dash characters are ASCII ``-`` before a literal is read.
    """
    raw = normalize_surface(value)
    if "Traceback" in raw or _consume_paths(raw, "") != raw:
        raw = designer_error(raw)
    raw = _consume_paths(raw, " ")
    raw = scrub_text(raw, symbols, scale=scale)
    return _allow_listed_text(raw, scale=scale)


def designer_error(text: str) -> str:
    """Exception summary with filesystem paths removed.

    The operator view keeps the traceback. A designer cell and its tooltip
    show this summary instead.
    """
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
    """Last path segment, with dates and tickers already removed. Not a path."""
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
    """A job row a designer may see. No filesystem path and no ticker."""
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

    A designer audience is always redacted. ``designer/`` is the directory
    when that tree was published. The operator folder is read otherwise.
    Both go through the same allow-list.
    """
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
    """Text used to decide whether a designer may receive ``path``.

    Bytes are decoded as latin-1, so a binary file cannot skip the check by
    failing a UTF-8 decode. PNG text chunks are included even when compressed.
    A file over the read cap is refused before it is read whole.
    """
    suffix = path.suffix.lower()
    if suffix == ".png":
        limit = PNG_READ_LIMIT
    elif suffix == ".csv":
        limit = CSV_READ_LIMIT
    else:
        limit = JSON_READ_LIMIT
    refuse_oversized(path, limit)
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

    JSON and CSV are structured. A numeric value of an allow-listed key is
    not scale-scanned. Free text, identifiers, filenames, labels, and string
    values still are. Text is NFKC-normalized, and unicode minus and dash
    characters are ASCII ``-``, before that scan.
    """
    text = normalize_surface(text)
    structured = _structured_document(text)
    if structured is None:
        return _free_text_allowed(text, symbols, scale)
    if not _preamble_allowed(text, symbols):
        return False
    kind, payload = structured
    if kind == "json":
        return _json_allowed(payload, symbols, scale)
    return _csv_allowed(payload, symbols, scale)


def _preamble_allowed(text: str, symbols: tuple[str, ...]) -> bool:
    """Secrets, dates, paths, and tickers. Scale is a separate pass."""
    folded = text.casefold()
    if any(token in folded for token in _SECRET_TOKENS):
        return False
    if any(pattern.search(text) for pattern in _EXPORT_DATE_PATTERNS):
        return False
    if _month_near_year(text) or _consume_paths(text, "") != text:
        return False
    for symbol in symbols:
        if symbol and _ticker_spans(text, symbol):
            return False
    return True


def _free_text_allowed(text: str, symbols: tuple[str, ...], scale: float | None) -> bool:
    if not _preamble_allowed(text, symbols):
        return False
    if _contains_scale(text, scale):
        return False
    _pieces, allowed = _surface_pieces(text, scale=scale)
    return allowed


def _structured_document(text: str):
    """``("json", value)`` or ``("csv", rows)`` when the file is a table."""
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
    """A rendered allow-listed number: dollars, percent, or basis points."""
    token = cell.strip().replace(",", "").replace("$", "").replace("%", "")
    if token.endswith(" bp"):
        token = token[: -len(" bp")]
    token = token.strip()
    if token in {"", "—", "-", "∞", "-∞"}:
        return True
    return _numeric_cell(token)


def _json_allowed(value, symbols: tuple[str, ...], scale: float | None, *, key: str | None = None) -> bool:
    if isinstance(value, dict):
        for child_key, item in value.items():
            name = str(child_key)
            if not _free_text_allowed(name, symbols, scale):
                return False
            if not _json_allowed(item, symbols, scale, key=name):
                return False
        return True
    if isinstance(value, list):
        return all(_json_allowed(item, symbols, scale, key=key) for item in value)
    if isinstance(value, str):
        return _free_text_allowed(value, symbols, scale)
    if isinstance(value, bool) or value is None:
        return True
    if isinstance(value, (int, float)):
        if key is not None and redacted_key_allowed(key):
            return True
        return not _contains_scale(str(value), scale)
    return _free_text_allowed(str(value), symbols, scale)


def _csv_allowed(rows: list[list[str]], symbols: tuple[str, ...], scale: float | None) -> bool:
    header = [cell.strip() for cell in rows[0]]
    if header == ["key", "raw", "display"]:
        for name in header:
            if not _free_text_allowed(name, symbols, scale):
                return False
        for row in rows[1:]:
            if not row:
                continue
            metric = row[0]
            if not _free_text_allowed(metric, symbols, scale):
                return False
            listed = redacted_key_allowed(metric)
            for cell in row[1:]:
                if listed and (_numeric_cell(cell) or _formatted_number(cell)):
                    if not _preamble_allowed(cell, symbols):
                        return False
                    continue
                if not _free_text_allowed(cell, symbols, scale):
                    return False
        return True
    for name in header:
        if not _free_text_allowed(name, symbols, scale):
            return False
    listed = [redacted_key_allowed(name) for name in header]
    for row in rows[1:]:
        for index, cell in enumerate(row):
            safe = index < len(listed) and listed[index] and _numeric_cell(cell)
            if safe:
                continue
            if not _free_text_allowed(cell, symbols, scale):
                return False
    return True
