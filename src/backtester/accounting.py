
from __future__ import annotations

import math

from .profiles import get_profile

ACCRUAL = "session_boundary_1_252"
SESSIONS_PER_YEAR = 252
PRICE_BASES = frozenset({"raw", "total_return"})
METHODS = frozenset({"none", "profile_dividends", "cash_dividends", "registry_series"})

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

EXACT_RESERVED = frozenset(
    set(VOCABULARY)
    | set(LEADERBOARD_KEYS)
    | {"accounting", "cash_interest", "financing_cost", "dividends_net"}
)

_BOOL_NAMES = frozenset({"bool", "bool_", "bool8"})


class Accounting:

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
    if value is None:
        return "raw"
    if isinstance(value, str):
        text = value.strip().lower().replace("-", "_")
        if text in PRICE_BASES:
            return text
    raise ValueError(f"price_basis must be raw or total_return, got {value!r}")


def normalize_series_kind(value) -> str:
    if value is None:
        return "raw"
    if isinstance(value, str):
        text = value.strip().lower().replace("-", "_")
        if text in PRICE_BASES:
            return text
    raise ValueError(f"series kind must be raw or total_return, got {value!r}")


def borrowed_base(shares, price, equity) -> float:
    borrowed = abs(shares * price) - equity
    if borrowed < 0.0:
        borrowed = 0.0
    return borrowed


def idle_base(shares, price, equity) -> float:
    idle = equity - abs(shares * price)
    if idle < 0.0:
        idle = 0.0
    return idle


def session_accrual(rate_annual: float, base: float, sessions_per_year: int = SESSIONS_PER_YEAR) -> float:
    return rate_annual / sessions_per_year * base


def dividend_cash(shares, amount, withholding_rate: float) -> float:
    if withholding_rate == 0.0:
        return shares * amount
    if shares > 0.0 and amount > 0.0:
        return shares * amount * (1.0 - withholding_rate)
    return shares * amount


def price_basis_refusal(symbol: str | None, *, splits: bool, declared: str | None = None) -> str:
    kind = "total_return" if declared is None else str(declared)
    who = f"{symbol} is {kind}" if symbol else f"series is {kind}"
    head = f"{who} and is not runnable. "
    if splits:
        return (
            head
            + "A declared split list is not applied. "
            + "--price-basis total_return does not run a series with splits. "
            + "Prices are not rewritten."
        )
    return (
        head
        + "The raw price basis was refused. "
        + "Pass --price-basis total_return to run this series. "
        + "Prices are not rewritten."
    )


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
        raise ValueError(price_basis_refusal(None, splits=False))
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
    record = accounting.as_dict()
    metrics.update(record)
    metrics["accounting"] = dict(record)
    metrics.setdefault("financing_cost", 0.0)
    metrics.setdefault("cash_interest", 0.0)
    metrics.setdefault("dividends_net", 0.0)
    return metrics


def row_identity(row: dict):
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
