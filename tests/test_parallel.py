"""Parallel scoring: same numbers, one bad strategy, and a pack that stays anonymous."""

from __future__ import annotations

import gzip
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest
from pathlib import PureWindowsPath

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
    rows = [
        {"status": "ok", "excess_total_return": 0.1, "strategy_id": "b", "source_id": "s"},
        {"status": "ok", "excess_total_return": 0.5, "strategy_id": "a", "source_id": "s"},
        {"status": "ok", "excess_total_return": 0.5, "strategy_id": "a", "source_id": "t"},
        {"status": "error", "excess_total_return": 9.0, "strategy_id": "c", "source_id": "s"},
        {"status": "ok", "excess_total_return": float("nan"), "strategy_id": "d", "source_id": "s"},
    ]
    rows.sort(key=_rank_key)
    assert [(row["strategy_id"], row["source_id"]) for row in rows] == [
        ("a", "s"),
        ("a", "t"),
        ("b", "s"),
        ("d", "s"),
        ("c", "s"),
    ]


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
        "import json, os, sys\n"
        "from pathlib import Path\n"
        f"Path({str(seen_dir / 'one.json')!r}).write_text(json.dumps({{'argv': sys.argv, 'cwd': os.getcwd(), 'canary': os.environ.get('BT_CANARY'), 'listing': sorted(os.listdir(os.getcwd()))}}))\n"
        "Path('marker.txt').write_text('one')\n"
        "class Strategy:\n"
        "    def on_bar(self, bar, state):\n"
        "        return None\n"
    )
    reader = (
        "import json, os, sys, time\n"
        "from pathlib import Path\n"
        "class Strategy:\n"
        "    def on_bar(self, bar, state):\n"
        "        time.sleep(0.3)\n"
        f"        Path({str(seen_dir / 'two.json')!r}).write_text(json.dumps({{'argv': sys.argv, 'cwd': os.getcwd(), 'canary': os.environ.get('BT_CANARY'), 'listing': sorted(os.listdir(os.getcwd()))}}))\n"
        "        return None\n"
    )
    strategies = [
        _strategy(tmp_path / "s", "probe", probe),
        _strategy(tmp_path / "s", "reader", reader),
    ]
    recorded = []
    real = runner_mod._isolated_books

    def spy(strategy_cls, frame, profile, **kwargs):
        recorded.append({"kwargs": dict(kwargs), "csv": frame.to_csv(index=False)})
        return real(strategy_cls, frame, profile, **kwargs)

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
    one = json.loads((seen_dir / "one.json").read_text(encoding="utf-8"))
    two = json.loads((seen_dir / "two.json").read_text(encoding="utf-8"))
    assert one["argv"] == ["backtester-design"]
    assert two["argv"] == ["backtester-design"]
    assert one["canary"] is None and two["canary"] is None
    assert one["cwd"] != two["cwd"]
    assert "marker.txt" not in two["listing"]
    assert str(src) not in one["cwd"]
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
