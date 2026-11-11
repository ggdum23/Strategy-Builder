"""Desk profile engine.

Specified in the engineering plan as sim.py. That file does not exist in the
reference material, so this module is the implementation. It does not call the
Arena engine or the Arena fee function.

Rules and the interpretations they required are in docs/DESK_SPEC_DECISIONS.md.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from . import costs_desk as D


@dataclass
class DeskConfig:
    """Run configuration. Official schedule defaults; ticker facts are not baked in."""

    initial_equity: float = 1_000_000.0
    # Counted shares are abs(pack shares) * share_factor. When every pack price
    # is the real price times k, pass k: pack shares are about 1/k of the real
    # count, and per-share fees then use real shares. 1.0 is the raw-file default.
    share_factor: float = 1.0
    borrow_rate_annual: float = D.BORROW_RATE_ANNUAL
    financing_rate_annual: float = D.FINANCING_RATE_ANNUAL
    sessions_per_year: int = D.SESSIONS_PER_YEAR
    sessions_per_month: int = D.SESSIONS_PER_MONTH
    fee_mult: float = 1.0
    slippage_bps: float = D.SLIPPAGE_BPS
    max_abs_target: float = 3.0
    # Same numbers as profiles.DESK. test_desk_engine_defaults_match_the_profile locks them.
    margin_call_leverage: float = 3.15
    margin_call_target: float = 3.0
    liquidation_hwm_fraction: float = 0.30
    live_start_ordinal: int = 0
    dividends: dict | None = None

    def __post_init__(self):
        if self.initial_equity <= 0.0:
            raise ValueError("initial_equity must be positive")
        if self.share_factor <= 0.0:
            raise ValueError("share_factor must be positive")
        if self.borrow_rate_annual < 0.0 or self.financing_rate_annual < 0.0:
            raise ValueError("borrow and financing rates cannot be negative")
        if self.sessions_per_year <= 0 or self.sessions_per_month <= 0:
            raise ValueError("session counts must be positive")
        if self.fee_mult <= 0.0 or self.slippage_bps < 0.0:
            raise ValueError("fee_mult must be positive and slippage_bps cannot be negative")
        if self.max_abs_target <= 0.0 or self.margin_call_target <= 0.0:
            raise ValueError("leverage caps must be positive")
        if not 0.0 < self.liquidation_hwm_fraction < 1.0:
            raise ValueError("liquidation fraction must be between 0 and 1")
        if self.live_start_ordinal < 0:
            raise ValueError("live_start_ordinal cannot be negative")
        if self.dividends is None:
            self.dividends = {}


def parse_target(raw, limit):
    """None holds. Finite numbers are clipped to [-limit, limit]. Bool and str raise."""
    if raw is None:
        return None
    # Python bool and numpy.bool both report a bool type name. numpy.bool is
    # not a subclass of bool on current NumPy, and float(True) would be 1.0.
    if type(raw).__name__ in {"bool", "bool_", "bool8"}:
        raise ValueError("desk target must be a real number or None, not bool")
    if isinstance(raw, (str, bytes, bytearray)):
        raise ValueError("desk target must be a real number or None, not str")
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"desk target is not a real number: {raw!r}") from exc
    if not math.isfinite(value):
        raise ValueError(f"non-finite target {value!r}")
    if value > limit:
        return float(limit)
    if value < -limit:
        return float(-limit)
    return value


_STRATEGY_KEYS = (
    "session",
    "time",
    "phase",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "wap",
    "count",
    "filled",
    "new_session",
)


def _minute_of(row):
    if row.get("minute") is not None:
        return int(row["minute"])
    text = str(row["time"])
    hour, minute = text.split(":")
    return int(hour) * 60 + int(minute)


def _time_of(row, minute):
    if row.get("time"):
        return str(row["time"])
    return f"{minute // 60:02d}:{minute % 60:02d}"


def normalize_bars(bars):
    """Copy bars into plain dicts and reject a stream that cannot be simulated."""
    if hasattr(bars, "to_dict"):
        records = bars.to_dict("records")
    else:
        records = [dict(row) for row in bars]
    if not records:
        raise ValueError("desk engine received no bars")
    out = []
    seen_sessions = set()
    prev_session = None
    prev_minute = None
    for index, row in enumerate(records):
        try:
            session = row["session"]
            phase = row["phase"]
            close = float(row["close"])
            open_ = float(row["open"])
            high = float(row["high"])
            low = float(row["low"])
        except KeyError as exc:
            raise ValueError(f"bar {index} is missing {exc}") from exc
        if phase not in {"pre", "rth", "post"}:
            raise ValueError(f"bar {index} phase {phase!r} is not pre, rth, or post")
        minute = _minute_of(row)
        if not all(math.isfinite(x) and x > 0.0 for x in (open_, high, low, close)):
            raise ValueError(f"bar {index} has a non-positive price")
        if high < low or high < open_ or high < close or low > open_ or low > close:
            raise ValueError(f"bar {index} OHLC is inconsistent")
        volume = float(row.get("volume", 0.0) or 0.0)
        if volume < 0.0:
            raise ValueError(f"bar {index} has negative volume")
        wap_raw = row.get("wap", close)
        wap = close if wap_raw is None else float(wap_raw)
        count = int(row.get("count", 0) or 0)
        filled = int(row.get("filled", 0) or 0)
        if filled not in (0, 1):
            raise ValueError(f"bar {index} filled flag must be 0 or 1")
        if session != prev_session:
            if session in seen_sessions:
                raise ValueError(f"session {session} is not contiguous")
            seen_sessions.add(session)
            prev_session = session
            prev_minute = None
        if prev_minute is not None and minute <= prev_minute:
            raise ValueError(f"session {session} minute {minute} is not increasing")
        prev_minute = minute
        out.append(
            {
                "session": session,
                "phase": phase,
                "minute": minute,
                "time": _time_of(row, minute),
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
                "wap": wap,
                "count": count,
                "filled": filled,
            }
        )
    return out


def _strategy_bar(row, new_session):
    view = {key: row[key] for key in _STRATEGY_KEYS if key != "new_session"}
    view["new_session"] = bool(new_session)
    return view


class _Book:
    def __init__(self, cfg: DeskConfig):
        self.cfg = cfg
        self.cash = float(cfg.initial_equity)
        self.shares = 0
        self.target = 0.0
        self.pending = None
        self.pending_kind = None
        self.hwm = float(cfg.initial_equity)
        self.liquidated = False
        self.disqualified = False
        self.postmortem = None
        self.snap = None
        self.month_shares = {}
        self.fees = {key: 0.0 for key in ("commission", "exchange", "clearing", "pass_through", "cat", "sec", "taf", "total")}
        self.slippage = 0.0
        self.financing = 0.0
        self.borrow = 0.0
        self.dividends = 0.0
        self.traded_notional = 0.0
        self.n_fills = 0
        self.margin_queued = 0
        self.margin_filled = 0
        self.fills = []
        self.daily = []
        self.financing_events = []
        self.borrow_events = []
        self.dividend_events = []
        self.worst_ratio = 1.0
        self.gross_max = 0.0
        self.gross_sum = 0.0
        self.gross_n = 0
        self.sum_abs_exp = 0.0
        self.n_exp = 0
        self.n_long = 0
        self.n_short = 0
        self.ep = None
        self.episodes = []
        self.last_close = 0.0
        self.last_ordinal = 0

    def enter_live(self):
        """Flat $1,000,000. Warm-up targets are not kept."""
        self.cash = float(self.cfg.initial_equity)
        self.shares = 0
        self.target = 0.0
        self.pending = None
        self.pending_kind = None
        self.hwm = float(self.cfg.initial_equity)
        self.liquidated = False
        self.disqualified = False
        self.postmortem = None
        self.snap = None
        self.month_shares = {}
        self.fees = {key: 0.0 for key in self.fees}
        self.slippage = 0.0
        self.financing = 0.0
        self.borrow = 0.0
        self.dividends = 0.0
        self.traded_notional = 0.0
        self.n_fills = 0
        self.margin_queued = 0
        self.margin_filled = 0
        self.fills = []
        self.daily = []
        self.financing_events = []
        self.borrow_events = []
        self.dividend_events = []
        self.worst_ratio = 1.0
        self.gross_max = 0.0
        self.gross_sum = 0.0
        self.gross_n = 0
        self.sum_abs_exp = 0.0
        self.n_exp = 0
        self.n_long = 0
        self.n_short = 0
        self.ep = None
        self.episodes = []

    def account(self, close, *, live):
        equity = self.cash + self.shares * close
        if equity > 0.0:
            exposure = self.shares * close / equity
            gross = abs(exposure)
        elif self.shares == 0:
            exposure = 0.0
            gross = 0.0
        else:
            exposure = None
            gross = None
        drawdown = (equity / self.hwm - 1.0) if self.hwm > 0.0 else 0.0
        return {
            "cash": self.cash,
            "position_shares": int(self.shares),
            "equity": equity,
            "exposure": exposure,
            "gross_leverage": gross,
            "hwm": self.hwm,
            "drawdown": drawdown,
            "live": bool(live),
            "liquidated": bool(self.liquidated),
        }

    def apply_boundary(self, session_id):
        snap = self.snap
        if snap is None:
            return
        fin = D.financing_due(
            snap["shares"],
            snap["price"],
            snap["equity"],
            rate=self.cfg.financing_rate_annual,
            sessions_per_year=self.cfg.sessions_per_year,
        )
        # fee_mult does not scale borrow. Spec item 11.
        bor = D.borrow_due(
            snap["shares"],
            snap["price"],
            rate=self.cfg.borrow_rate_annual,
            sessions_per_year=self.cfg.sessions_per_year,
        )
        amount = float(self.cfg.dividends.get(session_id, 0.0))
        div = snap["shares"] * amount
        self.cash -= fin
        self.cash -= bor
        self.cash += div
        self.financing += fin
        self.borrow += bor
        self.dividends += div
        if self.ep is not None:
            self.ep["cf"] -= fin
            self.ep["cf"] -= bor
            self.ep["cf"] += div
        self.financing_events.append(
            {
                "session": session_id,
                "amount": fin,
                "shares": snap["shares"],
                "price": snap["price"],
                "equity": snap["equity"],
            }
        )
        self.borrow_events.append(
            {
                "session": session_id,
                "amount": bor,
                "shares": snap["shares"],
                "price": snap["price"],
            }
        )
        self.dividend_events.append({"session": session_id, "amount": div, "per_share": amount, "shares": snap["shares"]})

    def take_snap(self, close):
        equity = self.cash + self.shares * close
        self.snap = {"shares": self.shares, "price": close, "equity": equity, "cash": self.cash}

    def fill_pending(self, price, live_ordinal, row):
        tgt = self.pending
        kind = self.pending_kind
        self.pending = None
        self.pending_kind = None
        equity_open = self.cash + self.shares * price
        if equity_open <= 0.0:
            new_shares = 0
        else:
            new_shares = int(tgt * equity_open / price)
        self._execute(new_shares, price, kind, live_ordinal, row)
        self.target = float(tgt)

    def queue_margin(self):
        sign = 1.0 if self.shares > 0 else -1.0
        target = sign * self.cfg.margin_call_target
        self.target = target
        self.pending = target
        self.pending_kind = "margin"
        self.margin_queued += 1

    def take_target(self, raw):
        parsed = parse_target(raw, self.cfg.max_abs_target)
        if parsed is None:
            return
        if parsed != self.target:
            self.target = parsed
            self.pending = parsed
            self.pending_kind = "strategy"

    def liquidate(self, close, live_ordinal, row, equity_before):
        self.postmortem = {
            "session": row["session"],
            "time": row["time"],
            "phase": row["phase"],
            "close": close,
            "equity_before": equity_before,
            "hwm": self.hwm,
            "shares_before": self.shares,
        }
        if self.shares != 0:
            self._execute(0, close, "liquidation", live_ordinal, row)
        self.target = 0.0
        self.pending = None
        self.pending_kind = None
        self.liquidated = True
        self.disqualified = True
        self.postmortem["equity_after"] = self.cash + self.shares * close
        self.postmortem["shares_after"] = self.shares

    def note_gross(self, gross):
        self.gross_n += 1
        self.gross_sum += gross
        if gross > self.gross_max:
            self.gross_max = gross

    def note_exposure(self, close):
        equity = self.cash + self.shares * close
        if equity <= 0.0:
            return
        exposure = self.shares * close / equity
        self.sum_abs_exp += abs(exposure)
        self.n_exp += 1
        if self.shares > 0:
            self.n_long += 1
        elif self.shares < 0:
            self.n_short += 1

    def note_worst(self, close):
        equity = self.cash + self.shares * close
        if self.hwm > 0.0:
            ratio = equity / self.hwm
            if ratio < self.worst_ratio:
                self.worst_ratio = ratio

    def mark_daily(self, row, ordinal, close):
        equity = self.cash + self.shares * close
        if equity > 0.0:
            exposure = self.shares * close / equity
        else:
            exposure = 0.0
        self.daily.append(
            {
                "session": row["session"],
                "ordinal": ordinal,
                "equity": equity,
                "exposure": exposure,
                "shares": self.shares,
                "close": close,
            }
        )

    def bump_episode_bar(self):
        if self.ep is not None:
            self.ep["bars"] += 1

    def _close_episode(self, ordinal, pnl):
        self.episodes.append(
            {
                "side": self.ep["side"],
                "start_ordinal": self.ep["start_ordinal"],
                "end_ordinal": ordinal,
                "bars": self.ep["bars"],
                "pnl": pnl,
                "entry_notional": self.ep["entry_notional"],
            }
        )

    def _execute(self, new_shares, price, kind, live_ordinal, row):
        delta = new_shares - self.shares
        if delta == 0:
            return
        px = D.slipped_price(price, delta, self.cfg.slippage_bps, self.cfg.fee_mult)
        month = live_ordinal // self.cfg.sessions_per_month
        before = self.month_shares.get(month, 0.0)
        fees = D.fill_costs(delta, px, before, self.cfg.share_factor, self.cfg.fee_mult)
        self.month_shares[month] = before + fees["tier_shares"]
        self.cash -= delta * px + fees["total"]
        for key in self.fees:
            self.fees[key] += fees[key]
        slip = abs(delta) * abs(px - price)
        self.slippage += slip
        self.traded_notional += abs(delta) * price
        self.n_fills += 1
        if kind == "margin":
            self.margin_filled += 1
        old = self.shares
        self._touch_episode(old, new_shares, delta, px, fees["total"], live_ordinal)
        self.shares = new_shares
        self.fills.append(
            {
                "session": row["session"],
                "time": row["time"],
                "phase": row["phase"],
                "minute": row["minute"],
                "shares_delta": delta,
                "price": price,
                "fill_price": px,
                "fees": fees["total"],
                "commission": fees["commission"],
                "exchange": fees["exchange"],
                "clearing": fees["clearing"],
                "pass_through": fees["pass_through"],
                "cat": fees["cat"],
                "sec": fees["sec"],
                "taf": fees["taf"],
                "shares_after": new_shares,
                "kind": kind,
                "tier_month": month,
                "tier_shares": fees["tier_shares"],
            }
        )

    def _touch_episode(self, old, new_shares, delta, px, fee_total, ordinal):
        if old != 0 and (new_shares == 0 or (new_shares > 0) != (old > 0)):
            frac = abs(old) / abs(delta)
            pnl = self.ep["cf"] + old * px - fee_total * frac
            self._close_episode(ordinal, pnl)
            self.ep = None
            if new_shares != 0:
                self.ep = {
                    "side": 1 if new_shares > 0 else -1,
                    "start_ordinal": ordinal,
                    "cf": -new_shares * px - fee_total * (1.0 - frac),
                    "entry_notional": abs(new_shares * px),
                    "bars": 0,
                }
        elif old == 0:
            self.ep = {
                "side": 1 if new_shares > 0 else -1,
                "start_ordinal": ordinal,
                "cf": -delta * px - fee_total,
                "entry_notional": abs(new_shares * px),
                "bars": 0,
            }
        else:
            self.ep["cf"] += -delta * px - fee_total
            self.ep["entry_notional"] = max(self.ep["entry_notional"], abs(new_shares * px))


def _session_index(rows):
    """Per-bar session ordinal, last RTH index, and last bar index."""
    ordinals = []
    last_rth = {}
    last_index = {}
    ordinal = -1
    prev = None
    for index, row in enumerate(rows):
        if row["session"] != prev:
            ordinal += 1
            prev = row["session"]
        ordinals.append(ordinal)
        last_index[row["session"]] = index
        if row["phase"] == "rth":
            last_rth[row["session"]] = index
    return ordinals, last_rth, last_index


def run(strategy, bars, config=None):
    """Simulate `strategy` on desk rules. `bars` is a frame or a sequence of mappings."""
    cfg = config if config is not None else DeskConfig()
    if not isinstance(cfg, DeskConfig):
        raise TypeError("config must be a DeskConfig")
    rows = normalize_bars(bars)
    ordinals, last_rth, last_index = _session_index(rows)
    book = _Book(cfg)
    entered_live = False
    warmup_account = {
        "cash": float(cfg.initial_equity),
        "position_shares": 0,
        "equity": float(cfg.initial_equity),
        "exposure": 0.0,
        "gross_leverage": 0.0,
        "hwm": float(cfg.initial_equity),
        "drawdown": 0.0,
        "live": False,
        "liquidated": False,
    }

    for index, row in enumerate(rows):
        ordinal = ordinals[index]
        new_session = index == 0 or rows[index - 1]["session"] != row["session"]
        live = ordinal >= cfg.live_start_ordinal
        view = _strategy_bar(row, new_session)
        if not live:
            strategy.on_bar(view, dict(warmup_account))
            continue
        if not entered_live:
            book.enter_live()
            entered_live = True
        live_ordinal = ordinal - cfg.live_start_ordinal
        if new_session and not book.liquidated:
            book.apply_boundary(row["session"])
        if book.liquidated:
            book.note_worst(row["close"])
            if last_rth.get(row["session"]) == index:
                book.mark_daily(row, live_ordinal, row["close"])
            book.last_close = row["close"]
            book.last_ordinal = live_ordinal
            strategy.on_bar(view, book.account(row["close"], live=True))
            continue
        if book.pending is not None and row["phase"] == "rth":
            book.fill_pending(row["open"], live_ordinal, row)
        close = row["close"]
        equity = book.cash + book.shares * close
        if equity > book.hwm:
            book.hwm = equity
        if equity > 0.0:
            book.note_gross(D.gross_leverage(book.shares, close, equity))
        book.note_exposure(close)
        if D.breached_liquidation(equity, book.hwm, cfg.liquidation_hwm_fraction):
            book.liquidate(close, live_ordinal, row, equity)
        account = book.account(close, live=True)
        raw = strategy.on_bar(view, account)
        if not book.liquidated:
            book.take_target(raw)
            # Every RTH bar close, not only the last one. "An RTH close" in the
            # spec is not the phrase used for the daily mark ("last RTH close").
            if row["phase"] == "rth" and book.pending is None and book.shares != 0:
                marked = book.cash + book.shares * close
                if marked > 0.0 and D.margin_call_required(
                    D.gross_leverage(book.shares, close, marked),
                    cfg.margin_call_leverage,
                ):
                    book.queue_margin()
        book.note_worst(close)
        book.bump_episode_bar()
        if last_rth.get(row["session"]) == index:
            book.mark_daily(row, live_ordinal, close)
        if last_index[row["session"]] == index:
            book.take_snap(close)
        book.last_close = close
        book.last_ordinal = live_ordinal

    open_episode = None
    if book.ep is not None:
        open_episode = {
            "side": book.ep["side"],
            "start_ordinal": book.ep["start_ordinal"],
            "end_ordinal": book.last_ordinal,
            "bars": book.ep["bars"],
            "pnl": book.ep["cf"] + book.shares * book.last_close,
            "entry_notional": book.ep["entry_notional"],
            "open": True,
        }
    return {
        "initial_equity": float(cfg.initial_equity),
        "cash": book.cash,
        "shares": book.shares,
        "daily": book.daily,
        "fills": book.fills,
        "fees": dict(book.fees),
        "slippage": book.slippage,
        "borrow": book.borrow,
        "financing": book.financing,
        "dividends": book.dividends,
        "traded_notional": book.traded_notional,
        "n_fills": book.n_fills,
        "margin_calls_queued": book.margin_queued,
        "margin_calls_filled": book.margin_filled,
        "disqualified": book.disqualified,
        "liquidation": book.postmortem,
        "worst_equity_over_hwm": book.worst_ratio,
        "max_gross_leverage": book.gross_max,
        "avg_gross_leverage": (book.gross_sum / book.gross_n) if book.gross_n else 0.0,
        "episodes": book.episodes,
        "open_episode": open_episode,
        "financing_events": book.financing_events,
        "borrow_events": book.borrow_events,
        "dividend_events": book.dividend_events,
        "bar_exposure": {
            "avg_abs": book.sum_abs_exp / book.n_exp if book.n_exp else 0.0,
            "frac_long": book.n_long / book.n_exp if book.n_exp else 0.0,
            "frac_short": book.n_short / book.n_exp if book.n_exp else 0.0,
            "n_bars": book.n_exp,
        },
        "config": {
            "share_factor": cfg.share_factor,
            "borrow_rate_annual": cfg.borrow_rate_annual,
            "financing_rate_annual": cfg.financing_rate_annual,
            "fee_mult": cfg.fee_mult,
            "slippage_bps": cfg.slippage_bps,
            "max_abs_target": cfg.max_abs_target,
            "margin_call_leverage": cfg.margin_call_leverage,
            "margin_call_target": cfg.margin_call_target,
            "liquidation_hwm_fraction": cfg.liquidation_hwm_fraction,
            "live_start_ordinal": cfg.live_start_ordinal,
            "sessions_per_year": cfg.sessions_per_year,
            "sessions_per_month": cfg.sessions_per_month,
        },
    }


def _session_return(current, previous):
    """Session return. A previous mark of 0 is undefined and is stored as 0.

    It is not an up day, and it does not raise. A positive previous mark uses
    ``current / previous - 1``.
    """
    if previous == 0:
        return 0.0
    return current / previous - 1.0


def _cagr(final, initial, years):
    """Compound growth. A finite equity path that overflows the power returns a non-finite CAGR.

    Arena's numpy power does the same: the value is infinite rather than an exception,
    and the JSON writer stores that as null. One huge session must not kill a walk-forward.
    A zero initial equity has no defined growth rate. That case is -1, the same
    value a non-positive final equity already uses, not an infinite gain.
    """
    if initial == 0:
        return -1.0
    if not final > 0.0:
        return -1.0
    if not years > 0.0:
        return math.nan
    ratio = final / initial
    try:
        grown = ratio ** (1.0 / years)
    except OverflowError:
        return math.inf if ratio >= 1.0 else -math.inf
    if not math.isfinite(grown):
        return math.inf if ratio >= 1.0 else -math.inf
    return grown - 1.0


def metrics(res):
    """Statistics on last-RTH-close marks. Definitions match the Arena metrics function."""
    daily = res["daily"]
    initial = res["initial_equity"]
    if not daily:
        return {"error": "no daily marks", "disqualified": bool(res["disqualified"])}
    equity = [row["equity"] for row in daily]
    exposure = [row["exposure"] for row in daily]
    n = len(equity)
    years = n / 252.0
    previous = [initial] + equity[:-1]
    returns = [_session_return(equity[i], previous[i]) for i in range(n)]
    total = _session_return(equity[-1], initial)
    cagr = _cagr(equity[-1], initial, years)
    if n > 1:
        mean = sum(returns) / n
        var = sum((r - mean) ** 2 for r in returns) / (n - 1)
        stdev = math.sqrt(var)
    else:
        mean = returns[0]
        stdev = 0.0
    vol = stdev * math.sqrt(252.0)
    sharpe = mean / stdev * math.sqrt(252.0) if stdev > 0.0 else 0.0
    downside = math.sqrt(sum(min(r, 0.0) ** 2 for r in returns) / n)
    sortino = mean / downside * math.sqrt(252.0) if downside > 0.0 else 0.0
    path = [initial] + equity
    peak = path[0]
    drawdowns = []
    for value in path:
        if value > peak:
            peak = value
        if peak == 0:
            drawdowns.append(0.0)
        else:
            drawdowns.append(value / peak - 1.0)
    max_dd = min(drawdowns)
    longest = 0
    current = 0
    for value in drawdowns[1:]:
        current = current + 1 if value < 0.0 else 0
        if current > longest:
            longest = current
    calmar = cagr / abs(max_dd) if max_dd < 0.0 else math.inf
    episodes = res["episodes"]
    pnls = [ep["pnl"] for ep in episodes]
    hold_sessions = [ep["end_ordinal"] - ep["start_ordinal"] for ep in episodes]
    hold_bars = [ep["bars"] for ep in episodes]
    mean_equity = sum(equity) / n
    up = sum(1 for r in returns if r > 0.0) / n
    start = daily[0]["ordinal"]
    labels = []
    per = {}
    prev_end = initial
    for i, row in enumerate(daily):
        label = f"B{(row['ordinal'] - start) // 252 + 1:02d}"
        labels.append(label)
        if label not in per:
            per[label] = [prev_end, None]
        per[label][1] = equity[i]
        prev_end = equity[i]
    return {
        "sessions": n,
        "years": round(years, 3),
        "first_session": daily[0]["session"],
        "last_session": daily[-1]["session"],
        "initial_equity": initial,
        "final_equity": equity[-1],
        "final_shares": res["shares"],
        "total_return": total,
        "cagr": cagr,
        "ann_vol": vol,
        "sharpe": sharpe,
        "sortino": sortino,
        "max_drawdown": max_dd,
        "max_dd_duration_sessions": longest,
        "calmar": calmar,
        "worst_day": min(returns),
        "best_day": max(returns),
        "percent_up_days": up,
        "n_fills": res["n_fills"],
        "round_trips": len(episodes),
        "hit_rate": (sum(1 for pnl in pnls if pnl > 0.0) / len(pnls)) if pnls else None,
        "avg_trade_pnl": (sum(pnls) / len(pnls)) if pnls else None,
        "avg_holding_sessions": (sum(hold_sessions) / len(hold_sessions)) if hold_sessions else None,
        "avg_holding_bars": (sum(hold_bars) / len(hold_bars)) if hold_bars else None,
        "long_round_trips": sum(1 for ep in episodes if ep["side"] > 0),
        "short_round_trips": sum(1 for ep in episodes if ep["side"] < 0),
        "long_pnl": sum(ep["pnl"] for ep in episodes if ep["side"] > 0),
        "short_pnl": sum(ep["pnl"] for ep in episodes if ep["side"] < 0),
        "open_position_at_end": res["open_episode"],
        "traded_notional": res["traded_notional"],
        "turnover_per_year": (res["traded_notional"] / mean_equity / years) if mean_equity > 0.0 and years > 0.0 else None,
        "fees_total": res["fees"]["total"],
        "fees_breakdown": dict(res["fees"]),
        "slippage_cost": res["slippage"],
        "borrow_cost": res["borrow"],
        "financing_cost": res["financing"],
        "dividends_net": res["dividends"],
        "costs_total": res["fees"]["total"] + res["slippage"] + res["borrow"] + res["financing"],
        "margin_calls_queued": res["margin_calls_queued"],
        "margin_calls_filled": res["margin_calls_filled"],
        "max_gross_leverage": res["max_gross_leverage"],
        "avg_gross_leverage": res["avg_gross_leverage"],
        "worst_equity_over_hwm": res["worst_equity_over_hwm"],
        "disqualified": bool(res["disqualified"]),
        "liquidation": res["liquidation"],
        "avg_abs_exposure_bars": res["bar_exposure"]["avg_abs"],
        "frac_bars_long": res["bar_exposure"]["frac_long"],
        "frac_bars_short": res["bar_exposure"]["frac_short"],
        "avg_net_exposure_close": sum(exposure) / n,
        "avg_abs_exposure_close": sum(abs(x) for x in exposure) / n,
        "frac_sessions_long_at_close": sum(1 for x in exposure if x > 0.0) / n,
        "frac_sessions_short_at_close": sum(1 for x in exposure if x < 0.0) / n,
        "share_factor": res["config"]["share_factor"],
        "borrow_rate_annual": res["config"]["borrow_rate_annual"],
        "financing_rate_annual": res["config"]["financing_rate_annual"],
        "fee_mult": res["config"]["fee_mult"],
        "period_returns": {
            label: _session_return(per[label][1], per[label][0]) for label in per
        },
    }


class BuyAndHold:
    """Passive 1x long through the desk engine. One fill, then hold."""

    def on_bar(self, bar, account):
        return 1.0


class TwoXLong:
    """Constant 2x long. Financing is whatever the desk engine charges (7% by default)."""

    def on_bar(self, bar, account):
        return 2.0
