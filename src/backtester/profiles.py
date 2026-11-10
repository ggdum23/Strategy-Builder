"""Arena and Desk profiles.

The two profiles do not share a fill venue, leverage cap, financing rule,
borrow rate, dividend rule, fee table, warm-up, or liquidation rule.
This module names those choices. It does not price a fill.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Profile:
    """Switches that must stay on one side of the Arena/Desk line."""

    name: str
    max_abs_target: float
    fill_venue: str
    borrow_rate_annual: float
    financing_rate_annual: float
    dividends: bool
    liquidation_hwm_fraction: float | None
    margin_call_gross: float | None
    warmup: str
    fee_table: str
    sessions_per_fee_month: int
    share_factor_default: float


ARENA = Profile(
    name="arena",
    max_abs_target=2.0,
    fill_venue="next_delivered_bar",
    borrow_rate_annual=0.0030,
    financing_rate_annual=0.0,
    dividends=False,
    liquidation_hwm_fraction=None,
    margin_call_gross=None,
    warmup="rescale_keep_exposure",
    fee_table="arena",
    sessions_per_fee_month=21,
    share_factor_default=1.0,
)

DESK = Profile(
    name="desk",
    max_abs_target=3.0,
    fill_venue="next_rth_open",
    borrow_rate_annual=0.0025,
    financing_rate_annual=0.07,
    dividends=True,
    liquidation_hwm_fraction=0.30,
    margin_call_gross=3.15,
    warmup="flat_until_live",
    fee_table="desk",
    sessions_per_fee_month=21,
    share_factor_default=1.0,
)

PROFILES = {"arena": ARENA, "desk": DESK}


def get_profile(name: str) -> Profile:
    key = name.strip().lower()
    try:
        return PROFILES[key]
    except KeyError as exc:
        known = ", ".join(sorted(PROFILES))
        raise ValueError(f"unknown profile {name!r}; expected one of: {known}") from exc
