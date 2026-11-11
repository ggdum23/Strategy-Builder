"""Report adapter, validation, and paths. No Qt."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from backtester.gui.backend import load_run
from backtester.gui.format import MISSING, format_number
from backtester.gui.preferences import as_path
from backtester.gui.schema import WITHHELD_KEYS
from backtester.gui.validate import config_record, transcript, validate_config

from synthetic import ARENA_METRICS, write_book

ROOT = Path(__file__).resolve().parents[2]


def _draft(**overrides):
    draft = {
        "mode": "single",
        "profile": "arena",
        "source": "named",
        "strategies": [str(ROOT / "examples" / "desk" / "buy_and_hold.py")],
        "src": str(ROOT / "assets" / "sample" / "QQQ" / "filled"),
        "ticker": "QQQ",
        "pack": "",
        "sidecar": "",
        "out": str(ROOT / "out" / "gui-draft"),
        "bar_minutes": 1,
        "initial_equity": 1_000_000,
        "borrow_rate_annual": None,
        "financing_rate_annual": None,
        "fee_mult": 1,
        "dividends": "",
        "warmup_sessions": 0,
        "warmup_days": 0,
        "design_end_day": None,
        "design_end_date": "",
        "design_fraction": None,
    }
    draft.update(overrides)
    return draft


def test_gui_package_does_not_import_qt():
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import backtester.gui, backtester.gui.backend, backtester.gui.jobs, backtester.gui.validate; "
            "import sys; raise SystemExit(1 if 'PySide6' in sys.modules else 0)",
        ],
        check=False,
        cwd=ROOT,
    )
    assert proc.returncode == 0


def test_gui_command_is_registered():
    from backtester.__main__ import _build_parser

    args = _build_parser().parse_args(["gui"])
    assert args.command == "gui"


def test_windows_path_with_spaces_is_one_path():
    text = r"C:\Users\Jeu\Desktop\Strategy Builder\SPY\filled"
    quoted = '"' + text + '"'
    assert as_path(quoted) == as_path(text)
    draft = _draft(src=text, out=r"C:\Users\Jeu\Desktop\Strategy Builder\out")
    record = config_record(draft)
    assert record["arguments"]["src"] == text
    assert " " in record["arguments"]["src"]
    rendered = transcript(draft)
    assert "Strategy Builder" in rendered
    assert "share_factor: null" in rendered
    assert "null  (engine default, not substituted)" in rendered


def test_arena_rejects_desk_switches_and_walkforward_needs_one_cut(tmp_path):
    strategy = ROOT / "examples" / "desk" / "buy_and_hold.py"
    src = ROOT / "assets" / "sample" / "QQQ" / "filled"
    base = _draft(strategies=[str(strategy)], src=str(src), out=str(tmp_path / "out"))
    assert validate_config(base) == []
    assert any("fee" in item.lower() for item in validate_config(_draft(fee_mult=2)))
    assert any("financing" in item.lower() for item in validate_config(_draft(financing_rate_annual=0.07)))
    assert any("dividend" in item.lower() for item in validate_config(_draft(dividends=str(strategy))))
    blank = _draft(profile="")
    assert any("profile" in item.lower() for item in validate_config(blank))
    walk = _draft(mode="walkforward")
    assert any("exactly one" in item for item in validate_config(walk))
    both = _draft(mode="walkforward", design_end_date="2024-01-02", design_fraction=0.5)
    assert any("exactly one" in item for item in validate_config(both))
    one = _draft(mode="walkforward", design_fraction=0.8)
    assert validate_config(one) == []


def test_pack_report_must_not_sit_inside_the_pack(tmp_path):
    pack = tmp_path / "pack"
    pack.mkdir()
    sidecar = tmp_path / "operator" / "sidecar.json"
    sidecar.parent.mkdir()
    sidecar.write_text("{}", encoding="utf-8")
    strategy = ROOT / "examples" / "desk" / "buy_and_hold.py"
    inside = validate_config(
        _draft(
            source="pack",
            pack=str(pack),
            sidecar=str(sidecar),
            out=str(pack / "results"),
            strategies=[str(strategy)],
        )
    )
    assert any("inside the pack" in item for item in inside)
    (pack / "sidecar.json").write_text("{}\n", encoding="utf-8")
    nested = validate_config(
        _draft(
            source="pack",
            pack=str(pack),
            sidecar=str(pack / "sidecar.json"),
            out=str(tmp_path / "out"),
            strategies=[str(strategy)],
        )
    )
    assert any("inside" in item for item in nested)


def test_missing_fields_stay_missing_and_sidecar_keys_are_withheld(tmp_path):
    metrics = {
        "profile": "arena",
        "symbol": "QQQ",
        "total_return": 0.04,
        "final_equity": 1_040_000,
        "price_factor": "CANARY_FACTOR",
        "volume_factor": 0.2,
        "date_map": {"1": "2020-01-02"},
        "operator_note": "do not show",
        "phase3_custom": 1.25,
        "benchmarks": {"buy_and_hold_1x": {"total_return": 0.5, "final_equity": 9}},
    }
    folder = write_book(tmp_path / "run", metrics, benchmark=True)
    (folder / "benchmarks" / "buy_and_hold_1x" / "metrics.json").write_text(
        json.dumps({"total_return": 0.02, "final_equity": 1_020_000, "initial_equity": 1_000_000}) + "\n",
        encoding="utf-8",
    )
    (folder / "sidecar.json").write_text(
        json.dumps({"volume_factor": "CANARY_SIDECAR", "date_map": {"1": "1999-01-01"}}) + "\n",
        encoding="utf-8",
    )
    report = load_run(folder)
    blob = json.dumps(report.metrics)
    assert "CANARY" not in blob
    assert "1999-01-01" not in blob
    for key in WITHHELD_KEYS:
        assert key not in report.metrics
    assert report.withheld
    assert report.other_fields["phase3_custom"] == 1.25
    assert any("disagree" in warning for warning in report.warnings)
    assert "sharpe" not in report.metrics
    assert format_number("ratio", report.metrics.get("sharpe")) == MISSING
    assert report.metrics["total_return"] == 0.04
    assert report.curves and report.curves[0].equity[0] == 1_000_000
    assert report.fills is not None and len(report.fills.rows) == 2
    assert report.figure is None
    assert "equity.png" in report.missing


def test_leaderboard_csv_is_preferred_and_batch_assembly_copies_metrics(tmp_path):
    parent = tmp_path / "batch"
    parent.mkdir()
    write_book(parent / "one", dict(ARENA_METRICS, symbol="QQQ"), benchmark=False)
    other = dict(ARENA_METRICS, symbol="SPY", total_return=0.01, final_equity=1_010_000)
    write_book(parent / "two", other, benchmark=False)
    assembled = load_run(parent)
    assert assembled.kind == "batch"
    assert assembled.leaderboard is not None
    assert "metrics.json" in assembled.leaderboard.source
    assert "leaderboard.csv" in assembled.missing
    returns = {row["symbol"]: row["total_return"] for row in assembled.leaderboard.rows}
    assert returns["QQQ"] == 0.04
    assert returns["SPY"] == 0.01
    (parent / "leaderboard.csv").write_text(
        "strategy,total_return,phase3_note\nalpha,0.2,kept\n",
        encoding="utf-8",
    )
    official = load_run(parent)
    assert official.leaderboard is not None
    assert official.leaderboard.source == "leaderboard.csv"
    assert official.leaderboard.columns == ("strategy", "total_return", "phase3_note")
    assert official.leaderboard.rows[0]["phase3_note"] == "kept"


def test_equity_csv_alias_and_desk_liquidation_file(tmp_path):
    folder = tmp_path / "desk"
    folder.mkdir()
    metrics = {
        "profile": "desk",
        "symbol": "SPY",
        "total_return": -0.1,
        "final_equity": 900_000,
        "initial_equity": 1_000_000,
        "disqualified": False,
        "financing_cost": 12.5,
    }
    (folder / "metrics.json").write_text(json.dumps(metrics) + "\n", encoding="utf-8")
    (folder / "equity.csv").write_text(
        "session,equity,exposure\nS00001,1000000,1\nS00002,900000,0\n",
        encoding="utf-8",
    )
    (folder / "liquidation.json").write_text(
        json.dumps({"session": "S00002", "equity": 900000, "volume_factor": "CANARY"}) + "\n",
        encoding="utf-8",
    )
    report = load_run(folder)
    assert report.curves and report.curves[0].source.endswith("equity.csv")
    assert report.curves[0].exposure == [1.0, 0.0]
    assert report.liquidation is not None
    assert "volume_factor" not in report.liquidation
    assert report.liquidation["session"] == "S00002"
    assert "CANARY" in report.withheld or any("volume_factor" in item for item in report.withheld)
