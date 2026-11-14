"""Prepared columns, loader equivalence, jail framing, and the venv mount rule."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from backtester.data import load_named, prepare_desk_bars
from backtester.engine_desk import DeskConfig
from backtester.prepared import (
    delete_entry,
    list_entries,
    load_frame,
    read_manifest,
    rebuild_entry,
    store_frame,
)
from backtester.sandbox import _deny_roots, runtime_overlap_error
from backtester.synthetic import write_series

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = json.loads((ROOT / "tests" / "golden_phase9.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _prepared_dir(tmp_path, monkeypatch):
    cache = tmp_path / "prepared"
    cache.mkdir()
    monkeypatch.setenv("STRATEGY_BUILDER_PREPARED_DIR", str(cache))
    monkeypatch.setenv("STRATEGY_BUILDER_PREPARED_MAX_BYTES", str(64 * 1024 * 1024))
    yield cache


def _memmap(array):
    current = np.asarray(array)
    seen = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, np.memmap):
            return current
        current = getattr(current, "base", None)
    return None


def _digest(frame) -> str:
    def conv(value):
        if isinstance(value, np.integer):
            return int(value)
        if isinstance(value, np.floating):
            return float(value)
        if isinstance(value, (list, tuple)):
            return [conv(item) for item in value]
        if isinstance(value, dict):
            return {str(key): conv(item) for key, item in value.items()}
        return value

    payload = {
        "columns": list(frame.columns),
        "dtypes": {column: str(frame[column].dtype) for column in frame.columns},
        "rows": conv(frame.to_dict(orient="list")),
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(raw).hexdigest()


class _Hold:
    def on_bar(self, bar, _state):
        phase = bar.get("phase", bar.get("session"))
        if phase == "rth" and bar.get("time") == "09:30":
            return 1.0
        return None


def test_vectorized_loader_matches_the_golden_frame_and_books(tmp_path):
    from backtester import engine_arena, engine_desk

    write_series(tmp_path / "src", "SYN", sessions=3)
    loaded = load_named(tmp_path / "src", "SYN")
    assert _digest(loaded.frame) == GOLDEN["frame_sha256"]
    assert loaded.inspection.native_tick == GOLDEN["native_tick"]
    assert loaded.inspection.typical_volume_bar == GOLDEN["typical_volume_bar"]
    assert loaded.inspection.typical_volume_session == GOLDEN["typical_volume_session"]
    assert int(loaded.frame["short_session"].sum()) == GOLDEN["short_sessions"]
    arena = engine_arena.run(_Hold(), loaded.frame, record_fills=True)
    desk = engine_desk.run(_Hold(), prepare_desk_bars(loaded.frame), DeskConfig())
    assert engine_arena.metrics(arena)["final_equity"] == GOLDEN["arena_final_equity"]
    assert arena["fees"]["total"] == GOLDEN["arena_fees"]
    assert engine_desk.metrics(desk)["final_equity"] == GOLDEN["desk_final_equity"]
    assert desk["fees"]["total"] == GOLDEN["desk_fees"]


def test_cached_load_matches_the_parse_and_maps_the_close_column(tmp_path):
    write_series(tmp_path / "src", "SYN", sessions=3)
    first = load_named(tmp_path / "src", "SYN")
    assert first.prepared
    second = load_named(tmp_path / "src", "SYN")
    assert second.prepared == first.prepared
    assert _digest(second.frame) == GOLDEN["frame_sha256"]
    assert second.inspection.as_dict() == first.inspection.as_dict()
    mapped = _memmap(second.frame["close"].to_numpy())
    assert mapped is not None
    assert str(mapped.filename).endswith("close.npy")


def test_a_corrupt_cache_is_rebuilt(tmp_path):
    write_series(tmp_path / "src", "SYN", sessions=3)
    first = load_named(tmp_path / "src", "SYN")
    close = next(Path(first.prepared).glob("*close.npy"))
    blob = bytearray(close.read_bytes())
    blob[-1] ^= 0xFF
    close.write_bytes(blob)
    again = load_named(tmp_path / "src", "SYN")
    assert _digest(again.frame) == GOLDEN["frame_sha256"]
    assert again.prepared


def test_early_close_session_is_short(tmp_path):
    write_series(tmp_path / "src", "SYN", sessions=20)
    loaded = load_named(tmp_path / "src", "SYN")
    short_days = {
        int(day)
        for day, flag in zip(loaded.frame["day"].tolist(), loaded.frame["short_session"].tolist())
        if int(flag) == 1
    }
    assert short_days == {19}


def test_designer_cache_refuses_a_ticker_and_a_date_without_echoing_them(tmp_path):
    leaked = pd.DataFrame(
        {
            "close": [1.0, 1.01],
            "session": ["rth", "rth"],
            "session_id": ["S00001", "S00001"],
            "note": ["ZZQQ", "other"],
        }
    )
    with pytest.raises(ValueError) as raised:
        store_frame(leaked, kind="designer", symbol="ZZQQ")
    text = str(raised.value)
    assert "ZZQQ" not in text
    assert text == "designer view contains the ticker"
    dated = pd.DataFrame(
        {
            "close": [1.0],
            "session": ["rth"],
            "session_id": ["S00001"],
            "when": ["2003-01-02"],
        }
    )
    with pytest.raises(ValueError) as dated_raise:
        store_frame(dated, kind="designer", symbol="ZZQQ")
    assert "2003-01-02" not in str(dated_raise.value)
    assert str(dated_raise.value) == "designer view contains a calendar date"
    clean = pd.DataFrame(
        {
            "close": [1.0, 1.01],
            "session": ["rth", "rth"],
            "session_id": ["S00001", "S00001"],
            "time": ["09:30", "09:31"],
        }
    )
    path = store_frame(clean, kind="designer", symbol="ZZQQ")
    manifest = (path / "manifest.json").read_text(encoding="utf-8")
    assert "ZZQQ" not in manifest
    assert "price_factor" not in manifest
    assert "date_map" not in manifest
    listed = list_entries()
    assert listed[0]["kind"] == "designer"
    assert listed[0]["symbol"] == "RANDOM"
    assert read_manifest(path)["kind"] == "designer"


def test_budget_drops_the_oldest_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("STRATEGY_BUILDER_PREPARED_MAX_BYTES", "1")
    first = pd.DataFrame({"close": np.arange(100, dtype=float), "volume": np.arange(100, dtype=float)})
    second = pd.DataFrame({"close": np.arange(100, 200, dtype=float), "volume": np.arange(100, dtype=float)})
    older = store_frame(first, kind="window")
    newer = store_frame(second, kind="window")
    keys = {row["key"] for row in list_entries()}
    assert newer.name in keys
    assert older.name not in keys


def test_prepared_cli_lists_deletes_and_rebuilds(tmp_path):
    source = tmp_path / "src"
    write_series(source, "SYN", sessions=3)
    loaded = load_named(source, "SYN")
    key = Path(loaded.prepared).name
    env = os.environ.copy()
    listed = subprocess.run(
        [sys.executable, "-m", "backtester", "prepared", "list"],
        check=False,
        capture_output=True,
        text=True,
        cwd=ROOT,
        env=env,
    )
    assert listed.returncode == 0
    assert key in listed.stdout
    assert "SYN" in listed.stdout
    rebuilt = subprocess.run(
        [sys.executable, "-m", "backtester", "prepared", "rebuild", key],
        check=False,
        capture_output=True,
        text=True,
        cwd=ROOT,
        env=env,
    )
    assert rebuilt.returncode == 0, rebuilt.stderr
    assert "SYN" in rebuilt.stdout
    removed = subprocess.run(
        [sys.executable, "-m", "backtester", "prepared", "delete", key],
        check=False,
        capture_output=True,
        text=True,
        cwd=ROOT,
        env=env,
    )
    assert removed.returncode == 0, removed.stderr
    assert list_entries() == []
    with pytest.raises(ValueError):
        delete_entry("../" + key)


def test_rebuild_entry_reloads_the_named_source(tmp_path):
    source = tmp_path / "src"
    write_series(source, "SYN", sessions=3)
    loaded = load_named(source, "SYN")
    key = Path(loaded.prepared).name
    again = rebuild_entry(key)
    assert _digest(again.frame) == GOLDEN["frame_sha256"]


def test_strategy_path_is_absolute_before_a_worker_changes_directory(tmp_path, monkeypatch):
    from backtester.runner import _bind_strategy

    monkeypatch.chdir(tmp_path)
    script = tmp_path / "strategy.py"
    script.write_text("class Strategy:\n    def on_bar(self, bar, state):\n        return None\n", encoding="utf-8")
    _cls, path = _bind_strategy("strategy.py", in_process=False)
    assert path == str(script.resolve())
    monkeypatch.chdir(ROOT)
    assert Path(path).is_file()


def test_runtime_overlap_allows_a_venv_inside_a_denied_tree(tmp_path):
    denied = tmp_path / "repo"
    venv = denied / ".venv"
    assert runtime_overlap_error(venv, denied, venv=venv) is None
    assert runtime_overlap_error(venv / "bin", denied, venv=venv) is None
    refused = runtime_overlap_error(denied, denied, venv=denied)
    assert "python -m venv .venv" in refused
    assert "interpreter outside the clone" in refused
    inside = runtime_overlap_error(denied / "src", denied, venv=None)
    assert inside is not None
    assert "python -m venv .venv" not in inside
    assert runtime_overlap_error(Path("/usr"), denied, venv=venv) is None


def test_jail_deny_list_includes_the_prepared_cache(tmp_path, monkeypatch):
    cache = tmp_path / "prepared"
    monkeypatch.setenv("STRATEGY_BUILDER_PREPARED_DIR", str(cache))
    roots = _deny_roots([])
    assert cache.resolve() in roots


def test_length_prefixed_decisions_keep_bool_int_and_nan(tmp_path):
    from backtester.referee import _Pipe

    work = tmp_path / "jail"
    work.mkdir()
    package = ROOT / "src" / "backtester"
    (work / "sandbox_child.py").write_bytes((package / "sandbox_child.py").read_bytes())
    (work / "bar_ipc.py").write_bytes((package / "bar_ipc.py").read_bytes())
    (work / "strategy.py").write_text(
        "import os\n"
        "class Strategy:\n"
        "    def on_bar(self, bar, state):\n"
        "        probe = bar.get('probe')\n"
        "        if probe == 'none':\n"
        "            return None\n"
        "        if probe == 'true':\n"
        "            return True\n"
        "        if probe == 'false':\n"
        "            return False\n"
        "        if probe == 'one':\n"
        "            return 1\n"
        "        if probe == 'one_float':\n"
        "            return 1.0\n"
        "        if probe == 'nan':\n"
        "            return float('nan')\n"
        "        if probe == 'extra':\n"
        "            import json, struct\n"
        "            body = json.dumps({'op': 'noise', 'target': 0}, separators=(',', ':')).encode()\n"
        "            os.write(1, struct.pack('<I', len(body)) + body)\n"
        "            return 2\n"
        "        return 0\n",
        encoding="utf-8",
    )
    proc = subprocess.Popen(
        [sys.executable, "-I", "-S", "-B", "sandbox_child.py"],
        cwd=work,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    class _Box:
        def __init__(self, process):
            self.process = process
            self.stdin_fd = process.stdin.fileno()
            self.stdout_fd = process.stdout.fileno()
            self.stderr_fd = process.stderr.fileno()

        def terminate(self):
            self.process.terminate()

        def close(self):
            self.process.kill()

    box = _Box(proc)
    pipe = _Pipe(box)
    try:
        if os.name == "nt":
            # An unconfined sandbox_child must not run the strategy. The
            # frames themselves are covered above; a real jail is an AppContainer.
            from backtester.referee import Disqualified

            with pytest.raises(Disqualified, match="not an AppContainer"):
                pipe.hello(10)
            return
        ready = pipe.hello(10)
        assert ready["op"] == "ready"
        assert pipe.decision({"op": "bar", "bar": {"probe": "none"}, "state": {}}, 10) is None
        assert pipe.decision({"op": "bar", "bar": {"probe": "true"}, "state": {}}, 10) is True
        assert pipe.decision({"op": "bar", "bar": {"probe": "false"}, "state": {}}, 10) is False
        one = pipe.decision({"op": "bar", "bar": {"probe": "one"}, "state": {}}, 10)
        assert one == 1 and type(one) is int
        one_float = pipe.decision({"op": "bar", "bar": {"probe": "one_float"}, "state": {}}, 10)
        assert one_float == 1.0 and type(one_float) is float
        nan = pipe.decision({"op": "bar", "bar": {"probe": "nan"}, "state": {}}, 10)
        assert isinstance(nan, float) and nan != nan
        with pytest.raises(Exception) as raised:
            pipe.decision({"op": "bar", "bar": {"probe": "extra"}, "state": {}}, 10)
        assert "one-line decision" in str(raised.value) or "no request was outstanding" in str(raised.value)
    finally:
        pipe.close()
        proc.kill()
        proc.wait(timeout=5)


def test_compact_bar_frames_roundtrip_the_engine_shape():
    from backtester import bar_ipc

    arena = {
        "op": "bar",
        "bar": {
            "day": 3,
            "weekday": 4,
            "minute": 570,
            "time": "09:30",
            "session": "rth",
            "open": 100.01,
            "high": 100.5,
            "low": 99.5,
            "close": 100.25,
            "volume": 125.0,
            "wap": 100.1,
            "count": 7,
            "filled": 0,
            "short_session": 0,
        },
        "state": {
            "equity": 1001631.2114307251,
            "cash": 900000.0,
            "shares": 100,
            "position": 1.0,
            "target": 1.0,
            "pending_order": False,
        },
    }
    packed = bar_ipc.pack_bar(arena)
    assert packed is not None and packed[4] == 1
    assert bar_ipc.loads(packed[4:]) == arena
    desk = {
        "op": "bar",
        "bar": {
            "session": "2003-01-02",
            "time": "09:30",
            "phase": "rth",
            "open": 100.01,
            "high": 100.5,
            "low": 99.5,
            "close": 100.25,
            "volume": 125.0,
            "wap": 100.1,
            "count": 1.5,
            "filled": 0,
            "new_session": True,
        },
        "account": {
            "cash": 1000000.0,
            "position_shares": 0,
            "equity": 1000000.0,
            "exposure": None,
            "gross_leverage": None,
            "hwm": 1000000.0,
            "drawdown": 0.0,
            "live": True,
            "liquidated": False,
        },
    }
    packed = bar_ipc.pack_bar(desk)
    assert packed is not None and packed[4] == 1
    assert bar_ipc.loads(packed[4:]) == desk
    for target in (None, False, True, 1, 1.0, float("nan")):
        frame = bar_ipc.pack_decision(target)
        assert frame is not None and frame[4] == 2
        got = bar_ipc.loads(frame[4:])["target"]
        if isinstance(target, float) and target != target:
            assert isinstance(got, float) and got != got
        else:
            assert got == target and type(got) is type(target)
    assert bar_ipc.pack_bar({"op": "bar", "bar": {"probe": "none"}, "state": {}}) is None


def test_workers_share_one_mapped_frame(tmp_path):
    write_series(tmp_path / "src", "SYN", sessions=3)
    loaded = load_named(tmp_path / "src", "SYN")
    path = store_frame(loaded.frame, kind="window")
    left = load_frame(path)
    right = load_frame(path)
    left_map = _memmap(left["close"].to_numpy())
    right_map = _memmap(right["close"].to_numpy())
    assert left_map is not None and right_map is not None
    assert Path(left_map.filename) == Path(right_map.filename)
    assert left_map.ctypes.data != right_map.ctypes.data
    assert left["close"].iloc[0] == right["close"].iloc[0]
