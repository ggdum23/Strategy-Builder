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
SEC_FEE_RATE = 0.0000206
TAF_PER_SHARE = 0.000195
TAF_MAX = 9.79
SLIPPAGE_BPS = 1.0
BORROW_RATE_ANNUAL = 0.0030
SESSIONS_PER_YEAR = 252
LOCAL_SESSIONS_PER_MONTH = 21


def tiered_commission(shares, month_shares_before):
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


def fill_costs(shares_signed, price, month_shares_before, share_scale=1.0, taf_active=True):
    q = abs(shares_signed) * share_scale
    value = abs(shares_signed) * price
    if q == 0:
        return {"commission": 0.0, "exchange": 0.0, "clearing": 0.0, "pass_through": 0.0,
                "cat": 0.0, "sec": 0.0, "taf": 0.0, "total": 0.0, "counted_shares": 0.0, "tier_shares": 0.0}
    comm = tiered_commission(q, month_shares_before)
    comm = max(comm, COMMISSION_MIN)
    capped = comm > COMMISSION_MAX_FRAC * value
    if capped:
        comm = COMMISSION_MAX_FRAC * value
    exch = EXCHANGE_PER_SHARE * q
    clr = min(CLEARING_PER_SHARE * q, CLEARING_MAX_FRAC * value)
    pt = comm * (PASS_THROUGH_NYSE + PASS_THROUGH_FINRA)
    cat = CAT_PER_SHARE * q
    sell = shares_signed < 0
    sec = SEC_FEE_RATE * value if sell else 0.0
    taf = min(TAF_PER_SHARE * q, TAF_MAX) if (sell and taf_active) else 0.0
    tot = comm + exch + clr + pt + cat + sec + taf
    return {"commission": comm, "exchange": exch, "clearing": clr, "pass_through": pt,
            "cat": cat, "sec": sec, "taf": taf, "total": tot, "counted_shares": q,
            "tier_shares": 0.0 if capped else q}


def slipped_price(open_price, shares_signed):
    s = SLIPPAGE_BPS * 1e-4
    return open_price * (1.0 + s) if shares_signed > 0 else open_price * (1.0 - s)


def daily_borrow(short_notional):
    return max(0.0, short_notional) * BORROW_RATE_ANNUAL / SESSIONS_PER_YEAR
