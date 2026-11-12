"""Parallel scoring: same numbers, one bad strategy, and a pack that stays anonymous."""

from __future__ import annotations

import gzip
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest
from pathlib import PureWindowsPath

from windows_open import OWNER_PC

from backtester.parallel import (
    CancelToken,
    FrameMarket,
    PackMarket,
    StrategyRef,
    _rank_key,
    discover_strategies,
    run_parallel,
)
from backtester.report import assert_designer_clean

ROOT = Path(__file__).resolve().parents[1]
ET = timezone(timedelta(hours=-4))


def _strategy(directory: Path, name: str, source: str) -> StrategyRef:
    folder = directory / name
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "strategy.py"
    path.write_text(source, encoding="utf-8")
    return StrategyRef(strategy_id=name, path=str(path))


def _frame():
    rows = []
    for day, price in ((0, 100.0), (1, 110.0)):
        for minute in (570, 571):
            rows.append(
                {
                    "date": f"2026-03-0{2 + day}",
                    "day": day,
                    "weekday": day,
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


FLAT = "class Strategy:\n    def on_bar(self, bar, state):\n        return None\n"
LONG = "class Strategy:\n    def on_bar(self, bar, state):\n        return 1.0\n"
TWO = (
    "class Strategy:\n"
    "    def __init__(self):\n"
    "        self.sent = False\n"
    "    def on_bar(self, bar, state):\n"
    "        if not self.sent:\n"
    "            self.sent = True\n"
    "            return 2.0\n"
    "        return None\n"
)


def test_rank_is_excess_return_then_id_and_failures_go_last():
    # total_return order is the reverse of excess order. Ranking by the raw
    # total would put high_total first and a/s last among the finite rows.
    rows = [
        {
            "status": "ok",
            "excess_total_return": 0.01,
            "total_return": 0.90,
            "strategy_id": "high_total",
            "source_id": "s",
        },
        {
            "status": "ok",
            "excess_total_return": 0.5,
            "total_return": 0.02,
            "strategy_id": "a",
            "source_id": "s",
        },
        {
            "status": "ok",
            "excess_total_return": 0.5,
            "total_return": 0.99,
            "strategy_id": "a",
            "source_id": "t",
        },
        {
            "status": "ok",
            "excess_total_return": 0.1,
            "total_return": 0.80,
            "strategy_id": "b",
            "source_id": "s",
        },
        {
            "status": "error",
            "excess_total_return": 9.0,
            "total_return": 9.0,
            "strategy_id": "c",
            "source_id": "s",
        },
        {
            "status": "ok",
            "excess_total_return": float("nan"),
            "total_return": 5.0,
            "strategy_id": "d",
            "source_id": "s",
        },
    ]
    rows.sort(key=_rank_key)
    ranked = [(row["strategy_id"], row["source_id"]) for row in rows]
    assert ranked == [
        ("a", "s"),
        ("a", "t"),
        ("b", "s"),
        ("high_total", "s"),
        ("d", "s"),
        ("c", "s"),
    ]
    by_total = sorted(
        rows,
        key=lambda row: (
            0 if row["status"] == "ok" else 1,
            -(row["total_return"] if isinstance(row["total_return"], float) else 0.0),
            row["strategy_id"],
        ),
    )
    assert ranked != [(row["strategy_id"], row["source_id"]) for row in by_total]


def test_discover_reads_immediate_children_and_disambiguates(tmp_path):
    found = discover_strategies(ROOT / "examples" / "strategies")
    assert [item.strategy_id for item in found] == [f"D{number:02d}" for number in range(1, 21)]
    root = tmp_path / "flat"
    root.mkdir()
    (root / "strategy.py").write_text(FLAT, encoding="utf-8")
    nested = root / "flat"
    nested.mkdir()
    (nested / "strategy.py").write_text(FLAT, encoding="utf-8")
    ids = [item.strategy_id for item in discover_strategies(root)]
    assert ids == ["flat", "flat-2"]
    with pytest.raises(ValueError, match="unsafe"):
        run_parallel(
            [StrategyRef("a/b", "missing.py")],
            [FrameMarket("book", "TEST", frame=_frame())],
            tmp_path / "out",
            profile="arena",
            workers=1,
        )


def test_bad_batch_settings_do_not_spawn(tmp_path, monkeypatch):
    def boom(*_args, **_kwargs):
        raise AssertionError("spawned")

    monkeypatch.setattr("backtester.runner._isolated_books", boom)
    market = FrameMarket("book", "TEST", frame=_frame())
    with pytest.raises(ValueError, match="workers"):
        run_parallel([StrategyRef("flat", "missing.py")], [market], tmp_path / "out", profile="arena", workers=0)
    with pytest.raises(ValueError, match="does not credit dividends"):
        run_parallel(
            [StrategyRef("flat", "missing.py")],
            [market],
            tmp_path / "out",
            profile="arena",
            workers=1,
            dividends={"2024-01-02": 1.0},
        )


def test_workers_do_not_change_the_leaderboard_or_the_metrics(tmp_path):
    strategies = [
        _strategy(tmp_path / "s", "flat", FLAT),
        _strategy(tmp_path / "s", "long", LONG),
    ]
    market = FrameMarket("book", "TEST", frame=_frame())

    def once(workers, name):
        return run_parallel(
            strategies,
            [market],
            tmp_path / name,
            profile="arena",
            workers=workers,
            timeout_s=60,
        )

    serial = once(1, "serial")
    parallel = once(2, "parallel")
    assert serial.failures == 0 and parallel.failures == 0
    assert (tmp_path / "serial" / "leaderboard.csv").read_bytes() == (
        tmp_path / "parallel" / "leaderboard.csv"
    ).read_bytes()
    for item in serial.results:
        left = Path(item.out_dir) / "metrics.json"
        twin = next(other for other in parallel.results if other.strategy_id == item.strategy_id)
        right = Path(twin.out_dir) / "metrics.json"
        assert left.read_bytes() == right.read_bytes()
        assert (Path(item.out_dir) / "equity_curve.csv").read_bytes() == (
            Path(twin.out_dir) / "equity_curve.csv"
        ).read_bytes()

    from backtester.runner import run_books
    from backtester.strategy_loader import load_strategy_class

    books = run_books(load_strategy_class(strategies[0].path), _frame(), "arena")
    written = json.loads((Path(serial.results[0].out_dir) / "metrics.json").read_text(encoding="utf-8"))
    # results are rank order, not input order. Compare the flat book by id.
    flat = next(item for item in serial.results if item.strategy_id == "flat")
    written = json.loads((Path(flat.out_dir) / "metrics.json").read_text(encoding="utf-8"))
    live = books["strategy"][1]
    for key in ("final_equity", "fees_total", "n_fills", "total_return"):
        assert written[key] == live[key]


def test_desk_financing_matches_the_in_process_book(tmp_path):
    from backtester.runner import execute
    from backtester.strategy_loader import load_strategy_class

    strategy = _strategy(tmp_path / "s", "two", TWO)
    result = run_parallel(
        [strategy],
        [FrameMarket("book", "TEST", frame=_frame())],
        tmp_path / "out",
        profile="desk",
        workers=1,
        timeout_s=60,
    )
    assert result.failures == 0
    _engine_result, metrics = execute(load_strategy_class(strategy.path)(), _frame(), "desk")
    written = json.loads((Path(result.results[0].out_dir) / "metrics.json").read_text(encoding="utf-8"))
    assert written["financing_cost"] == metrics["financing_cost"] == _engine_result["financing"]
    assert written["financing_cost"] > 0.0
    assert written["benchmarks"]["long_2x"]["total_return"] is not None
    assert "buy_and_hold_1x" in written["benchmarks"]


def test_a_crash_a_hang_signal_and_bad_syntax_stay_inside_one_row(tmp_path):
    good = _strategy(tmp_path / "s", "good", FLAT)
    _strategy(tmp_path / "s", "boom", "class Strategy:\n    def on_bar(self, bar, state):\n        raise RuntimeError('boom-isolated')\n")
    _strategy(tmp_path / "s", "exit7", "import os\nclass Strategy:\n    def on_bar(self, bar, state):\n        os._exit(7)\n")
    _strategy(tmp_path / "s", "syntax", "def this is not python\n")
    _strategy(
        tmp_path / "s",
        "sysexit",
        "class Strategy:\n    def on_bar(self, bar, state):\n        raise SystemExit(3)\n",
    )
    strategies = discover_strategies(tmp_path / "s")
    result = run_parallel(
        strategies,
        [FrameMarket("book", "TEST", frame=_frame())],
        tmp_path / "out",
        profile="arena",
        workers=2,
        timeout_s=30,
        with_benchmarks=False,
    )
    by_id = {item.strategy_id: item for item in result.results}
    assert by_id["good"].status == "ok"
    assert by_id["boom"].status == "error"
    assert "boom-isolated" in by_id["boom"].error
    assert "timed out" not in by_id["boom"].error
    assert by_id["exit7"].status == "crash"
    assert "exited 7" in by_id["exit7"].error
    assert "timed out" not in by_id["exit7"].error
    assert by_id["syntax"].status == "error"
    assert "SyntaxError" in by_id["syntax"].error
    assert by_id["sysexit"].status == "error"
    assert "SystemExit" in by_id["sysexit"].error
    assert "timed out" not in by_id["sysexit"].error
    from backtester.runner import run_books
    from backtester.strategy_loader import load_strategy_class

    live = run_books(load_strategy_class(good.path), _frame(), "arena", with_benchmarks=False)["strategy"][1]
    written = json.loads((Path(by_id["good"].out_dir) / "metrics.json").read_text(encoding="utf-8"))
    for key in ("final_equity", "fees_total", "n_fills", "total_return"):
        assert written[key] == live[key]


@OWNER_PC
def test_a_hang_times_out_without_stopping_the_other_strategy(tmp_path):
    _strategy(tmp_path / "s", "fast", FLAT)
    _strategy(
        tmp_path / "s",
        "hang",
        "import time\nclass Strategy:\n    def on_bar(self, bar, state):\n        time.sleep(60)\n        return None\n",
    )
    started = time.monotonic()
    result = run_parallel(
        discover_strategies(tmp_path / "s"),
        [FrameMarket("book", "TEST", frame=_frame())],
        tmp_path / "out",
        profile="arena",
        workers=2,
        timeout_s=6,
        with_benchmarks=False,
    )
    elapsed = time.monotonic() - started
    by_id = {item.strategy_id: item for item in result.results}
    assert by_id["fast"].status == "ok", [(item.strategy_id, item.status, item.error) for item in result.results]
    assert by_id["hang"].status == "timeout"
    assert "timed out" in by_id["hang"].error
    assert elapsed < 45


def test_a_memory_hog_is_one_row(tmp_path):
    from backtester.limits import current_rss

    cap = max(current_rss(), 600 * 1024 * 1024) + 500 * 1024 * 1024
    _strategy(tmp_path / "s", "good", FLAT)
    _strategy(
        tmp_path / "s",
        "bomb",
        "import time\n"
        "class Strategy:\n"
        "    def on_bar(self, bar, state):\n"
        "        blobs = []\n"
        "        for _ in range(250):\n"
        "            chunk = bytearray(8 * 1024 * 1024)\n"
        "            for index in range(0, len(chunk), 4096):\n"
        "                chunk[index] = 1\n"
        "            blobs.append(chunk)\n"
        "            time.sleep(0.05)\n"
        "        return None\n",
    )
    result = run_parallel(
        discover_strategies(tmp_path / "s"),
        [FrameMarket("book", "TEST", frame=_frame())],
        tmp_path / "out",
        profile="arena",
        workers=2,
        timeout_s=45,
        memory_bytes=cap,
        with_benchmarks=False,
    )
    by_id = {item.strategy_id: item for item in result.results}
    detail = [(item.strategy_id, item.status, item.error) for item in result.results]
    assert by_id["bomb"].status == "memory", detail
    assert "exceeded memory limit" in by_id["bomb"].error
    assert by_id["good"].status == "ok", detail


def test_cancel_stops_the_batch(tmp_path):
    source = "import time\nclass Strategy:\n    def on_bar(self, bar, state):\n        time.sleep(60)\n        return None\n"
    _strategy(tmp_path / "s", "one", source)
    _strategy(tmp_path / "s", "two", source)
    token = CancelToken()

    def on_progress(event):
        if event.status == "started":
            token.cancel()

    started = time.monotonic()
    result = run_parallel(
        discover_strategies(tmp_path / "s"),
        [FrameMarket("book", "TEST", frame=_frame())],
        tmp_path / "out",
        profile="arena",
        workers=2,
        timeout_s=60,
        with_benchmarks=False,
        on_progress=on_progress,
        cancel=token,
    )
    assert time.monotonic() - started < 45
    assert result.cancelled is True
    assert result.failures == len(result.results)
    assert all(item.status != "ok" for item in result.results)
    assert any(item.status == "cancelled" for item in result.results)


def _write_rows(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = [
        "timestamp_et", "epoch_utc", "open", "high", "low", "close", "volume", "wap", "count", "filled", "session",
    ]
    lines = [",".join(header)]
    for row in rows:
        lines.append(",".join(str(row[name]) for name in header))
    with gzip.open(path, "wt", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines) + "\n")


def _row(day: datetime, minute: int, price: float) -> dict:
    stamp = day.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(minutes=minute)
    stamp = stamp.replace(tzinfo=ET)
    return {
        "timestamp_et": stamp.isoformat(),
        "epoch_utc": int(stamp.timestamp()),
        "open": price,
        "high": price,
        "low": price,
        "close": price,
        "volume": 100,
        "wap": price,
        "count": 3,
        "filled": 0,
        "session": "rth",
    }


def _named_series(directory: Path, symbol: str) -> Path:
    rows = []
    day = datetime(2024, 1, 2)
    made = 0
    while made < 2:
        if day.weekday() < 5:
            rows.append(_row(day, 9 * 60 + 30, 50.0 + made))
            rows.append(_row(day, 9 * 60 + 31, 50.0 + made))
            made += 1
        day += timedelta(days=1)
    path = directory / f"{symbol}_1min_filled_2024-01.csv.gz"
    _write_rows(path, rows)
    return directory


def test_anonymized_pack_keeps_the_sidecar_in_the_parent(tmp_path, monkeypatch):
    from backtester.data import load_named
    from backtester.pack import build_pack
    from backtester.registry import entry_from_inspection, save_registry, upsert_series
    import backtester.runner as runner_mod

    symbol = "NIK"
    src = _named_series(tmp_path / "src", symbol)
    loaded = load_named(src, symbol)
    registry = tmp_path / "registry.json"
    entry = entry_from_inspection(loaded.inspection, source_root=str(src), profile="desk")
    save_registry(registry, upsert_series({"schema_version": 1, "series": []}, entry))
    pack = tmp_path / "pack"
    sidecar = tmp_path / "operator" / "secret.json"
    note = "OPERATOR-CANARY-NOTE"
    built = build_pack(
        src,
        symbol,
        pack,
        sidecar,
        anonymize=True,
        profile="desk",
        registry_path=registry,
        price_factor=1.5,
        volume_factor=0.5,
        operator_note=note,
    )
    assert built.share_scale == 1.5
    seen_dir = tmp_path / "seen"
    seen_dir.mkdir()
    probe = (
        "import os, sys\n"
        "class Strategy:\n"
        "    def __init__(self):\n"
        "        self.report = None\n"
        "    def on_bar(self, bar, state):\n"
        "        if self.report is None:\n"
        "            open('marker.txt', 'w', encoding='utf-8').write('one')\n"
        "            self.report = {\n"
        "                'argv': list(sys.argv),\n"
        "                'cwd': os.getcwd(),\n"
        "                'canary': os.environ.get('BT_CANARY'),\n"
        "                'listing': sorted(os.listdir(os.getcwd())),\n"
        "            }\n"
        "        return None\n"
    )
    reader = (
        "import os, sys, time\n"
        "class Strategy:\n"
        "    def __init__(self):\n"
        "        self.report = None\n"
        "    def on_bar(self, bar, state):\n"
        "        time.sleep(0.3)\n"
        "        if self.report is None:\n"
        "            self.report = {\n"
        "                'argv': list(sys.argv),\n"
        "                'cwd': os.getcwd(),\n"
        "                'canary': os.environ.get('BT_CANARY'),\n"
        "                'listing': sorted(os.listdir(os.getcwd())),\n"
        "            }\n"
        "        return None\n"
    )
    strategies = [
        _strategy(tmp_path / "s", "probe", probe),
        _strategy(tmp_path / "s", "reader", reader),
    ]
    recorded = []
    real = runner_mod._isolated_books

    def spy(strategy_cls, frame, profile, **kwargs):
        books = real(strategy_cls, frame, profile, **kwargs)
        recorded.append(
            {
                "kwargs": dict(kwargs),
                "csv": frame.to_csv(index=False),
                "observer": books["strategy"][0].get("observer"),
            }
        )
        return books

    monkeypatch.setattr(runner_mod, "_isolated_books", spy)
    os.environ["BT_CANARY"] = "present"
    try:
        result = run_parallel(
            strategies,
            [PackMarket(str(pack), str(sidecar))],
            tmp_path / "out",
            profile="desk",
            workers=2,
            timeout_s=60,
            dividends={"2024-01-02": 2.0},
        )
    finally:
        os.environ.pop("BT_CANARY", None)
    assert result.failures == 0, [(item.strategy_id, item.status, item.error) for item in result.results]
    assert len(recorded) == 2
    for item in recorded:
        kwargs = item["kwargs"]
        assert kwargs["share_factor"] == 1.5
        assert kwargs["anonymized"] is True
        assert kwargs["dividends"] == {"S00001": 2.0 * 1.5}
        for banned in ("date_map", "symbol", "volume_factor", "operator_note", "sidecar"):
            assert banned not in kwargs
        assert symbol not in item["csv"]
        assert "2024-01-02" not in item["csv"]
    reports = [item["observer"].report for item in recorded]
    one = next(item for item in reports if "marker.txt" in item["listing"])
    two = next(item for item in reports if "marker.txt" not in item["listing"])
    assert one["argv"] == ["strategy"]
    assert two["argv"] == ["strategy"]
    assert one["canary"] is None and two["canary"] is None
    assert one["cwd"] != two["cwd"]
    assert "marker.txt" not in two["listing"]
    assert str(src) not in one["cwd"]
    assert not (seen_dir / "one.json").exists()
    out = tmp_path / "out"
    operator_files = [path for path in (out / "jobs").rglob("metrics.json") if "designer" not in path.parts]
    operator = json.loads(operator_files[0].read_text(encoding="utf-8"))
    assert operator["symbol"] == symbol
    assert operator["cost_basis"]["share_scale"] == 1.5
    run_text = (out / "run.json").read_text(encoding="utf-8")
    assert note not in run_text
    assert str(sidecar) not in run_text
    assert_designer_clean(out / "designer", symbol=symbol)
    for path in out.rglob("*"):
        if "designer" not in path.parts or not path.is_file() or path.suffix.lower() == ".png":
            continue
        text = path.read_text(encoding="utf-8")
        assert symbol not in text
        assert "2024-01-02" not in text
        assert note not in text
        assert "share_scale" not in text


def test_windows_path_with_spaces_runs_from_the_command_line(tmp_path):
    shown = "C:\\Users\\Jeu\\Desktop\\Strategy Builder\\strategies\\D02\\strategy.py"
    assert PureWindowsPath(shown) == PureWindowsPath("C:\\Users\\Jeu\\Desktop\\Strategy Builder\\strategies\\D02\\strategy.py")
    assert "Strategy Builder" in shown
    root = tmp_path / "Strategy Builder"
    _strategy(root / "strategies", "flat", FLAT)
    data = _named_series(root / "data", "NIK")
    out = root / "out"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "backtester",
            "parallel",
            "--strategies",
            str(root / "strategies"),
            "--workers",
            "1",
            "--profile",
            "arena",
            "--src",
            str(data),
            "--ticker",
            "NIK",
            "--out",
            str(out),
            "--timeout",
            "60",
        ],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert (out / "leaderboard.csv").is_file()
    assert (out / "jobs" / "flat" / "NIK" / "equity.png").read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    match_line = [line for line in proc.stdout.splitlines() if line.startswith("run r")]
    assert match_line
    run_id = match_line[-1].split()[1]
    reported = subprocess.run(
        [sys.executable, "-m", "backtester", "report", "--run", run_id],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    assert reported.returncode == 0, reported.stderr
    assert "total_return=" in reported.stdout
    assert "symbol=NIK" in reported.stdout


def _alive(pid: int) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        listed = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            check=False,
        )
        # tasklist CSV quotes the pid. A substring match would treat 12 as 123.
        return f"\"{pid}\"" in listed.stdout
    stat = Path(f"/proc/{pid}/stat")
    if not stat.exists():
        return False
    try:
        text = stat.read_text(encoding="utf-8")
    except OSError:
        return False
    state = text.rsplit(")", 1)[-1].split()[0]
    return state != "Z"


def _reap(pid: int | None) -> None:
    if not pid:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
            check=False,
        )
        return
    try:
        os.killpg(int(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        os.kill(int(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _pids(path: Path) -> tuple[int | None, int | None]:
    if not path.is_file():
        return None, None
    payload = json.loads(path.read_text(encoding="utf-8"))
    return int(payload["worker"]), int(payload["child"])


def _sleeper_strategy(_marker: Path, *, allocate: int = 0) -> str:
    alloc = ""
    if allocate:
        alloc = (
            "        blobs = []\n"
            f"        remaining = {int(allocate)}\n"
            "        while remaining > 0:\n"
            "            chunk = bytearray(min(remaining, 8 * 1024 * 1024))\n"
            "            for index in range(0, len(chunk), 4096):\n"
            "                chunk[index] = 1\n"
            "            blobs.append(chunk)\n"
            "            remaining -= len(chunk)\n"
            "        time.sleep(3)\n"
        )
    return (
        "import time\n"
        "class Strategy:\n"
        "    def on_bar(self, bar, state):\n"
        f"{alloc}"
        "        time.sleep(300)\n"
        "        return None\n"
    )


def _wait_dead(pid: int | None) -> bool:
    if pid is None:
        return True
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return not _alive(pid)


def _assert_grandchild_gone(marker: Path, label: str) -> None:
    if not marker.is_file():
        return
    worker, child = _pids(marker)
    try:
        assert child is not None, f"{label}: strategy never started its child"
        assert _wait_dead(child), f"{label}: grandchild {child} is still running"
        if worker is not None:
            assert _wait_dead(worker), f"{label}: worker {worker} is still running"
    finally:
        _reap(child)
        _reap(worker)


def test_grandchild_is_reaped_on_timeout_cancel_and_memory(tmp_path):
    """A long-lived child of the strategy dies on timeout, cancel, and the RSS kill.

    Each scenario is bounded. If stopping the worker is a no-op, the assertion
    fails and the cleanup signal still reaps the process so pytest can exit.
    """
    market = FrameMarket("book", "TEST", frame=_frame())

    timeout_marker = tmp_path / "timeout.json"
    _strategy(tmp_path / "timeout", "sleep", _sleeper_strategy(timeout_marker))
    started = time.monotonic()
    try:
        timed = run_parallel(
            discover_strategies(tmp_path / "timeout"),
            [market],
            tmp_path / "timeout-out",
            profile="arena",
            workers=1,
            timeout_s=3,
            with_benchmarks=False,
        )
        assert time.monotonic() - started < 20
        assert timed.results[0].status == "timeout"
    finally:
        _assert_grandchild_gone(timeout_marker, "timeout")

    cancel_marker = tmp_path / "cancel.json"
    _strategy(tmp_path / "cancel", "sleep", _sleeper_strategy(cancel_marker))
    token = CancelToken()

    def _after_gate():
        time.sleep(1.5)
        token.cancel()

    watcher = threading.Thread(target=_after_gate, daemon=True)
    watcher.start()
    started = time.monotonic()
    cancelled = None
    try:
        cancelled = run_parallel(
            discover_strategies(tmp_path / "cancel"),
            [market],
            tmp_path / "cancel-out",
            profile="arena",
            workers=1,
            timeout_s=25,
            with_benchmarks=False,
            cancel=token,
        )
        assert time.monotonic() - started < 12, [(item.status, item.error) for item in cancelled.results]
        assert cancelled.results[0].status == "cancelled"
        assert "cancelled" in cancelled.results[0].error
    finally:
        watcher.join(timeout=2)
        _assert_grandchild_gone(cancel_marker, "cancel")

    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import backtester.runner\nfrom backtester.limits import current_rss\nprint(current_rss())\n",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert probe.returncode == 0, probe.stderr
    baseline = int(probe.stdout.strip())
    cap = baseline + 128 * 1024 * 1024
    memory_marker = tmp_path / "memory.json"
    _strategy(tmp_path / "memory", "hog", _sleeper_strategy(memory_marker, allocate=192 * 1024 * 1024))
    _strategy(tmp_path / "memory", "good", FLAT)
    try:
        remembered = run_parallel(
            discover_strategies(tmp_path / "memory"),
            [market],
            tmp_path / "memory-out",
            profile="arena",
            workers=2,
            timeout_s=40,
            memory_bytes=cap,
            with_benchmarks=False,
        )
        by_id = {item.strategy_id: item for item in remembered.results}
        detail = [(item.strategy_id, item.status, item.error) for item in remembered.results]
        assert by_id["hog"].status == "memory", detail
        assert "exceeded memory limit" in (by_id["hog"].error or "")
        assert "MemoryError" not in (by_id["hog"].error or "")
        assert by_id["good"].status == "ok", detail
    finally:
        _assert_grandchild_gone(memory_marker, "memory")


def test_strategy_cannot_read_operator_metrics_through_original_dir_or_sys_path(tmp_path):
    import multiprocessing.process as mp_process

    operator = tmp_path / "operator"
    operator.mkdir()
    secret = {"symbol": "ZZZX", "share_scale": 1.337}
    (operator / "metrics.json").write_text(json.dumps(secret), encoding="utf-8")
    source = (
        "import os, sys\n"
        "from pathlib import Path\n"
        "import multiprocessing.process as mp\n"
        "class Strategy:\n"
        "    def __init__(self):\n"
        "        self.report = None\n"
        "    def on_bar(self, bar, state):\n"
        "        hits = []\n"
        "        roots = []\n"
        "        if mp.ORIGINAL_DIR:\n"
        "            roots.append(mp.ORIGINAL_DIR)\n"
        "        roots.extend(sys.path)\n"
        "        for root in roots:\n"
        "            if not root:\n"
        "                root = os.getcwd()\n"
        "            candidate = Path(root) / 'metrics.json'\n"
        "            try:\n"
        "                if candidate.is_file():\n"
        "                    hits.append(candidate.read_text(encoding='utf-8'))\n"
        "            except OSError:\n"
        "                pass\n"
        "        self.report = {'hits': hits, 'orig': mp.ORIGINAL_DIR, 'path': list(sys.path)}\n"
        "        return None\n"
    )
    strategy = _strategy(tmp_path / "s", "reader", source)
    old_original = mp_process.ORIGINAL_DIR
    old_path = list(sys.path)
    mp_process.ORIGINAL_DIR = str(operator.resolve())
    sys.path.insert(0, str(operator.resolve()))
    captured = []
    import backtester.runner as runner_mod

    real = runner_mod._isolated_books

    def spy(*args, **kwargs):
        books = real(*args, **kwargs)
        captured.append(books["strategy"][0].get("observer"))
        return books

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(runner_mod, "_isolated_books", spy)
    try:
        result = run_parallel(
            [strategy],
            [FrameMarket("book", "TEST", frame=_frame())],
            tmp_path / "out",
            profile="arena",
            workers=1,
            timeout_s=30,
            with_benchmarks=False,
        )
    finally:
        monkeypatch.undo()
        mp_process.ORIGINAL_DIR = old_original
        sys.path[:] = old_path
    assert result.failures == 0, [(item.status, item.error) for item in result.results]
    seen = captured[0].report
    assert seen["hits"] == []
    dumped = json.dumps(seen)
    assert "ZZZX" not in dumped
    assert "1.337" not in dumped
    assert seen["orig"] is None or Path(seen["orig"]).resolve() != operator.resolve()


def test_a_large_print_is_not_a_timeout(tmp_path, monkeypatch):
    source = (
        "import time\n"
        "from pathlib import Path\n"
        "class Strategy:\n"
        "    def on_bar(self, bar, state):\n"
        "        print('X' * (8 * 1024 * 1024), flush=True)\n"
        "        path = Path('stdio.log')\n"
        "        deadline = time.time() + 5\n"
        "        size = 0\n"
        "        while time.time() < deadline:\n"
        "            size = path.stat().st_size if path.is_file() else 0\n"
        "            if size >= 64 * 1024:\n"
        "                break\n"
        "            time.sleep(0.05)\n"
        "        self.size = size\n"
        "        return None\n"
    )
    from backtester import runner as runner_mod

    captured = {}
    real = runner_mod._isolated_books

    def spy(*args, **kwargs):
        books = real(*args, **kwargs)
        captured["size"] = books["strategy"][0]["observer"].size
        return books

    monkeypatch.setattr(runner_mod, "_isolated_books", spy)
    started = time.monotonic()
    result = run_parallel(
        [_strategy(tmp_path / "s", "loud", source)],
        [FrameMarket("book", "TEST", frame=_frame())],
        tmp_path / "out",
        profile="arena",
        workers=1,
        timeout_s=20,
        with_benchmarks=False,
    )
    assert time.monotonic() - started < 20
    assert result.results[0].status == "ok", (result.results[0].status, result.results[0].error)
    assert 0 < captured["size"] < 1_000_000


def test_enter_child_scrubs_a_dirty_argv_and_environment(tmp_path):
    import multiprocessing.process as mp_process

    from backtester.runner import _enter_child

    folder = tmp_path / "empty"
    folder.mkdir()
    old_argv = list(sys.argv)
    old_cwd = os.getcwd()
    old_env = dict(os.environ)
    old_original = mp_process.ORIGINAL_DIR
    old_path = list(sys.path)
    sys.argv = ["backtester", "run", "--ticker", "QQQ", "--src", "secret-path"]
    os.environ["BT_OPERATOR_SECRET"] = "canary-value"
    try:
        inherited_argv, _inherited_cwd = _enter_child(
            {"cwd": str(folder), "env": {"PATH": old_env.get("PATH", "")}}
        )
        assert "QQQ" in inherited_argv
        assert sys.argv == ["backtester-design"]
        assert "BT_OPERATOR_SECRET" not in os.environ
        assert os.environ.get("PATH") == old_env.get("PATH", "")
        assert os.path.normcase(os.getcwd()) == os.path.normcase(str(folder))
        assert os.path.normcase(mp_process.ORIGINAL_DIR) == os.path.normcase(str(folder))
    finally:
        sys.argv = old_argv
        os.chdir(old_cwd)
        os.environ.clear()
        os.environ.update(old_env)
        mp_process.ORIGINAL_DIR = old_original
        sys.path[:] = old_path


def _anon_pack(tmp_path: Path, symbol: str, name: str = "", price_factor: float = 1.1, profile: str = "arena"):
    from backtester.data import load_named
    from backtester.pack import build_pack
    from backtester.registry import entry_from_inspection, save_registry, upsert_series

    root = tmp_path / name if name else tmp_path
    src = _named_series(root / "src", symbol)
    loaded = load_named(src, symbol)
    registry = root / "registry.json"
    entry = entry_from_inspection(loaded.inspection, source_root=str(src), profile=profile)
    save_registry(registry, upsert_series({"schema_version": 1, "series": []}, entry))
    pack = root / "pack"
    sidecar = root / "operator" / "secret.json"
    built = build_pack(
        src,
        symbol,
        pack,
        sidecar,
        anonymize=True,
        profile=profile,
        registry_path=registry,
        price_factor=price_factor,
        volume_factor=0.5,
    )
    return pack, sidecar, built


def _pack_for_ticker_two(tmp_path: Path, name: str, price_factor: float):
    """One Monday session and two prices, with no anonymized cell equal to ``2``.

    A second anonymized day is numbered 2, and Wednesday's weekday is 2. Either
    cell is the ticker, so this pack stays on Monday and scales away from ``.2``.
    """
    from backtester.data import load_named
    from backtester.pack import build_pack
    from backtester.registry import entry_from_inspection, save_registry, upsert_series

    root = tmp_path / name
    src = root / "src"
    day = datetime(2024, 1, 8)
    rows = [
        _row(day, 9 * 60 + 30, 50.0),
        _row(day, 9 * 60 + 31, 51.0),
    ]
    _write_rows(src / "2_1min_filled_2024-01.csv.gz", rows)
    loaded = load_named(src, "2")
    registry = root / "registry.json"
    entry = entry_from_inspection(loaded.inspection, source_root=str(src), profile="arena")
    save_registry(registry, upsert_series({"schema_version": 1, "series": []}, entry))
    pack = root / "pack"
    sidecar = root / "operator" / "secret.json"
    built = build_pack(
        src,
        "2",
        pack,
        sidecar,
        anonymize=True,
        profile="arena",
        registry_path=registry,
        price_factor=price_factor,
        volume_factor=0.5,
    )
    return pack, sidecar, built


def test_designer_error_hides_the_operator_traceback(tmp_path):
    pack, sidecar, _built = _anon_pack(tmp_path, "NIK")
    source = (
        "class Strategy:\n"
        "    def on_bar(self, bar, state):\n"
        "        print('OPERATOR-STDIO-CANARY', flush=True)\n"
        "        raise RuntimeError('boom-isolated')\n"
    )
    result = run_parallel(
        [_strategy(tmp_path / "s", "boom", source)],
        [PackMarket(str(pack), str(sidecar))],
        tmp_path / "out",
        profile="arena",
        workers=1,
        timeout_s=30,
        with_benchmarks=False,
    )
    assert result.results[0].status == "error"
    operator = json.loads((Path(result.results[0].out_dir) / "error.json").read_text(encoding="utf-8"))
    designer = json.loads(
        (Path(result.results[0].out_dir) / "designer" / "error.json").read_text(encoding="utf-8")
    )
    assert "Traceback" in operator["error"]
    assert "boom-isolated" in operator["error"]
    assert "OPERATOR-STDIO-CANARY" in operator["error"]
    assert designer["error"] == "the strategy failed"
    assert "Traceback" not in designer["error"]
    assert "boom-isolated" not in designer["error"]
    assert "OPERATOR-STDIO-CANARY" not in designer["error"]
    tree = []
    for path in (tmp_path / "out" / "designer").rglob("*"):
        if path.is_file() and path.suffix.lower() != ".png":
            tree.append(path.read_text(encoding="utf-8"))
    blob = "\n".join(tree)
    assert "Traceback" not in blob
    assert "OPERATOR-STDIO-CANARY" not in blob
    assert "boom-isolated" not in blob


def test_ticker_scrub_ignores_case(tmp_path):
    pack, sidecar, _built = _anon_pack(tmp_path, "ZZZX")
    result = run_parallel(
        [_strategy(tmp_path / "strategies", "my_zzzx_v2", FLAT)],
        [PackMarket(str(pack), str(sidecar))],
        tmp_path / "out",
        profile="arena",
        workers=1,
        timeout_s=30,
        with_benchmarks=False,
    )
    assert result.failures == 0, [(item.status, item.error) for item in result.results]
    designer = tmp_path / "out" / "designer"
    board = (designer / "leaderboard.csv").read_text(encoding="utf-8")
    assert "my_RANDOM_v2" in board
    assert "zzzx" not in board.casefold()
    names = [path.name for path in designer.rglob("*")]
    assert all("zzzx" not in name.casefold() for name in names)
    for path in designer.rglob("*"):
        if path.is_file() and path.suffix.lower() != ".png":
            assert "zzzx" not in path.read_text(encoding="utf-8").casefold()
    assert_designer_clean(designer, symbol="ZZZX")
    operator = [
        path
        for path in (tmp_path / "out" / "jobs").rglob("metrics.json")
        if "designer" not in path.parts
    ]
    assert "ZZZX" in operator[0].read_text(encoding="utf-8")


def test_parent_rejects_metrics_the_child_rewrote(tmp_path):
    source = (
        "import backtester.engine_arena as arena\n"
        "_real = arena.metrics\n"
        "def lied(res, period_label_of_day=None):\n"
        "    out = _real(res, period_label_of_day)\n"
        "    out['total_return'] = 123.0\n"
        "    return out\n"
        "arena.metrics = lied\n"
        "class Strategy:\n"
        "    def on_bar(self, bar, state):\n"
        "        return None\n"
    )
    result = run_parallel(
        [_strategy(tmp_path / "s", "liar", source)],
        [FrameMarket("book", "TEST", frame=_frame())],
        tmp_path / "out",
        profile="arena",
        workers=1,
        timeout_s=30,
    )
    assert result.results[0].total_return != 123.0
    for path in (tmp_path / "out").rglob("metrics.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if "total_return" in payload:
            assert payload["total_return"] != 123.0


def test_parent_rejects_a_benchmark_run_at_a_patched_financing_rate(tmp_path):
    source = (
        "import backtester.engine_desk as desk\n"
        "desk.D.FINANCING_RATE_ANNUAL = 0.0\n"
        "class Strategy:\n"
        "    def on_bar(self, bar, state):\n"
        "        return None\n"
    )
    result = run_parallel(
        [_strategy(tmp_path / "s", "patched", source)],
        [FrameMarket("book", "TEST", frame=_frame())],
        tmp_path / "out",
        profile="desk",
        workers=1,
        timeout_s=30,
    )
    from backtester.engine_desk import D

    assert D.FINANCING_RATE_ANNUAL != 0.0
    for path in (tmp_path / "out").rglob("metrics.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if "financing_rate_annual" in payload:
            assert payload["financing_rate_annual"] == D.FINANCING_RATE_ANNUAL


def test_cancel_after_the_gate_stops_the_worker(tmp_path):
    source = (
        "import time\n"
        "class Strategy:\n"
        "    def on_bar(self, bar, state):\n"
        "        time.sleep(60)\n"
        "        return None\n"
    )
    token = CancelToken()

    def _after_gate():
        time.sleep(1.5)
        token.cancel()

    watcher = threading.Thread(target=_after_gate, daemon=True)
    watcher.start()
    started = time.monotonic()
    try:
        result = run_parallel(
            [_strategy(tmp_path / "s", "slow", source)],
            [FrameMarket("book", "TEST", frame=_frame())],
            tmp_path / "out",
            profile="arena",
            workers=1,
            timeout_s=25,
            with_benchmarks=False,
            cancel=token,
        )
    finally:
        watcher.join(timeout=2)
    assert time.monotonic() - started < 12
    assert result.results[0].status == "cancelled"
    assert "cancelled" in (result.results[0].error or "")


def test_rss_poll_kills_without_a_memory_error(tmp_path):
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import backtester.runner\nfrom backtester.limits import current_rss\nprint(current_rss())\n",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert probe.returncode == 0, probe.stderr
    baseline = int(probe.stdout.strip())
    cap = baseline + 96 * 1024 * 1024
    source = (
        "import time\n"
        "class Strategy:\n"
        "    def on_bar(self, bar, state):\n"
        "        blobs = []\n"
        "        remaining = 160 * 1024 * 1024\n"
        "        while remaining > 0:\n"
        "            chunk = bytearray(min(remaining, 8 * 1024 * 1024))\n"
        "            for index in range(0, len(chunk), 4096):\n"
        "                chunk[index] = 1\n"
        "            blobs.append(chunk)\n"
        "            remaining -= len(chunk)\n"
        "        time.sleep(3)\n"
        "        return None\n"
    )
    _strategy(tmp_path / "s", "hog", source)
    _strategy(tmp_path / "s", "good", FLAT)
    result = run_parallel(
        discover_strategies(tmp_path / "s"),
        [FrameMarket("book", "TEST", frame=_frame())],
        tmp_path / "out",
        profile="arena",
        workers=2,
        timeout_s=30,
        memory_bytes=cap,
        with_benchmarks=False,
    )
    by_id = {item.strategy_id: item for item in result.results}
    detail = [(item.strategy_id, item.status, item.error) for item in result.results]
    assert by_id["hog"].status == "memory", detail
    assert "exceeded memory limit" in (by_id["hog"].error or "")
    assert "MemoryError" not in (by_id["hog"].error or "")
    assert by_id["good"].status == "ok", detail


def _orphan_strategy(_marker: Path, _ready: Path, *, ignore_term: bool, action: str) -> str:
    # The jail does not let the strategy start a grandchild. These actions
    # cover the worker's own exit paths: a clean return, an error, a timeout,
    # and a cancel. ``ignore_term`` is kept so the call sites stay explicit.
    del ignore_term
    if action == "raise":
        finish = "        raise RuntimeError('grandchild-left-behind')\n"
    elif action == "sleep":
        finish = "        time.sleep(300)\n        return None\n"
    else:
        finish = "        return None\n"
    return (
        "import time\n"
        "class Strategy:\n"
        "    def on_bar(self, bar, state):\n"
        f"{finish}"
    )


def _run_orphan(tmp_path: Path, name: str, *, ignore_term: bool, action: str, timeout_s: float, cancel: bool):
    marker = tmp_path / f"{name}.json"
    ready = tmp_path / f"{name}.ready"
    _strategy(tmp_path / name, "orphan", _orphan_strategy(marker, ready, ignore_term=ignore_term, action=action))
    token = CancelToken()
    watcher = None
    if cancel:
        def _after_gate():
            time.sleep(1.5)
            token.cancel()

        watcher = threading.Thread(target=_after_gate, daemon=True)
        watcher.start()
    started = time.monotonic()
    try:
        result = run_parallel(
            discover_strategies(tmp_path / name),
            [FrameMarket("book", "TEST", frame=_frame())],
            tmp_path / f"{name}-out",
            profile="arena",
            workers=1,
            timeout_s=timeout_s,
            with_benchmarks=False,
            cancel=token if cancel else None,
        )
        return result, time.monotonic() - started
    finally:
        if watcher is not None:
            watcher.join(timeout=2)
        _assert_grandchild_gone(marker, name)


def test_grandchildren_are_reaped_after_return_error_timeout_and_cancel(tmp_path):
    """A process the strategy starts dies on every exit, including a clean return.

    The SIGTERM-ignoring grandchild stays alive if the group is not SIGKILLed
    after the leader has exited. Plain ``sleep`` dies on SIGTERM, so it only
    proves the success and error paths signal the group at all.
    """
    returned, elapsed = _run_orphan(
        tmp_path, "sleep-ok", ignore_term=False, action="return", timeout_s=30, cancel=False
    )
    assert elapsed < 20
    assert returned.results[0].status == "ok", (returned.results[0].status, returned.results[0].error)

    raised, elapsed = _run_orphan(
        tmp_path, "sleep-err", ignore_term=False, action="raise", timeout_s=30, cancel=False
    )
    assert elapsed < 20
    assert raised.results[0].status == "error"
    assert "grandchild-left-behind" in (raised.results[0].error or "")

    ignored, elapsed = _run_orphan(
        tmp_path, "ign-ok", ignore_term=True, action="return", timeout_s=30, cancel=False
    )
    assert elapsed < 20
    assert ignored.results[0].status == "ok", (ignored.results[0].status, ignored.results[0].error)

    timed, elapsed = _run_orphan(
        tmp_path, "ign-timeout", ignore_term=True, action="sleep", timeout_s=6, cancel=False
    )
    assert elapsed < 20
    assert timed.results[0].status == "timeout"

    cancelled, elapsed = _run_orphan(
        tmp_path, "ign-cancel", ignore_term=True, action="sleep", timeout_s=25, cancel=True
    )
    assert elapsed < 12, [(item.status, item.error) for item in cancelled.results]
    assert cancelled.results[0].status == "cancelled"


def test_job_directories_keep_case_variants_apart(tmp_path):
    from backtester.parallel import _job_directories

    assignments = [
        (StrategyRef("ZzZx", "one.py"), ("pack_same",), None),
        (StrategyRef("zzzx", "two.py"), ("pack_same",), None),
        (StrategyRef("flat", "three.py"), ("Pack",), None),
        (StrategyRef("flat", "three.py"), ("pack",), None),
    ]
    paths = _job_directories(tmp_path, assignments)
    folded = [str(path).casefold() for path in paths.values()]
    assert len(folded) == len(set(folded)) == 4
    assert paths[("ZzZx", "pack_same")] == tmp_path / "jobs" / "ZzZx" / "pack_same"
    assert paths[("zzzx", "pack_same")] == tmp_path / "jobs" / "zzzx-2" / "pack_same"
    assert paths[("flat", "Pack")] == tmp_path / "jobs" / "flat" / "Pack"
    assert paths[("flat", "pack")] == tmp_path / "jobs" / "flat" / "pack-2"


def test_designer_leaderboard_keeps_distinct_ids_for_ticker_case_variants(tmp_path):
    from backtester.report import read_leaderboard

    pack, sidecar, _built = _anon_pack(tmp_path, "ZZZX")
    result = run_parallel(
        [
            _strategy(tmp_path / "one", "ZzZx", FLAT),
            _strategy(tmp_path / "two", "zzzx", FLAT),
        ],
        [PackMarket(str(pack), str(sidecar))],
        tmp_path / "out",
        profile="arena",
        workers=2,
        timeout_s=30,
        with_benchmarks=False,
    )
    assert result.failures == 0, [(item.strategy_id, item.status, item.error) for item in result.results]
    job_dirs = [Path(item.out_dir) for item in result.results]
    # A case-insensitive disk would otherwise make both workers share equity.png.
    assert len({str(path).casefold() for path in job_dirs}) == 2
    for path in job_dirs:
        assert (path / "designer" / "equity.png").read_bytes().startswith(b"\x89PNG")
    designer = tmp_path / "out" / "designer"
    rows = read_leaderboard(designer / "leaderboard.csv")
    ids = [row["strategy_id"] for row in rows]
    folders = sorted(path.name for path in designer.iterdir() if path.is_dir())
    assert len(ids) == 2
    assert len(set(ids)) == 2
    assert sorted(ids) == folders
    assert all("zzzx" not in item.casefold() for item in ids)
    operator = read_leaderboard(tmp_path / "out" / "leaderboard.csv")
    assert {row["strategy_id"] for row in operator} == {"ZzZx", "zzzx"}
    assert_designer_clean(designer, symbol="ZZZX")


def test_child_cannot_read_a_report_through_pythonpath(tmp_path):
    secret = tmp_path / "via-pythonpath"
    secret.mkdir()
    (secret / "metrics.json").write_text(
        json.dumps({"symbol": "QQQ", "share_scale": 9.001}), encoding="utf-8"
    )
    source = (
        "import os, sys\n"
        "from pathlib import Path\n"
        "class Strategy:\n"
        "    def __init__(self):\n"
        "        self.report = None\n"
        "    def on_bar(self, bar, state):\n"
        "        hits = []\n"
        "        pythonpath = os.environ.get('PYTHONPATH', '')\n"
        "        roots = [part for part in pythonpath.split(os.pathsep) if part]\n"
        "        roots.extend(sys.path)\n"
        "        for root in roots:\n"
        "            candidate = Path(root) / 'metrics.json'\n"
        "            try:\n"
        "                if candidate.is_file():\n"
        "                    hits.append(candidate.read_text(encoding='utf-8'))\n"
        "            except OSError:\n"
        "                pass\n"
        "        self.report = {'hits': hits, 'pythonpath': pythonpath, 'path': list(sys.path)}\n"
        "        return None\n"
    )
    old = os.environ.get("PYTHONPATH")
    old_path = list(sys.path)
    os.environ["PYTHONPATH"] = str(secret.resolve())
    sys.path.insert(0, str(secret.resolve()))
    captured = []
    import backtester.runner as runner_mod

    real = runner_mod._isolated_books

    def spy(*args, **kwargs):
        books = real(*args, **kwargs)
        captured.append(books["strategy"][0].get("observer"))
        return books

    patch = pytest.MonkeyPatch()
    patch.setattr(runner_mod, "_isolated_books", spy)
    try:
        result = run_parallel(
            [_strategy(tmp_path / "s", "reader", source)],
            [FrameMarket("book", "TEST", frame=_frame())],
            tmp_path / "out",
            profile="arena",
            workers=1,
            timeout_s=30,
            with_benchmarks=False,
        )
    finally:
        patch.undo()
        if old is None:
            os.environ.pop("PYTHONPATH", None)
        else:
            os.environ["PYTHONPATH"] = old
        sys.path[:] = old_path
    assert result.failures == 0, [(item.status, item.error) for item in result.results]
    seen = captured[0].report
    dumped = json.dumps(seen)
    assert seen["hits"] == []
    assert "9.001" not in dumped
    assert "QQQ" not in dumped
    assert str(secret.resolve()) not in seen["pythonpath"]
    assert all(os.path.normcase(os.path.abspath(entry)) != os.path.normcase(str(secret.resolve())) for entry in seen["path"] if entry)


def test_a_missing_windows_job_fails_the_run(tmp_path, monkeypatch):
    monkeypatch.setattr("backtester.limits.arm_worker_job", lambda *_args, **_kwargs: None)
    result = run_parallel(
        [_strategy(tmp_path / "s", "flat", FLAT)],
        [FrameMarket("book", "TEST", frame=_frame())],
        tmp_path / "out",
        profile="arena",
        workers=1,
        timeout_s=30,
        with_benchmarks=False,
    )
    if os.name == "nt":
        assert result.results[0].status == "error"
        assert "job object" in (result.results[0].error or "")
    else:
        assert result.results[0].status == "ok", (result.results[0].status, result.results[0].error)


_SCHEMA_TICKERS = ("COST", "DD", "NET", "MAX", "OK", "INF", "ID", "TRUE")


def _registered(index: Path, run_id: str, out: Path) -> None:
    assert (out / "run.json").is_file()
    document = json.loads((out / "run.json").read_text(encoding="utf-8"))
    assert document["id"] == run_id
    index_doc = json.loads(index.read_text(encoding="utf-8"))
    assert run_id in index_doc["runs"]
    assert Path(index_doc["runs"][run_id]["path"]) == out.resolve()


@OWNER_PC
def test_schema_token_tickers_publish_and_register(tmp_path):
    """Headers such as borrow_cost are not the ticker COST."""
    from backtester.report import read_leaderboard

    for symbol in _SCHEMA_TICKERS:
        pack, sidecar, _built = _anon_pack(tmp_path, symbol, name=symbol)
        index = tmp_path / symbol / "runs.json"
        out = tmp_path / symbol / "out"
        result = run_parallel(
            [_strategy(tmp_path / symbol / "strategies", "flat", FLAT)],
            [PackMarket(str(pack), str(sidecar))],
            out,
            profile="arena",
            workers=1,
            timeout_s=60,
            index_path=index,
        )
        assert result.failures == 0, (symbol, [(item.status, item.error) for item in result.results])
        _registered(index, result.run_id, out)
        board = read_leaderboard(out / "designer" / "leaderboard.csv")
        assert board[0]["status"] == "ok"
        if symbol == "INF":
            assert board[0]["calmar"] == "inf"
        if symbol == "OK":
            assert "RANDOM" not in board[0]["status"]
        assert_designer_clean(out / "designer", symbol=symbol)
        info = json.loads((out / "run.json").read_text(encoding="utf-8"))
        assert info["jobs"][0]["status"] == "ok"
        assert info["jobs"][0]["symbol"] == symbol


def test_mixed_zzzx_and_cost_batch_registers_both(tmp_path):
    from backtester.report import read_leaderboard

    zzzx_pack, zzzx_side, _built = _anon_pack(tmp_path, "ZZZX", name="zzzx", price_factor=1.1)
    cost_pack, cost_side, _built = _anon_pack(tmp_path, "COST", name="cost", price_factor=1.3)
    index = tmp_path / "runs.json"
    out = tmp_path / "out"
    result = run_parallel(
        [_strategy(tmp_path / "strategies", "flat", FLAT)],
        [
            PackMarket(str(zzzx_pack), str(zzzx_side)),
            PackMarket(str(cost_pack), str(cost_side)),
        ],
        out,
        profile="arena",
        workers=2,
        timeout_s=60,
        index_path=index,
    )
    assert result.failures == 0, [(item.source_id, item.status, item.error) for item in result.results]
    _registered(index, result.run_id, out)
    info = json.loads((out / "run.json").read_text(encoding="utf-8"))
    assert {job["symbol"] for job in info["jobs"]} == {"ZZZX", "COST"}
    assert {job["status"] for job in info["jobs"]} == {"ok"}
    board = read_leaderboard(out / "designer" / "leaderboard.csv")
    assert len(board) == 2
    assert {row["status"] for row in board} == {"ok"}
    assert_designer_clean(out / "designer", symbol="ZZZX")
    assert_designer_clean(out / "designer", symbol="COST")


_REAL_COPYTREE = shutil.copytree


def _plant_ticker_file(src, dst, *args, **kwargs):
    """Drop a ticker-named file beside the copied job, not inside it.

    The per-job audit looks at the copied directory. A sibling is only visible
    to the whole-tree audit. That failure must not blank the other strategies.
    """
    _REAL_COPYTREE(src, dst, *args, **kwargs)
    metrics = Path(dst) / "metrics.json"
    if not metrics.is_file():
        return
    document = json.loads(metrics.read_text(encoding="utf-8"))
    strategy = (document.get("run") or {}).get("strategy_id") or document.get("strategy_id")
    if strategy != "leaky":
        return
    (Path(dst).parent / "COST.json").write_text("{}\n", encoding="utf-8")


def _plant_v_json(src, dst, *args, **kwargs):
    _REAL_COPYTREE(src, dst, *args, **kwargs)
    metrics = Path(dst) / "metrics.json"
    if not metrics.is_file():
        return
    document = json.loads(metrics.read_text(encoding="utf-8"))
    strategy = (document.get("run") or {}).get("strategy_id") or document.get("strategy_id")
    if strategy != "leaky":
        return
    (Path(dst) / "V.json").write_text("{}\n", encoding="utf-8")


def test_a_ticker_in_a_value_fails_only_that_row(tmp_path, monkeypatch):
    monkeypatch.setattr("backtester.parallel.shutil.copytree", _plant_ticker_file)
    pack, sidecar, _built = _anon_pack(tmp_path, "COST", name="cost")
    index = tmp_path / "runs.json"
    out = tmp_path / "out"
    result = run_parallel(
        [
            _strategy(tmp_path / "strategies", "flat", FLAT),
            _strategy(tmp_path / "strategies", "leaky", FLAT),
        ],
        [PackMarket(str(pack), str(sidecar))],
        out,
        profile="arena",
        workers=2,
        timeout_s=60,
        with_benchmarks=False,
        index_path=index,
    )
    by_id = {item.strategy_id: item for item in result.results}
    assert by_id["flat"].status == "ok", by_id["flat"].error
    assert by_id["leaky"].status == "error"
    assert "ticker" in (by_id["leaky"].error or "")
    assert result.failures == 1
    _registered(index, result.run_id, out)
    info = json.loads((out / "run.json").read_text(encoding="utf-8"))
    statuses = {job["strategy_id"]: job["status"] for job in info["jobs"]}
    assert statuses == {"flat": "ok", "leaky": "error"}
    blob = []
    for path in (out / "designer").rglob("*"):
        if path.is_file() and path.suffix.lower() != ".png":
            blob.append(path.read_text(encoding="utf-8"))
    assert "COST" not in "\n".join(blob)
    assert (out / "designer" / "flat").is_dir()
    assert any((out / "designer" / "flat").rglob("metrics.json"))
    assert not (out / "designer" / "error.json").is_file()
    assert_designer_clean(out / "designer", symbol="COST")


def test_parallel_cli_exits_without_a_traceback_when_one_row_fails(tmp_path, monkeypatch, capsys):
    from backtester.__main__ import main

    monkeypatch.setattr("backtester.parallel.shutil.copytree", _plant_ticker_file)
    monkeypatch.chdir(tmp_path)
    pack, sidecar, _built = _anon_pack(tmp_path, "COST", name="cost")
    strategies = tmp_path / "strategies"
    _strategy(strategies, "flat", FLAT)
    _strategy(strategies, "leaky", FLAT)
    out = tmp_path / "out"
    code = main(
        [
            "parallel",
            "--strategies",
            str(strategies),
            "--workers",
            "2",
            "--profile",
            "arena",
            "--pack",
            str(pack),
            "--sidecar",
            str(sidecar),
            "--out",
            str(out),
            "--timeout",
            "60",
            "--no-benchmarks",
        ]
    )
    captured = capsys.readouterr()
    assert code == 1
    assert "Traceback" not in captured.out
    assert "Traceback" not in captured.err
    assert (out / "run.json").is_file()
    info = json.loads((out / "run.json").read_text(encoding="utf-8"))
    assert {job["status"] for job in info["jobs"]} == {"ok", "error"}
    index = json.loads((tmp_path / ".backtester-runs.json").read_text(encoding="utf-8"))
    assert info["id"] in index["runs"]


# The names in README.md. PROGRAM.md and PLAN.md are not in this repo.
_REQUIRED_SYMBOLS = (
    "AAPL",
    "ABBV",
    "AMD",
    "BRK.B",
    "CVX",
    "GOOG",
    "GOOGL",
    "INTC",
    "JNJ",
    "JPM",
    "LLY",
    "MA",
    "META",
    "MSFT",
    "MU",
    "NVDA",
    "PLTR",
    "QQQ",
    "SPY",
    "TSLA",
    "V",
    "WMT",
    "XOM",
)


def _designer_text(designer: Path) -> str:
    parts = []
    for path in designer.rglob("*"):
        parts.append(path.name)
        if path.is_file() and path.suffix.lower() != ".png":
            parts.append(path.read_text(encoding="utf-8"))
    return "\n".join(parts)


@pytest.mark.parametrize("symbol", _REQUIRED_SYMBOLS + ("SS", "FI", "AS", "ET"))
def test_each_required_symbol_publishes_an_embedded_id(tmp_path, symbol):
    from backtester.report import read_leaderboard, ticker_token_spans

    pack, sidecar, _built = _anon_pack(tmp_path, symbol, name="pack", price_factor=1.2)
    strategy_id = f"my_{symbol}_v2"
    index = tmp_path / "runs.json"
    out = tmp_path / "out"
    result = run_parallel(
        [
            _strategy(tmp_path / "strategies", "flat", FLAT),
            _strategy(tmp_path / "strategies", strategy_id, FLAT),
        ],
        [PackMarket(str(pack), str(sidecar))],
        out,
        profile="arena",
        workers=2,
        timeout_s=60,
        with_benchmarks=False,
        index_path=index,
    )
    assert result.failures == 0, (symbol, [(item.strategy_id, item.status, item.error) for item in result.results])
    _registered(index, result.run_id, out)
    designer = out / "designer"
    board = read_leaderboard(designer / "leaderboard.csv")
    assert {row["status"] for row in board} == {"ok"}
    assert {row["strategy_id"] for row in board} == {"flat", "my_RANDOM_v2"}
    assert (designer / "my_RANDOM_v2").is_dir()
    assert not (designer / strategy_id).exists()
    assert ticker_token_spans(_designer_text(designer), symbol) == []
    assert_designer_clean(designer, symbol=symbol)
    assert (out / "jobs" / strategy_id).is_dir()


@pytest.mark.parametrize(
    ("symbol", "strategy_id"),
    [("SS", "my_ß_v2"), ("FI", "my_ﬁ_v2")],
)
def test_casefold_embedded_ids_publish(tmp_path, symbol, strategy_id):
    from backtester.report import read_leaderboard

    pack, sidecar, _built = _anon_pack(tmp_path, symbol, name="pack", price_factor=1.25)
    index = tmp_path / "runs.json"
    out = tmp_path / "out"
    result = run_parallel(
        [_strategy(tmp_path / "strategies", strategy_id, FLAT)],
        [PackMarket(str(pack), str(sidecar))],
        out,
        profile="arena",
        workers=1,
        timeout_s=60,
        with_benchmarks=False,
        index_path=index,
    )
    assert result.failures == 0, (symbol, result.results[0].error)
    _registered(index, result.run_id, out)
    designer = out / "designer"
    board = (designer / "leaderboard.csv").read_text(encoding="utf-8")
    assert "my_RANDOM_v2" in board
    assert strategy_id not in board
    assert all(strategy_id not in path.name for path in designer.rglob("*"))
    assert_designer_clean(designer, symbol=symbol)
    rows = read_leaderboard(designer / "leaderboard.csv")
    assert rows[0]["status"] == "ok"
    assert rows[0]["strategy_id"] == "my_RANDOM_v2"


def test_one_embedded_id_does_not_fail_the_mixed_batch(tmp_path):
    from backtester.report import read_leaderboard

    zzzx_pack, zzzx_side, _built = _anon_pack(tmp_path, "ZZZX", name="zzzx", price_factor=1.1)
    cost_pack, cost_side, _built = _anon_pack(tmp_path, "COST", name="cost", price_factor=1.3)
    index = tmp_path / "runs.json"
    out = tmp_path / "out"
    result = run_parallel(
        [
            _strategy(tmp_path / "strategies", "flat", FLAT),
            _strategy(tmp_path / "strategies", "my_COST_v2", FLAT),
        ],
        [
            PackMarket(str(zzzx_pack), str(zzzx_side)),
            PackMarket(str(cost_pack), str(cost_side)),
        ],
        out,
        profile="arena",
        workers=2,
        timeout_s=60,
        with_benchmarks=False,
        index_path=index,
    )
    assert result.failures == 0, [(item.strategy_id, item.source_id, item.status, item.error) for item in result.results]
    _registered(index, result.run_id, out)
    info = json.loads((out / "run.json").read_text(encoding="utf-8"))
    assert len(info["jobs"]) == 4
    assert {job["status"] for job in info["jobs"]} == {"ok"}
    designer = out / "designer"
    board = read_leaderboard(designer / "leaderboard.csv")
    assert len(board) == 4
    assert {row["status"] for row in board} == {"ok"}
    assert {row["strategy_id"] for row in board} == {"flat", "my_RANDOM_v2"}
    assert (designer / "flat").is_dir()
    assert (designer / "my_RANDOM_v2").is_dir()
    assert not (designer / "my_COST_v2").exists()
    assert any((designer / "flat").rglob("metrics.json"))
    assert any((designer / "my_RANDOM_v2").rglob("metrics.json"))
    text = _designer_text(designer)
    assert "my_COST_v2" not in text
    assert "zzzx" not in text.casefold()
    assert_designer_clean(designer, symbol="COST")
    assert_designer_clean(designer, symbol="ZZZX")


def test_an_id_carrying_both_batch_tickers_is_scrubbed(tmp_path):
    """One row's symbol is not enough: COST and ZZZX are both in the id."""
    from backtester.report import read_leaderboard

    zzzx_pack, zzzx_side, _built = _anon_pack(tmp_path, "ZZZX", name="zzzx", price_factor=1.15)
    cost_pack, cost_side, _built = _anon_pack(tmp_path, "COST", name="cost", price_factor=1.35)
    out = tmp_path / "out"
    result = run_parallel(
        [_strategy(tmp_path / "strategies", "my_COST_ZZZX_v2", FLAT)],
        [
            PackMarket(str(zzzx_pack), str(zzzx_side)),
            PackMarket(str(cost_pack), str(cost_side)),
        ],
        out,
        profile="arena",
        workers=2,
        timeout_s=60,
        with_benchmarks=False,
    )
    assert result.failures == 0, [(item.source_id, item.status, item.error) for item in result.results]
    designer = out / "designer"
    board = read_leaderboard(designer / "leaderboard.csv")
    assert {row["strategy_id"] for row in board} == {"my_RANDOM_RANDOM_v2"}
    assert {row["status"] for row in board} == {"ok"}
    assert (designer / "my_RANDOM_RANDOM_v2").is_dir()
    assert_designer_clean(designer, symbol="COST")
    assert_designer_clean(designer, symbol="ZZZX")


def test_colliding_tickers_keep_distinct_designer_ids(tmp_path):
    from backtester.report import read_leaderboard

    zzzx_pack, zzzx_side, _built = _anon_pack(tmp_path, "ZZZX", name="zzzx", price_factor=1.1)
    cost_pack, cost_side, _built = _anon_pack(tmp_path, "COST", name="cost", price_factor=1.4)
    out = tmp_path / "out"
    result = run_parallel(
        [
            _strategy(tmp_path / "strategies", "COST", FLAT),
            _strategy(tmp_path / "strategies", "ZZZX", FLAT),
        ],
        [
            PackMarket(str(zzzx_pack), str(zzzx_side)),
            PackMarket(str(cost_pack), str(cost_side)),
        ],
        out,
        profile="arena",
        workers=2,
        timeout_s=60,
        with_benchmarks=False,
    )
    assert result.failures == 0, [(item.strategy_id, item.status, item.error) for item in result.results]
    designer = out / "designer"
    folders = sorted(path.name for path in designer.iterdir() if path.is_dir())
    assert folders == ["RANDOM", "RANDOM-2"]
    board = read_leaderboard(designer / "leaderboard.csv")
    assert len(board) == 4
    assert {row["strategy_id"] for row in board} == {"RANDOM", "RANDOM-2"}
    assert {row["status"] for row in board} == {"ok"}
    assert_designer_clean(designer, symbol="COST")
    assert_designer_clean(designer, symbol="ZZZX")
    assert (out / "jobs" / "COST").is_dir()
    assert (out / "jobs" / "ZZZX").is_dir()


def test_a_file_named_v_json_fails_only_that_job(tmp_path, monkeypatch):
    monkeypatch.setattr("backtester.parallel.shutil.copytree", _plant_v_json)
    pack, sidecar, _built = _anon_pack(tmp_path, "V", name="v", price_factor=1.2)
    index = tmp_path / "runs.json"
    out = tmp_path / "out"
    result = run_parallel(
        [
            _strategy(tmp_path / "strategies", "flat", FLAT),
            _strategy(tmp_path / "strategies", "leaky", FLAT),
        ],
        [PackMarket(str(pack), str(sidecar))],
        out,
        profile="arena",
        workers=2,
        timeout_s=60,
        with_benchmarks=False,
        index_path=index,
    )
    by_id = {item.strategy_id: item for item in result.results}
    assert by_id["flat"].status == "ok", by_id["flat"].error
    assert by_id["leaky"].status == "error"
    assert "ticker" in (by_id["leaky"].error or "")
    assert result.failures == 1
    _registered(index, result.run_id, out)
    designer = out / "designer"
    assert (designer / "flat").is_dir()
    assert any((designer / "flat").rglob("metrics.json"))
    assert not (designer / "error.json").is_file()
    assert all(path.name != "V.json" for path in designer.rglob("*"))
    assert_designer_clean(designer, symbol="V")


def _assert_ids_match_directories(designer):
    from backtester.report import read_leaderboard

    folders = [path for path in designer.iterdir() if path.is_dir()]
    names = [path.name for path in folders]
    assert len(names) == len({name.casefold() for name in names})
    board_path = designer / "leaderboard.csv"
    assert board_path.is_file()
    board = read_leaderboard(board_path)
    assert {row["strategy_id"] for row in board} == set(names)
    for row in board:
        source = designer / row["strategy_id"] / row["source_id"]
        assert source.is_dir(), (row["strategy_id"], row["source_id"])
    for folder in folders:
        sources = [path.name for path in folder.iterdir() if path.is_dir()]
        assert len(sources) == len({name.casefold() for name in sources})
        for source in folder.iterdir():
            if not source.is_dir():
                continue
            for path in source.rglob("*.json"):
                if not path.is_file():
                    continue
                document = json.loads(path.read_text(encoding="utf-8"))
                _assert_published_ids(document, folder.name, source.name)
    return board


def _assert_published_ids(document, folder: str, source: str) -> None:
    if isinstance(document, dict):
        if isinstance(document.get("strategy_id"), str):
            assert document["strategy_id"] == folder
        if isinstance(document.get("source_id"), str):
            assert document["source_id"] == source
        for value in document.values():
            _assert_published_ids(value, folder, source)
    elif isinstance(document, list):
        for item in document:
            _assert_published_ids(item, folder, source)


def test_designer_components_are_casefold_unique_and_screened():
    from backtester.parallel import _designer_component
    from backtester.report import ticker_token_spans

    used: set[str] = set()
    first = _designer_component("My_COST_v2", ["COST"], used)
    second = _designer_component("my_cost_v2", ["COST"], used)
    assert first.casefold() != second.casefold()
    assert ticker_token_spans(first, "COST") == []
    assert ticker_token_spans(second, "COST") == []

    sources: set[str] = set()
    pack = _designer_component("Pack", ["2", "QQQ"], sources)
    pack_folded = _designer_component("pack", ["2", "QQQ"], sources)
    assert pack.casefold() != pack_folded.casefold()
    assert ticker_token_spans(pack, "2") == []
    assert ticker_token_spans(pack_folded, "2") == []
    assert ticker_token_spans(pack_folded, "QQQ") == []

    tickers: set[str] = set()
    two = _designer_component("2", ["2", "QQQ"], tickers)
    qqq = _designer_component("QQQ", ["2", "QQQ"], tickers)
    assert two.casefold() != qqq.casefold()
    for name in (two, qqq):
        assert ticker_token_spans(name, "2") == []
        assert ticker_token_spans(name, "QQQ") == []
    assert "RANDOM-2" not in {two, qqq}

    tens: set[str] = set()
    names = [_designer_component("10", ["10"], tens) for _ in range(12)]
    assert len(names) == len({name.casefold() for name in names})
    assert "RANDOM-10" not in names
    assert all(ticker_token_spans(name, "10") == [] for name in names)

    stand_in: set[str] = set()
    renamed = _designer_component("asset", ["asset"], stand_in)
    assert renamed == "RANDOM"
    assert ticker_token_spans(renamed, "asset") == []


def test_case_variant_designer_dirs_stay_distinct(tmp_path):
    from backtester.report import ticker_token_spans

    pack, sidecar, _built = _anon_pack(tmp_path, "COST", name="cost", price_factor=1.2)
    out = tmp_path / "out"
    result = run_parallel(
        [
            _strategy(tmp_path / "strategies", "My_COST_v2", FLAT),
            _strategy(tmp_path / "strategies", "my_cost_v2", FLAT),
        ],
        [PackMarket(str(pack), str(sidecar))],
        out,
        profile="arena",
        workers=2,
        timeout_s=60,
        with_benchmarks=False,
    )
    assert result.failures == 0, [(item.strategy_id, item.status, item.error) for item in result.results]
    designer = out / "designer"
    board = _assert_ids_match_directories(designer)
    assert {row["status"] for row in board} == {"ok"}
    folders = [path for path in designer.iterdir() if path.is_dir()]
    assert len(folders) == 2
    for folder in folders:
        assert any(folder.rglob("metrics.json"))
        assert ticker_token_spans(folder.name, "COST") == []
    assert_designer_clean(designer, symbol="COST")


def test_numeric_ticker_suffix_is_not_published(tmp_path):
    from backtester.report import ticker_token_spans

    two_pack, two_side, _built = _pack_for_ticker_two(tmp_path, "two", 1.5)
    qqq_pack, qqq_side, _built = _anon_pack(tmp_path, "QQQ", name="qqq", price_factor=1.35)
    out = tmp_path / "out"
    result = run_parallel(
        [
            _strategy(tmp_path / "strategies", "2", FLAT),
            _strategy(tmp_path / "strategies", "QQQ", FLAT),
        ],
        [
            PackMarket(str(two_pack), str(two_side), source_id="book"),
            PackMarket(str(qqq_pack), str(qqq_side), source_id="book"),
        ],
        out,
        profile="arena",
        workers=2,
        timeout_s=60,
        with_benchmarks=False,
    )
    assert result.failures == 0, [(item.strategy_id, item.status, item.error) for item in result.results]
    designer = out / "designer"
    assert not (designer / "RANDOM-2").exists()
    board = _assert_ids_match_directories(designer)
    assert len(board) == 4
    assert {row["status"] for row in board} == {"ok"}
    for path in designer.rglob("*"):
        assert ticker_token_spans(path.name, "2") == []
        assert ticker_token_spans(path.name, "QQQ") == []
    for folder in [path for path in designer.iterdir() if path.is_dir()]:
        assert any(folder.rglob("metrics.json"))
    assert_designer_clean(designer, symbol="2")
    assert_designer_clean(designer, symbol="QQQ")


def test_a_forced_ticker_suffix_is_renamed_off_disk(tmp_path, monkeypatch):
    """A directory named with a ticker must not survive the audit."""
    from backtester.parallel import _designer_component as real_component

    state = {"poisoned": False}

    def poison(value, symbols, used):
        if not state["poisoned"] and value == "QQQ":
            state["poisoned"] = True
            used.add("random-2")
            return "RANDOM-2"
        return real_component(value, symbols, used)

    monkeypatch.setattr("backtester.parallel._designer_component", poison)
    two_pack, two_side, _built = _pack_for_ticker_two(tmp_path, "two", 1.5)
    qqq_pack, qqq_side, _built = _anon_pack(tmp_path, "QQQ", name="qqq", price_factor=1.45)
    out = tmp_path / "out"
    result = run_parallel(
        [
            _strategy(tmp_path / "strategies", "2", FLAT),
            _strategy(tmp_path / "strategies", "QQQ", FLAT),
        ],
        [
            PackMarket(str(two_pack), str(two_side)),
            PackMarket(str(qqq_pack), str(qqq_side)),
        ],
        out,
        profile="arena",
        workers=2,
        timeout_s=60,
        with_benchmarks=False,
    )
    assert state["poisoned"]
    by_id = {item.strategy_id: item for item in result.results}
    assert by_id["2"].status == "ok", by_id["2"].error
    assert by_id["QQQ"].status == "ok", by_id["QQQ"].error
    designer = out / "designer"
    assert not (designer / "RANDOM-2").exists()
    board = _assert_ids_match_directories(designer)
    assert {row["status"] for row in board} == {"ok"}
    for folder in [path for path in designer.iterdir() if path.is_dir()]:
        assert any(folder.rglob("metrics.json"))
    assert_designer_clean(designer, symbol="2")
    assert_designer_clean(designer, symbol="QQQ")


_PASSING_EXTRA_SYMBOLS = ("asset", "2", "10", "SS")


def _publish_profile(symbol: str) -> tuple[str, bool]:
    del symbol
    return "arena", False


def _assert_operator_names(out: Path, *, benchmarks: bool, profile: str) -> None:
    books = [
        source
        for strategy in (out / "jobs").iterdir()
        if strategy.is_dir()
        for source in strategy.iterdir()
        if source.is_dir() and (source / "metrics.json").is_file()
    ]
    assert books
    for book in books:
        assert (book / "equity_curve.csv").is_file()
        assert (book / "equity.csv").is_file()
        assert (book / "equity.png").is_file()
        if benchmarks:
            assert (book / "benchmarks" / "buy_and_hold_1x").is_dir()
            if profile == "desk":
                assert (book / "benchmarks" / "long_2x").is_dir()


def _assert_designer_names(designer: Path, symbols) -> None:
    from backtester.report import ticker_token_spans

    for symbol in symbols:
        for path in [designer, *designer.rglob("*")]:
            assert ticker_token_spans(path.name, symbol) == [], (symbol, path)


@pytest.mark.parametrize("symbol", _REQUIRED_SYMBOLS + _PASSING_EXTRA_SYMBOLS)
def test_every_symbol_publishes_a_clean_book(tmp_path, symbol):
    """A symbol that passes the guard publishes under canonical names as RANDOM.

    The designer tree audits clean, readers open metrics.json and
    leaderboard.csv by those names, and in-book ids equal the directory names.
    The operator leaderboard keeps the original strategy id.
    """
    from backtester.report import load_report, read_leaderboard

    profile, benchmarks = _publish_profile(symbol)
    if symbol == "2":
        pack, sidecar, _built = _pack_for_ticker_two(tmp_path, "sym", 1.5)
    else:
        pack, sidecar, _built = _anon_pack(
            tmp_path, symbol, name="pack", price_factor=1.25, profile=profile
        )
    strategy_id = f"my_{symbol}_v2"
    out = tmp_path / "out"
    result = run_parallel(
        [
            _strategy(tmp_path / "strategies", "flat", FLAT),
            _strategy(tmp_path / "strategies", strategy_id, FLAT),
        ],
        [PackMarket(str(pack), str(sidecar))],
        out,
        profile=profile,
        workers=2,
        timeout_s=90,
        with_benchmarks=benchmarks,
        index_path=tmp_path / "runs.json",
    )
    assert result.failures == 0, (symbol, [(item.strategy_id, item.status, item.error) for item in result.results])
    operator = read_leaderboard(out / "leaderboard.csv")
    assert {row["strategy_id"] for row in operator} == {"flat", strategy_id}
    assert {row["status"] for row in operator} == {"ok"}
    for row in operator:
        assert row["total_return"] != ""
    _assert_operator_names(out, benchmarks=benchmarks, profile=profile)
    designer = out / "designer"
    board = _assert_ids_match_directories(designer)
    assert {row["status"] for row in board} == {"ok"}
    assert {row["strategy_id"] for row in board} == {"flat", "my_RANDOM_v2"}
    _assert_designer_names(designer, [symbol])
    assert_designer_clean(designer, symbol="")
    assert_designer_clean(designer, symbol=symbol)
    for name in ("metrics.json", "equity_curve.csv", "equity.csv", "equity.png", "leaderboard.csv"):
        assert any(path.name == name for path in designer.rglob("*")), name
    viewed = load_report(out, audience="designer")
    assert viewed["rows"]
    assert {row["status"] for row in viewed["rows"]} == {"ok"}
    job = next(
        path
        for path in (out / "jobs" / "flat").iterdir()
        if path.is_dir() and (path / "metrics.json").is_file()
    )
    book = load_report(job, audience="designer")
    assert book["metrics"]["audience"] == "designer"
    assert (job / "designer" / "metrics.json").is_file()
    assert (job / "designer" / "equity_curve.csv").is_file()
    assert (job / "designer" / "equity.png").is_file()
    if any(char.isalpha() for char in symbol):
        assert symbol in (job / "metrics.json").read_text(encoding="utf-8")
    assert (out / "jobs" / strategy_id).is_dir()


def test_a_reserved_symbol_rejects_the_batch_before_output(tmp_path):
    """EQUITY is reserved. The batch does not start and leaves no tree."""
    from backtester.pack import load_sidecar

    aapl_pack, aapl_side, _built = _anon_pack(tmp_path, "AAPL", name="aapl", price_factor=1.2)
    secret = load_sidecar(aapl_side)
    secret["symbol"] = "EQUITY"
    aapl_side.write_text(json.dumps(secret), encoding="utf-8")
    out = tmp_path / "out"
    with pytest.raises(ValueError, match="Symbol 'EQUITY' collides with a reserved report name"):
        run_parallel(
            [_strategy(tmp_path / "strategies", "flat", FLAT)],
            [PackMarket(str(aapl_pack), str(aapl_side))],
            out,
            profile="arena",
            workers=1,
            timeout_s=30,
            with_benchmarks=False,
        )
    assert not out.exists()


def test_setsid_cannot_move_a_grandchild_out_of_the_worker_group(tmp_path):
    """The worker filter still denies setsid, and a jailed strategy cannot spawn.

    The worker installs the filter after its own setsid and after the jail is
    up. A grandchild of the worker, not of the strategy, stays in the group.
    The strategy process is the jail, which does not allow the spawn at all.
    """
    if sys.platform != "linux":
        pytest.skip("setsid is the Linux process-group escape")
    marker = tmp_path / "setsid.json"
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "os.setsid()\n"
        "from backtester.sandbox import lock_worker_session\n"
        "lock_worker_session()\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        "    escaped = False\n"
        "    err = 0\n"
        "    try:\n"
        "        os.setsid()\n"
        "        escaped = True\n"
        "    except OSError as exc:\n"
        "        err = int(exc.errno)\n"
        "    Path(sys.argv[1]).write_text(json.dumps({'pid': os.getpid(), 'escaped': escaped, 'errno': err}))\n"
        "    os._exit(0)\n"
        "os.waitpid(pid, 0)\n",
        encoding="utf-8",
    )
    completed = subprocess.run([sys.executable, str(probe), str(marker)], check=False, capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(marker.read_text(encoding="utf-8"))
    assert payload["escaped"] is False
    assert payload["errno"] == 1
    assert _wait_dead(payload["pid"]), f"grandchild {payload['pid']} survived setsid"

    host = tmp_path / "host.json"
    source = (
        "import subprocess, sys\n"
        "from pathlib import Path\n"
        "class Strategy:\n"
        "    def on_bar(self, bar, state):\n"
        f"        subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        f"        Path({str(host)!r}).write_text('spawned')\n"
        "        return None\n"
    )
    _strategy(tmp_path / "setsid", "orphan", source)
    result = run_parallel(
        discover_strategies(tmp_path / "setsid"),
        [FrameMarket("book", "TEST", frame=_frame())],
        tmp_path / "out",
        profile="arena",
        workers=1,
        timeout_s=30,
        with_benchmarks=False,
    )
    assert result.results[0].status == "error", (result.results[0].status, result.results[0].error)
    assert not host.is_file()
