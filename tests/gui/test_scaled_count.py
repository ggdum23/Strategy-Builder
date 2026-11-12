"""A fractional anonymized trade count stays fractional through the GUI."""

from __future__ import annotations

import csv
import gzip
import io
import json
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYTEST_QT_API", "pyside6")

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")
pytest.importorskip("pytestqt")

from PySide6.QtWidgets import QApplication

from backtester.gui.format import format_number
from backtester.gui.jobs import build_pack_job, execute_run, record_series
from backtester.pack import load_sidecar

from synthetic import write_filled
from test_window import _window


def _count_cells(pack: Path, name: str) -> list[str]:
    cells = []
    for path in sorted(pack.glob(name)):
        text = gzip.decompress(path.read_bytes()).decode("ascii")
        cells.extend(row["count"] for row in csv.DictReader(io.StringIO(text)))
    return cells


def _strategy(path: Path, log: Path, scaled: float) -> None:
    path.write_text(
        "class Strategy:\n"
        "    def on_bar(self, bar, state):\n"
        f"        handle = open({str(log)!r}, 'a', encoding='ascii')\n"
        "        handle.write(repr(float(bar['count'])) + '\\n')\n"
        "        handle.close()\n"
        f"        if float(bar['count']) + 1e-9 >= {scaled!r}:\n"
        "            return 1.0\n"
        "        return 0.0\n",
        encoding="utf-8",
    )


def _run(strategy: Path, pack: Path, sidecar: Path, profile: str, out: Path) -> dict:
    return execute_run(
        {
            "mode": "single",
            "profile": profile,
            "source": "pack",
            "strategies": [str(strategy)],
            "pack": str(pack),
            "sidecar": str(sidecar),
            "out": str(out),
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
    )


def _activity(page, label: str):
    model = page.activity.model()
    for row in range(model.rowCount()):
        if model.index(row, 0).data() == label:
            return model.index(row, 1).data()
    return None


def test_gui_pack_run_and_export_keep_a_fractional_count(qtbot, tmp_path):
    assert format_number("count", 1.26) == "1.26"
    assert format_number("count", 0.42) == "0.42"
    assert format_number("count", 3.0) == "3"

    src = write_filled(tmp_path / "Strategy Builder" / "QQQ" / "filled")
    registry = tmp_path / "registry.json"
    record_series(src, "QQQ", registry, profile="arena", price_kind="raw", bar_minutes=1)
    pack = tmp_path / "pack"
    sidecar = tmp_path / "operator" / "sidecar.json"
    built = build_pack_job(
        src,
        "QQQ",
        pack,
        sidecar,
        registry,
        anonymize=True,
        profile="arena",
        bar_minutes=1,
        price_kind="raw",
    )
    assert built["manifest"]["asset_name"] == "RANDOM"
    assert "volume_factor" not in built
    factor = float(load_sidecar(sidecar)["volume_factor"])
    cells = _count_cells(pack, "bars_part_*.csv.gz")
    assert cells
    assert set(cells) == {format(5 * factor, ".17g")}
    assert cells[0] != "5"
    scaled = float(cells[0])
    assert scaled == pytest.approx(5 * factor)
    daily = _count_cells(pack, "daily_rth.csv.gz")
    assert set(daily) == {format(4 * scaled, ".17g")}

    strategy = tmp_path / "count_signal.py"
    for profile in ("arena", "desk"):
        log = tmp_path / f"{profile}.log"
        _strategy(strategy, log, scaled)
        out = tmp_path / profile
        result = _run(strategy, pack, sidecar, profile, out)
        assert result["headline"]["n_fills"] == 1
        operator = json.loads((out / "metrics.json").read_text(encoding="utf-8"))
        designer = json.loads((out / "designer" / "metrics.json").read_text(encoding="utf-8"))
        assert operator["n_fills"] == 1
        assert designer["n_fills"] == 1
        seen = [float(line) for line in log.read_text(encoding="ascii").splitlines()]
        assert seen
        assert seen == pytest.approx([scaled] * len(seen))

        window = _window(qtbot, tmp_path)
        page = window.results_page
        page.load_path(str(out), audience="operator")
        QApplication.processEvents()
        assert _activity(page, "Fills") == "1"
        operator_export = tmp_path / f"export-{profile}-operator"
        written = page.export_to(operator_export)
        assert "metrics.json" in written
        assert json.loads((operator_export / "metrics.json").read_text(encoding="utf-8"))["n_fills"] == 1

        page.load_path(str(out), audience="designer")
        QApplication.processEvents()
        assert page._report.audience == "designer"
        assert _activity(page, "Fills") == "1"
        designer_export = tmp_path / f"export-{profile}-designer"
        page.export_to(designer_export)
        names = "\n".join(path.name for path in designer_export.rglob("*"))
        texts = []
        for path in designer_export.rglob("*"):
            if path.is_file() and path.suffix.lower() in {".json", ".csv", ".txt", ".md"}:
                texts.append(path.read_text(encoding="utf-8"))
        blob = names + "\n" + "\n".join(texts)
        for needle in ("QQQ", "2024-01-02", "price_factor", "volume_factor", "share_scale", "factor_key", "factor_mac"):
            assert needle not in blob
