"""Desk fee schedule.

This is not the Arena schedule. SEC and FINRA TAF differ on purpose.
Shared IBKR Pro Tiered pieces (tiers, minimum, cap, exchange, clearing,
pass-through, CAT) use the same numbers and the same order of operations as
the Arena reference, duplicated here so a change to one file cannot retune
the other. See docs/DESK_SPEC_DECISIONS.md.

Per-share pieces are charged on abs(shares) * share_factor.
Trade value for the caps and for SEC is abs(shares) * fill price, unscaled.
"""

from __future__ import annotations

import math

COMMISSION_TIERS = (
    (300_000, 0.0035),
    (3_000_000, 0.0020),
    (20_000_000, 0.0015),
    (100_000_000, 0.0010),
    (math.inf, 0.0005),
)
COMMISSION_MIN = 0.35
COMMISSION_MAX_FRAC = 0.01
EXCHANGE_PER_SHARE = 0.0030
CLEARING_PER_SHARE = 0.00020
CLEARING_MAX_FRAC = 0.005
PASS_THROUGH_NYSE = 0.000175
PASS_THROUGH_FINRA = 0.00056
CAT_PER_SHARE = 0.000003
# Desk sell fees. Not the Arena rates.
SEC_FEE_RATE = 27.80e-6
TAF_PER_SHARE = 0.000166
TAF_MAX = 8.30
SLIPPAGE_BPS = 1.0
BORROW_RATE_ANNUAL = 0.0025
FINANCING_RATE_ANNUAL = 0.07
SESSIONS_PER_YEAR = 252
SESSIONS_PER_MONTH = 21

_FEE_KEYS = ("commission", "exchange", "clearing", "pass_through", "cat", "sec", "taf", "total")


def tiered_commission(shares, month_shares_before):
    """Marginal tiered commission. Same breakpoints and walk as the Arena schedule."""
    remaining = shares
    pos = month_shares_before
    total = 0.0
    lower = 0
    for upper, rate in COMMISSION_TIERS:
        if remaining <= 0:
            break
        if pos < upper:
            take = min(remaining, upper - pos)
            total += take * rate
            remaining -= take
            pos += take
        lower = upper
    return total


def fill_costs(shares_signed, price, month_shares_before, share_factor=1.0, fee_mult=1.0):
    """Dollar fees for one Desk fill. fee_mult scales the invoice after the schedule."""
    if share_factor <= 0.0:
        raise ValueError("share_factor must be positive")
    if fee_mult <= 0.0:
        raise ValueError("fee_mult must be positive")
    q = abs(shares_signed) * share_factor
    value = abs(shares_signed) * price
    if q == 0:
        return {
            "commission": 0.0,
            "exchange": 0.0,
            "clearing": 0.0,
            "pass_through": 0.0,
            "cat": 0.0,
            "sec": 0.0,
            "taf": 0.0,
            "total": 0.0,
            "counted_shares": 0.0,
            "tier_shares": 0.0,
        }
    comm = tiered_commission(q, month_shares_before)
    comm = max(comm, COMMISSION_MIN)
    capped = comm > COMMISSION_MAX_FRAC * value
    if capped:
        comm = COMMISSION_MAX_FRAC * value
    exch = EXCHANGE_PER_SHARE * q
    clr = min(CLEARING_PER_SHARE * q, CLEARING_MAX_FRAC * value)
    # Arena reference formula: commission * (0.000175 + 0.00056). See decisions doc.
    pt = comm * (PASS_THROUGH_NYSE + PASS_THROUGH_FINRA)
    cat = CAT_PER_SHARE * q
    sell = shares_signed < 0
    sec = SEC_FEE_RATE * value if sell else 0.0
    taf = min(TAF_PER_SHARE * q, TAF_MAX) if sell else 0.0
    parts = {
        "commission": comm,
        "exchange": exch,
        "clearing": clr,
        "pass_through": pt,
        "cat": cat,
        "sec": sec,
        "taf": taf,
    }
    if fee_mult != 1.0:
        for key in parts:
            parts[key] *= fee_mult
    total = 0.0
    for key in _FEE_KEYS:
        if key == "total":
            continue
        total += parts[key]
    parts["total"] = total
    parts["counted_shares"] = q
    parts["tier_shares"] = 0.0 if capped else q
    return parts


def slipped_price(price, shares_signed, slippage_bps=SLIPPAGE_BPS, fee_mult=1.0):
    """Adverse slippage. Buys pay up, sells receive less. fee_mult scales the bps."""
    s = slippage_bps * fee_mult * 1e-4
    if shares_signed > 0:
        return price * (1.0 + s)
    return price * (1.0 - s)


def financing_due(shares, price, equity, *, rate=FINANCING_RATE_ANNUAL, sessions_per_year=SESSIONS_PER_YEAR):
    """0.07/252 * max(0, |position value| - equity), one session boundary."""
    borrowed = abs(shares * price) - equity
    if borrowed < 0.0:
        borrowed = 0.0
    return rate / sessions_per_year * borrowed


def borrow_due(shares, price, *, rate=BORROW_RATE_ANNUAL, sessions_per_year=SESSIONS_PER_YEAR):
    """0.25%/252 of short notional. Longs pay zero."""
    notional = -shares * price
    if notional < 0.0:
        notional = 0.0
    return rate / sessions_per_year * notional


def breached_liquidation(equity, hwm, fraction=0.30):
    """True when equity is strictly below fraction * running high-water mark."""
    return hwm > 0.0 and equity < fraction * hwm


def margin_call_required(gross_leverage, limit=3.15):
    """True when gross leverage is strictly above the desk limit."""
    return gross_leverage > limit


def gross_leverage(shares, price, equity):
    if equity > 0.0:
        return abs(shares * price) / equity
    if shares == 0:
        return 0.0
    return math.inf
