"""Arena profile engine, ported from the reference pack.

``run`` and ``metrics`` follow that engine. Defaults are bit-identical: same
fills, fees, borrow and equity. See docs/ARENA_PORT_NOTES.md. The verbatim
reference is tests/reference/arena/backtest.py (SHA-256 pinned).

Official backtest engine (behavior-identical port of the immutable reference).

Execution model
- Bars are delivered one at a time, in time order. Strategies that set WANTS_EXTENDED_HOURS = True
  receive pre/post-session bars too; otherwise only regular-session ('rth') bars are delivered.
- After bar t has CLOSED the engine calls strategy.on_bar(bar, state) -> target exposure in [-2, 2]
  (fraction of current equity, negative = short) or None (= keep the current target).
- A changed target is filled at the OPEN of the NEXT DELIVERED bar (a decision on the last bar of
  a session therefore fills at the first delivered bar of the next session, gap included).
  There are no same-bar fills.
- Shares = trunc(target * equity_at_fill_open / fill_open), whole shares; equity_at_fill_open is
  cash + shares * open of the fill bar. Orders are sent only when the target changes: holding the
  same target never rebalances for drift.
- Targets are clamped to [-2, 2]. A non-finite target is an error.
- Costs: see costs.py (commission tiers, exchange/clearing/regulatory fees, 1 bp slippage, short
  borrow). The default call earns 0 % on cash and charges 0 financing, and it does not construct
  the session-boundary carry. A non-zero cash rate, a non-zero financing rate, or a dividend map
  accrues once per session boundary at rate/252. That path is off unless the caller opts in.
- Daily equity is marked at the close of the last regular-session bar of each session.
- strategy.on_day_end(summary) (optional) is called after the last delivered bar of each session.
"""
import json
import math
import os

import numpy as np
import pandas as pd

from . import costs as C
from .accounting import (
    borrowed_base,
    dividend_cash,
    idle_base,
    require_annual_rate,
    require_withholding_rate,
    session_accrual,
)

PACK_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(PACK_DIR, "data")
CACHE_DIR = os.path.join(PACK_DIR, ".cache")
INITIAL_EQUITY = 1_000_000.0
MAX_ABS_TARGET = 2.0
DESIGN_LAST_DAY = 2999       # design window = days 0..2999
TRAIN_LAST_DAY = 3795        # validation window = days 3000..3795 (locked, one-shot)
VALIDATION_WARMUP_DAYS = 504  # validation runs replay the previous 504 sessions first, then reset to $1M
BAR_FIELDS = ("day", "weekday", "minute", "time", "session", "open", "high", "low", "close",
              "volume", "wap", "count", "filled", "short_session")
# Day cuts of the anonymized QQQ arena pack this engine was scored on.
# Named local runs and walk-forward must not use these as a default.
WINDOWS = {"full": (0, TRAIN_LAST_DAY), "design": (0, DESIGN_LAST_DAY),
           "validation": (DESIGN_LAST_DAY + 1, TRAIN_LAST_DAY)}


# ----------------------------------------------------------------------------------------- data
def _chunks():
    """[(path, first_day, last_day)] from data/MANIFEST.json."""
    with open(os.path.join(DATA_DIR, "MANIFEST.json")) as f:
        man = json.load(f)
    return [(os.path.join(DATA_DIR, name), int(r[0]), int(r[1])) for name, r in sorted(man["chunks"].items())]


def _read_chunk(path):
    """Read one pack chunk.

    The reference writes a pickle beside the pack. This port does not: an
    installed package must not write next to its source. The frame is the same
    one ``pandas.read_csv`` produced for the cache.
    """
    return pd.read_csv(path, dtype={"time": str, "session": str})


def load_bars(first_day=0, last_day=TRAIN_LAST_DAY):
    """All 1-minute bars (pre, rth, post) for day indices [first_day, last_day]."""
    parts = []
    for p, a, z in _chunks():
        if z < first_day or a > last_day:
            continue
        df = _read_chunk(p)
        parts.append(df[(df["day"] >= first_day) & (df["day"] <= last_day)])
    if not parts:
        raise FileNotFoundError(f"no bar data for days {first_day}..{last_day} in {DATA_DIR}")
    return pd.concat(parts, ignore_index=True)


def load_daily():
    return pd.read_csv(os.path.join(DATA_DIR, "daily_rth.csv"))


# ------------------------------------------------------------------------------------- engine
def _local_month(day):
    return day // C.LOCAL_SESSIONS_PER_MONTH


class _SessionCarry:
    """Session-boundary financing, cash interest, and dividends.

    Built only when a caller opts in. The default ``run`` leaves this object
    as None and does not touch cash here. The snap is the book after the
    previous session's last bar, including a post-session fill, and before
    this session's fill. Borrow stays on the last regular bar and is not
    moved here. A warm-up reset clears the snap and the totals before
    ``due``, so the first live session is not charged.
    """

    def __init__(self, cash_rate, financing_rate, withholding, dividends):
        self.cash_rate = float(cash_rate)
        self.financing_rate = float(financing_rate)
        self.withholding = float(withholding)
        mapped = {}
        for key, amount in (dividends or {}).items():
            day = int(key)
            value = float(amount)
            if not math.isfinite(value):
                raise ValueError(f"dividend for day {day} is not finite")
            if day in mapped:
                raise ValueError(f"duplicate dividend for day {day}")
            mapped[day] = value
        self.dividends = mapped
        self.snap = None
        self.financing = 0.0
        self.cash_interest = 0.0
        self.dividends_total = 0.0

    def snap_close(self, shares, cash, price):
        price = float(price)
        self.snap = (shares, price, cash + shares * price)

    def due(self, day):
        snap = self.snap
        if snap is None:
            return 0.0, 0.0, 0.0
        shares, price, equity = snap
        fin = 0.0
        interest = 0.0
        div = 0.0
        if self.financing_rate != 0.0:
            fin = session_accrual(
                self.financing_rate, borrowed_base(shares, price, equity), C.SESSIONS_PER_YEAR
            )
        if self.cash_rate != 0.0:
            interest = session_accrual(
                self.cash_rate, idle_base(shares, price, equity), C.SESSIONS_PER_YEAR
            )
        if int(day) in self.dividends:
            amount = self.dividends[int(day)]
            if self.withholding == 0.0:
                div = shares * amount
            else:
                div = dividend_cash(shares, amount, self.withholding)
        return fin, interest, div


def run(strategy, bars, *, initial_equity=INITIAL_EQUITY, share_scale=1.0, month_of_day=None,
        taf_active_of_day=None, reset_day=None, period_label_of_day=None, record_fills=True,
        borrow_rate_annual=None, cash_rate_annual=0.0, financing_rate_annual=0.0,
        withholding_rate=0.0, dividends=None, cost_model=None):
    """Run `strategy` over `bars` (DataFrame with BAR_FIELDS, all sessions, time-ordered).

    reset_day: if given, the account is rescaled to `initial_equity` (keeping exposure) at the
      first bar of that day and all statistics are computed from that point (earlier days are a
      warm-up replay that trades normally).
    month_of_day / taf_active_of_day / period_label_of_day: callables day -> value (defaults: 21-session
      fee months, TAF always charged, 252-session periods).
    share_scale: multiply pack share counts to get the shares per-share fees count.
      When every pack price is the real price times k, the engine's share count is
      about 1/k of the real count, so pass share_scale=k. SEC and the value caps
      stay on pack shares times pack price, which is the real trade value. 1.0 is
      the raw-file default.
    borrow_rate_annual: None uses costs.daily_borrow (0.30%/252). Any other value is an
      explicit override and is not the reference default.
    cash_rate_annual / financing_rate_annual: 0 leaves the reference loop unchanged.
      Any other rate accrues once per session boundary on the previous session's
      last bar, after that bar's fill. A post-session fill is in the snap when
      the strategy receives extended hours. Borrow stays on the last regular bar.
    dividends: None leaves the reference loop unchanged. A map of day -> amount
      is credited at that day's boundary, before the fill, on the shares in that snap.
    cost_model: None uses costs.fill_costs and costs.slipped_price, including
      share_scale. A model prices the fill itself and this loop does not read
      share_scale for that fill. The default, None, is the operator schedule.
    """
    month_of_day = month_of_day or _local_month
    taf_active_of_day = taf_active_of_day or (lambda d: True)
    wants_ext = bool(getattr(strategy, "WANTS_EXTENDED_HOURS", False))
    has_day_end = callable(getattr(strategy, "on_day_end", None))

    bars = bars.reset_index(drop=True)
    is_rth = (bars["session"].values == "rth")
    # last regular-session bar of each day (mark-to-market point)
    rth_idx = np.flatnonzero(is_rth)
    rth_days = bars["day"].values[rth_idx]
    last_rth = np.zeros(len(bars), bool)
    if len(rth_idx):
        last_rth[rth_idx[np.r_[rth_days[1:] != rth_days[:-1], True]]] = True
    deliver = np.ones(len(bars), bool) if wants_ext else is_rth
    # per-day regular-session summary for on_day_end
    rth = bars[is_rth]
    g = rth.groupby("day", sort=True)
    dsum = pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
                         "close": g["close"].last(), "volume": g["volume"].sum(),
                         "short_session": g["short_session"].first(), "weekday": g["weekday"].first()})
    dsum_rec = {int(k): v for k, v in dsum.to_dict("index").items()}

    sel = np.flatnonzero(deliver | last_rth)  # mark bars are always processed (not delivered if eth-only)
    cols = {k: bars[k].values[sel].tolist() for k in BAR_FIELDS}
    dlv = deliver[sel].tolist()
    lrth = last_rth[sel].tolist()
    days = cols["day"]
    n = len(sel)
    # last delivered bar of each day
    last_dlv = [False] * n
    seen = set()
    for j in range(n - 1, -1, -1):
        if dlv[j] and days[j] not in seen:
            seen.add(days[j]); last_dlv[j] = True
    # Last processed bar of each session, post included when those bars are processed.
    last_of_session = [False] * n
    seen_session = set()
    for j in range(n - 1, -1, -1):
        if days[j] not in seen_session:
            seen_session.add(days[j])
            last_of_session[j] = True

    cash = float(initial_equity); shares = 0; target = 0.0; pending = None
    month_vol = {}
    busted = False
    started = reset_day is None
    fees_tot = {k: 0.0 for k in ("commission", "exchange", "clearing", "pass_through", "cat", "sec", "taf", "total")}
    slip_tot = 0.0; borrow_tot = 0.0; traded_notional = 0.0; eth_notional = 0.0; n_fills = 0
    fills = []
    daily = []  # (day, equity, exposure, shares, close, period)
    sum_abs_exp = 0.0; n_exp = 0; n_long = 0; n_short = 0
    # episode tracking
    ep = None  # dict(start_day, start_j, side, cf)
    episodes = []
    cur_day = None
    O = cols["open"]; Cl = cols["close"]; S = cols["session"]
    keys = BAR_FIELDS

    def close_episode(j, day, cf):
        episodes.append({"side": ep["side"], "start_day": ep["start_day"], "end_day": day,
                         "bars": ep_bar_count[0], "pnl": cf, "entry_notional": ep["entry_notional"]})

    carry = None
    if cash_rate_annual != 0.0 or financing_rate_annual != 0.0 or withholding_rate != 0.0 or dividends:
        cash_rate_annual = require_annual_rate("cash_rate_annual", cash_rate_annual)
        financing_rate_annual = require_annual_rate("financing_rate_annual", financing_rate_annual)
        withholding_rate = require_withholding_rate("withholding_rate", withholding_rate)
        carry = _SessionCarry(cash_rate_annual, financing_rate_annual, withholding_rate, dividends)
    ep_bar_count = [0]
    for j in range(n):
        day = days[j]
        if day != cur_day:
            cur_day = day
            did_reset = False
            if not started and day >= reset_day:
                # rescale account to initial_equity keeping exposure, using the last mark
                last_eq = daily[-1][1] if daily else cash
                last_px = daily[-1][4] if daily else O[j]
                if last_eq > 0:
                    k = initial_equity / last_eq
                    shares = int(shares * k)
                    cash = initial_equity - shares * last_px
                else:
                    shares = 0; cash = initial_equity
                busted = False
                month_vol = {}
                for kk in fees_tot: fees_tot[kk] = 0.0
                slip_tot = borrow_tot = traded_notional = eth_notional = 0.0; n_fills = 0
                fills = []; daily = []; episodes = []
                sum_abs_exp = 0.0; n_exp = n_long = n_short = 0
                ep = None if shares == 0 else {"side": 1 if shares > 0 else -1, "start_day": day,
                                                "cf": -shares * last_px, "entry_notional": abs(shares * last_px)}
                ep_bar_count[0] = 0
                started = True
                did_reset = True
            if carry is not None:
                # The reset already rescaled the book and dropped the warm-up
                # marks. Drop the warm-up snap and totals too, before due, so
                # the first live session is not charged. N live sessions then
                # pay N-1 charges, and the first live mark is the reset equity.
                if did_reset:
                    carry.financing = 0.0
                    carry.cash_interest = 0.0
                    carry.dividends_total = 0.0
                    carry.snap = None
                fin, interest, div = carry.due(day)
                if fin != 0.0:
                    cash -= fin
                    if ep is not None:
                        ep["cf"] -= fin
                if div != 0.0:
                    cash += div
                    if ep is not None:
                        ep["cf"] += div
                if interest != 0.0:
                    cash += interest
                    if ep is not None:
                        ep["cf"] += interest
                carry.financing += fin
                carry.cash_interest += interest
                carry.dividends_total += div
        o = O[j]
        # ---- fill pending order at this bar's open
        if pending is not None and dlv[j]:
            tgt = pending; pending = None
            eq_open = cash + shares * o
            if eq_open <= 0:
                busted = True; new_sh = 0
            else:
                new_sh = int(tgt * eq_open / o)  # trunc toward zero
            delta = new_sh - shares
            if delta != 0:
                mk = month_of_day(day)
                if cost_model is None:
                    px = C.slipped_price(o, delta)
                    fc = C.fill_costs(delta, px, month_vol.get(mk, 0.0), share_scale, taf_active_of_day(day))
                else:
                    px = cost_model.slipped_price(o, delta)
                    fc = cost_model.fill_costs(delta, px, month_vol.get(mk, 0.0))
                month_vol[mk] = month_vol.get(mk, 0.0) + fc["tier_shares"]
                cash -= delta * px + fc["total"]
                for kk in fees_tot: fees_tot[kk] += fc[kk]
                slip = abs(delta) * abs(px - o)
                slip_tot += slip
                notional = abs(delta) * o
                traded_notional += notional
                if S[j] != "rth": eth_notional += notional
                n_fills += 1
                if record_fills:
                    fills.append((day, cols["minute"][j], S[j], delta, o, px, fc["total"], new_sh))
                # episodes
                old = shares
                if old != 0 and (new_sh == 0 or (new_sh > 0) != (old > 0)):
                    frac = abs(old) / abs(delta)
                    cf = ep["cf"] + old * px - fc["total"] * frac
                    close_episode(j, day, cf)
                    ep = None
                    if new_sh != 0:
                        ep = {"side": 1 if new_sh > 0 else -1, "start_day": day,
                              "cf": -new_sh * px - fc["total"] * (1 - frac), "entry_notional": abs(new_sh * px)}
                        ep_bar_count[0] = 0
                elif old == 0:
                    ep = {"side": 1 if new_sh > 0 else -1, "start_day": day,
                          "cf": -delta * px - fc["total"], "entry_notional": abs(new_sh * px)}
                    ep_bar_count[0] = 0
                else:
                    ep["cf"] += -delta * px - fc["total"]
                    ep["entry_notional"] = max(ep["entry_notional"], abs(new_sh * px))
                shares = new_sh
        c = Cl[j]
        eq = cash + shares * c
        if ep is not None:
            ep_bar_count[0] += 1
        if started and dlv[j]:
            if eq > 0:
                x = shares * c / eq
                sum_abs_exp += abs(x); n_exp += 1
                if shares > 0: n_long += 1
                elif shares < 0: n_short += 1
        # ---- daily mark at regular-session close
        if lrth[j]:
            if shares < 0:
                if borrow_rate_annual is None:
                    b = C.daily_borrow(-shares * c)
                else:
                    b = max(0.0, (-shares * c) * borrow_rate_annual / C.SESSIONS_PER_YEAR)
                cash -= b; borrow_tot += b
                if ep is not None: ep["cf"] -= b
            eq = cash + shares * c
            if eq <= 0 and not busted:
                busted = True
            daily.append((day, eq, (shares * c / eq) if eq > 0 else 0.0, shares, c))
        # After this bar's fill. Borrow, when this is the last regular bar, is
        # already in cash. The snap is this session's last processed bar, so a
        # post-session fill is included and a regular-only session still snaps
        # at the regular close.
        if carry is not None and last_of_session[j]:
            carry.snap_close(shares, cash, c)
        # ---- strategy decision
        if dlv[j]:
            bar = {k: cols[k][j] for k in keys}
            state = {"equity": eq, "cash": cash, "shares": shares, "position": (shares * c / eq) if eq > 0 else 0.0,
                     "target": target if pending is None else pending, "pending_order": pending is not None}
            t = strategy.on_bar(bar, state)
            if t is not None and not busted:
                t = float(t)
                if not math.isfinite(t):
                    raise ValueError(f"non-finite target {t!r} at day {day} minute {cols['minute'][j]}")
                t = max(-MAX_ABS_TARGET, min(MAX_ABS_TARGET, t))
                if t != target:
                    target = t
                    pending = t
            if busted:
                pending = None if shares == 0 else 0.0
                target = 0.0
            if has_day_end and last_dlv[j]:
                s = dsum_rec.get(day, {})
                summary = {"day": day, "weekday": cols["weekday"][j], "rth_open": s.get("open"), "rth_high": s.get("high"),
                           "rth_low": s.get("low"), "rth_close": s.get("close"), "rth_volume": s.get("volume"),
                           "short_session": s.get("short_session"), "equity": eq, "shares": shares,
                           "position": (shares * c / eq) if eq > 0 else 0.0}
                strategy.on_day_end(summary)

    last_close = Cl[-1] if n else 0.0
    open_ep = None
    if ep is not None:
        open_ep = {"side": ep["side"], "start_day": ep["start_day"], "end_day": cur_day, "bars": ep_bar_count[0],
                   "pnl": ep["cf"] + shares * last_close, "entry_notional": ep["entry_notional"], "open": True}
    out = {
        "initial_equity": initial_equity, "share_scale": share_scale,
        "daily": daily, "fills": fills, "episodes": episodes, "open_episode": open_ep,
        "fees": fees_tot, "slippage": slip_tot, "borrow": borrow_tot, "traded_notional": traded_notional,
        "eth_notional": eth_notional, "n_fills": n_fills, "busted": busted,
        "bar_exposure": {"avg_abs": sum_abs_exp / n_exp if n_exp else 0.0, "frac_long": n_long / n_exp if n_exp else 0.0,
                         "frac_short": n_short / n_exp if n_exp else 0.0, "n_bars": n_exp},
        "wants_extended_hours": wants_ext, "period_label_of_day": period_label_of_day,
    }
    if cost_model is not None:
        out.pop("share_scale", None)
    if carry is not None:
        out["financing"] = carry.financing
        out["cash_interest"] = carry.cash_interest
        out["dividends"] = carry.dividends_total
    return out


# ------------------------------------------------------------------------------------ metrics
def metrics(res, period_label_of_day=None):
    """Performance statistics. Conventions: 252 sessions/year, rf = 0, daily returns from
    regular-session-close marks, first return measured against initial equity."""
    d = res["daily"]
    E0 = res["initial_equity"]
    if not d:
        return {"error": "no daily marks"}
    days = np.array([x[0] for x in d]); eq = np.array([x[1] for x in d], float)
    expo = np.array([x[2] for x in d], float)
    prev = np.r_[E0, eq[:-1]]
    # A zero mark has no defined return. Store 0 for that session. Every other
    # session uses the same division as the reference, so a path with no zero
    # mark stays byte-identical.
    if np.any(prev == 0):
        r = np.zeros(len(eq), dtype=float)
        safe = prev != 0
        np.divide(eq, prev, out=r, where=safe)
        r[safe] -= 1.0
    else:
        r = eq / prev - 1.0
    n = len(r)
    yrs = n / 252.0
    if E0 == 0:
        tot = 0.0
        cagr = -1.0
    else:
        tot = eq[-1] / E0 - 1.0
        cagr = (eq[-1] / E0) ** (1 / yrs) - 1 if eq[-1] > 0 else -1.0
    sd = r.std(ddof=1) if n > 1 else 0.0
    vol = sd * math.sqrt(252)
    sharpe = r.mean() / sd * math.sqrt(252) if sd > 0 else 0.0
    dd_dev = math.sqrt(np.mean(np.minimum(r, 0) ** 2))
    sortino = r.mean() / dd_dev * math.sqrt(252) if dd_dev > 0 else 0.0
    path = np.r_[E0, eq]
    peak = np.maximum.accumulate(path)
    if np.any(peak == 0):
        dd = np.zeros_like(path, dtype=float)
        safe_peak = peak != 0
        np.divide(path, peak, out=dd, where=safe_peak)
        dd[safe_peak] -= 1.0
    else:
        dd = path / peak - 1
    mdd = dd.min()
    # longest drawdown duration in sessions (peak to recovery or end)
    longest = 0; cur = 0
    for v in dd[1:]:
        cur = cur + 1 if v < 0 else 0
        longest = max(longest, cur)
    calmar = cagr / abs(mdd) if mdd < 0 else float("inf")
    eps = res["episodes"]
    pnl = np.array([e["pnl"] for e in eps]) if eps else np.zeros(0)
    hold_days = np.array([e["end_day"] - e["start_day"] for e in eps]) if eps else np.zeros(0)
    hold_bars = np.array([e["bars"] for e in eps]) if eps else np.zeros(0)
    mean_eq = eq.mean()
    out = {
        "sessions": int(n), "years": round(yrs, 3), "first_day": int(days[0]), "last_day": int(days[-1]),
        "initial_equity": E0, "final_equity": float(eq[-1]),
        "total_return": float(tot), "cagr": float(cagr), "ann_vol": float(vol), "sharpe": float(sharpe),
        "sortino": float(sortino), "max_drawdown": float(mdd), "max_dd_duration_sessions": int(longest),
        "calmar": float(calmar), "worst_day": float(r.min()), "best_day": float(r.max()),
        "n_fills": int(res["n_fills"]), "round_trips": int(len(eps)),
        "hit_rate": float((pnl > 0).mean()) if len(pnl) else None,
        "avg_trade_pnl": float(pnl.mean()) if len(pnl) else None,
        "avg_holding_sessions": float(hold_days.mean()) if len(hold_days) else None,
        "avg_holding_bars": float(hold_bars.mean()) if len(hold_bars) else None,
        "long_round_trips": int(sum(1 for e in eps if e["side"] > 0)),
        "short_round_trips": int(sum(1 for e in eps if e["side"] < 0)),
        "long_pnl": float(sum(e["pnl"] for e in eps if e["side"] > 0)),
        "short_pnl": float(sum(e["pnl"] for e in eps if e["side"] < 0)),
        "open_position_at_end": res["open_episode"],
        "traded_notional": float(res["traded_notional"]),
        "turnover_per_year": float(res["traded_notional"] / mean_eq / yrs) if mean_eq > 0 else None,
        "fees_total": float(res["fees"]["total"]), "fees_breakdown": {k: float(v) for k, v in res["fees"].items()},
        "slippage_cost": float(res["slippage"]), "borrow_cost": float(res["borrow"]),
        "costs_total": float(res["fees"]["total"] + res["slippage"] + res["borrow"]),
        "extended_hours_fill_notional_frac": float(res["eth_notional"] / res["traded_notional"]) if res["traded_notional"] else 0.0,
        "avg_abs_exposure_bars": res["bar_exposure"]["avg_abs"], "frac_bars_long": res["bar_exposure"]["frac_long"],
        "frac_bars_short": res["bar_exposure"]["frac_short"],
        "avg_net_exposure_close": float(expo.mean()), "avg_abs_exposure_close": float(np.abs(expo).mean()),
        "frac_sessions_long_at_close": float((expo > 0).mean()), "frac_sessions_short_at_close": float((expo < 0).mean()),
        "busted": bool(res["busted"]),
    }
    if "financing" in res:
        out["financing_cost"] = float(res["financing"])
        out["cash_interest"] = float(res.get("cash_interest", 0.0))
        out["dividends_net"] = float(res.get("dividends", 0.0))
        out["costs_total"] = float(res["fees"]["total"] + res["slippage"] + res["borrow"] + res["financing"])
    # per-period returns: 252-session blocks (local) or labels supplied by caller
    lab = period_label_of_day or res.get("period_label_of_day")
    if lab is None:
        start = days[0]
        labels = [f"S{(int(x) - int(start)) // 252 + 1:02d}" for x in days]
    else:
        labels = [lab(int(x)) for x in days]
    per = {}
    prev_end = E0
    for i, L in enumerate(labels):
        if L not in per:
            per[L] = [prev_end, None]
        per[L][1] = eq[i]
        prev_end = eq[i]
    out["period_returns"] = {
        L: (0.0 if v[0] == 0 else float(v[1] / v[0] - 1)) for L, v in per.items()
    }
    return out


class BuyAndHold:
    """Benchmark: target +1.0 on the first bar and hold (no drift rebalancing)."""
    WANTS_EXTENDED_HOURS = False

    def on_bar(self, bar, state):
        return 1.0
