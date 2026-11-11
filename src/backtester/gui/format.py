"""Format a value that is already in a report. No formulas."""

from __future__ import annotations

import math
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

MISSING = "—"


def is_missing(value: Any) -> bool:
    """True when the report did not contain a usable number or flag.

    ``False`` and ``0`` are present. ``None``, blanks, and NaN are not.
    Infinity is present: the report stored a limit (Calmar with no drawdown).
    """
    if value is None:
        return True
    if isinstance(value, bool):
        return False
    if isinstance(value, float):
        return math.isnan(value)
    if isinstance(value, str):
        return value.strip() == ""
    return False


def infinity_text(number: float) -> str:
    """Display for a stored non-finite limit. NaN is not passed here."""
    return "∞" if number > 0 else "−∞"


def _grouped(value: float, digits: int) -> str:
    sign = "-" if value < 0 else ""
    text = f"{abs(value):,.{digits}f}"
    return sign + text


def format_number(kind: str, value: Any) -> str:
    """Render ``value`` for ``kind``. Missing stays an em dash, never zero."""
    if isinstance(value, float) and math.isinf(value):
        return infinity_text(value)
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
    if math.isnan(number):
        return MISSING
    if math.isinf(number):
        return infinity_text(number)
    if kind == "fraction":
        return _fraction_text(number)
    if kind == "money":
        return "$" + _grouped(number, 2)
    if kind == "money_full":
        return "$" + _grouped(number, _money_digits(number))
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
    if kind == "ratio2":
        return _grouped(number, 2)
    if kind == "number":
        if float(number).is_integer() and abs(number) < 1e15:
            return f"{int(number):,}"
        return f"{number:.6g}"
    return str(value)


def _money_digits(number: float) -> int:
    """The value's own scale, at least two places, so fee rows can add up."""
    try:
        exponent = Decimal(str(number)).as_tuple().exponent
    except Exception:
        return 2
    if isinstance(exponent, str):
        return 2
    return max(2, min(8, -int(exponent)))


def _fraction_text(number: float) -> str:
    """Percent. A non-zero value that would round to 0.00% keeps more digits."""
    scaled = number * 100.0
    if scaled == 0.0:
        return "0.00%"
    if round(scaled, 2) == 0.0:
        # Eight places still print 0.00000000% for a tiny fraction. Say so.
        if abs(scaled) < 5e-9:
            return f"{scaled:.2e}%"
        magnitude = math.floor(math.log10(abs(scaled)))
        decimals = min(8, max(4, -magnitude + 1))
        return _grouped(scaled, decimals) + "%"
    # Half away from zero, so a stored -0.04215 reads -4.22%.
    rounded = Decimal(str(scaled)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    sign = "-" if rounded < 0 else ""
    return sign + f"{abs(rounded):,.2f}%"


def format_cell(value: Any) -> str:
    """Table cell. Numbers keep full precision to 6 significant figures, not a new metric."""
    if is_missing(value):
        return MISSING
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int) and not isinstance(value, bool):
        return f"{value:,}"
    if isinstance(value, float):
        if math.isnan(value):
            return MISSING
        if math.isinf(value):
            return infinity_text(value)
        if value.is_integer() and abs(value) < 1e15:
            return f"{int(value):,}"
        return f"{value:.6g}"
    return str(value)


def sort_token(value: Any):
    """A comparable sort key. Missing sorts first. Types are not mixed at the same rank.

    NaN sorts with the missing group. Infinity sorts as a number, after every
    finite value when ascending and before them when it is negative.
    """
    if isinstance(value, float) and math.isinf(value):
        return (1, float(value))
    if is_missing(value):
        return (0, "")
    if isinstance(value, bool):
        return (1, int(value))
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and math.isnan(value):
            return (0, "")
        return (1, float(value))
    return (2, str(value).lower())
