"""Design versus locked tail. Future bars must not reach a decision."""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta

import pandas as pd
import pytest

from backtester import engine_arena, runner
from backtester.arena_runner import run_window
from backtester.data import slice_walkforward
from backtester.runner import execute, walk_forward

SENTINEL = 99_999.0


def make_frame(n_days: int, *, price_of=None, sentinel_day=None) -> pd.DataFrame:
    rows = []
    for day in range(n_days):
        date = (datetime(2026, 3, 2) + timedelta(days=day)).date().isoformat()
        for minute, phase in ((240, "pre"), (570, "rth"), (571, "rth"), (960, "post")):
            price = 100.0 + day if price_of is None else float(price_of(day))
            close = SENTINEL if sentinel_day == day else price
            high = max(price, close)
            low = min(price, close)
            rows.append(
                {
                    "timestamp_et": f"{date}T{minute // 60:02d}:{minute % 60:02d}:00-04:00",
                    "epoch_utc": 1_800_000_000 + day * 100_000 + minute,
                    "date": date,
                    "day": day,
                    "weekday": datetime.fromisoformat(date).weekday(),
                    "minute": minute,
                    "time": f"{minute // 60:02d}:{minute % 60:02d}",
                    "session": phase,
                    "open": price,
                    "high": high,
                    "low": low,
                    "close": close,
                    "volume": 1000.0,
                    "wap": price,
                    "count": 1,
                    "filled": 0,
                    "short_session": 0,
                    "symbol": "TEST",
                }
            )
    return pd.DataFrame(rows)


class ArenaProbe:
    WANTS_EXTENDED_HOURS = True

    def __init__(self):
        self.rows = []

    def on_bar(self, bar, state):
        self.rows.append((int(bar["day"]), float(bar["close"]), float(state["equity"])))
        return 1.0 if int(bar["minute"]) == 570 else None


class DeskProbe:
    def __init__(self):
        self.rows = []

    def on_bar(self, bar, account):
        self.rows.append((bar["session"], float(bar["close"]), float(account["equity"])))
        return 1.0 if bar["time"] == "09:30" and bar["phase"] == "rth" else None


def test_design_prefix_matches_the_full_sample_and_hides_the_tail():
    frame = make_frame(10, sentinel_day=9)
    slices = slice_walkforward(frame, design_end_day=5, warmup_days=2)
    full = ArenaProbe()
    design = ArenaProbe()
    execute(full, frame, "arena")
    execute(design, slices.design, "arena", max_day=slices.design_end_day)
    prefix = [row for row in full.rows if row[0] <= slices.design_end_day]
    assert design.rows == prefix
    assert prefix
    assert all(close != SENTINEL for _day, close, _eq in design.rows)
    assert any(close == SENTINEL for _day, close, _eq in full.rows)
    full_desk = DeskProbe()
    design_desk = DeskProbe()
    execute(full_desk, frame, "desk")
    execute(design_desk, slices.design, "desk", max_day=slices.design_end_day)
    design_dates = set(slices.design["date"].astype(str))
    assert [row for row in full_desk.rows if row[0] in design_dates] == design_desk.rows
    assert all(close != SENTINEL for _session, close, _eq in design_desk.rows)
    with pytest.raises(RuntimeError, match="refusing"):
        execute(ArenaProbe(), frame, "arena", max_day=slices.design_end_day)


def test_profile_warmup_and_no_future_bars(tmp_path):
    frame = make_frame(8, price_of=lambda _day: 100.0, sentinel_day=7)
    seen = {"arena": [], "desk": []}

    class ArenaRec:
        WANTS_EXTENDED_HOURS = False

        def __init__(self):
            self.rows = []
            seen["arena"].append(self)

        def on_bar(self, bar, state):
            self.rows.append(int(bar["day"]))
            return 1.0

    class DeskRec:
        def __init__(self):
            self.rows = []
            seen["desk"].append(self)

        def on_bar(self, bar, account):
            self.rows.append(
                (bar["session"], float(bar["close"]), bar["time"], account["live"], account["equity"], account["position_shares"])
            )
            return 1.0 if bar["time"] == "09:30" and bar["phase"] == "rth" else None

    arena_summary, arena_slices, _ad, arena_locked = walk_forward(
        ArenaRec, frame, "arena", tmp_path / "arena", symbol="TEST", bar_minutes=1,
        design_end_day=4, warmup_days=2, with_benchmarks=False,
    )
    desk_summary, desk_slices, _dd, desk_locked = walk_forward(
        DeskRec, frame, "desk", tmp_path / "desk", symbol="TEST", bar_minutes=1,
        design_end_day=4, warmup_days=2, with_benchmarks=False,
    )
    assert arena_summary["design_end_day"] == 4
    assert arena_summary["locked_start_day"] == 5
    assert arena_slices.warmup_sessions == 2
    assert int(arena_slices.design["day"].max()) < int(arena_slices.locked["day"].min())
    design_days = seen["arena"][0].rows
    locked_days = seen["arena"][1].rows
    assert design_days
    assert max(design_days) <= 4
    assert locked_days == sorted(locked_days)
    assert min(locked_days) == 3
    assert max(locked_days) == 7
    assert 8 not in locked_days
    result, _metrics = arena_locked["strategy"]
    assert result["n_fills"] == 0
    assert result["daily"][0][0] == 5
    assert abs(result["daily"][0][2] - 1.0) < 0.01

    design_sessions = [row[0] for row in seen["desk"][0].rows]
    locked_rows = seen["desk"][1].rows
    assert max(design_sessions) <= desk_slices.design_end_date
    assert all(close != SENTINEL for _session, close, _time, _live, _eq, _shares in seen["desk"][0].rows)
    live = [row for row in locked_rows if row[3]]
    warmup = [row for row in locked_rows if not row[3]]
    assert warmup and live
    assert all(row[0] < desk_slices.locked_start_date for row in warmup)
    assert all(row[0] >= desk_slices.locked_start_date for row in live)
    assert live[0][4] == 1_000_000.0
    assert live[0][5] == 0
    fills = desk_locked["strategy"][0]["fills"]
    assert len(fills) == 1
    assert fills[0]["session"] == desk_slices.locked_start_date
    assert fills[0]["time"] == "09:31"
    assert fills[0]["shares_after"] == int(1_000_000 / 100.0)
    assert "2999" not in inspect.getsource(runner)
    assert "3795" not in inspect.getsource(runner)
    assert desk_summary["warmup_rule"] == "flat_until_live"
    assert arena_summary["warmup_rule"] == "rescale_keep_exposure"


def test_slice_requires_exactly_one_cut_and_clips_warmup():
    frame = make_frame(10)
    with pytest.raises(ValueError, match="exactly one"):
        slice_walkforward(frame)
    with pytest.raises(ValueError, match="exactly one"):
        slice_walkforward(frame, design_end_day=3, design_fraction=0.5)
    with pytest.raises(ValueError, match="empty locked"):
        slice_walkforward(frame, design_end_day=9)
    sliced = slice_walkforward(frame, design_fraction=0.8)
    assert sliced.design_end_day == 7
    assert sliced.locked_start_day == 8
    warm = slice_walkforward(frame, design_end_day=5, warmup_days=100)
    assert warm.warmup_sessions == 6
    assert int(warm.locked_with_warmup["day"].min()) == 0
    assert int(warm.design["day"].max()) == 5
    assert set(warm.design["day"]).isdisjoint(set(warm.locked["day"]))


def test_pack_windows_still_exist_and_are_not_the_named_default():
    rows = []
    for day in (0, 2999, 3000, 3795, 3796):
        rows.append(
            {
                "day": day, "weekday": 0, "minute": 570, "time": "09:30", "session": "rth",
                "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1,
                "wap": 100.0, "count": 1, "filled": 0, "short_session": 0,
            }
        )
        rows.append(
            {
                "day": day, "weekday": 0, "minute": 571, "time": "09:31", "session": "rth",
                "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1,
                "wap": 100.0, "count": 1, "filled": 0, "short_session": 0,
            }
        )
    bars = pd.DataFrame(rows)
    _res, metrics = run_window(engine_arena.BuyAndHold, "design", bars)
    assert metrics["first_day"] == 0
    assert metrics["last_day"] == 2999
    _res, validation = run_window(engine_arena.BuyAndHold, "validation", bars)
    assert validation["first_day"] == 3000
    assert validation["last_day"] == 3795
    assert engine_arena.WINDOWS["validation"] == (3000, 3795)
    assert engine_arena.VALIDATION_WARMUP_DAYS == 504
