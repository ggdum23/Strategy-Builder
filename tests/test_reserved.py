"""Reserved report names are rejected before an anonymized run writes anything.

Fixed designer and pack files keep their canonical names. The designer-facing
stand-in for a ticker is RANDOM. A raw operator run is not checked.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from backtester.__main__ import main
from backtester.pack import build_pack
from backtester.accounting import EXACT_RESERVED
from backtester.report import (
    RESERVED_VOCABULARY,
    TICKER_STAND_IN,
    load_report,
    require_anonymizable_symbol,
    reserved_symbol_conflict,
)
from backtester.runner import run_pack
from windows_open import OWNER_PC, runner_timeout

ROOT = Path(__file__).resolve().parents[1]
_README_SYMBOLS = (
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
_REJECTED = (
    "GZ",
    "JSON",
    "MANIFEST",
    "METRICS",
    "CSV",
    "TBL",
    "EQUITY",
    "PNG",
    "CURVE",
    "BUY",
    "HOLD",
    "LONG",
    "BARS",
    "PART",
    "RTH",
    "RANDOM",
    "random",
)


def _reserved_message(symbol: str) -> str:
    return (
        f"Symbol {symbol!r} collides with a reserved report name and cannot be anonymized; "
        "run it as an operator (raw) book or rename the symbol in the data."
    )


@pytest.mark.parametrize("symbol", _README_SYMBOLS + ("1", "2", "10", "SS", "FI", "AS", "ET", "asset", "SESSION", "RETURN"))
def test_readme_symbols_pass_the_guard(symbol):
    assert reserved_symbol_conflict(symbol) is None
    require_anonymizable_symbol(symbol)


@pytest.mark.parametrize("symbol", _REJECTED)
def test_reserved_symbols_are_rejected_up_front(symbol):
    assert reserved_symbol_conflict(symbol) is not None
    with pytest.raises(ValueError, match="reserved report name") as caught:
        require_anonymizable_symbol(symbol)
    assert str(caught.value) == _reserved_message(symbol)


def test_the_stand_in_is_random_and_reserved():
    assert TICKER_STAND_IN == "RANDOM"
    assert "random" in RESERVED_VOCABULARY
    assert "asset" not in RESERVED_VOCABULARY
    assert "book" not in RESERVED_VOCABULARY


def test_readme_lists_every_reserved_token_and_the_exit_code():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "exits 2" in text
    assert "RANDOM" in text
    for token in RESERVED_VOCABULARY:
        assert f"`{token}`" in text, token
    for token in EXACT_RESERVED:
        assert f"`{token}`" in text, token


def test_directory_component_reads_the_stand_in_constant(monkeypatch):
    from backtester import report
    from backtester.parallel import _designer_component

    monkeypatch.setattr(report, "TICKER_STAND_IN", "SENTINEL")
    assert report.designer_visible_name(["COST"]) == "SENTINEL"
    assert report.scrub_symbols("COST", ["COST"]) == "SENTINEL"
    assert _designer_component("bad/name", ["QQQ"], set()) == "SENTINEL"


@pytest.mark.parametrize("symbol", _REJECTED)
def test_anonymized_pack_rejects_before_any_file_is_written(tmp_path, symbol):
    pack = tmp_path / "pack"
    sidecar = tmp_path / "secret.json"
    with pytest.raises(ValueError, match="reserved report name") as caught:
        build_pack(
            tmp_path / "missing",
            symbol,
            pack,
            sidecar,
            anonymize=True,
            price_factor=1.5,
            volume_factor=0.5,
        )
    assert str(caught.value) == _reserved_message(symbol)
    assert not pack.exists()
    assert not sidecar.exists()


@pytest.mark.parametrize("symbol", ("CSV", "RANDOM", "METRICS", "GZ"))
def test_cli_pack_exits_2_without_a_traceback(tmp_path, capsys, symbol):
    code = main(
        [
            "pack",
            "--ticker",
            symbol,
            "--src",
            str(tmp_path / "src"),
            "--out",
            str(tmp_path / "out"),
            "--sidecar",
            str(tmp_path / "side.json"),
            "--registry",
            str(tmp_path / "registry.json"),
            "--anonymize",
        ]
    )
    captured = capsys.readouterr()
    assert code == 2
    assert "Traceback" not in captured.err
    assert _reserved_message(symbol) in captured.err
    assert captured.err.startswith("error: ")
    assert not (tmp_path / "out").exists()
    assert not (tmp_path / "side.json").exists()


def test_cli_process_rejects_csv_without_a_traceback(tmp_path):
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "backtester",
            "pack",
            "--ticker",
            "CSV",
            "--src",
            str(tmp_path / "src"),
            "--out",
            str(tmp_path / "out"),
            "--sidecar",
            str(tmp_path / "side.json"),
            "--registry",
            str(tmp_path / "registry.json"),
            "--anonymize",
        ],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 2
    assert "Traceback" not in proc.stderr
    assert _reserved_message("CSV") in proc.stderr
    assert not (tmp_path / "out").exists()


def test_run_pack_rejects_a_reserved_symbol_before_output(tmp_path):
    from test_parallel import _anon_pack

    pack, sidecar, _built = _anon_pack(tmp_path, "NIK", name="nik", price_factor=1.4)
    secret = json.loads(sidecar.read_text(encoding="utf-8"))
    secret["symbol"] = "CSV"
    sidecar.write_text(json.dumps(secret), encoding="utf-8")
    out = tmp_path / "book"

    class Strategy:
        def on_bar(self, bar, state):
            return None

    with pytest.raises(ValueError, match="Symbol 'CSV' collides with a reserved report name"):
        run_pack(Strategy, pack, sidecar, "arena", out, with_benchmarks=False)
    assert not out.exists()


@OWNER_PC
def test_a_passing_symbol_publishes_canonical_benchmark_names(tmp_path):
    from backtester.parallel import PackMarket, run_parallel
    from backtester.report import assert_designer_clean
    from test_parallel import FLAT, _anon_pack, _strategy

    pack, sidecar, _built = _anon_pack(tmp_path, "QQQ", name="qqq", price_factor=1.2, profile="desk")
    out = tmp_path / "out"
    result = run_parallel(
        [_strategy(tmp_path / "strategies", "flat", FLAT)],
        [PackMarket(str(pack), str(sidecar))],
        out,
        profile="desk",
        workers=1,
        timeout_s=runner_timeout(2, 90, deadline=True),
        with_benchmarks=True,
    )
    assert result.failures == 0, result.results[0].error
    designer = out / "designer"
    assert (designer / "leaderboard.csv").is_file()
    assert any(path.name == "buy_and_hold_1x" for path in designer.rglob("*") if path.is_dir())
    assert any(path.name == "long_2x" for path in designer.rglob("*") if path.is_dir())
    assert any(path.name == "equity_curve.csv" for path in designer.rglob("*"))
    viewed = load_report(out, audience="designer")
    assert viewed["rows"] and viewed["rows"][0]["strategy_id"] == "flat"
    assert_designer_clean(designer, symbol="QQQ")
    operator_metrics = next(
        path for path in (out / "jobs").rglob("metrics.json") if "designer" not in path.parts
    )
    assert "QQQ" in operator_metrics.read_text(encoding="utf-8")
