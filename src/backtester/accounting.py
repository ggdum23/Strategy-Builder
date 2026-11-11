"""Price basis, cash interest, and financing. One record for every run.

The Arena and Desk engines do not share a fill, a borrow charge, or a
liquidation rule. This module names the switches those engines record and
the cash amounts a session boundary may add. It does not price a fill.

Rates are decimals. ``0.07`` is 7 percent per year, not 7. A rate above
``1.0`` (100 percent per year) is rejected. Withholding is the fraction of
a positive long dividend kept back, from ``0`` through ``1``.

Accrual is one step of ``rate / 252`` at each session boundary. The base is
the previous session's snap, taken before that charge. There is no
calendar-day count here. The referee's real-month mode is a different
switch and is not applied by this module.
"""

from __future__ import annotations

import math

from .profiles import get_profile

ACCRUAL = "session_boundary_1_252"
SESSIONS_PER_YEAR = 252
PRICE_BASES = frozenset({"raw", "total_return"})
METHODS = frozenset({"none", "profile_dividends", "cash_dividends", "registry_series"})

# Closed tokens a designer report may print. They are not tickers.
VOCABULARY = frozenset(
    {
        "raw",
        "total_return",
        "none",
        "profile_dividends",
        "cash_dividends",
        "registry_series",
        ACCRUAL,
    }
)

# Leaderboard columns, in order. ``profile`` is already its own column.
LEADERBOARD_KEYS = (
    "price_basis",
    "total_return_method",
    "series_kind",
    "cash_rate_annual",
    "financing_rate_annual",
    "withholding_rate",
    "accrual",
    "sessions_per_year",
    "dividends_applied",
)

# Compared as a tuple. A leaderboard with two different tuples is refused.
IDENTITY_KEYS = (
    "profile",
    "price_basis",
    "total_return_method",
    "series_kind",
    "cash_rate_annual",
    "financing_rate_annual",
    "withholding_rate",
    "accrual",
    "sessions_per_year",
    "dividends_applied",
)

RECORD_KEYS = IDENTITY_KEYS

_BOOL_NAMES = frozenset({"bool", "bool_", "bool8"})


class Accounting:
    """The switches one run used. ``as_dict`` is the public record."""

    def __init__(
        self,
        *,
        profile: str,
        price_basis: str,
        total_return_method: str,
        series_kind: str,
        cash_rate_annual: float,
        financing_rate_annual: float,
        withholding_rate: float,
        dividends_applied: bool,
    ):
        self.profile = profile
        self.price_basis = price_basis
        self.total_return_method = total_return_method
        self.series_kind = series_kind
        self.cash_rate_annual = cash_rate_annual
        self.financing_rate_annual = financing_rate_annual
        self.withholding_rate = withholding_rate
        self.accrual = ACCRUAL
        self.sessions_per_year = SESSIONS_PER_YEAR
        self.dividends_applied = bool(dividends_applied)

    def as_dict(self) -> dict:
        return {key: getattr(self, key) for key in RECORD_KEYS}

    def identity(self) -> tuple:
        return tuple(getattr(self, key) for key in IDENTITY_KEYS)

    def leaderboard_fields(self) -> dict:
        return {key: getattr(self, key) for key in LEADERBOARD_KEYS}


def _reject_bool_or_text(name: str, value) -> None:
    if type(value).__name__ in _BOOL_NAMES:
        raise ValueError(f"{name} must be a decimal, not bool")
    if isinstance(value, (str, bytes, bytearray)):
        raise ValueError(
            f"{name} must be a decimal, not text. 0.07 means 7% per year and 0.15 means 15% withheld."
        )


def require_annual_rate(name: str, value) -> float:
    """A finite annual decimal in ``[0, 1]``. ``1`` is 100 percent per year."""
    _reject_bool_or_text(name, value)
    try:
        rate = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a decimal annual rate") from exc
    if not math.isfinite(rate):
        raise ValueError(f"{name} must be a finite decimal annual rate")
    if rate < 0.0:
        raise ValueError(f"{name} cannot be negative")
    if rate > 1.0:
        raise ValueError(
            f"{name} {rate} is above 1.0. Rates are decimals: 0.07 means 7% per year, and 1.0 means 100%."
        )
    return rate


def require_withholding_rate(name: str, value) -> float:
    """Fraction of a positive long dividend withheld, from 0 through 1."""
    _reject_bool_or_text(name, value)
    try:
        rate = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a decimal fraction") from exc
    if not math.isfinite(rate):
        raise ValueError(f"{name} must be a finite decimal fraction")
    if rate < 0.0:
        raise ValueError(f"{name} cannot be negative")
    if rate > 1.0:
        raise ValueError(
            f"{name} {rate} is above 1.0. Withholding is a fraction of the dividend: 0.15 means 15%."
        )
    return rate


def normalize_price_basis(value) -> str:
    """``raw`` or ``total_return``. ``None`` is ``raw``. ``total-return`` is accepted."""
    if value is None:
        return "raw"
    if isinstance(value, str):
        text = value.strip().lower().replace("-", "_")
        if text in PRICE_BASES:
            return text
    raise ValueError(f"price_basis must be raw or total_return, got {value!r}")


def normalize_series_kind(value) -> str:
    """The kind of series the file is. Same vocabulary as the price basis."""
    if value is None:
        return "raw"
    if isinstance(value, str):
        text = value.strip().lower().replace("-", "_")
        if text in PRICE_BASES:
            return text
    raise ValueError(f"series kind must be raw or total_return, got {value!r}")


def borrowed_base(shares, price, equity) -> float:
    """``max(0, |position value| - equity)`` on one pre-charge snap."""
    borrowed = abs(shares * price) - equity
    if borrowed < 0.0:
        borrowed = 0.0
    return borrowed


def idle_base(shares, price, equity) -> float:
    """``max(0, equity - |position value|)`` on the same snap.

    Idle cash and borrowed money are not both positive. They are both zero
    when the position value equals equity.
    """
    idle = equity - abs(shares * price)
    if idle < 0.0:
        idle = 0.0
    return idle


def session_accrual(rate_annual: float, base: float, sessions_per_year: int = SESSIONS_PER_YEAR) -> float:
    """One session of simple accrual. ``rate_annual / 252 * base`` at the default."""
    return rate_annual / sessions_per_year * base


def dividend_cash(shares, amount, withholding_rate: float) -> float:
    """Cash from one dividend.

    ``withholding_rate == 0`` evaluates ``shares * amount`` and returns that
    product. A positive long dividend with a positive withholding rate
    receives ``shares * amount * (1 - withholding)``. A short pays the gross
    ``shares * amount``. A non-positive amount is not withheld.
    """
    if withholding_rate == 0.0:
        return shares * amount
    if shares > 0.0 and amount > 0.0:
        return shares * amount * (1.0 - withholding_rate)
    return shares * amount


def resolve_accounting(
    profile: str,
    *,
    price_basis=None,
    series_kind=None,
    cash_rate_annual=None,
    financing_rate_annual=None,
    withholding_rate=None,
    dividends=None,
) -> Accounting:
    """Turn omitted switches into the profile default and name the method.

    Arena's omitted financing rate is 0. Desk's omitted financing rate is the
    desk rate (0.07). Omitted cash and withholding are 0. A raw Arena run
    still refuses a dividend file. A raw Desk run with a dividend file is
    ``profile_dividends``. ``price_basis="total_return"`` on a raw series is
    ``cash_dividends`` and requires that file. A series declared
    ``total_return`` runs only when the price basis is ``total_return``, and
    then as ``registry_series`` with no dividend file.
    """
    name = get_profile(profile).name
    chosen = get_profile(name)
    basis = normalize_price_basis(price_basis)
    kind = normalize_series_kind(series_kind)
    cash = 0.0 if cash_rate_annual is None else require_annual_rate("cash_rate_annual", cash_rate_annual)
    if financing_rate_annual is None:
        financing = float(chosen.financing_rate_annual)
    else:
        financing = require_annual_rate("financing_rate_annual", financing_rate_annual)
    withholding = (
        0.0 if withholding_rate is None else require_withholding_rate("withholding_rate", withholding_rate)
    )
    has_dividends = bool(dividends)
    if kind == "total_return" and basis != "total_return":
        raise ValueError(
            f"series is total_return and is not runnable. This phase executes raw prices only."
        )
    if kind == "total_return":
        if has_dividends:
            raise ValueError(
                "a total-return series already includes distributions. Refusing a dividends file on top of it."
            )
        if withholding != 0.0:
            raise ValueError(
                "withholding applies to cash dividends. A registry total-return series has withholding 0."
            )
        method = "registry_series"
        applied = False
    elif basis == "total_return":
        if not has_dividends:
            raise ValueError(
                "price basis total_return on a raw series needs a dividends file. "
                "A registry series declared total_return is the other method."
            )
        method = "cash_dividends"
        applied = True
    elif has_dividends:
        if name == "arena":
            raise ValueError("The arena profile does not credit dividends.")
        method = "profile_dividends"
        applied = True
    else:
        if withholding != 0.0:
            raise ValueError("withholding needs a dividends file")
        method = "none"
        applied = False
    return Accounting(
        profile=name,
        price_basis=basis,
        total_return_method=method,
        series_kind=kind,
        cash_rate_annual=cash,
        financing_rate_annual=financing,
        withholding_rate=withholding,
        dividends_applied=applied,
    )


def stamp_accounting(metrics: dict, accounting: Accounting) -> dict:
    """Write the flat keys and a nested ``accounting`` object onto ``metrics``."""
    record = accounting.as_dict()
    metrics.update(record)
    metrics["accounting"] = dict(record)
    metrics.setdefault("financing_cost", 0.0)
    metrics.setdefault("cash_interest", 0.0)
    metrics.setdefault("dividends_net", 0.0)
    return metrics


def row_identity(row: dict):
    """The accounting tuple on one leaderboard row, or None when the row has none.

    Rows written before these columns existed have no ``price_basis``. Those
    rows are not compared. A row that records a price basis is compared.
    """
    basis = row.get("price_basis")
    if basis is None or basis == "":
        return None
    values = []
    for key in IDENTITY_KEYS:
        value = row.get(key)
        if key == "dividends_applied" and isinstance(value, str):
            folded = value.strip().casefold()
            if folded == "true":
                value = True
            elif folded == "false":
                value = False
        values.append(value)
    return tuple(values)


def refuse_mixed_rows(rows) -> None:
    """Raise when stamped rows disagree. Rows with no price basis are ignored."""
    seen = []
    for row in rows:
        ident = row_identity(row)
        if ident is None or ident in seen:
            continue
        seen.append(ident)
    if len(seen) > 1:
        raise ValueError(
            "leaderboard refuses rows with different price basis, series kind, or cash and financing settings. "
            "Compare those runs separately."
        )
