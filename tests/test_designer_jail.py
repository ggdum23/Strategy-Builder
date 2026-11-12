"""File-backed design, pack, and parallel runs execute the strategy in the jail.

The worker keeps the engine, the accounting record, and that window's
dividends. The strategy process does not.
"""

from __future__ import annotations

import gzip
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from backtester.data import load_named
from backtester.pack import build_pack
from backtester.parallel import FrameMarket, StrategyRef, run_parallel
from backtester.registry import entry_from_inspection, save_registry, upsert_series
from backtester.runner import walk_forward
from backtester.strategy_loader import load_strategy_class
from windows_open import OWNER_PC

ROOT = Path(__file__).resolve().parents[1]
SELFTEST = ROOT / "examples" / "selftest"
ET = timezone(timedelta(hours=-4))


def _frame(n_days: int = 4) -> pd.DataFrame:
    rows = []
    for day in range(n_days):
        date = (datetime(2024, 6, 3) + timedelta(days=day)).date().isoformat()
        for minute in (570, 571):
            rows.append(
                {
                    "timestamp_et": f"{date}T{minute // 60:02d}:{minute % 60:02d}:00-04:00",
                    "epoch_utc": 1_700_000_000 + day * 100_000 + minute,
                    "date": date,
                    "day": day,
                    "weekday": datetime.fromisoformat(date).weekday(),
                    "minute": minute,
                    "time": f"{minute // 60:02d}:{minute % 60:02d}",
                    "session": "rth",
                    "open": 100.0,
                    "high": 101.0,
                    "low": 99.0,
                    "close": 100.0,
                    "volume": 1000.0,
                    "wap": 100.0,
                    "count": 1,
                    "filled": 0,
                    "short_session": 0,
                }
            )
    return pd.DataFrame(rows)


def _write(path: Path, source: str) -> type:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return load_strategy_class(path)


def _fills(books) -> int:
    return int(books["strategy"][0]["n_fills"])


def _walk(strategy, frame, tmp_path: Path, name: str, *, dividends=None, price_basis="raw"):
    return walk_forward(
        strategy,
        frame,
        "arena",
        tmp_path / name,
        symbol="TEST",
        bar_minutes=1,
        design_end_day=1,
        warmup_days=0,
        dividends=dividends,
        price_basis=price_basis,
        with_benchmarks=False,
    )


def _pack(tmp_path: Path, strategy, name: str):
    day = datetime(2024, 3, 4, tzinfo=ET)
    rows = []
    for offset, minute in enumerate((9 * 60 + 30, 9 * 60 + 31)):
        stamp = day + timedelta(minutes=minute)
        price = 15.0 + offset
        rows.append(
            {
                "timestamp_et": stamp.isoformat(),
                "epoch_utc": int(stamp.timestamp()),
                "open": price,
                "high": price + 0.4,
                "low": price - 0.4,
                "close": price + 0.1,
                "volume": 50,
                "wap": price,
                "count": 2,
                "filled": 0,
                "session": "rth",
            }
        )
    src = tmp_path / name / "src"
    path = src / "NIK_1min_filled_2024-03.csv.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    header = [
        "timestamp_et", "epoch_utc", "open", "high", "low", "close",
        "volume", "wap", "count", "filled", "session",
    ]
    lines = [",".join(header)]
    for row in rows:
        lines.append(",".join(str(row[column]) for column in header))
    with gzip.open(path, "wt", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines) + "\n")
    loaded = load_named(src, "NIK")
    registry = tmp_path / name / "registry.json"
    entry = entry_from_inspection(loaded.inspection, source_root=str(src), profile="arena")
    save_registry(registry, upsert_series({"schema_version": 1, "series": []}, entry))
    pack = tmp_path / name / "pack"
    sidecar = tmp_path / name / "side.json"
    build_pack(
        src, "NIK", pack, sidecar,
        anonymize=True, registry_path=registry, profile="arena",
        price_factor=1.7, volume_factor=0.4,
    )
    from backtester.runner import run_pack

    return run_pack(strategy, pack, sidecar, "arena", tmp_path / name / "out", with_benchmarks=False)


def _parallel(tmp_path: Path, strategy_path: Path, name: str):
    result = run_parallel(
        [StrategyRef(name, str(strategy_path))],
        [FrameMarket("book", "TEST", frame=_frame())],
        tmp_path / name,
        profile="arena",
        workers=1,
        timeout_s=60,
        with_benchmarks=False,
    )
    assert result.results[0].status == "ok", (result.results[0].status, result.results[0].error)
    metrics = json.loads((Path(result.results[0].out_dir) / "metrics.json").read_text(encoding="utf-8"))
    return metrics


@OWNER_PC
def test_absolute_read_arm_memory_and_network_stay_in_the_jail(tmp_path):
    secret = tmp_path / "reader" / "secret.txt"
    secret.parent.mkdir(parents=True)
    secret.write_text("SB-JAIL-MARKER", encoding="utf-8")
    reader = _write(
        tmp_path / "reader" / "strategy.py",
        "from pathlib import Path\n"
        f"SECRET = {str(secret)!r}\n"
        "class Strategy:\n"
        "    def __init__(self):\n"
        "        self.leaked = False\n"
        "    def on_bar(self, bar, state):\n"
        "        if not self.leaked:\n"
        "            try:\n"
        "                self.leaked = 'SB-JAIL-MARKER' in Path(SECRET).read_text(encoding='utf-8')\n"
        "            except OSError:\n"
        "                self.leaked = False\n"
        "        return 1.0 if self.leaked else 0.0\n",
    )
    _summary, _slices, design, locked = _walk(reader, _frame(), tmp_path, "walk-read")
    assert design["strategy"][0]["observer"].leaked is False
    assert locked["strategy"][0]["observer"].leaked is False
    assert _fills(design) == 0 and _fills(locked) == 0
    packed = _pack(tmp_path, reader, "pack-read")
    assert packed["strategy_observer"].leaked is False
    assert packed["n_fills"] == 0
    assert _parallel(tmp_path, tmp_path / "reader" / "strategy.py", "parallel-read")["n_fills"] == 0

    flag = tmp_path / "arm" / "arm-flag.txt"
    armed = _write(
        tmp_path / "arm" / "strategy.py",
        "from pathlib import Path\n"
        f"FLAG = {str(flag)!r}\n"
        "class Strategy:\n"
        "    def __init__(self):\n"
        "        self.leaked = False\n"
        "    def on_bar(self, bar, state):\n"
        "        if not self.leaked:\n"
        "            path = Path(FLAG)\n"
        "            try:\n"
        "                path.write_text('armed', encoding='utf-8')\n"
        "            except OSError:\n"
        "                pass\n"
        "            try:\n"
        "                self.leaked = path.read_text(encoding='utf-8') == 'armed'\n"
        "            except OSError:\n"
        "                self.leaked = False\n"
        "        return 1.0 if self.leaked else 0.0\n",
    )
    _summary, _slices, design, locked = _walk(armed, _frame(), tmp_path, "walk-arm")
    assert design["strategy"][0]["observer"].leaked is False
    assert locked["strategy"][0]["observer"].leaked is False
    assert not flag.exists()
    assert _fills(design) == 0 and _fills(locked) == 0
    packed = _pack(tmp_path, armed, "pack-arm")
    assert packed["strategy_observer"].leaked is False
    assert not flag.exists()
    assert _parallel(tmp_path, tmp_path / "arm" / "strategy.py", "parallel-arm")["n_fills"] == 0
    assert not flag.exists()

    memory = load_strategy_class(SELFTEST / "parentmem.py")
    _summary, _slices, design, locked = _walk(memory, _frame(), tmp_path, "walk-mem")
    assert _fills(design) == 0 and _fills(locked) == 0
    assert _pack(tmp_path, memory, "pack-mem")["n_fills"] == 0
    assert _parallel(tmp_path, SELFTEST / "parentmem.py", "parallel-mem")["n_fills"] == 0

    network = load_strategy_class(SELFTEST / "net.py")
    _summary, _slices, design, locked = _walk(network, _frame(), tmp_path, "walk-net")
    assert _fills(design) == 0 and _fills(locked) == 0
    assert _pack(tmp_path, network, "pack-net")["n_fills"] == 0
    assert _parallel(tmp_path, SELFTEST / "net.py", "parallel-net")["n_fills"] == 0


def test_a_jailed_walk_forward_does_not_receive_the_locked_ex_date(tmp_path):
    dates = ["2024-06-03", "2024-06-04", "2024-06-05", "2024-06-06"]
    rows = []
    for day, date in enumerate(dates):
        for minute in (570, 571):
            rows.append(
                {
                    "date": date,
                    "day": day,
                    "weekday": day,
                    "minute": minute,
                    "time": f"{minute // 60:02d}:{minute % 60:02d}",
                    "session": "rth",
                    "open": 100.0,
                    "high": 100.5,
                    "low": 99.5,
                    "close": 100.25,
                    "volume": 1000.0,
                    "wap": 100.0,
                    "count": 10,
                    "filled": 0,
                    "short_session": 0,
                }
            )
    frame = pd.DataFrame(rows)
    locked_date = "2024-06-06"
    locked_amount = 19.19
    strategy = _write(
        tmp_path / "spy" / "strategy.py",
        "import sys\n"
        "class Strategy:\n"
        "    def __init__(self):\n"
        "        self.seen = []\n"
        "    def on_bar(self, bar, state):\n"
        "        frame = sys._getframe()\n"
        "        while frame is not None:\n"
        "            for value in frame.f_locals.values():\n"
        "                if isinstance(value, dict) and value and all(\n"
        "                    isinstance(item, (int, float)) for item in value.values()\n"
        "                ):\n"
        "                    self.seen.append({str(key): value[key] for key in value})\n"
        "            frame = frame.f_back\n"
        "        return 1.0\n",
    )
    summary, _slices, design, locked = walk_forward(
        strategy,
        frame,
        "arena",
        tmp_path / "out",
        symbol="NIK",
        bar_minutes=1,
        design_end_day=1,
        warmup_days=1,
        dividends={"2024-06-04": 1.25, locked_date: locked_amount},
        price_basis="total_return",
        with_benchmarks=False,
    )
    assert summary["design_sessions"] == 2
    seen = design["strategy"][0]["observer"].seen
    assert seen
    for mapping in seen:
        assert locked_date not in mapping
        assert locked_amount not in mapping.values()
    assert design["strategy"][1]["dividends_net"] != 0.0
    locked_seen = locked["strategy"][0]["observer"].seen
    assert all(locked_date not in mapping for mapping in locked_seen)
    assert locked["strategy"][1]["dividends_net"] != 0.0
