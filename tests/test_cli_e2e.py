"""Command line on the real October 2026 sample, from a directory that is not the repo."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from backtester.__main__ import main

ROOT = Path(__file__).resolve().parents[1]
QQQ = ROOT / "assets" / "sample" / "QQQ" / "filled"
SPY = ROOT / "assets" / "sample" / "SPY" / "filled"
D02 = ROOT / "examples" / "strategies" / "D02" / "strategy.py"
HOLD = ROOT / "examples" / "desk" / "buy_and_hold.py"


def _run(args, cwd: Path):
    return subprocess.run(
        [sys.executable, "-m", "backtester", *args],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )


def test_stubs_return_2_without_writing():
    for command in ("pack", "parallel", "referee", "report"):
        assert main([command]) == 2


def test_arena_sample_month(tmp_path):
    out = tmp_path / "arena"
    proc = _run(
        ["run", "--profile", "arena", "--ticker", "QQQ", "--strategy", str(D02), "--src", str(QQQ), "--out", str(out)],
        tmp_path,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads((out / "metrics.json").read_text(encoding="utf-8"))
    assert payload["profile"] == "arena"
    assert payload["symbol"] == "QQQ"
    assert payload["n_fills"] >= 1
    assert payload["final_equity"] > 0
    assert "long_2x" not in payload["benchmarks"]
    bench = payload["benchmarks"]["buy_and_hold_1x"]
    assert bench["n_fills"] == 1
    assert payload["financing_rate_annual"] == 0.0
    bench_file = json.loads((out / "benchmarks" / "buy_and_hold_1x" / "metrics.json").read_text(encoding="utf-8"))
    assert bench_file["financing_rate_annual"] == 0.0
    assert bench_file["n_fills"] == 1
    curve = (out / "equity_curve.csv").read_text(encoding="utf-8").strip().splitlines()
    assert len(curve) == 23  # header + 22 sessions
    assert "2026-10-01" in curve[1]
    fills = (out / "fills.csv").read_text(encoding="utf-8")
    assert "shares_delta" in fills
    assert (out / "trades.csv").is_file()
    assert (out / "benchmarks" / "buy_and_hold_1x" / "metrics.json").is_file()
    inspection = payload["run"]["inspection"]
    assert inspection["share_factor"] == 1.0
    assert inspection["price_adjustment"] == "raw"
    assert inspection["n_rows"] == 21120
    assert inspection["first_timestamp_et"].startswith("2026-10-01T04:00:00")


def test_desk_sample_month_benchmarks(tmp_path):
    out = tmp_path / "desk"
    proc = _run(
        ["run", "--profile", "desk", "--ticker", "SPY", "--strategy", str(HOLD), "--src", str(SPY), "--out", str(out)],
        tmp_path,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads((out / "metrics.json").read_text(encoding="utf-8"))
    assert payload["n_fills"] == 1
    assert payload["financing_rate_annual"] == 0.07
    assert payload["borrow_rate_annual"] == 0.0025
    assert payload["disqualified"] is False
    one = payload["benchmarks"]["buy_and_hold_1x"]
    two = payload["benchmarks"]["long_2x"]
    assert one["n_fills"] == 1
    assert two["n_fills"] == 1
    assert two["financing_cost"] > 0.0
    assert two["financing_cost"] > one["financing_cost"]
    assert payload["financing_cost"] == one["financing_cost"]
    assert (out / "benchmarks" / "long_2x" / "equity_curve.csv").is_file()
    curve = (out / "equity_curve.csv").read_text(encoding="utf-8").strip().splitlines()
    assert len(curve) == 23
    assert "2026-10-01" in curve[1]


def test_walkforward_on_the_sample_has_no_default_cut(tmp_path):
    out = tmp_path / "walk"
    missing = _run(
        ["walkforward", "--profile", "arena", "--ticker", "QQQ", "--strategy", str(D02), "--src", str(QQQ), "--out", str(out)],
        tmp_path,
    )
    assert missing.returncode == 2
    proc = _run(
        [
            "walkforward", "--profile", "arena", "--ticker", "QQQ", "--strategy", str(D02),
            "--src", str(QQQ), "--out", str(out), "--design-end-date", "2026-10-16", "--warmup-days", "3",
        ],
        tmp_path,
    )
    assert proc.returncode == 0, proc.stderr
    summary = json.loads((out / "walkforward.json").read_text(encoding="utf-8"))
    assert summary["design_end_date"] == "2026-10-16"
    assert summary["locked_start_date"] == "2026-10-19"
    assert summary["design_sessions"] == 12
    assert summary["locked_sessions"] == 10
    assert summary["warmup_sessions"] == 3
    assert summary["design_days_max"] < summary["locked_days_min"]
    assert summary["warmup_rule"] == "rescale_keep_exposure"
    assert (out / "design" / "metrics.json").is_file()
    assert (out / "locked" / "metrics.json").is_file()
    both = _run(
        [
            "walkforward", "--profile", "desk", "--ticker", "SPY", "--strategy", str(HOLD),
            "--src", str(SPY), "--out", str(tmp_path / "both"),
            "--design-end-date", "2026-10-16", "--design-fraction", "0.5",
        ],
        tmp_path,
    )
    assert both.returncode == 2


def test_ingest_and_guards(tmp_path):
    inspect_path = tmp_path / "inspect.json"
    proc = _run(["ingest", "--ticker", "QQQ", "--src", str(QQQ), "--out", str(inspect_path)], tmp_path)
    assert proc.returncode == 0, proc.stderr
    info = json.loads(inspect_path.read_text(encoding="utf-8"))
    assert info["n_sessions"] == 22
    assert info["share_factor"] == 1.0
    assert info["cost_schedule"] == "chosen at run time by --profile"
    warned = _run(
        [
            "run", "--profile", "desk", "--ticker", "SPY", "--strategy", str(HOLD),
            "--src", str(SPY), "--out", str(tmp_path / "warn"), "--share-factor", "0.9", "--bar-minutes", "30",
        ],
        tmp_path,
    )
    assert warned.returncode == 0, warned.stderr
    payload = json.loads((tmp_path / "warn" / "metrics.json").read_text(encoding="utf-8"))
    assert payload["share_factor"] == 0.9
    assert payload["run"]["inspection"]["share_factor"] == 1.0
    assert any("share_factor" in note for note in payload["run"]["warnings"])
    refused = _run(
        [
            "run", "--profile", "arena", "--ticker", "QQQ", "--strategy", str(D02),
            "--src", str(QQQ), "--out", str(tmp_path / "fee"), "--fee-mult", "2", "--bar-minutes", "60",
        ],
        tmp_path,
    )
    assert refused.returncode == 2
    assert "fee-mult" in refused.stderr
