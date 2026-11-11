"""Design versus locked tail. Future bars must not reach a decision."""

from __future__ import annotations

import gc
import inspect
import os
import pickle
import random
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from backtester import engine_arena, runner
from backtester.arena_runner import run_window
from backtester.data import slice_walkforward
from backtester.runner import execute, walk_forward

SENTINEL = 99_999.0


def make_frame(n_days: int, *, price_of=None, sentinel_day=None, sentinel=SENTINEL) -> pd.DataFrame:
    rows = []
    for day in range(n_days):
        date = (datetime(2026, 3, 2) + timedelta(days=day)).date().isoformat()
        for minute, phase in ((240, "pre"), (570, "rth"), (571, "rth"), (960, "post")):
            price = 100.0 + day if price_of is None else float(price_of(day))
            close = sentinel if sentinel_day == day else price
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


class ArenaRec:
    """Records delivered days. Importable so the spawned design run can unpickle it."""

    WANTS_EXTENDED_HOURS = False

    def __init__(self):
        self.rows = []

    def on_bar(self, bar, state):
        self.rows.append(int(bar["day"]))
        return 1.0


class DeskRec:
    def __init__(self):
        self.rows = []

    def on_bar(self, bar, account):
        self.rows.append(
            (bar["session"], float(bar["close"]), bar["time"], account["live"], account["equity"], account["position_shares"])
        )
        return 1.0 if bar["time"] == "09:30" and bar["phase"] == "rth" else None


def _holds_close(obj, needle, depth=0):
    if depth > 8:
        return False
    try:
        if isinstance(obj, float):
            return obj == needle
        if isinstance(obj, pd.DataFrame):
            if "close" in obj.columns and obj["close"].size and (obj["close"].to_numpy() == needle).any():
                return True
            return False
        if isinstance(obj, pd.Series):
            return bool(obj.size and (obj.to_numpy() == needle).any())
        if isinstance(obj, np.ndarray) and obj.dtype.kind in "fiu" and obj.size:
            return bool((obj == needle).any())
        if isinstance(obj, dict):
            return any(_holds_close(value, needle, depth + 1) for value in obj.values())
        if isinstance(obj, (list, tuple)):
            return any(_holds_close(value, needle, depth + 1) for value in obj)
    except Exception:
        return False
    return False


def _read_temp_pickles(needle):
    """Unpickle every readable temp candidate. Returns whether the close was found, and how many files were read."""
    root = Path(tempfile.gettempdir())
    paths = set(root.glob("bt-locked-*")) | set(root.glob("*.pkl"))
    saw = False
    files = 0
    for path in paths:
        if not path.is_file():
            continue
        try:
            if path.stat().st_size > 32 * 1024 * 1024:
                continue
            blob = path.read_bytes()
        except OSError:
            continue
        files += 1
        try:
            obj = pickle.loads(blob)
        except Exception:
            continue
        if _holds_close(obj, needle):
            saw = True
    return saw, files


class SpillHunter:
    """Globs the temp directory for the locked spill and tries to unpickle it.

    The needle is a class constant so the child does not need a parent-set
    attribute. It exists only on the locked slice.
    """

    needle = 91_827_364.0
    WANTS_EXTENDED_HOURS = False

    def __init__(self):
        self.checked = False
        self.saw = False
        self.files = 0

    def on_bar(self, _bar, _state):
        if not self.checked:
            self.checked = True
            self.saw, self.files = _read_temp_pickles(self.needle)
        return 0.0


class MemoryScanner:
    """Trades only if a locked-tail close is visible on the stack or in gc.

    The needle is a class attribute, so a scan of every float would see it even
    in a clean process. The scan looks for that close inside market-data
    containers: a frame column, a bar dict, or a numeric array long enough to
    be a column.
    """

    needle = 87_654_321.0
    WANTS_EXTENDED_HOURS = False

    def __init__(self):
        self.checked = False
        self.saw = False

    def on_bar(self, _bar, _state):
        if not self.checked:
            self.checked = True
            self.saw = market_holds(type(self).needle)
        return 1.0 if self.saw else 0.0


class DesignInfluence:
    """A class flag set during design must not arm the locked run."""

    primed = False
    WANTS_EXTENDED_HOURS = False

    def __init__(self):
        self.arm = None

    def on_bar(self, _bar, _state):
        if self.arm is None:
            self.arm = DesignInfluence.primed
            DesignInfluence.primed = True
            return 1.0 if self.arm else 0.0
        return 1.0 if self.arm else 0.0


def _close_equals(value, needle) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and float(value) == needle


def market_holds(needle: float) -> bool:
    """True when ``needle`` appears as a bar close reachable from this process."""

    def holds(obj) -> bool:
        try:
            if isinstance(obj, pd.DataFrame):
                if "close" in getattr(obj, "columns", ()):
                    values = obj["close"].to_numpy(dtype=float, copy=False)
                    if values.size and bool(np.any(values == needle)):
                        return True
                return False
            if isinstance(obj, pd.Series) and obj.name == "close":
                values = obj.to_numpy(dtype=float, copy=False)
                return bool(values.size and np.any(values == needle))
            if isinstance(obj, np.ndarray) and obj.dtype.kind in "fiu" and obj.size >= 4:
                return bool(np.any(obj == needle))
            if isinstance(obj, dict) and _close_equals(obj.get("close"), needle):
                return True
            if isinstance(obj, (list, tuple)) and obj and isinstance(obj[0], dict):
                return any(isinstance(item, dict) and _close_equals(item.get("close"), needle) for item in obj)
        except Exception:
            return False
        return False

    frame = inspect.currentframe()
    while frame is not None:
        for value in tuple(frame.f_locals.values()):
            if holds(value):
                return True
        frame = frame.f_back
    for obj in gc.get_objects():
        if holds(obj):
            return True
    return False


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
    arena_summary, arena_slices, arena_design, arena_locked = walk_forward(
        ArenaRec, frame, "arena", tmp_path / "arena", symbol="TEST", bar_minutes=1,
        design_end_day=4, warmup_days=2, with_benchmarks=False,
    )
    desk_summary, desk_slices, desk_design, desk_locked = walk_forward(
        DeskRec, frame, "desk", tmp_path / "desk", symbol="TEST", bar_minutes=1,
        design_end_day=4, warmup_days=2, with_benchmarks=False,
    )
    assert arena_summary["design_end_day"] == 4
    assert arena_summary["locked_start_day"] == 5
    assert arena_slices.warmup_sessions == 2
    assert int(arena_slices.design["day"].max()) < int(arena_slices.locked["day"].min())
    design_days = arena_design["strategy"][0]["observer"].rows
    locked_days = arena_locked["strategy"][0]["observer"].rows
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

    design_sessions = [row[0] for row in desk_design["strategy"][0]["observer"].rows]
    locked_rows = desk_locked["strategy"][0]["observer"].rows
    assert max(design_sessions) <= desk_slices.design_end_date
    assert all(close != SENTINEL for _session, close, _time, _live, _eq, _shares in desk_design["strategy"][0]["observer"].rows)
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


@pytest.mark.parametrize("profile", ["arena", "desk"])
def test_design_child_cannot_unpickle_the_locked_spill(tmp_path, profile):
    needle = SpillHunter.needle
    frame = make_frame(6, sentinel_day=5, sentinel=needle)
    assert needle not in set(frame.loc[frame["day"] <= 3, "close"].tolist())
    assert needle in set(frame.loc[frame["day"] > 3, "close"].tolist())
    _summary, slices, design, _locked = walk_forward(
        SpillHunter, frame, profile, tmp_path / profile, symbol="TEST", bar_minutes=1,
        design_end_day=3, with_benchmarks=False,
    )
    assert needle not in set(slices.design["close"].tolist())
    assert needle in set(slices.locked["close"].tolist())
    observer = design["strategy"][0]["observer"]
    assert observer.checked is True
    assert observer.files >= 1
    assert observer.saw is False


@pytest.mark.parametrize("profile", ["arena", "desk"])
def test_design_process_cannot_see_the_locked_close(tmp_path, profile):
    needle = MemoryScanner.needle
    frame = make_frame(6, sentinel_day=5, sentinel=needle)
    assert market_holds(needle) is True
    summary, slices, design, locked = walk_forward(
        MemoryScanner, frame, profile, tmp_path / profile, symbol="TEST", bar_minutes=1,
        design_end_day=3, with_benchmarks=False,
    )
    assert needle not in set(slices.design["close"].tolist())
    assert needle in set(slices.locked["close"].tolist())
    observer = design["strategy"][0]["observer"]
    assert observer.checked is True
    assert observer.saw is False
    assert design["strategy"][0]["n_fills"] == 0
    assert summary["design"]["n_fills"] == 0
    # The locked book is a different interpreter. It may see its own tail.
    assert locked["strategy"][0]["observer"].checked is True


@pytest.mark.parametrize("profile", ["arena", "desk"])
def test_locked_run_is_not_armed_by_the_design_run(tmp_path, profile):
    DesignInfluence.primed = False
    frame = make_frame(5, price_of=lambda _day: 100.0)
    _summary, _slices, design, locked = walk_forward(
        DesignInfluence, frame, profile, tmp_path / profile, symbol="TEST", bar_minutes=1,
        design_end_day=2, with_benchmarks=False,
    )
    assert design["strategy"][0]["observer"].arm is False
    assert design["strategy"][0]["n_fills"] == 0
    assert locked["strategy"][0]["observer"].arm is False
    assert locked["strategy"][0]["n_fills"] == 0
    assert DesignInfluence.primed is False


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


class ChildSurface:
    """Records the argv, cwd, and environment the strategy actually sees."""

    WANTS_EXTENDED_HOURS = False

    def __init__(self):
        self.argv = None
        self.cwd = None
        self.cwd_listing = None
        self.env_text = None

    def on_bar(self, _bar, _state):
        if self.argv is None:
            self.argv = list(sys.argv)
            self.cwd = os.getcwd()
            self.cwd_listing = sorted(os.listdir(self.cwd))
            self.env_text = "\n".join(f"{key}={value}" for key, value in os.environ.items())
        return 0.0


class SystemExitChild:
    WANTS_EXTENDED_HOURS = False

    def on_bar(self, _bar, _state):
        raise SystemExit(3)


class HardExitChild:
    WANTS_EXTENDED_HOURS = False

    def on_bar(self, _bar, _state):
        os._exit(7)


def _frame_holding(needle: float) -> pd.DataFrame:
    frame = make_frame(6, sentinel_day=5, sentinel=needle)
    assert market_holds(needle) is True
    assert needle not in set(frame.loc[frame["day"] <= 3, "close"].tolist())
    assert needle in set(frame.loc[frame["day"] > 3, "close"].tolist())
    return frame


def _linux_dumpable() -> int | None:
    if sys.platform != "linux":
        return None
    import ctypes

    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    return int(libc.prctl(3, 0, 0, 0, 0))


def test_design_child_has_no_source_path_and_parent_drops_the_locked_close(tmp_path):
    token = "DATA_ROOT_TOKEN_9f3c"
    needle = float(random.Random(20261110).randrange(100_000_000, 900_000_000))
    source = tmp_path / token
    source.mkdir()
    parent_cwd = os.getcwd()
    planted = ["backtester", "walkforward", "--src", str(source)]
    old_argv = list(sys.argv)
    old_env = os.environ.get("MARKET_DATA_ROOT")
    held = {}

    def during():
        gc.collect()
        held["seen"] = market_holds(needle)
        held["dumpable"] = _linux_dumpable()

    sys.argv = list(planted)
    os.environ["MARKET_DATA_ROOT"] = str(source)
    os.chdir(source)
    try:
        summary, slices, design, _locked = walk_forward(
            ChildSurface,
            _frame_holding(needle),
            "arena",
            tmp_path / "out",
            symbol="TEST",
            bar_minutes=1,
            design_end_day=3,
            with_benchmarks=False,
            during_design=during,
        )
        assert os.path.samefile(os.getcwd(), source)
        assert list(sys.argv) == planted
    finally:
        os.chdir(parent_cwd)
        sys.argv = old_argv
        if old_env is None:
            os.environ.pop("MARKET_DATA_ROOT", None)
        else:
            os.environ["MARKET_DATA_ROOT"] = old_env

    assert held["seen"] is False
    if sys.platform == "linux":
        assert held["dumpable"] == 0
        assert _linux_dumpable() == 1
    observer = design["strategy"][0]["observer"]
    assert observer.argv == ["backtester-design"]
    assert observer.inherited_argv == ["backtester-design"]
    assert token not in observer.cwd
    assert token not in observer.inherited_cwd
    assert os.path.normcase(observer.cwd) != os.path.normcase(str(source))
    assert observer.cwd_listing == []
    assert token not in observer.env_text
    assert "MARKET_DATA_ROOT" not in observer.env_text
    assert needle in set(slices.locked["close"].tolist())
    assert needle not in set(slices.design["close"].tolist())
    assert summary["inspection_notes"] == []


def test_systemexit_in_the_child_fails_fast(tmp_path):
    cwd = os.getcwd()
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="SystemExit") as caught:
        walk_forward(
            SystemExitChild,
            make_frame(4),
            "arena",
            tmp_path,
            symbol="TEST",
            bar_minutes=1,
            design_end_day=1,
            with_benchmarks=False,
        )
    assert time.monotonic() - started < 30
    assert "timed out" not in str(caught.value)
    assert os.getcwd() == cwd


def test_os_exit_in_the_child_fails_fast(tmp_path):
    cwd = os.getcwd()
    started = time.monotonic()
    with pytest.raises(RuntimeError, match=r"exited 7") as caught:
        walk_forward(
            HardExitChild,
            make_frame(4),
            "arena",
            tmp_path,
            symbol="TEST",
            bar_minutes=1,
            design_end_day=1,
            with_benchmarks=False,
        )
    assert time.monotonic() - started < 30
    assert "timed out" not in str(caught.value)
    assert os.getcwd() == cwd


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
