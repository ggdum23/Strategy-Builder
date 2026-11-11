"""Locks that fail if a Phase 1 carry-forward mutation comes back.

Each test is aimed at one named edit. The production code is already correct.
The assertion is the thing that dies when that edit is applied.
"""

from __future__ import annotations

import math
import os

import pandas as pd
import pytest

from backtester import costs as arena_costs
from backtester import costs_desk as desk_costs
from backtester import engine_arena
from backtester import engine_desk
from backtester.engine_desk import DeskConfig, metrics
from backtester.runner import _keystream, _open_sealed, _seal, execute


class _Plan:
    WANTS_EXTENDED_HOURS = False

    def __init__(self, plan):
        self.plan = plan

    def on_bar(self, bar, _state):
        return self.plan.get((bar["day"], bar["minute"]))


def _flat_days(days, price):
    rows = []
    for day in range(days):
        for minute in (570, 571):
            rows.append(
                {
                    "day": day,
                    "weekday": day % 5,
                    "minute": minute,
                    "time": f"{minute // 60:02d}:{minute % 60:02d}",
                    "session": "rth",
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price,
                    "volume": 1000,
                    "wap": price,
                    "count": 1,
                    "filled": 0,
                    "short_session": 0,
                }
            )
    return pd.DataFrame(rows)


def _desk_bar(session, time, phase, price, **kw):
    hour, minute = (int(part) for part in time.split(":"))
    close = float(kw.get("close", price))
    return {
        "session": session,
        "time": time,
        "minute": hour * 60 + minute,
        "phase": phase,
        "open": float(price),
        "high": max(float(price), close),
        "low": min(float(price), close),
        "close": close,
        "volume": 1000.0,
        "wap": close,
        "count": 1,
        "filled": 0,
        "new_session": False,
    }


def test_arena_month_accumulator_uses_tier_shares_not_pack_shares():
    """Kills month_vol += abs(delta) in place of tier_shares.

    k=10 turns about 20_000 pack shares into about 200_000 counted shares.
    Two buys in the same fee month stay under 300_000 pack shares and cross
    300_000 counted shares, so the second commission changes by about $150
    when the accumulator uses the wrong quantity.
    """
    k = 10.0
    bars = _flat_days(2, 50.0)
    result = engine_arena.run(_Plan({(0, 570): 1.0, (1, 570): 2.0}), bars, share_scale=k)
    assert len(result["fills"]) == 2
    assert result["fills"][0][0] // 21 == result["fills"][1][0] // 21
    delta1, px1 = result["fills"][0][3], result["fills"][0][5]
    delta2, px2 = result["fills"][1][3], result["fills"][1][5]
    first = arena_costs.fill_costs(delta1, px1, 0.0, k)
    right = arena_costs.fill_costs(delta2, px2, first["tier_shares"], k)
    wrong = arena_costs.fill_costs(delta2, px2, abs(delta1), k)
    assert abs(delta1) + abs(delta2) < 300_000
    assert first["tier_shares"] + right["tier_shares"] > 300_000
    assert abs(right["commission"] - wrong["commission"]) > 100.0
    got = result["fees"]["commission"]
    assert got == pytest.approx(first["commission"] + right["commission"], abs=1e-6)
    assert abs(got - (first["commission"] + wrong["commission"])) > 100.0


class _Ladder:
    def on_bar(self, row, _account):
        if row["session"] == "S0" and row["time"] == "09:30":
            return 1.0
        if row["session"] == "S1" and row["time"] == "09:30":
            return 2.0
        return None


def test_desk_month_accumulator_uses_tier_shares_not_pack_shares():
    """Kills month_shares += abs(delta) in place of tier_shares. Same shape as the Arena lock."""
    k = 10.0
    bars = []
    for session in ("S0", "S1"):
        bars.append(_desk_bar(session, "09:30", "rth", 50.0))
        bars.append(_desk_bar(session, "09:31", "rth", 50.0))
    result = engine_desk.run(_Ladder(), bars, DeskConfig(share_factor=k))
    assert len(result["fills"]) == 2
    assert result["fills"][0]["tier_month"] == result["fills"][1]["tier_month"]
    delta1 = result["fills"][0]["shares_delta"]
    delta2 = result["fills"][1]["shares_delta"]
    px1 = result["fills"][0]["fill_price"]
    px2 = result["fills"][1]["fill_price"]
    first = desk_costs.fill_costs(delta1, px1, 0.0, k)
    right = desk_costs.fill_costs(delta2, px2, first["tier_shares"], k)
    wrong = desk_costs.fill_costs(delta2, px2, abs(delta1), k)
    assert abs(delta1) + abs(delta2) < 300_000
    assert first["tier_shares"] + right["tier_shares"] > 300_000
    assert abs(right["commission"] - wrong["commission"]) > 100.0
    got = result["fills"][1]["commission"]
    assert got == pytest.approx(right["commission"], abs=1e-6)
    assert abs(got - wrong["commission"]) > 100.0


def test_desk_fee_mult_does_not_scale_cash_borrow_or_financing():
    """Kills cash -= bor * fee_mult and cash -= fin * fee_mult.

    The borrow and financing accumulators already ignore fee_mult. This locks
    the cash account at fee_mult=2, where doubling those two charges moves cash
    by fin+bor.
    """
    bars = [
        _desk_bar("S1", "09:30", "rth", 100.0),
        _desk_bar("S1", "09:31", "rth", 100.0),
        _desk_bar("S1", "16:00", "post", 100.0, close=150.0),
        _desk_bar("S2", "09:30", "rth", 150.0),
    ]

    class Short:
        def on_bar(self, row, _account):
            if row["session"] == "S1" and row["time"] == "09:30":
                return -1.0
            return None

    result = engine_desk.run(Short(), bars, DeskConfig(fee_mult=2.0))
    fill = result["fills"][0]
    assert fill["shares_delta"] < 0
    shares = fill["shares_after"]
    cash_before = 1_000_000.0 - fill["shares_delta"] * fill["fill_price"] - fill["fees"]
    equity = cash_before + shares * 150.0
    fin = desk_costs.financing_due(shares, 150.0, equity)
    bor = desk_costs.borrow_due(shares, 150.0)
    assert fin > 1.0 and bor > 1.0
    assert result["financing"] == pytest.approx(fin, abs=1e-9)
    assert result["borrow"] == pytest.approx(bor, abs=1e-9)
    assert result["config"]["fee_mult"] == 2.0
    assert result["cash"] == pytest.approx(cash_before - fin - bor, abs=1e-6)
    doubled = cash_before - 2.0 * fin - 2.0 * bor
    assert abs(result["cash"] - doubled) == pytest.approx(fin + bor, abs=1e-6)


def test_desk_metrics_use_252_session_years():
    """Kills years = n/365, and a 365-day vol or Sharpe that would ride along with it."""
    closes = [100.0, 110.0, 90.0, 120.0]
    bars = []
    for index, close in enumerate(closes):
        session = f"S{index}"
        bars.append(_desk_bar(session, "09:30", "rth", 100.0, close=100.0))
        bars.append(_desk_bar(session, "15:59", "rth", 100.0, close=close))

    class Once:
        def on_bar(self, row, _account):
            if row["session"] == "S0" and row["time"] == "09:30":
                return 1.0
            return None

    result = engine_desk.run(Once(), bars, DeskConfig())
    reported = metrics(result)
    equity = [row["equity"] for row in result["daily"]]
    n = len(equity)
    assert n == 4
    initial = result["initial_equity"]
    previous = [initial] + equity[:-1]
    returns = [equity[i] / previous[i] - 1.0 for i in range(n)]
    mean = sum(returns) / n
    var = sum((item - mean) ** 2 for item in returns) / (n - 1)
    stdev = math.sqrt(var)
    assert stdev > 0.0
    years = n / 252.0
    assert reported["years"] == round(years, 3)
    assert reported["years"] != round(n / 365.0, 3)
    assert reported["cagr"] == pytest.approx(engine_desk._cagr(equity[-1], initial, years))
    assert reported["cagr"] != pytest.approx(engine_desk._cagr(equity[-1], initial, n / 365.0))
    assert reported["ann_vol"] == pytest.approx(stdev * math.sqrt(252.0))
    assert reported["ann_vol"] != pytest.approx(stdev * math.sqrt(365.0))
    sharpe = mean / stdev * math.sqrt(252.0)
    assert reported["sharpe"] == pytest.approx(sharpe)
    assert reported["sharpe"] != pytest.approx(mean / stdev * math.sqrt(365.0))


def test_runner_hands_the_arena_engine_share_scale_k(monkeypatch):
    """Kills execute() passing share_scale=1.0 instead of the caller's factor."""
    from backtester import runner

    captured = {}
    real = runner.engine_arena.run

    def spy(strategy, bars, **kwargs):
        captured.update(kwargs)
        return real(strategy, bars, **kwargs)

    monkeypatch.setattr(runner.engine_arena, "run", spy)
    k = 10.0
    bars = _flat_days(2, 50.0)
    _result, metrics_out = execute(_Plan({(0, 570): 1.0, (1, 570): 2.0}), bars, "arena", share_factor=k)
    assert captured["share_scale"] == k
    direct = engine_arena.run(_Plan({(0, 570): 1.0, (1, 570): 2.0}), bars, share_scale=k)
    wrong = engine_arena.run(_Plan({(0, 570): 1.0, (1, 570): 2.0}), bars, share_scale=1.0)
    assert metrics_out["fees_breakdown"]["commission"] == pytest.approx(direct["fees"]["commission"])
    assert abs(metrics_out["fees_breakdown"]["commission"] - wrong["fees"]["commission"]) > 100.0
    assert metrics_out["cost_basis"]["share_scale"] == k


def test_a_nonce_derived_key_cannot_open_the_seal():
    """Kills a seal whose HMAC key is nonce||nonce instead of the caller key."""
    payload = b"locked-tail-frame"
    key = os.urandom(32)
    blob = _seal(payload, key)
    assert blob[:4] == b"BTLK"
    nonce = blob[4:20]
    assert len(nonce) == 16
    forged = nonce + nonce
    assert len(forged) == 32
    assert forged != key
    # The keystream itself must depend on the caller key. Replacing that key
    # with nonce||nonce inside the HMAC makes these two streams identical.
    fixed = bytes(range(16))
    assert _keystream(key, fixed, 32) != _keystream(fixed + fixed, fixed, 32)
    with pytest.raises(ValueError, match="authentication"):
        _open_sealed(blob, forged)
    assert _open_sealed(blob, key) == payload
