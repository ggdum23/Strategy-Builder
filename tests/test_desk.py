"""Desk locks. Tolerances are not loosened: equalities are exact."""

from __future__ import annotations

import math

import numpy as np
import pytest

from backtester import costs as arena_costs
from backtester import costs_desk as D
from backtester.engine_desk import DeskConfig, metrics, run


def bar(session, time, phase, price, **kw):
    hour, minute = (int(part) for part in time.split(":"))
    o = float(kw.get("open", price))
    c = float(kw.get("close", price))
    h = float(kw["high"]) if "high" in kw else max(o, c, float(price))
    low = float(kw["low"]) if "low" in kw else min(o, c, float(price))
    return {
        "session": session,
        "time": time,
        "minute": hour * 60 + minute,
        "phase": phase,
        "open": o,
        "high": h,
        "low": low,
        "close": c,
        "volume": float(kw.get("volume", 1000)),
        "wap": float(kw.get("wap", c)),
        "count": int(kw.get("count", 10)),
        "filled": int(kw.get("filled", 0)),
        "new_session": kw.get("new_session", False),
    }


class Fn:
    def __init__(self, fn):
        self.fn = fn
        self.seen = []

    def on_bar(self, row, account):
        self.seen.append((dict(row), dict(account)))
        return self.fn(row, account)


def go(fn, bars, **cfg):
    strategy = fn if hasattr(fn, "on_bar") else Fn(fn)
    return strategy, run(strategy, bars, DeskConfig(**cfg))


def test_fee_schedules_are_distinct_published_numbers():
    assert arena_costs.TAF_PER_SHARE == 0.000195
    assert arena_costs.TAF_MAX == 9.79
    assert arena_costs.SEC_FEE_RATE == 0.0000206
    assert D.SEC_FEE_RATE == 27.80e-6
    assert D.TAF_PER_SHARE == 0.000166
    assert D.TAF_MAX == 8.30
    assert D.BORROW_RATE_ANNUAL == 0.0025
    assert D.FINANCING_RATE_ANNUAL == 0.07
    assert arena_costs.TAF_PER_SHARE != D.TAF_PER_SHARE
    assert arena_costs.SEC_FEE_RATE != D.SEC_FEE_RATE
    arena = arena_costs.fill_costs(-1_000_000, 0.9999, 0.0)
    desk = D.fill_costs(-1_000_000, 0.9999, 0.0)
    assert arena["taf"] == 9.79
    assert desk["taf"] == 8.30
    assert arena["sec"] != desk["sec"]
    assert desk["pass_through"] == desk["commission"] * (0.000175 + 0.00056)
    sell = D.fill_costs(-1000, 10.0, 0.0, share_factor=0.9)
    assert sell["sec"] == 27.80e-6 * 1000 * 10.0
    assert sell["taf"] == 0.000166 * 900
    assert sell["exchange"] == 0.0030 * 900
    buy = D.fill_costs(1000, 10.0, 0.0)
    assert buy["sec"] == 0.0
    assert buy["taf"] == 0.0
    assert buy["pass_through"] == buy["commission"] * (0.000175 + 0.00056)


def test_fee_mult_applies_after_min_and_cap():
    base = D.fill_costs(-10, 10.0, 0.0)
    stress = D.fill_costs(-10, 10.0, 0.0, fee_mult=2.0)
    assert base["commission"] == 0.35
    assert stress["commission"] == 0.70
    assert stress["total"] == base["total"] * 2
    capped = D.fill_costs(1_000_000, 0.01, 0.0, fee_mult=2.0)
    assert capped["commission"] == 200.0
    assert capped["tier_shares"] == 0.0
    assert capped["commission"] > 0.01 * 1_000_000 * 0.01


def test_thresholds_are_strict():
    assert D.breached_liquidation(299_999.99, 1_000_000.0) is True
    assert D.breached_liquidation(300_000.0, 1_000_000.0) is False
    assert D.breached_liquidation(0.30 * 1_000_000.0, 1_000_000.0) is False
    assert D.margin_call_required(3.15) is False
    assert D.margin_call_required(3.15 + 1e-12) is True
    assert D.financing_due(0, 100.0, 1_000_000.0) == 0.0


def test_next_rth_can_be_the_next_minute():
    bars = [bar("S1", "09:30", "rth", 100), bar("S1", "09:31", "rth", 100)]
    _strategy, res = go(lambda row, _account: 1.0 if row["time"] == "09:30" else None, bars)
    assert len(res["fills"]) == 1
    assert res["fills"][0]["time"] == "09:31"
    assert res["fills"][0]["phase"] == "rth"
    assert res["fills"][0]["session"] == "S1"
    assert res["fills"][0]["shares_after"] == int(1_000_000 / 100)


def test_pre_and_post_do_not_fill_and_a_newer_signal_replaces():
    bars = [
        bar("S1", "16:00", "post", 100),
        bar("S2", "08:00", "pre", 100),
        bar("S2", "09:30", "rth", 100),
    ]

    def signal(row, _account):
        if row["time"] == "16:00":
            return 1.0
        if row["time"] == "08:00":
            return 0.5
        return None

    _strategy, res = go(signal, bars)
    assert [fill["phase"] for fill in res["fills"]] == ["rth"]
    assert res["fills"][0]["time"] == "09:30"
    assert res["fills"][0]["session"] == "S2"
    assert res["fills"][0]["kind"] == "strategy"
    assert res["fills"][0]["shares_after"] == int(0.5 * 1_000_000 / 100)


def test_none_does_not_cancel_a_queued_order():
    bars = [
        bar("S1", "16:00", "post", 100),
        bar("S2", "08:00", "pre", 100),
        bar("S2", "09:30", "rth", 100),
    ]

    def signal(row, _account):
        if row["time"] == "16:00":
            return 1.0
        return None

    _strategy, res = go(signal, bars)
    assert res["fills"][0]["shares_after"] == int(1_000_000 / 100)


def test_same_bar_does_not_fill():
    _strategy, res = go(lambda _row, _account: 1.0, [bar("S1", "09:30", "rth", 100)])
    assert res["fills"] == []
    assert res["n_fills"] == 0


def test_synthesized_rth_bar_fills_at_its_open():
    bars = [
        bar("S1", "09:30", "rth", 100),
        bar("S1", "09:31", "rth", 100, open=80, close=80, high=80, low=80, filled=1),
    ]
    _strategy, res = go(lambda row, _account: 1.0 if row["time"] == "09:30" else None, bars)
    assert res["fills"][0]["price"] == 80.0
    assert res["fills"][0]["time"] == "09:31"
    assert res["shares"] == int(1_000_000 / 80)


def test_clip_to_3_not_reject():
    bars = [bar("S1", "09:30", "rth", 50), bar("S1", "09:31", "rth", 50)]
    _a, long = go(lambda row, _account: 9.0 if row["time"] == "09:30" else None, bars)
    _b, short = go(lambda row, _account: -9.0 if row["time"] == "09:30" else None, bars)
    assert long["fills"][0]["shares_after"] == int(3 * 1_000_000 / 50)
    assert short["fills"][0]["shares_after"] == int(-3 * 1_000_000 / 50)
    assert isinstance(long["fills"][0]["shares_after"], int)


def test_bad_targets_raise_and_numpy_reals_work():
    bars = [bar("S1", "09:30", "rth", 100), bar("S1", "09:31", "rth", 100)]
    with pytest.raises(ValueError):
        go(lambda _row, _account: True, bars)
    with pytest.raises(ValueError):
        go(lambda _row, _account: np.bool_(True), bars)
    with pytest.raises(ValueError):
        go(lambda _row, _account: "1", bars)
    with pytest.raises(ValueError):
        go(lambda _row, _account: float("nan"), bars)
    _strategy, res = go(lambda row, _account: np.float64(1.0) if row["time"] == "09:30" else None, bars)
    assert res["n_fills"] == 1


def test_unchanged_target_does_not_rebalance():
    bars = [bar("S1", f"09:{minute:02d}", "rth", 100 + minute) for minute in range(30, 36)]
    _strategy, res = go(lambda _row, _account: 1.0, bars)
    assert res["n_fills"] == 1
    _flat, flat = go(lambda _row, _account: 0.0, bars)
    assert flat["n_fills"] == 0
    _hold, held = go(lambda row, _account: 1.0 if row["time"] == "09:30" else None, bars)
    assert held["n_fills"] == 1
    assert held["shares"] == res["shares"]


def test_strategy_view_is_exactly_the_spec_keys():
    seen = {}

    def signal(row, account):
        seen.setdefault("flags", []).append(row["new_session"])
        seen["bar"] = set(row)
        seen["account"] = set(account)
        return None

    go(signal, [bar("S1", "09:30", "rth", 100, new_session=False), bar("S1", "09:31", "rth", 100, new_session=True)])
    assert seen["flags"] == [True, False]
    assert seen["bar"] == {
        "session", "time", "phase", "open", "high", "low", "close", "volume", "wap", "count", "filled", "new_session",
    }
    assert seen["account"] == {
        "cash", "position_shares", "equity", "exposure", "gross_leverage", "hwm", "drawdown", "live", "liquidated",
    }
    assert "target" not in seen["account"]
    assert "minute" not in seen["bar"]


def test_long_financing_dividend_and_fee_mult_use_the_formula():
    def bars():
        return [
            bar("S1", "08:00", "pre", 100),
            bar("S1", "09:30", "rth", 100),
            bar("S1", "16:00", "post", 100, close=101, high=101),
            bar("S2", "08:00", "pre", 101),
            bar("S2", "09:30", "rth", 101),
        ]

    def signal(row, _account):
        if row["session"] == "S1" and row["time"] == "08:00":
            return 1.0
        return None

    def expect(fee_mult, dividend):
        shares = int(1_000_000 / 100.0)
        px = D.slipped_price(100.0, shares, fee_mult=fee_mult)
        fees = D.fill_costs(shares, px, 0.0, fee_mult=fee_mult)
        cash = 1_000_000.0
        cash -= shares * px + fees["total"]
        equity = cash + shares * 101.0
        fin = D.financing_due(shares, 101.0, equity)
        cash -= fin
        cash += shares * dividend
        return {"shares": shares, "px": px, "fees": fees["total"], "fin": fin, "cash": cash}

    _a, res = go(signal, bars(), dividends={"S2": 1.25})
    want = expect(1.0, 1.25)
    assert res["n_fills"] == 1
    assert res["fills"][0]["time"] == "09:30"
    assert res["fills"][0]["phase"] == "rth"
    assert res["shares"] == want["shares"]
    assert res["financing"] == want["fin"]
    assert res["dividends"] == want["shares"] * 1.25
    assert res["borrow"] == 0.0
    assert res["cash"] == want["cash"]
    assert res["financing_events"][0]["price"] == 101.0

    _b, stressed = go(signal, bars(), fee_mult=2.0)
    want2 = expect(2.0, 0.0)
    assert stressed["fills"][0]["fill_price"] == 100.0 * (1.0 + D.SLIPPAGE_BPS * 2.0 * 1e-4)
    assert res["fills"][0]["fill_price"] == 100.0 * (1.0 + D.SLIPPAGE_BPS * 1e-4)
    assert stressed["financing"] == want2["fin"]
    assert stressed["fees"]["total"] == want2["fees"]
    # At a price where trunc(equity/price)*price is not the starting equity,
    # doubled fees do not double the financing dollars. The formula is not multiplied.
    odd = [bar("A", "09:30", "rth", 33), bar("A", "09:31", "rth", 33), bar("B", "09:30", "rth", 33)]

    def odd_signal(row, _account):
        return 1.0 if row["session"] == "A" and row["time"] == "09:30" else None

    _c, plain = go(odd_signal, odd, fee_mult=1.0)
    _d, doubled = go(odd_signal, odd, fee_mult=2.0)
    assert doubled["fees"]["total"] == plain["fees"]["total"] * 2
    assert doubled["financing"] == D.financing_due(
        doubled["financing_events"][0]["shares"],
        doubled["financing_events"][0]["price"],
        doubled["financing_events"][0]["equity"],
    )
    assert doubled["financing"] != plain["financing"] * 2


def test_short_borrow_and_dividend_debit_at_the_boundary():
    bars = [
        bar("S1", "09:30", "rth", 100),
        bar("S1", "09:31", "rth", 100),
        bar("S1", "16:00", "post", 100, close=150, high=150),
        bar("S2", "09:30", "rth", 150),
    ]
    _strategy, res = go(
        lambda row, _account: -1.0 if row["time"] == "09:30" and row["session"] == "S1" else None,
        bars,
        dividends={"S2": 1.5},
    )
    shares = int(-1_000_000 / 100.0)
    px = D.slipped_price(100.0, shares)
    fees = D.fill_costs(shares, px, 0.0)
    cash = 1_000_000.0 - shares * px - fees["total"]
    equity = cash + shares * 150.0
    fin = D.financing_due(shares, 150.0, equity)
    bor = D.borrow_due(shares, 150.0)
    assert bor == D.BORROW_RATE_ANNUAL / D.SESSIONS_PER_YEAR * (-shares * 150.0)
    assert bor != D.BORROW_RATE_ANNUAL / D.SESSIONS_PER_YEAR * (-shares * 100.0)
    cash = cash - fin - bor + shares * 1.5
    assert res["borrow"] == bor
    assert res["financing"] == fin
    assert res["dividends"] == shares * 1.5
    assert res["dividends"] < 0.0
    assert res["cash"] == cash
    assert res["financing_events"][0]["price"] == 150.0
    assert len(res["borrow_events"]) == 1


def test_weekend_gap_is_one_accrual_not_three_calendar_days():
    bars = [
        bar("2026-10-02", "09:30", "rth", 100),
        bar("2026-10-02", "09:31", "rth", 100),
        bar("2026-10-05", "09:30", "rth", 100),
    ]
    _strategy, res = go(lambda row, _account: 2.0 if row["time"] == "09:30" and row["session"] == "2026-10-02" else None, bars)
    assert len(res["financing_events"]) == 1
    event = res["financing_events"][0]
    assert event["amount"] == D.financing_due(event["shares"], event["price"], event["equity"])
    assert event["amount"] != 3 * event["amount"]


def test_margin_call_on_an_rth_close_fills_the_next_minute_even_at_3x():
    bars = [
        bar("S1", "09:30", "rth", 100),
        bar("S1", "09:31", "rth", 100, close=90, low=90),
        bar("S1", "09:32", "rth", 90),
        bar("S2", "09:30", "rth", 90),
    ]
    _strategy, res = go(lambda row, _account: 3.0 if row["time"] == "09:30" and row["session"] == "S1" else None, bars)
    assert res["disqualified"] is False
    assert [fill["kind"] for fill in res["fills"]] == ["strategy", "margin"]
    assert res["fills"][1]["time"] == "09:32"
    assert res["fills"][1]["session"] == "S1"
    assert res["margin_calls_queued"] == 1
    assert res["margin_calls_filled"] == 1
    shares = int(3 * 1_000_000 / 100.0)
    px = D.slipped_price(100.0, shares)
    fees = D.fill_costs(shares, px, 0.0)
    cash = 1_000_000.0 - shares * px - fees["total"]
    equity_crash = cash + shares * 90.0
    assert abs(shares * 90.0) / equity_crash > 3.15
    assert equity_crash > 0.30 * 1_000_000.0
    new_shares = int(3.0 * equity_crash / 90.0)
    assert new_shares != shares
    assert res["fills"][1]["shares_after"] == new_shares


def test_a_pending_signal_blocks_the_margin_call():
    bars = [
        bar("S1", "09:30", "rth", 100),
        bar("S1", "09:31", "rth", 100, close=90, low=90),
        bar("S1", "09:32", "rth", 90),
    ]

    def signal(row, _account):
        if row["time"] == "09:30":
            return 3.0
        if row["time"] == "09:31":
            return 1.0
        return None

    _strategy, res = go(signal, bars)
    assert res["margin_calls_queued"] == 0
    assert res["fills"][1]["kind"] == "strategy"
    assert res["fills"][1]["time"] == "09:32"


def test_post_close_does_not_queue_a_margin_call():
    bars = [
        bar("S1", "09:30", "rth", 100),
        bar("S1", "09:31", "rth", 100),
        bar("S1", "15:59", "rth", 98),
        bar("S1", "16:00", "post", 98, close=90, low=90),
        bar("S2", "09:30", "rth", 100),
        bar("S2", "09:31", "rth", 100),
    ]
    _strategy, res = go(lambda row, _account: 3.0 if row["time"] == "09:30" and row["session"] == "S1" else None, bars)
    shares = res["fills"][0]["shares_after"]
    # Reconstruct cash from the fill the engine reported, then prove the two closes
    # sit on opposite sides of 3.15. The post close must not create a second fill.
    cash = 1_000_000.0 - shares * res["fills"][0]["fill_price"] - res["fills"][0]["fees"]
    rth_eq = cash + shares * 98.0
    post_eq = cash + shares * 90.0
    assert abs(shares * 98.0) / rth_eq <= 3.15
    assert abs(shares * 90.0) / post_eq > 3.15
    assert res["n_fills"] == 1
    assert res["margin_calls_queued"] == 0
    assert res["disqualified"] is False


def test_later_signal_replaces_an_unfilled_margin_order():
    bars = [
        bar("S1", "09:30", "rth", 100),
        bar("S1", "15:59", "rth", 100, close=90, low=90),
        bar("S2", "08:00", "pre", 90),
        bar("S2", "09:30", "rth", 90),
    ]

    def signal(row, _account):
        if row["session"] == "S1" and row["time"] == "09:30":
            return 3.0
        if row["session"] == "S2" and row["time"] == "08:00":
            return 1.0
        return None

    _strategy, res = go(signal, bars)
    assert res["margin_calls_queued"] == 1
    assert res["fills"][-1]["kind"] == "strategy"
    assert res["fills"][-1]["time"] == "09:30"
    assert res["fills"][-1]["session"] == "S2"
    # Boundary financing changes cash before the fill. Size from the engine's own
    # financing event so the assertion is the replacement, not a second formula.
    cash = 1_000_000.0
    shares = 0
    for fill in res["fills"][:-1]:
        cash -= fill["shares_delta"] * fill["fill_price"] + fill["fees"]
        shares = fill["shares_after"]
    cash -= res["financing"]
    equity_open = cash + shares * 90.0
    assert res["fills"][-1]["shares_after"] == int(1.0 * equity_open / 90.0)


def test_running_high_water_liquidation_not_the_starting_equity():
    def path(close):
        bars = [
            bar("S1", "09:30", "rth", 100),
            bar("S1", "09:31", "rth", 100),
            bar("S1", "15:59", "rth", 150),
            bar("S2", "09:30", "rth", 150, close=close, low=min(150, close), high=max(150, close)),
            bar("S3", "09:30", "rth", close),
        ]
        _strategy, res = go(lambda row, _account: 1.0 if row["session"] == "S1" and row["time"] == "09:30" else None, bars, dividends={"S3": 5.0})
        return res

    crash = path(40)
    assert crash["disqualified"] is True
    liq = crash["liquidation"]
    assert liq["session"] == "S2"
    assert liq["time"] == "09:30"
    assert liq["close"] == 40
    assert liq["hwm"] > 1_000_000.0
    assert liq["equity_before"] < 0.30 * liq["hwm"]
    assert liq["equity_before"] > 0.30 * 1_000_000.0
    assert crash["fills"][-1]["kind"] == "liquidation"
    assert crash["fills"][-1]["session"] == "S2"
    assert crash["shares"] == 0
    assert [event["session"] for event in crash["financing_events"]] == ["S2"]
    assert [event["session"] for event in crash["dividend_events"]] == ["S2"]
    assert crash["dividends"] == 0.0
    hold = path(80)
    assert hold["disqualified"] is False
    assert hold["worst_equity_over_hwm"] >= 0.30
    assert hold["liquidation"] is None


def test_post_liquidation_hits_the_next_daily_mark():
    bars = [
        bar("S1", "09:30", "rth", 100),
        bar("S1", "09:31", "rth", 100),
        bar("S1", "15:59", "rth", 100),
        bar("S1", "16:00", "post", 100, close=20, low=20),
        bar("S2", "09:30", "rth", 20),
    ]
    _strategy, res = go(lambda row, _account: 1.0 if row["time"] == "09:30" and row["session"] == "S1" else None, bars)
    assert res["disqualified"] is True
    assert res["liquidation"]["phase"] == "post"
    assert res["daily"][0]["shares"] != 0
    assert res["daily"][0]["close"] == 100
    assert res["daily"][0]["equity"] > 900_000.0
    assert res["daily"][1]["shares"] == 0
    assert res["daily"][1]["equity"] == res["cash"]
    assert res["worst_equity_over_hwm"] < 0.30
    reported = metrics(res)
    assert reported["disqualified"] is True
    assert reported["liquidation"]["phase"] == "post"
    assert math.isfinite(reported["final_equity"])


def test_share_factor_scales_per_share_fees_not_the_position():
    bars = [bar("S1", "09:30", "rth", 100), bar("S1", "09:31", "rth", 100)]

    def short(row, _account):
        return -1.0 if row["time"] == "09:30" else None

    _a, raw = go(short, bars, share_factor=1.0)
    _b, scaled = go(short, bars, share_factor=0.9)
    assert raw["shares"] == scaled["shares"]
    assert scaled["fees"]["exchange"] == raw["fees"]["exchange"] * 0.9
    assert scaled["fees"]["sec"] == raw["fees"]["sec"]
    assert scaled["fees"]["taf"] == raw["fees"]["taf"] * 0.9


def test_tier_month_resets_after_21_live_sessions():
    def sessions(n):
        return [bar(f"S{i:02d}", "09:30", "rth", 1.0) for i in range(n)]

    def cross(row, _account):
        if row["session"] == "S00":
            return 1.0
        if row["session"] == "S20":
            return 0.0
        return None

    def same(row, _account):
        if row["session"] == "S00":
            return 1.0
        if row["session"] == "S01":
            return 0.0
        return None

    _a, crossed = go(cross, sessions(22))
    _b, stayed = go(same, sessions(4))
    assert crossed["fills"][0]["shares_after"] == 1_000_000
    assert crossed["fills"][0]["commission"] == 2450.0
    assert crossed["fills"][0]["tier_month"] == 0
    assert crossed["fills"][1]["shares_delta"] == -1_000_000
    assert crossed["fills"][1]["commission"] == 2450.0
    assert crossed["fills"][1]["tier_month"] == 1
    assert crossed["fills"][1]["taf"] == 8.30
    assert stayed["fills"][1]["commission"] == 2000.0
    assert stayed["fills"][1]["tier_month"] == 0
    assert stayed["fills"][0]["pass_through"] == stayed["fills"][0]["commission"] * (0.000175 + 0.00056)
    sale_value = abs(stayed["fills"][1]["shares_delta"]) * stayed["fills"][1]["fill_price"]
    assert stayed["fills"][1]["sec"] == 27.80e-6 * sale_value


def test_warmup_stays_flat_and_does_not_leak_onto_the_first_live_open():
    bars = []
    for session, price in (("W0", 50.0), ("W1", 50.0), ("L0", 100.0)):
        bars.append(bar(session, "09:30", "rth", price))
        bars.append(bar(session, "09:31", "rth", price, close=80.0 if price == 50.0 else 100.0))
    strategy, res = go(lambda row, _account: 2.0 if row["time"] == "09:30" else None, bars, live_start_ordinal=2)
    assert [account["live"] for _row, account in strategy.seen] == [False, False, False, False, True, True]
    assert strategy.seen[4][1]["equity"] == 1_000_000.0
    assert strategy.seen[4][1]["position_shares"] == 0
    assert len(res["fills"]) == 1
    assert res["fills"][0]["session"] == "L0"
    assert res["fills"][0]["time"] == "09:31"
    assert res["fills"][0]["shares_after"] == int(2 * 1_000_000 / 100.0)
    assert res["daily"][0]["session"] == "L0"


def test_dividend_is_in_cash_before_the_ex_session_fill():
    bars = [
        bar("S1", "09:30", "rth", 100),
        bar("S1", "09:31", "rth", 100),
        bar("S1", "15:59", "rth", 100),
        bar("S2", "09:30", "rth", 100),
    ]

    def signal(row, _account):
        if row["session"] == "S1" and row["time"] == "09:30":
            return 1.0
        if row["session"] == "S1" and row["time"] == "15:59":
            return 2.0
        return None

    def sized(dividend):
        shares = int(1_000_000 / 100.0)
        px = D.slipped_price(100.0, shares)
        fees = D.fill_costs(shares, px, 0.0)
        cash = 1_000_000.0 - shares * px - fees["total"]
        equity = cash + shares * 100.0
        fin = D.financing_due(shares, 100.0, equity)
        cash = cash - fin + shares * dividend
        equity_open = cash + shares * 100.0
        return int(2.0 * equity_open / 100.0)

    with_div = sized(10.0)
    without = sized(0.0)
    assert with_div != without
    _strategy, res = go(signal, bars, dividends={"S2": 10.0})
    assert res["fills"][1]["shares_after"] == with_div
    assert res["fills"][1]["session"] == "S2"
    assert res["dividends"] == int(1_000_000 / 100.0) * 10.0


def test_accounting_identity_includes_financing_borrow_and_dividends():
    bars = [
        bar("S1", "09:30", "rth", 100),
        bar("S1", "09:31", "rth", 110),
        bar("S1", "15:59", "rth", 90),
        bar("S2", "09:30", "rth", 90),
        bar("S2", "09:31", "rth", 95),
    ]

    def signal(row, _account):
        if row["session"] == "S1" and row["time"] == "09:30":
            return -1.0
        if row["session"] == "S2" and row["time"] == "09:30":
            return 0.5
        return None

    _strategy, res = go(signal, bars, dividends={"S2": 0.25})
    # Same association as the engine: one boundary (fin, then borrow, then dividend)
    # before that session's fills, and each fill subtracts delta * fill_price + fees.
    cash = 1_000_000.0
    fills = res["fills"]
    assert len(res["financing_events"]) == len(res["borrow_events"]) == len(res["dividend_events"]) == 1
    assert fills[0]["session"] == "S1"
    assert fills[1]["session"] == "S2"
    cash -= fills[0]["shares_delta"] * fills[0]["fill_price"] + fills[0]["fees"]
    cash -= res["financing_events"][0]["amount"]
    cash -= res["borrow_events"][0]["amount"]
    cash += res["dividend_events"][0]["amount"]
    cash -= fills[1]["shares_delta"] * fills[1]["fill_price"] + fills[1]["fees"]
    assert cash == res["cash"]
    assert fills[-1]["shares_after"] == res["shares"]
    assert res["borrow"] > 0.0
    assert res["dividends"] < 0.0


def test_desk_does_not_call_on_day_end():
    class Strategy:
        def __init__(self):
            self.ends = 0

        def on_bar(self, _row, _account):
            return None

        def on_day_end(self, _summary):
            self.ends += 1

    strategy = Strategy()
    run(strategy, [bar("S1", "09:30", "rth", 100), bar("S1", "09:31", "rth", 100)], DeskConfig())
    assert strategy.ends == 0


def test_warmup_past_the_sample_stays_empty():
    _strategy, res = go(lambda _row, _account: 1.0, [bar("S1", "09:30", "rth", 100)], live_start_ordinal=3)
    assert res["n_fills"] == 0
    assert res["daily"] == []
    assert metrics(res)["error"] == "no daily marks"
