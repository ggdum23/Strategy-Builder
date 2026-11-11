"""Allow-list for every designer-facing artifact.

``publish_designer`` and the desktop window both import this module. A
designer surface keeps these names and drops every other key. There is no
second designer path that still shows share counts, prices, notionals,
per-component fees, or accounting dollars.

Exact equity stays. Fees are a cost drag in basis points of that equity,
rounded to 0.1 bp, because a dollar fee total divided by the public
per-share schedule recovers the share count. ``docs/PACK.md`` records that
residual.
"""

from __future__ import annotations

import hashlib
import math
import re
import zlib
from decimal import Decimal, ROUND_HALF_UP

# Returns, ratios, exposure, drawdowns, counts, bar and session indices, and
# the settings that cannot be inverted into a price. No share counts, prices,
# notionals, per-component fees, dollar fee totals, or accounting dollars.
REDACTED_KEYS = frozenset(
    {
        "total_return",
        "cagr",
        "ann_vol",
        "max_drawdown",
        "worst_day",
        "best_day",
        "hit_rate",
        "percent_up_days",
        "excess_total_return",
        "bh_total_return",
        "long_2x_total_return",
        "frac_bars_long",
        "frac_bars_short",
        "frac_sessions_long_at_close",
        "frac_sessions_short_at_close",
        "extended_hours_fill_notional_frac",
        "borrow_rate_annual",
        "financing_rate_annual",
        "cash_rate_annual",
        "withholding_rate",
        "sharpe",
        "sortino",
        "calmar",
        "turnover_per_year",
        "avg_abs_exposure_close",
        "avg_net_exposure_close",
        "avg_abs_exposure_bars",
        "worst_equity_over_hwm",
        "max_gross_leverage",
        "avg_gross_leverage",
        "fee_mult",
        "years",
        "sessions_per_year",
        "equity",
        "exposure",
        "position",
        "initial_equity",
        "final_equity",
        "sessions",
        "n_fills",
        "round_trips",
        "long_round_trips",
        "short_round_trips",
        "max_dd_duration_sessions",
        "margin_calls_queued",
        "margin_calls_filled",
        "avg_holding_sessions",
        "avg_holding_bars",
        "rank",
        "bar_minutes",
        "timeout_s",
        "disqualified",
        "busted",
        "open_position_at_end",
        "anonymized",
        "cancelled",
        "dividends_applied",
        "first_session",
        "last_session",
        "first_day",
        "last_day",
        "session",
        "ordinal",
        "day",
        "minute",
        "bars",
        "start_ordinal",
        "end_ordinal",
        "start_day",
        "end_day",
        "cost_drag_bps",
        "period_returns",
        "monthly",
        "yearly",
        "liquidation",
        "run",
        "benchmarks",
        "buy_and_hold_1x",
        "long_2x",
        "profile",
        "audience",
        "accounting",
        "price_basis",
        "total_return_method",
        "series_kind",
        "accrual",
        "status",
        "error",
        "state",
        "name",
        "strategy_id",
        "source_id",
        "kind",
        "phase",
        "time",
        "side",
        "leg",
        "message",
    }
)

# Engine block labels are zero-padded: Arena ``S01`` and Desk ``B01``, one
# block per 252 sessions. Sixty blocks is about sixty years. The month and
# year stand-ins are the ``M1`` / ``Y1`` labels the window writes when it
# relabels a monthly or yearly map. ``S99`` is outside this schema.
_BLOCK_KEY = re.compile(r"[SB](?:0[1-9]|[1-5][0-9]|60)\Z")
_MONTH_KEY = re.compile(r"M([1-9][0-9]{0,2})\Z")
_YEAR_KEY = re.compile(r"Y([1-9][0-9]{0,1})\Z")
_MONTH_LIMIT = 600
_YEAR_LIMIT = 60

# One clock token. Hours may be one or two digits. Minutes and seconds are two.
# The screen, the export, and ``publish_designer`` all use this spelling.
CLOCK_BODY = r"(?:[01]?\d|2[0-3]):[0-5]\d(?::[0-5]\d)?"
CLOCK_TEXT = re.compile(CLOCK_BODY + r"\Z")
_SESSION_TEXT = re.compile(r"(?:[SB]\d+|pre|rth|post|eth)\Z")
_SAFE_ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")
_ENUM_VALUES = {
    "phase": frozenset({"pre", "rth", "post"}),
    "profile": frozenset({"desk", "arena"}),
    "audience": frozenset({"designer", "operator"}),
    "status": frozenset({"ok", "error", "timeout", "cancelled", "memory", "crash"}),
    "kind": frozenset({"strategy", "margin", "liquidation"}),
    "price_basis": frozenset({"raw", "total_return"}),
    "series_kind": frozenset({"raw", "total_return"}),
    "total_return_method": frozenset(
        {"none", "profile_dividends", "cash_dividends", "registry_series"}
    ),
    "accrual": frozenset({"session_boundary_1_252"}),
    "leg": frozenset({"design", "locked"}),
    "state": frozenset({"ok", "error", "running", "done"}),
}
_ID_KEYS = frozenset({"strategy_id", "source_id", "name"})
_TIME_KEYS = frozenset({"time"})
_SESSION_KEYS = frozenset({"session", "first_session", "last_session", "session_id"})
_FREE_TEXT_KEYS = frozenset({"message", "error"})
_BOOL_KEYS = frozenset(
    {
        "disqualified",
        "busted",
        "anonymized",
        "cancelled",
        "open_position_at_end",
        "dividends_applied",
    }
)
_BOOL_TEXT = frozenset({"true", "false"})
# Sentences the report itself writes. Anything else on those keys is free text.
_FIXED_TEXT = frozenset(
    {
        "withheld",
        "the strategy failed",
        "not published",
        "stopped",
        "no daily marks",
    }
)
_NON_FINITE_TEXT = frozenset({"inf", "-inf", "nan"})
_DROP = object()


def period_key_allowed(name: str) -> bool:
    """True for an engine block label or a month/year stand-in in the schema."""
    key = str(name)
    if _BLOCK_KEY.fullmatch(key):
        return True
    month = _MONTH_KEY.fullmatch(key)
    if month is not None and int(month.group(1)) <= _MONTH_LIMIT:
        return True
    year = _YEAR_KEY.fullmatch(key)
    return year is not None and int(year.group(1)) <= _YEAR_LIMIT


def redacted_key_allowed(name: str) -> bool:
    """True when a designer surface may keep this field name.

    The screen, the export, and ``publish_designer`` all call this. A key
    that is not listed is dropped.
    """
    key = str(name)
    return key in REDACTED_KEYS or period_key_allowed(key)


def equity_base(metrics: dict):
    """Initial equity when it is a non-zero finite number, otherwise final equity."""
    if not isinstance(metrics, dict):
        return None
    for key in ("initial_equity", "final_equity"):
        value = metrics.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if math.isfinite(value) and value != 0:
            return float(value)
    return None


def cost_drag_bps(fees, equity) -> float | None:
    """``fees / equity * 10000``, rounded half-up to 0.1 bp.

    The dollar fee total is not returned. A missing or zero equity leaves
    the drag absent rather than inventing a zero.
    """
    if isinstance(fees, bool) or isinstance(equity, bool):
        return None
    if fees is None or equity is None:
        return None
    try:
        fee = Decimal(str(fees))
        base = Decimal(str(equity))
    except Exception:
        return None
    if not fee.is_finite() or not base.is_finite() or base == 0:
        return None
    bps = (fee / base) * Decimal(10000)
    return float(bps.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def with_cost_drag(value):
    """Copy ``value`` and add ``cost_drag_bps`` wherever ``fees_total`` sits.

    The dollar total stays on the copy so a later allow-list pass can drop
    it. Nested benchmark books get their own drag from their own equity.
    """
    if isinstance(value, list):
        return [with_cost_drag(item) for item in value]
    if not isinstance(value, dict):
        return value
    drag = cost_drag_bps(value.get("fees_total"), equity_base(value))
    copied = {key: with_cost_drag(item) for key, item in value.items()}
    if drag is not None:
        copied["cost_drag_bps"] = drag
    return copied


def project_designer(value):
    """Allow-listed copy of a metrics, fill, trade, liquidation, or benchmark tree.

    Fee dollars become ``cost_drag_bps``. Share counts, prices, notionals,
    per-component fees, and accounting dollars are absent. A string is kept
    only when the key's schema allows it. Free text such as ``message`` is
    dropped, so a price or a scale cannot ride along in a caption.
    """
    return _keep(with_cost_drag(value))


_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# Chunks a renderer needs. Text and unknown ancillary chunks are not in this set.
_PNG_KEEP = frozenset(
    {b"IHDR", b"PLTE", b"IDAT", b"IEND", b"tRNS", b"gAMA", b"sRGB", b"pHYs"}
)
_PACK_HEX = re.compile(r"pack_([0-9a-fA-F]{8,64})\Z")
# 0-9 then a-f. The result has no digits, so it cannot be read as a number.
_NIBBLE_LETTER = "abcdefghijklmnop"


def designer_source_id(source_id: str) -> str:
    """Opaque id for a ``pack_`` plus hex source id. Anything else is unchanged.

    The designer tree stores this form. The operator tree keeps the original
    id. Hex such as ``185e2`` is scientific notation to a scale check; the
    mapped id is letters only.
    """
    match = _PACK_HEX.fullmatch(str(source_id))
    if match is None:
        return str(source_id)
    hex_id = match.group(1).lower()
    if len(hex_id) > 59:
        hex_id = hashlib.sha256(hex_id.encode("ascii")).hexdigest()[:16]
    letters = "".join(_NIBBLE_LETTER[int(char, 16)] for char in hex_id)
    return "pack-" + letters


def canonical_png(data: bytes) -> bytes:
    """A PNG with only the chunks a renderer needs, and nothing after IEND.

    ``tEXt``, ``zTXt``, ``iTXt``, ``eXIf``, and any other ancillary chunk are
    dropped. An unknown critical chunk, a bad checksum, or a file that is not
    a PNG raises ``ValueError``. Kept chunks are copied unchanged, so the
    decoded pixels stay the same.
    """
    if isinstance(data, bytearray):
        data = bytes(data)
    if not isinstance(data, bytes) or not data.startswith(_PNG_SIGNATURE):
        raise ValueError("not a png")
    pos = len(_PNG_SIGNATURE)
    kept: list[bytes] = []
    saw_ihdr = False
    saw_idat = False
    saw_iend = False
    while pos + 12 <= len(data):
        length = int.from_bytes(data[pos : pos + 4], "big")
        kind = data[pos + 4 : pos + 8]
        end = pos + 8 + length
        if end + 4 > len(data):
            break
        chunk = data[pos + 8 : end]
        crc = int.from_bytes(data[end : end + 4], "big")
        if (zlib.crc32(kind + chunk) & 0xFFFFFFFF) != crc:
            raise ValueError("bad png crc")
        if kind == b"IEND":
            if length != 0 or not saw_ihdr or not saw_idat:
                raise ValueError("malformed png")
            kept.append(data[pos : end + 4])
            saw_iend = True
            break
        if kind == b"IHDR":
            if saw_ihdr or length != 13:
                raise ValueError("malformed png")
            saw_ihdr = True
        elif not saw_ihdr:
            raise ValueError("malformed png")
        if kind == b"IDAT":
            saw_idat = True
        if kind not in _PNG_KEEP:
            if kind[:1].isupper():
                raise ValueError("unknown critical png chunk")
            pos = end + 4
            continue
        kept.append(data[pos : end + 4])
        pos = end + 4
    if not saw_iend:
        raise ValueError("malformed png")
    return _PNG_SIGNATURE + b"".join(kept)


def designer_csv_cell(key: str, cell: str) -> str:
    """The cell ``publish_designer`` may write, or empty when it is free text.

    A numeric cell keeps its original spelling. A clock, a session label, an
    enum, and a safe id stay. Anything else is blank.
    """
    if cell == "":
        return ""
    if _numeric_text(cell):
        if period_key_allowed(key) or key not in _STRING_SCHEMA_KEYS:
            return cell
        return ""
    accepted = accept_string(key, cell)
    if not isinstance(accepted, str):
        return ""
    return accepted


def accept_string(key: str, text: str):
    """A schema string, a number parsed from text, or None when the text is free.

    None means the key is dropped. Numbers stay numbers. Period-key values
    have to be numeric. ``message`` and ``error`` keep a fixed code only.
    """
    if text == "":
        return ""
    if key in _BOOL_KEYS and text in _BOOL_TEXT:
        return text
    if key in _FREE_TEXT_KEYS:
        return text if text in _FIXED_TEXT else None
    if key in _ENUM_VALUES:
        return text if text in _ENUM_VALUES[key] else None
    if key in _ID_KEYS:
        if key == "source_id":
            text = designer_source_id(text)
        return text if _SAFE_ID.fullmatch(text) else None
    if key in _TIME_KEYS:
        return text if CLOCK_TEXT.fullmatch(text) else None
    if key in _SESSION_KEYS:
        return text if _SESSION_TEXT.fullmatch(text) else None
    if period_key_allowed(key):
        return _finite_number(text)
    number = _finite_number(text)
    if number is not None:
        return number
    if text in _NON_FINITE_TEXT:
        return text
    return None


_STRING_SCHEMA_KEYS = _FREE_TEXT_KEYS | frozenset(_ENUM_VALUES) | _ID_KEYS | _TIME_KEYS | _SESSION_KEYS


def _numeric_text(text: str) -> bool:
    number = _finite_number(text.strip())
    return number is not None


def _finite_number(text: str):
    token = text.strip()
    if not token:
        return None
    try:
        if any(mark in token.lower() for mark in (".", "e")):
            number = float(token)
        else:
            number = int(token)
    except ValueError:
        return None
    if isinstance(number, float) and not math.isfinite(number):
        return None
    return number


def _keep(value, key=None):
    if isinstance(value, list):
        kept = []
        for item in value:
            child = _keep(item, key)
            if child is _DROP:
                continue
            kept.append(child)
        return kept
    if isinstance(value, dict):
        kept = {}
        for child_key, item in value.items():
            name = str(child_key)
            if not redacted_key_allowed(name):
                continue
            child = _keep(item, name)
            if child is _DROP:
                continue
            kept[name] = child
        return kept
    if key is None:
        return value
    return _typed_leaf(key, value)


def _typed_leaf(key: str, value):
    if isinstance(value, str):
        accepted = accept_string(key, value)
        return _DROP if accepted is None else accepted
    if period_key_allowed(key):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return _DROP
        if isinstance(value, float) and not math.isfinite(value):
            return _DROP
    return value
