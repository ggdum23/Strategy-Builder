"""Format a value that is already in a report. No formulas."""

from __future__ import annotations

import math
from typing import Any

MISSING = "—"


def is_missing(value: Any) -> bool:
    """True when the report did not contain a usable number or flag.

    ``False`` and ``0`` are present. ``None``, blanks, and non-finite floats are not.
    """
    if value is None:
        return True
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return isinstance(value, float) and not math.isfinite(value)
    if isinstance(value, str):
        return value.strip() == ""
    return False


def _grouped(value: float, digits: int) -> str:
    sign = "-" if value < 0 else ""
    text = f"{abs(value):,.{digits}f}"
    return sign + text


def format_number(kind: str, value: Any) -> str:
    """Render ``value`` for ``kind``. Missing stays an em dash, never zero."""
    if is_missing(value):
        return MISSING
    if kind == "flag":
        if isinstance(value, str):
            return value
        return "DISQUALIFIED" if bool(value) else "no"
    if kind == "text":
        return str(value)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if not math.isfinite(number):
        return MISSING
    if kind == "fraction":
        return _grouped(number * 100.0, 2) + "%"
    if kind == "money":
        return "$" + _grouped(number, 2)
    if kind == "count":
        if float(number).is_integer():
            return f"{int(number):,}"
        return _grouped(number, 2)
    if kind == "year":
        return _grouped(number, 3)
    if kind == "leverage":
        return _grouped(number, 2) + "×"
    if kind == "ratio":
        return _grouped(number, 3)
    return str(value)


def format_cell(value: Any) -> str:
    """Table cell. Numbers keep full precision to 6 significant figures, not a new metric."""
    if is_missing(value):
        return MISSING
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int) and not isinstance(value, bool):
        return f"{value:,}"
    if isinstance(value, float):
        if not math.isfinite(value):
            return MISSING
        if value.is_integer() and abs(value) < 1e15:
            return f"{int(value):,}"
        return f"{value:.6g}"
    return str(value)


def sort_token(value: Any):
    """A comparable sort key. Missing sorts first. Types are not mixed at the same rank."""
    if is_missing(value):
        return (0, "")
    if isinstance(value, bool):
        return (1, int(value))
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and not math.isfinite(value):
            return (0, "")
        return (1, float(value))
    return (2, str(value).lower())
