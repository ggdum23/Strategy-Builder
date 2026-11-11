"""Hand values for the published metrics. The engines are not rewritten here."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from backtester import engine_arena, engine_desk
from backtester.engine_desk import DeskConfig
from backtester.report import enrich_arena_metrics, percent_up_days
from test_parity import _canon, load_reference


def _arena(**kwargs):
    equity = list(kwargs.pop("equity"))
    initial = kwargs.pop("initial", 100.0)
    days = kwargs.pop("days", list(range(len(equity))))
    exposure = kwargs.pop("exposure", [0.0] * len(equity))
    daily = [
        (int(days[i]), float(equity[i]), float(exposure[i]), 0, 1.0) for i in range(len(equity))
    ]
    fees = kwargs.pop("fees", 0.0)
    result = {
        "initial_equity": initial,
        "daily": daily,
        "episodes": kwargs.pop("episodes", []),
        "open_episode": kwargs.pop("open_episode", None),
        "n_fills": kwargs.pop("n_fills", 0),
        "fees": {"total": fees, "commission": fees},
        "slippage": kwargs.pop("slippage", 0.0),
        "borrow": kwargs.pop("borrow", 0.0),
        "traded_notional": kwargs.pop("traded_notional", 0.0),
        "eth_notional": kwargs.pop("eth", 0.0),
        "busted": False,
        "bar_exposure": {"avg_abs": 0.25, "frac_long": 0.0, "frac_short": 0.0},
    }
    return engine_arena.metrics(result), result


def _desk(**kwargs):
    equity = list(kwargs.pop("equity"))
    initial = kwargs.pop("initial", 100.0)
    ordinals = kwargs.pop("ordinals", list(range(len(equity))))
    exposure = kwargs.pop("exposure", [0.0] * len(equity))
    daily = []
    for i, value in enumerate(equity):
        daily.append(
            {
                "equity": float(value),
                "exposure": float(exposure[i]),
                "ordinal": int(ordinals[i]),
                "session": f"S{int(ordinals[i]) + 1:05d}",
            }
        )
    fees = kwargs.pop("fees", 0.0)
    result = {
        "daily": daily,
        "initial_equity": initial,
        "shares": 0,
        "episodes": kwargs.pop("episodes", []),
        "open_episode": kwargs.pop("open_episode", None),
        "n_fills": kwargs.pop("n_fills", 0),
        "traded_notional": kwargs.pop("traded_notional", 0.0),
        "fees": {"total": fees},
        "slippage": kwargs.pop("slippage", 0.0),
        "borrow": kwargs.pop("borrow", 0.0),
        "financing": kwargs.pop("financing", 0.0),
        "dividends": kwargs.pop("dividends", 0.0),
        "margin_calls_queued": 0,
        "margin_calls_filled": 0,
        "max_gross_leverage": 0.0,
        "avg_gross_leverage": 0.0,
        "worst_equity_over_hwm": 1.0,
        "disqualified": False,
        "liquidation": None,
        "bar_exposure": {"avg_abs": 0.25, "frac_long": 0.0, "frac_short": 0.0},
        "config": {
            "share_factor": 1.0,
            "borrow_rate_annual": 0.0025,
            "financing_rate_annual": 0.07,
            "fee_mult": 1.0,
        },
    }
    return engine_desk.metrics(result), result


def test_drawdown_duration_is_sessions_and_resets_on_a_tie():
    # path peak stays 100 through 95; the tie at 100 resets; 70 starts a new streak.
    reported, _ = _arena(equity=[90, 80, 95, 100, 70], initial=100)
    assert reported["max_drawdown"] == 70 / 100 - 1
    assert reported["max_drawdown"] == pytest.approx(-0.3)
    assert reported["max_dd_duration_sessions"] == 3
    # A new high at 110 is drawdown 0. The three closes under 110 are the streak.
    second, _ = _arena(equity=[110, 100, 105, 90], initial=100)
    assert second["max_dd_duration_sessions"] == 3
    assert second["max_drawdown"] == 90 / 110 - 1


def test_one_session_drawdown_counts_the_only_mark():
    reported, _ = _arena(equity=[50], initial=100)
    assert reported["total_return"] == -0.5
    assert reported["max_drawdown"] == -0.5
    assert reported["max_dd_duration_sessions"] == 1
    assert reported["sharpe"] == 0.0
    downside = 0.5
    assert reported["sortino"] == pytest.approx((-0.5) / downside * math.sqrt(252))
    assert reported["cagr"] == (0.5) ** 252 - 1


def test_hit_rate_ignores_zero_pnl_and_the_open_episode():
    closed = [
        {"side": 1, "start_day": 1, "end_day": 3, "bars": 4, "pnl": 50.0},
        {"side": -1, "start_day": 3, "end_day": 4, "bars": 2, "pnl": -10.0},
    ]
    open_episode = {"side": 1, "start_day": 4, "end_day": 4, "bars": 1, "pnl": 80.0, "open": True}
    reported, _ = _arena(equity=[100, 100], episodes=closed, open_episode=open_episode)
    assert reported["round_trips"] == 2
    assert reported["hit_rate"] == 0.5
    assert reported["avg_holding_sessions"] == (2 + 1) / 2
    assert reported["avg_holding_bars"] == (4 + 2) / 2
    assert reported["long_round_trips"] == 1
    assert reported["short_round_trips"] == 1
    with_flat = closed + [{"side": 1, "start_day": 0, "end_day": 1, "bars": 1, "pnl": 0.0}]
    zero, _ = _arena(equity=[100, 100], episodes=with_flat, open_episode=open_episode)
    assert zero["round_trips"] == 3
    assert zero["hit_rate"] == 1 / 3
    assert zero["hit_rate"] != 2 / 3
    empty, _ = _arena(equity=[100], episodes=[], open_episode=open_episode)
    assert empty["round_trips"] == 0
    assert empty["hit_rate"] is None


def test_turnover_uses_unrounded_sessions_not_365_and_not_the_rounded_year():
    equity = [100.0] * 100
    reported, _ = _arena(equity=equity, traded_notional=100.0)
    unrounded = 100.0 / 100.0 / (100 / 252)
    rounded = 100.0 / 100.0 / round(100 / 252, 3)
    calendar = 100.0 / 100.0 / (100 / 365)
    assert reported["years"] == round(100 / 252, 3)
    assert reported["turnover_per_year"] == unrounded
    assert reported["turnover_per_year"] != pytest.approx(rounded)
    assert reported["turnover_per_year"] != pytest.approx(calendar)


def test_blocks_follow_the_day_index_not_the_row_number():
    reported, _ = _arena(equity=[110, 120, 90], days=[0, 251, 252], initial=100)
    assert reported["period_returns"]["S01"] == 120 / 100 - 1
    assert reported["period_returns"]["S02"] == 90 / 120 - 1
    assert reported["period_returns"]["S01"] == pytest.approx(0.2)
    assert reported["period_returns"]["S02"] == pytest.approx(-0.25)
    # A row-count block would be a single return of 90/100 - 1.
    assert 90 / 100 - 1 not in reported["period_returns"].values()
    desk, _ = _desk(equity=[120, 90], ordinals=[0, 252], initial=100)
    assert desk["period_returns"]["B01"] == 120 / 100 - 1
    assert desk["period_returns"]["B02"] == 90 / 120 - 1


def test_close_exposure_splits_long_short_and_flat():
    reported, _ = _arena(equity=[100, 100, 100], exposure=[1.0, -0.5, 0.0])
    assert reported["avg_net_exposure_close"] == (1.0 - 0.5 + 0.0) / 3
    assert reported["avg_abs_exposure_close"] == 0.5
    assert reported["frac_sessions_long_at_close"] == 1 / 3
    assert reported["frac_sessions_short_at_close"] == 1 / 3
    # The bar statistic is a different input and must not be copied from the closes.
    assert reported["avg_abs_exposure_bars"] == 0.25


def test_sharpe_and_sortino_use_252_and_every_day_in_the_downside():
    # 100 -> 125 is +0.25. 125 -> 118.75 is -0.05. Mean 0.1. Sample variance 0.045.
    equity = [125.0, 118.75]
    reported, _ = _arena(equity=equity, initial=100.0)
    returns = np.array([equity[0] / 100.0 - 1.0, equity[1] / equity[0] - 1.0])
    mean = float(returns.mean())
    sd = float(returns.std(ddof=1))
    assert reported["sharpe"] == mean / sd * math.sqrt(252)
    assert reported["sharpe"] != pytest.approx(mean / sd * math.sqrt(365))
    assert reported["ann_vol"] == sd * math.sqrt(252)
    downside = math.sqrt(float(np.mean(np.minimum(returns, 0.0) ** 2)))
    assert reported["sortino"] == mean / downside * math.sqrt(252)
    # Dropping the flat... there is no flat day. Dropping the up day from the
    # denominator (down-days only) is the wrong Sortino.
    down_only = math.sqrt(float(np.sum(np.minimum(returns, 0.0) ** 2)) / float(np.sum(returns < 0)))
    assert downside != down_only
    assert reported["sortino"] != pytest.approx(mean / down_only * math.sqrt(252))


def test_cagr_over_252_sessions_is_the_total_return_when_the_year_is_exact():
    initial = 100.0
    final = 110.0
    reported, _ = _arena(equity=[final] * 252, initial=initial)
    assert reported["sessions"] == 252
    assert reported["years"] == 1.0
    assert reported["total_return"] == final / initial - 1
    assert reported["total_return"] == pytest.approx(0.1)
    assert reported["cagr"] == reported["total_return"]
    assert reported["cagr"] != pytest.approx((final / initial) ** (365 / 252) - 1)


def test_percent_up_days_is_report_only_and_flat_is_not_up():
    reported, result = _arena(equity=[100.0, 101.0], initial=100.0)
    assert "percent_up_days" not in reported
    enriched = enrich_arena_metrics(result, reported)
    assert enriched["percent_up_days"] == 0.5
    assert percent_up_days([100.0, 100.0], 100.0) == 0.0
    assert percent_up_days([], 100.0) is None


def test_reference_metrics_match_byte_for_byte_on_a_synthetic_book():
    reported, result = _arena(
        equity=[90, 80, 95, 100, 70],
        initial=100,
        exposure=[1.0, -0.5, 0.0, 0.25, -0.25],
        episodes=[
            {"side": 1, "start_day": 0, "end_day": 2, "bars": 3, "pnl": 50.0},
            {"side": -1, "start_day": 2, "end_day": 4, "bars": 2, "pnl": 0.0},
        ],
        n_fills=4,
        fees=1.25,
        slippage=0.5,
        borrow=0.25,
        traded_notional=1000.0,
    )
    ref = load_reference()
    left = _canon(ref.metrics(result)).encode("utf-8")
    right = _canon(engine_arena.metrics(result)).encode("utf-8")
    assert left == right
    assert reported["max_dd_duration_sessions"] == 3
    assert reported["hit_rate"] == 0.5


def test_desk_matches_arena_on_the_same_equity_and_not_the_365_day_year():
    equity = [125.0, 118.75, 130.0]
    arena, _ = _arena(equity=equity, initial=100.0, exposure=[0.5, -0.25, 0.0])
    desk, _ = _desk(equity=equity, initial=100.0, exposure=[0.5, -0.25, 0.0])
    for key in ("total_return", "cagr", "ann_vol", "sharpe", "sortino", "max_drawdown", "max_dd_duration_sessions", "calmar", "best_day", "worst_day"):
        assert desk[key] == arena[key]
    returns = np.array([0.25, 118.75 / 125.0 - 1.0, 130.0 / 118.75 - 1.0])
    mean = float(returns.mean())
    sd = float(returns.std(ddof=1))
    assert arena["sharpe"] == mean / sd * math.sqrt(252)
    assert arena["sharpe"] != pytest.approx(mean / sd * math.sqrt(365))
    assert desk["percent_up_days"] == percent_up_days(equity, 100.0)


def _frame():
    rows = []
    for day, price in ((0, 100.0), (1, 100.0)):
        for minute in (570, 571):
            rows.append(
                {
                    "date": f"2026-03-0{2 + day}",
                    "day": day,
                    "weekday": 0,
                    "minute": minute,
                    "time": f"{minute // 60:02d}:{minute % 60:02d}",
                    "session": "rth",
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price,
                    "volume": 1000.0,
                    "wap": price,
                    "count": 1,
                    "filled": 0,
                    "short_session": 0,
                }
            )
    return pd.DataFrame(rows)


class _Flat:
    def on_bar(self, _bar, _state):
        return None


def test_a_flat_book_has_zero_return_on_both_engines():
    from backtester.runner import execute

    frame = _frame()
    for profile in ("arena", "desk"):
        result, metrics = execute(_Flat(), frame, profile)
        assert metrics["n_fills"] == 0
        assert metrics["total_return"] == 0.0
        assert metrics["final_equity"] == metrics["initial_equity"]
        assert metrics["sharpe"] == 0.0
        assert metrics["max_drawdown"] == 0.0
        assert metrics["fees_total"] == 0.0
        assert metrics["percent_up_days"] == 0.0
        assert math.isinf(metrics["calmar"]) and metrics["calmar"] > 0
        if profile == "desk":
            assert result["financing"] == 0.0
            assert result["borrow"] == 0.0
            assert result["dividends"] == 0.0
            assert metrics["financing_cost"] == 0.0
            assert metrics["borrow_cost"] == 0.0
            assert metrics["dividends_net"] == 0.0
            assert metrics["disqualified"] is False


def test_scaling_the_metric_inputs_by_two_leaves_ratios_and_doubles_dollars():
    episodes = [
        {"side": 1, "start_day": 0, "end_day": 1, "bars": 2, "pnl": 50.0},
        {"side": -1, "start_day": 1, "end_day": 1, "bars": 1, "pnl": 0.0},
    ]
    base, result = _arena(
        equity=[110.0, 90.0, 120.0],
        initial=100.0,
        exposure=[1.0, -0.5, 0.25],
        episodes=episodes,
        n_fills=3,
        fees=2.5,
        slippage=0.4,
        borrow=0.1,
        traded_notional=800.0,
    )
    scaled = {
        "initial_equity": result["initial_equity"] * 2,
        "daily": [(day, equity * 2, exposure, shares, close) for day, equity, exposure, shares, close in result["daily"]],
        "episodes": [{**episode, "pnl": episode["pnl"] * 2} for episode in result["episodes"]],
        "open_episode": None,
        "n_fills": result["n_fills"],
        "fees": {key: value * 2 for key, value in result["fees"].items()},
        "slippage": result["slippage"] * 2,
        "borrow": result["borrow"] * 2,
        "traded_notional": result["traded_notional"] * 2,
        "eth_notional": 0.0,
        "busted": False,
        "bar_exposure": dict(result["bar_exposure"]),
    }
    doubled = engine_arena.metrics(scaled)
    for key in (
        "total_return",
        "cagr",
        "ann_vol",
        "sharpe",
        "sortino",
        "max_drawdown",
        "max_dd_duration_sessions",
        "hit_rate",
        "avg_holding_sessions",
        "turnover_per_year",
        "best_day",
        "worst_day",
        "avg_abs_exposure_close",
    ):
        assert doubled[key] == base[key]
    for key in ("final_equity", "fees_total", "slippage_cost", "borrow_cost", "traded_notional", "avg_trade_pnl", "long_pnl"):
        assert doubled[key] == base[key] * 2
    assert doubled["hit_rate"] == 0.5
    assert doubled["turnover_per_year"] == base["turnover_per_year"]


def _bars():
    def one(session, time, price):
        hour, minute = (int(part) for part in time.split(":"))
        return {
            "session": session,
            "time": time,
            "minute": hour * 60 + minute,
            "phase": "rth",
            "open": price,
            "high": price,
            "low": price,
            "close": price,
            "volume": 1000.0,
            "wap": price,
            "count": 1,
            "filled": 0,
            "new_session": False,
        }

    return [
        one("S1", "09:30", 100.0),
        one("S1", "15:59", 100.0),
        one("S2", "09:30", 100.0),
        one("S2", "15:59", 100.0),
    ]


def test_desk_financing_and_borrow_match_the_cost_functions():
    from backtester import costs_desk as costs

    def target(row, _account, value):
        if row["session"] == "S1" and row["time"] == "09:30":
            return value
        return None

    long_book = engine_desk.run(
        type("S", (), {"on_bar": lambda self, row, account: target(row, account, 2.0)})(),
        _bars(),
        DeskConfig(),
    )
    long_metrics = engine_desk.metrics(long_book)
    financing = sum(
        costs.financing_due(event["shares"], event["price"], event["equity"])
        for event in long_book["financing_events"]
    )
    assert financing == long_book["financing"] == long_metrics["financing_cost"]
    assert financing > 0.0
    short_book = engine_desk.run(
        type("S", (), {"on_bar": lambda self, row, account: target(row, account, -1.0)})(),
        _bars(),
        DeskConfig(),
    )
    short_metrics = engine_desk.metrics(short_book)
    borrow = sum(
        costs.borrow_due(event["shares"], event["price"]) for event in short_book["borrow_events"]
    )
    assert borrow == short_book["borrow"] == short_metrics["borrow_cost"]
    assert borrow > 0.0
