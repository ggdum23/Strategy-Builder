"""Official cost model (IMMUTABLE - do not modify; scoring uses an identical copy).

Broker: IBKR Pro "Tiered" schedule for US-listed shares/ETFs, as published at the time this
arena was built (rates below), applied as a constant schedule over the whole history.
All rates are in USD. Prices in the data pack are anonymized, so per-share components are
computed on the pack's share counts times SHARE_SCALE (1.0 for local runs).

Components per fill (one fill = one order, filled entirely at the next bar's open):
  commission  : marginal monthly-volume tiers (shares traded in the current 'month' so far)
                <=300k sh $0.0035 | 300k-3M $0.0020 | 3M-20M $0.0015 | 20M-100M $0.0010 | >100M $0.0005
                minimum $0.35 per order, maximum 1% of trade value
  exchange    : $0.0030 per share (liquidity-removing marketable order)
  clearing    : $0.00020 per share, capped at 0.5% of trade value
  pass-through: commission * (0.000175 + 0.00056)
  CAT         : $0.000003 per share
  SEC fee     : $0.0000206 * value of sales (sells only)
  FINRA TAF   : $0.000195 per share sold, max $9.79 per trade (sells only)
  slippage    : 1 bp of price, adverse, per side (applied to the fill price, reported separately)
  borrow      : 0.30% per year on short notional, charged per session as notional*0.0030/252
Cash earns 0%, and negative cash (leverage) is charged 0% financing. These are simplifying
assumptions; see README_PACK.md.
"""
import math

COMMISSION_TIERS = (  # (upper bound of monthly cumulative shares, rate per share)
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
LOCAL_SESSIONS_PER_MONTH = 21  # local runs: fee-tier 'month' = block of 21 sessions


def tiered_commission(shares, month_shares_before):
    """Marginal tiered commission for `shares` given shares already traded this month."""
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
    """Return dict of fees (all >=0, USD) for one fill.

    shares_signed : pack share delta (+buy / -sell), whole shares
    price         : fill price (pack units, after slippage)
    share_scale   : multiply pack share counts by this to get counted shares (1.0 locally)
    """
    q = abs(shares_signed) * share_scale  # counted shares
    value = abs(shares_signed) * price    # trade value (USD, scale-invariant)
    if q == 0:
        return {"commission": 0.0, "exchange": 0.0, "clearing": 0.0, "pass_through": 0.0,
                "cat": 0.0, "sec": 0.0, "taf": 0.0, "total": 0.0, "counted_shares": 0.0, "tier_shares": 0.0}
    comm = tiered_commission(q, month_shares_before)
    comm = max(comm, COMMISSION_MIN)
    capped = comm > COMMISSION_MAX_FRAC * value
    if capped:  # capped orders do not count toward monthly volume tiers
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
