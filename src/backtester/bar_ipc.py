"""Length-prefixed frames for the one-bar referee.

The child process imports this module by name from its work directory, or as
``backtester.bar_ipc`` when the interpreter is frozen. The standard library
only: the jail runs ``python -I -S``.

One frame is a little-endian length and then one body. A second frame in the
same response is unread output. The parent never sends the next bar until it
has read the decision for this one.

A bar the engine actually sends is a compact struct (body tag 1). Hello,
state, day-end, and any bar that does not match that shape stay JSON, which
is a body starting with ``{``. A decision is tag 2 when the target is a
scalar the engine accepts. The child still sees one dict per bar, and it
still writes one decision before it can read another request.
"""

from __future__ import annotations

import json
import math
import struct

MAX_FRAME = 1_000_000
_HEADER = struct.Struct("<I")
_TAG_BAR = 1
_TAG_DECISION = 2
_ARENA = 1
_DESK = 2

_ARENA_BAR = (
    "day",
    "weekday",
    "minute",
    "time",
    "session",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "wap",
    "count",
    "filled",
    "short_session",
)
_ARENA_STATE = ("equity", "cash", "shares", "position", "target", "pending_order")
_DESK_BAR = (
    "session",
    "time",
    "phase",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "wap",
    "count",
    "filled",
    "new_session",
)
_DESK_ACCOUNT = (
    "cash",
    "position_shares",
    "equity",
    "exposure",
    "gross_leverage",
    "hwm",
    "drawdown",
    "live",
    "liquidated",
)


def dumps(payload: dict) -> bytes:
    """One JSON frame. ``payload`` must already be JSON-safe (no bare NaN)."""
    if not isinstance(payload, dict):
        raise TypeError("protocol payload must be an object")
    body = json.dumps(payload, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(body) > MAX_FRAME:
        raise ValueError("request is too large")
    return _HEADER.pack(len(body)) + body


def loads(body: bytes) -> dict:
    """One body, JSON or a compact bar/decision struct, as a dict."""
    payload = _decode_body(body)
    if not isinstance(payload, dict):
        raise ValueError("protocol payload must be an object")
    return payload


def pack_bar(payload: dict) -> bytes | None:
    """A compact bar frame, or None when the bar is not the engine's shape.

    None tells the caller to send JSON. A test probe and a day-end stay JSON.
    """
    if not isinstance(payload, dict) or payload.get("op") != "bar":
        return None
    bar = payload.get("bar")
    if not isinstance(bar, dict):
        return None
    if "account" in payload and "state" not in payload:
        body = _pack_desk(bar, payload.get("account"))
    elif "state" in payload and "account" not in payload:
        body = _pack_arena(bar, payload.get("state"))
    else:
        return None
    if body is None:
        return None
    if len(body) > MAX_FRAME:
        return None
    return _HEADER.pack(len(body)) + body


def pack_decision(target) -> bytes | None:
    """A compact decision frame, or None when the target is not a scalar."""
    encoded = _decision_body(target)
    if encoded is None:
        return None
    if len(encoded) > MAX_FRAME:
        return None
    return _HEADER.pack(len(encoded)) + encoded


def _decode_body(body: bytes) -> dict:
    if not body:
        raise ValueError("protocol payload must be an object")
    if body[0] == _TAG_BAR:
        return _unpack_bar(body)
    if body[0] == _TAG_DECISION:
        return _unpack_decision(body)
    if body[0:1] != b"{":
        raise ValueError("protocol payload must be an object")
    payload = json.loads(body.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("protocol payload must be an object")
    return payload


def _clock(minute: int) -> str:
    return f"{minute // 60:02d}:{minute % 60:02d}"


def _as_int(value):
    if isinstance(value, bool) or type(value).__name__ in {"bool_", "bool8"}:
        return None
    if isinstance(value, int):
        return int(value)
    item = getattr(value, "item", None)
    if callable(item):
        try:
            raw = item()
        except Exception:
            return None
        if isinstance(raw, int) and not isinstance(raw, bool):
            return int(raw)
    return None


def _as_float(value):
    if isinstance(value, bool) or type(value).__name__ in {"bool_", "bool8"}:
        return None
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return float(value)
    number = _as_int(value)
    if number is not None:
        return float(number)
    if isinstance(value, float):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _as_bool(value):
    if isinstance(value, bool):
        return bool(value)
    if type(value).__name__ in {"bool_", "bool8"}:
        return bool(value)
    return None


def _text(value, limit: int = 64):
    if not isinstance(value, str):
        item = getattr(value, "item", None)
        if not callable(item):
            return None
        try:
            value = item()
        except Exception:
            return None
        if not isinstance(value, str):
            return None
    raw = value.encode("utf-8")
    if len(raw) > limit:
        return None
    return raw


def _pack_count(value) -> bytes | None:
    number = _as_int(value)
    if number is not None:
        return struct.pack("<Bq", 0, number)
    number = _as_float(value)
    if number is None:
        return None
    return struct.pack("<Bd", 1, number)


def _pack_optional_float(value) -> bytes | None:
    if value is None:
        return struct.pack("<B", 0)
    number = _as_float(value)
    if number is None:
        return None
    return struct.pack("<Bd", 1, number)


def _pack_arena(bar: dict, state) -> bytes | None:
    if set(bar) != set(_ARENA_BAR) or not isinstance(state, dict) or set(state) != set(_ARENA_STATE):
        return None
    day = _as_int(bar["day"])
    weekday = _as_int(bar["weekday"])
    minute = _as_int(bar["minute"])
    session = _text(bar["session"])
    if day is None or weekday is None or minute is None or session is None:
        return None
    if _clock(minute) != str(bar["time"]):
        return None
    floats = []
    for name in ("open", "high", "low", "close", "volume", "wap"):
        number = _as_float(bar[name])
        if number is None:
            return None
        floats.append(number)
    count = _pack_count(bar["count"])
    filled = _as_int(bar["filled"])
    short = _as_int(bar["short_session"])
    equity = _as_float(state["equity"])
    cash = _as_float(state["cash"])
    position = _as_float(state["position"])
    target = _as_float(state["target"])
    shares = _as_int(state["shares"])
    pending = _as_bool(state["pending_order"])
    if None in (count, filled, short, equity, cash, position, target, shares, pending):
        return None
    return b"".join(
        (
            struct.pack("<BBi h h B", _TAG_BAR, _ARENA, day, weekday, minute, len(session)),
            session,
            struct.pack("<6d", *floats),
            count,
            struct.pack("<hh", filled, short),
            struct.pack("<4d", equity, cash, position, target),
            struct.pack("<qB", shares, 1 if pending else 0),
        )
    )


def _pack_desk(bar: dict, account) -> bytes | None:
    if set(bar) != set(_DESK_BAR) or not isinstance(account, dict) or set(account) != set(_DESK_ACCOUNT):
        return None
    session = _text(bar["session"])
    phase = _text(bar["phase"])
    minute_text = bar.get("time")
    if not isinstance(minute_text, str) or len(minute_text) != 5 or minute_text[2] != ":":
        return None
    try:
        minute = int(minute_text[:2]) * 60 + int(minute_text[3:])
    except ValueError:
        return None
    if session is None or phase is None or _clock(minute) != minute_text:
        return None
    floats = []
    for name in ("open", "high", "low", "close", "volume", "wap"):
        number = _as_float(bar[name])
        if number is None:
            return None
        floats.append(number)
    count = _as_float(bar["count"])
    filled = _as_int(bar["filled"])
    new_session = _as_bool(bar["new_session"])
    cash = _as_float(account["cash"])
    shares = _as_int(account["position_shares"])
    equity = _as_float(account["equity"])
    exposure = _pack_optional_float(account["exposure"])
    gross = _pack_optional_float(account["gross_leverage"])
    hwm = _as_float(account["hwm"])
    drawdown = _as_float(account["drawdown"])
    live = _as_bool(account["live"])
    liquidated = _as_bool(account["liquidated"])
    if None in (
        count,
        filled,
        new_session,
        cash,
        shares,
        equity,
        exposure,
        gross,
        hwm,
        drawdown,
        live,
        liquidated,
    ):
        return None
    return b"".join(
        (
            struct.pack("<BB", _TAG_BAR, _DESK),
            struct.pack("<B", len(session)),
            session,
            struct.pack("<B", len(phase)),
            phase,
            struct.pack("<h", minute),
            struct.pack("<6d", *floats),
            struct.pack("<d", count),
            struct.pack("<hB", filled, 1 if new_session else 0),
            struct.pack("<d", cash),
            struct.pack("<q", shares),
            struct.pack("<d", equity),
            exposure,
            gross,
            struct.pack("<ddBB", hwm, drawdown, 1 if live else 0, 1 if liquidated else 0),
        )
    )


def _unpack_bar(body: bytes) -> dict:
    if len(body) < 2:
        raise ValueError("protocol payload must be an object")
    kind = body[1]
    if kind == _ARENA:
        return _unpack_arena(body)
    if kind == _DESK:
        return _unpack_desk(body)
    raise ValueError("protocol payload must be an object")


def _need(body: bytes, offset: int, size: int) -> None:
    if offset + size > len(body):
        raise ValueError("protocol payload must be an object")


def _read_text(body: bytes, offset: int) -> tuple[str, int]:
    _need(body, offset, 1)
    length = body[offset]
    offset += 1
    _need(body, offset, length)
    try:
        text = body[offset : offset + length].decode("utf-8")
    except UnicodeError as exc:
        raise ValueError("protocol payload must be an object") from exc
    return text, offset + length


def _unpack_count(body: bytes, offset: int):
    _need(body, offset, 1)
    kind = body[offset]
    offset += 1
    if kind == 0:
        _need(body, offset, 8)
        number = struct.unpack_from("<q", body, offset)[0]
        return int(number), offset + 8
    if kind == 1:
        _need(body, offset, 8)
        number = struct.unpack_from("<d", body, offset)[0]
        return float(number), offset + 8
    raise ValueError("protocol payload must be an object")


def _unpack_optional_float(body: bytes, offset: int):
    _need(body, offset, 1)
    kind = body[offset]
    offset += 1
    if kind == 0:
        return None, offset
    if kind == 1:
        _need(body, offset, 8)
        return float(struct.unpack_from("<d", body, offset)[0]), offset + 8
    raise ValueError("protocol payload must be an object")


def _unpack_arena(body: bytes) -> dict:
    _need(body, 0, 2 + 4 + 2 + 2 + 1)
    day, weekday, minute, session_len = struct.unpack_from("<ihhB", body, 2)
    offset = 2 + 4 + 2 + 2 + 1
    _need(body, offset, session_len)
    try:
        session = body[offset : offset + session_len].decode("utf-8")
    except UnicodeError as exc:
        raise ValueError("protocol payload must be an object") from exc
    offset += session_len
    _need(body, offset, 8 * 6)
    open_, high, low, close, volume, wap = struct.unpack_from("<6d", body, offset)
    offset += 8 * 6
    count, offset = _unpack_count(body, offset)
    _need(body, offset, 2 + 2 + 8 * 4 + 8 + 1)
    filled, short = struct.unpack_from("<hh", body, offset)
    offset += 4
    equity, cash, position, target = struct.unpack_from("<4d", body, offset)
    offset += 32
    shares, pending = struct.unpack_from("<qB", body, offset)
    offset += 9
    if offset != len(body):
        raise ValueError("protocol payload must be an object")
    minute = int(minute)
    return {
        "op": "bar",
        "bar": {
            "day": int(day),
            "weekday": int(weekday),
            "minute": minute,
            "time": _clock(minute),
            "session": session,
            "open": float(open_),
            "high": float(high),
            "low": float(low),
            "close": float(close),
            "volume": float(volume),
            "wap": float(wap),
            "count": count,
            "filled": int(filled),
            "short_session": int(short),
        },
        "state": {
            "equity": float(equity),
            "cash": float(cash),
            "shares": int(shares),
            "position": float(position),
            "target": float(target),
            "pending_order": bool(pending),
        },
    }


def _unpack_desk(body: bytes) -> dict:
    offset = 2
    session, offset = _read_text(body, offset)
    phase, offset = _read_text(body, offset)
    _need(body, offset, 2)
    minute = int(struct.unpack_from("<h", body, offset)[0])
    offset += 2
    _need(body, offset, 8 * 6)
    open_, high, low, close, volume, wap = struct.unpack_from("<6d", body, offset)
    offset += 48
    _need(body, offset, 8)
    count = float(struct.unpack_from("<d", body, offset)[0])
    offset += 8
    _need(body, offset, 3)
    filled, new_session = struct.unpack_from("<hB", body, offset)
    offset += 3
    _need(body, offset, 8)
    cash = float(struct.unpack_from("<d", body, offset)[0])
    offset += 8
    _need(body, offset, 8)
    shares = int(struct.unpack_from("<q", body, offset)[0])
    offset += 8
    _need(body, offset, 8)
    equity = float(struct.unpack_from("<d", body, offset)[0])
    offset += 8
    exposure, offset = _unpack_optional_float(body, offset)
    gross, offset = _unpack_optional_float(body, offset)
    _need(body, offset, 8 + 8 + 1 + 1)
    hwm, drawdown, live, liquidated = struct.unpack_from("<ddBB", body, offset)
    offset += 18
    if offset != len(body):
        raise ValueError("protocol payload must be an object")
    return {
        "op": "bar",
        "bar": {
            "session": session,
            "time": _clock(minute),
            "phase": phase,
            "open": float(open_),
            "high": float(high),
            "low": float(low),
            "close": float(close),
            "volume": float(volume),
            "wap": float(wap),
            "count": float(count),
            "filled": int(filled),
            "new_session": bool(new_session),
        },
        "account": {
            "cash": float(cash),
            "position_shares": int(shares),
            "equity": float(equity),
            "exposure": exposure,
            "gross_leverage": gross,
            "hwm": float(hwm),
            "drawdown": float(drawdown),
            "live": bool(live),
            "liquidated": bool(liquidated),
        },
    }


def _decision_body(target) -> bytes | None:
    if target is None:
        return bytes((_TAG_DECISION, 0))
    if isinstance(target, bool):
        return bytes((_TAG_DECISION, 2 if target else 1))
    if isinstance(target, int) and not isinstance(target, bool):
        return struct.pack("<BBq", _TAG_DECISION, 3, int(target))
    if isinstance(target, float):
        if math.isnan(target):
            return bytes((_TAG_DECISION, 5))
        if target == math.inf:
            return bytes((_TAG_DECISION, 6))
        if target == -math.inf:
            return bytes((_TAG_DECISION, 7))
        return struct.pack("<BBd", _TAG_DECISION, 4, float(target))
    if target == "NaN":
        return bytes((_TAG_DECISION, 5))
    if target == "Infinity":
        return bytes((_TAG_DECISION, 6))
    if target == "-Infinity":
        return bytes((_TAG_DECISION, 7))
    return None


def _unpack_decision(body: bytes) -> dict:
    if len(body) < 2 or body[0] != _TAG_DECISION:
        raise ValueError("protocol payload must be an object")
    kind = body[1]
    if kind == 0 and len(body) == 2:
        target = None
    elif kind == 1 and len(body) == 2:
        target = False
    elif kind == 2 and len(body) == 2:
        target = True
    elif kind == 3 and len(body) == 10:
        target = int(struct.unpack_from("<q", body, 2)[0])
    elif kind == 4 and len(body) == 10:
        target = float(struct.unpack_from("<d", body, 2)[0])
    elif kind == 5 and len(body) == 2:
        target = float("nan")
    elif kind == 6 and len(body) == 2:
        target = float("inf")
    elif kind == 7 and len(body) == 2:
        target = float("-inf")
    else:
        raise ValueError("protocol payload must be an object")
    return {"op": "decision", "target": target}
