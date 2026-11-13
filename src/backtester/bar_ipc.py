"""Length-prefixed JSON frames for the one-bar referee.

The child process imports this module by name from its work directory, or as
``backtester.bar_ipc`` when the interpreter is frozen. The standard library
only: the jail runs ``python -I -S``.

One frame is a little-endian length and then one JSON object. A second frame
in the same response is unread output. The parent never sends the next bar
until it has read the decision for this one.
"""

from __future__ import annotations

import json
import struct

MAX_FRAME = 1_000_000
_HEADER = struct.Struct("<I")


def dumps(payload: dict) -> bytes:
    """One frame. ``payload`` must already be JSON-safe (no bare NaN)."""
    if not isinstance(payload, dict):
        raise TypeError("protocol payload must be an object")
    body = json.dumps(payload, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(body) > MAX_FRAME:
        raise ValueError("request is too large")
    return _HEADER.pack(len(body)) + body


def loads(body: bytes) -> dict:
    payload = json.loads(body.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("protocol payload must be an object")
    return payload
