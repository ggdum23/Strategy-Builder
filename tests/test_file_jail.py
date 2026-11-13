"""The operator process does not import a file strategy.

The marker is written at import, next to the file the loader opened. The jail
loads a copy, so a jailed import does not create that marker. A thread started
at import walks frames and reads any file named on argv. The snapshot of that
walk must not contain the sidecar.
"""

from __future__ import annotations

import gzip
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backtester.__main__ import main
from backtester.data import load_named
from backtester.pack import build_pack
from backtester.registry import entry_from_inspection, save_registry, upsert_series
from windows_open import windows_jails

ET = timezone(timedelta(hours=-4))
SOURCE = """
from pathlib import Path
import sys
import threading

_marker = Path(__file__).resolve().parent / "host-import-marker"
try:
    _marker.write_text("imported", encoding="utf-8")
except OSError:
    pass

_found = []

def _scan():
    chunks = []
    for frame in sys._current_frames().values():
        current = frame
        while current is not None:
            for value in current.f_locals.values():
                if isinstance(value, str) and len(value) > 8:
                    chunks.append(value[:2000])
            current = current.f_back
    for arg in list(sys.argv):
        try:
            path = Path(arg)
            if path.is_file() and path.stat().st_size < 500000:
                chunks.append(path.read_text(encoding="utf-8", errors="ignore")[:4000])
        except OSError:
            pass
    _found.append("\\n".join(chunks)[:100000])

_thread = threading.Thread(target=_scan, daemon=True)
_thread.start()

class Strategy:
    def on_bar(self, bar, state):
        _thread.join(timeout=1)
        self.argv = list(sys.argv)
        self.seen = _found[0] if _found else ""
        return None
"""


def _modules():
    return {name for name in sys.modules if name.startswith("backtester_user_strategy_")}


def _write_days(src: Path, symbol: str, days: int) -> None:
    src.mkdir(parents=True, exist_ok=True)
    header = [
        "timestamp_et", "epoch_utc", "open", "high", "low", "close",
        "volume", "wap", "count", "filled", "session",
    ]
    rows = []
    for day in range(days):
        stamp = datetime(2024, 6, 3, tzinfo=ET) + timedelta(days=day)
        for minute in (9 * 60 + 30, 9 * 60 + 31):
            when = stamp + timedelta(minutes=minute)
            price = 20.0 + day
            rows.append(
                [
                    when.isoformat(), str(int(when.timestamp())),
                    str(price), str(price + 0.2), str(price - 0.2), str(price + 0.1),
                    "100", str(price), "1", "0", "rth",
                ]
            )
    path = src / f"{symbol}_1min_filled_2024-06.csv.gz"
    with gzip.open(path, "wt", encoding="utf-8", newline="\n") as handle:
        handle.write(",".join(header) + "\n")
        for row in rows:
            handle.write(",".join(row) + "\n")


def _strategy(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "strategy.py"
    path.write_text(SOURCE, encoding="utf-8")
    return path


def _assert_clean(path: Path, before: set[str], observers: list) -> None:
    marker = path.parent / "host-import-marker"
    assert not marker.exists()
    assert _modules() == before
    assert observers, "the jail did not return a strategy snapshot"
    for observer in observers:
        assert list(getattr(observer, "argv", [])) == ["strategy"]
        seen = str(getattr(observer, "seen", ""))
        assert "share_scale" not in seen
        assert "spill_key" not in seen
        assert "NIK" not in seen


@pytest.fixture
def capture_books(monkeypatch):
    from backtester import runner

    found = []
    real = runner._isolated_books

    def wrapped(*args, **kwargs):
        books = real(*args, **kwargs)
        found.append(books["strategy"][0].get("observer"))
        return books

    monkeypatch.setattr(runner, "_isolated_books", wrapped)
    return found


def _prepared_sample(tmp_path):
    src = tmp_path / "src"
    _write_days(src, "TEST", 4)
    # The anonymized sidecar carries the real symbol. The strategy must not
    # see that file.
    nik = tmp_path / "nik"
    _write_days(nik, "NIK", 2)
    nik_loaded = load_named(nik, "NIK")
    nik_registry = tmp_path / "nik-registry.json"
    nik_entry = entry_from_inspection(nik_loaded.inspection, source_root=str(nik), profile="arena")
    save_registry(nik_registry, upsert_series({"schema_version": 1, "series": []}, nik_entry))
    nik_pack = tmp_path / "nik-pack"
    nik_side = tmp_path / "nik-side.json"
    build_pack(
        nik, "NIK", nik_pack, nik_side,
        anonymize=True, registry_path=nik_registry, profile="arena",
        price_factor=1.7, volume_factor=0.4,
    )
    return src, nik_pack, nik_side


@pytest.mark.parametrize("kind", ["run", "pack", "walkforward", "parallel"])
@windows_jails(2)
def test_cli_paths_do_not_import_the_strategy(tmp_path, capsys, capture_books, kind):
    src, nik_pack, nik_side = _prepared_sample(tmp_path)
    before = _modules()
    if kind == "run":
        strategy = _strategy(tmp_path / "run-strategy")
        code = main(
            [
                "run", "--profile", "arena", "--ticker", "TEST",
                "--strategy", str(strategy), "--src", str(src),
                "--out", str(tmp_path / "run-out"),
            ]
        )
    elif kind == "pack":
        strategy = _strategy(tmp_path / "pack-strategy")
        code = main(
            [
                "run", "--profile", "arena", "--strategy", str(strategy),
                "--pack", str(nik_pack), "--sidecar", str(nik_side),
                "--out", str(tmp_path / "pack-out"),
            ]
        )
    elif kind == "walkforward":
        strategy = _strategy(tmp_path / "walk-strategy")
        code = main(
            [
                "walkforward", "--profile", "arena", "--ticker", "TEST",
                "--strategy", str(strategy), "--src", str(src),
                "--out", str(tmp_path / "walk-out"), "--design-end-day", "1",
            ]
        )
    else:
        strategy = _strategy(tmp_path / "parallel-strategy")
        code = main(
            [
                "parallel", "--strategies", str(strategy.parent), "--workers", "1",
                "--profile", "arena", "--ticker", "TEST", "--src", str(src),
                "--out", str(tmp_path / "parallel-out"), "--no-benchmarks",
            ]
        )
    assert code == 0, capsys.readouterr()
    _assert_clean(strategy, before, capture_books)


def test_unsafe_in_process_warns_and_refuses_a_pack(tmp_path, capsys):
    src = tmp_path / "src"
    _write_days(src, "TEST", 2)
    strategy = _strategy(tmp_path / "unsafe")
    code = main(
        [
            "run", "--profile", "arena", "--ticker", "TEST",
            "--strategy", str(strategy), "--src", str(src),
            "--out", str(tmp_path / "out"), "--unsafe-in-process",
        ]
    )
    captured = capsys.readouterr()
    assert code == 0, captured
    assert "WARNING: --unsafe-in-process" in captured.err
    assert (strategy.parent / "host-import-marker").is_file()
    refused = main(
        [
            "run", "--profile", "arena", "--strategy", str(strategy),
            "--pack", str(tmp_path / "missing-pack"),
            "--sidecar", str(tmp_path / "missing-side.json"),
            "--out", str(tmp_path / "pack-out"),
            "--unsafe-in-process",
        ]
    )
    assert refused == 2
    assert "cannot run a pack" in capsys.readouterr().err


@windows_jails(1)
def test_worker_failure_is_exit_2_without_a_traceback(tmp_path, capsys):
    src = tmp_path / "src"
    _write_days(src, "TEST", 2)
    strategy = tmp_path / "boom" / "strategy.py"
    strategy.parent.mkdir()
    strategy.write_text(
        "class Strategy:\n"
        "    def on_bar(self, bar, state):\n"
        "        raise RuntimeError('boom-cli-isolated')\n",
        encoding="utf-8",
    )
    code = main(
        [
            "run", "--profile", "arena", "--ticker", "TEST",
            "--strategy", str(strategy), "--src", str(src),
            "--out", str(tmp_path / "out"),
        ]
    )
    captured = capsys.readouterr()
    assert code == 2
    assert "boom-cli-isolated" in captured.err
    assert "Traceback" not in captured.err
