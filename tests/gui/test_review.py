"""Review fixes for log scale, designer export, and report display.

Each test asserts the behavior the review required. On the previous head the
log view, the percent formatting, the PNG copy, and the date scrub all fail.
"""

from __future__ import annotations

import json
import math
import os
import struct
import zlib
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYTEST_QT_API", "pyside6")

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")
pytest.importorskip("pytestqt")

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication, QLabel

from backtester.gui.audience import scrub_text
from backtester.gui.charts import ChartPanel
from backtester.gui.format import format_number
from backtester.gui.schema import leaderboard_kind
from synthetic import ARENA_METRICS, write_book, write_parallel_run
from test_window import _visible_blob, _window

# Phase 3 leaderboard at 3016825. Every column has a display kind.
PHASE3_COLUMNS = (
    "rank",
    "strategy_id",
    "source_id",
    "symbol",
    "profile",
    "status",
    "total_return",
    "excess_total_return",
    "bh_total_return",
    "long_2x_total_return",
    "cagr",
    "ann_vol",
    "sharpe",
    "sortino",
    "max_drawdown",
    "max_dd_duration_sessions",
    "calmar",
    "n_fills",
    "round_trips",
    "hit_rate",
    "avg_holding_sessions",
    "turnover_per_year",
    "fees_total",
    "best_day",
    "worst_day",
    "percent_up_days",
    "avg_abs_exposure_close",
    "frac_sessions_long_at_close",
    "frac_sessions_short_at_close",
    "long_round_trips",
    "short_round_trips",
    "financing_cost",
    "borrow_cost",
    "dividends_net",
    "margin_calls_filled",
    "max_gross_leverage",
    "avg_gross_leverage",
    "worst_equity_over_hwm",
    "disqualified",
    "timeout_s",
    "error",
)

# The row the review loaded from a real Phase 3 leaderboard.
REAL_ROW = {
    "total_return": 0.03995523164581805,
    "bh_total_return": 0.03995523164581805,
    "long_2x_total_return": 0.0793702,
    "max_drawdown": -3.410235418177532e-05,
    "calmar": 758530.504,
}


def test_phase3_leaderboard_kinds_and_the_real_row():
    for column in PHASE3_COLUMNS:
        assert leaderboard_kind(column), column
    for column in PHASE3_COLUMNS:
        if column.endswith("_return") or column.startswith("frac_sessions"):
            assert leaderboard_kind(column) == "fraction", column
    assert leaderboard_kind("calmar") == "ratio2"
    assert leaderboard_kind("fees_total") == "money"
    assert leaderboard_kind("sharpe") == "ratio2"
    assert format_number("fraction", REAL_ROW["total_return"]) == "4.00%"
    assert format_number("fraction", REAL_ROW["bh_total_return"]) == "4.00%"
    assert format_number("fraction", REAL_ROW["long_2x_total_return"]) == "7.94%"
    assert format_number("fraction", REAL_ROW["max_drawdown"]) == "-0.0034%"
    assert format_number("ratio2", REAL_ROW["calmar"]) == "758,530.50"
    assert format_number("fraction", 0.0) == "0.00%"


def test_date_scrubber_catches_spellings_other_than_iso():
    samples = (
        "2024/01/04",
        "04-Jan-2024",
        "04-Jul-2019",
        "4 January 2024",
        "Jan 4, 2024",
        "2024-01",
        "2024",
        "seen 2024/01/04 and 04-Jan-2024",
    )
    for sample in samples:
        cleaned = scrub_text(sample, ()).casefold()
        assert "2024" not in cleaned, sample
        assert "2019" not in cleaned, sample
        assert "jan" not in cleaned, sample
        assert "jul" not in cleaned, sample


def _png(text: str) -> bytes:
    """A 1×1 PNG with the secret in a tEXt chunk and in the raw bytes."""
    raw = b"\x00\x00\x00\x00\x00"
    compressed = zlib.compress(raw)

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    secret = text.encode("latin-1")
    return b"".join(
        [
            b"\x89PNG\r\n\x1a\n",
            chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)),
            chunk(b"tEXt", b"Comment\x00" + secret),
            chunk(b"IDAT", compressed),
            chunk(b"IEND", b""),
        ]
    )


def _panel(qtbot) -> ChartPanel:
    panel = ChartPanel()
    qtbot.addWidget(panel)
    panel.resize(900, 640)
    panel.show()
    qtbot.waitExposed(panel)
    QApplication.processEvents()
    return panel


def _painted(panel: ChartPanel, color: str) -> int:
    QApplication.processEvents()
    image = panel.canvas.grab().toImage()
    rect = panel.equity_plot.getViewBox().sceneBoundingRect()
    top_left = panel.canvas.mapFromScene(rect.topLeft())
    bottom_right = panel.canvas.mapFromScene(rect.bottomRight())
    x0 = max(0, int(min(top_left.x(), bottom_right.x())) + 2)
    x1 = min(image.width(), int(max(top_left.x(), bottom_right.x())) - 2)
    y0 = max(0, int(min(top_left.y(), bottom_right.y())) + 2)
    y1 = min(image.height(), int(max(top_left.y(), bottom_right.y())) - 2)
    target = QColor(color)
    count = 0
    for y in range(y0, y1, 1):
        for x in range(x0, x1, 1):
            pixel = image.pixelColor(x, y)
            distance = abs(pixel.red() - target.red()) + abs(pixel.green() - target.green()) + abs(pixel.blue() - target.blue())
            if distance < 80:
                count += 1
    return count


def test_log_scale_frames_the_series_in_log_space_and_paints_it(qtbot):
    panel = _panel(qtbot)
    panel.set_study(
        equity=[
            ("Strategy", [0, 1, 2, 3], [10, 50, 20, 80], "#8eb7f0"),
            ("1× buy and hold", [0, 1, 2, 3], [5, 12, 9, 30], "#b7aea2"),
        ],
        drawdown=[("drawdown", [0, 1, 2], [0, -0.4, -0.1], "#e06c75")],
        sessions=["d0", "d1", "d2", "d3"],
        equity_note="Equity and the benchmark, from the report files.",
        drawdown_note="Drawdown is equity divided by its running peak, minus one.",
    )
    panel.log_box.setChecked(True)
    QApplication.processEvents()
    assert panel.log_box.isChecked()
    _x_range, y_range = panel.equity_plot.viewRange()
    low = math.log10(5)
    high = math.log10(80)
    assert y_range[0] < low < high < y_range[1]
    assert y_range[1] < 5
    assert panel.covered() == []
    assert _painted(panel, "#8eb7f0") > 20
    million = _panel(qtbot)
    million.set_study(
        equity=[("Strategy", [0, 1, 2, 3], [1_000_000, 1_050_000, 980_000, 1_100_000], "#8eb7f0")],
        drawdown=[("drawdown", [0, 1, 2, 3], [0, -0.01, -0.04, -0.02], "#e06c75")],
        sessions=["d0", "d1", "d2", "d3"],
        equity_note="equity",
        drawdown_note="drawdown",
    )
    million.log_box.setChecked(True)
    QApplication.processEvents()
    _x_range, y_range = million.equity_plot.viewRange()
    assert y_range[0] < math.log10(980_000) < math.log10(1_100_000) < y_range[1]
    assert y_range[1] - y_range[0] < 2
    assert million.covered() == []
    assert _painted(million, "#8eb7f0") > 20


def test_crosshair_uses_money_and_a_percent(qtbot):
    panel = _panel(qtbot)
    panel.set_study(
        equity=[("Strategy", [0, 1], [1_000_000, 1_022_000], "#8eb7f0")],
        drawdown=[("drawdown", [0, 1], [0.0, -0.04215], "#e06c75")],
        sessions=["2024-01-02", "2024-01-03"],
        equity_note="equity",
        drawdown_note="drawdown",
    )
    panel.show_readout_at(1)
    text = panel.readout.text()
    assert "$1,022,000.00" in text
    assert "-4.22%" in text
    assert "e+" not in text.lower()
    assert "-0.042" not in text


def test_designer_export_skips_a_poisoned_png_and_scrubs_dates(qtbot, tmp_path):
    metrics = dict(ARENA_METRICS)
    metrics["symbol"] = "ZZZX"
    metrics["anonymized"] = True
    metrics["note"] = "seen 2024/01/04 and 04-Jan-2024 and 04-Jul-2019"
    metrics["period_returns"] = {"S01": 0.04, "monthly": {"2024-01": 0.021}, "yearly": {"2024": 0.04}}
    folder = tmp_path / "ZZZX" / "book"
    write_book(folder, metrics, benchmark=False)
    designer = folder / "designer"
    clean = {
        "profile": "arena",
        "bar_minutes": 1,
        "total_return": 0.04,
        "sharpe": 0.8,
        "initial_equity": 1_000_000,
        "final_equity": 1_040_000,
        "note": "seen 2024/01/04 and 04-Jan-2024",
        "period_returns": {"S01": 0.04, "monthly": {"2024-01": 0.02}, "yearly": {"2024": 0.04}},
    }
    write_book(designer, clean, benchmark=False)
    equity = (designer / "equity_curve.csv").read_text(encoding="utf-8")
    for old, new in (("2024-01-02", "S0001"), ("2024-01-03", "S0002"), ("2024-01-04", "S0003"), ("2024-01-05", "S0004")):
        equity = equity.replace(old, new)
    (designer / "equity_curve.csv").write_text(equity, encoding="utf-8")
    poison = "ZZZX 2019-07-04 share_scale 7.777 raw 50.00"
    (designer / "equity.png").write_bytes(_png(poison))
    window = _window(qtbot, tmp_path)
    window.results_page.load_path(str(folder))
    assert window.results_page._report.audience == "designer"
    blob = _visible_blob(window)
    for needle in ("zzzx", "2024/01/04", "04-jan-2024", "04-jul-2019", "2024-01", "2019-07-04", "share_scale", "7.777"):
        assert needle not in blob, needle
    assert "zzzx" not in window.results_page.path.text().casefold()
    names = [str(child.get("name") or "") for child in window.results_page._report.children]
    assert all("/" not in name and "\\" not in name for name in names)
    window.results_page.tabs.setCurrentIndex(4)
    QApplication.processEvents()
    note = window.results_page.figure_note.text().casefold()
    assert "withheld" not in note
    assert note.startswith("saved figure")
    pixmap = window.results_page.figure_label.pixmap()
    assert pixmap is not None and not pixmap.isNull()
    exported = tmp_path / "export"
    written = window.results_page.export_to(exported)
    assert "equity.png" in written
    assert b"share_scale" not in (exported / "equity.png").read_bytes()
    assert b"7.777" not in (exported / "equity.png").read_bytes()
    exported_blob = []
    for path in exported.rglob("*"):
        if path.is_file():
            exported_blob.append(path.read_bytes().decode("latin-1", "replace"))
    folded = "\n".join(exported_blob).casefold()
    for needle in ("zzzx", "share_scale", "7.777", "2019-07-04", "2024/01/04", "04-jan-2024"):
        assert needle not in folded, needle


def test_designer_jobs_are_not_paths(tmp_path):
    from backtester.gui.backend import load_run

    folder = write_parallel_run(tmp_path / "ZZZX-batch", anonymized=True)
    report = load_run(folder)
    assert report.audience == "designer"
    blob = json.dumps(report.children, default=str).casefold()
    assert "qqq" not in blob
    assert "/tmp" not in blob
    for child in report.children:
        name = str(child.get("name") or "")
        assert "/" not in name and "\\" not in name
        assert not child.get("path")


def test_results_banner_activity_fees_and_readout(qtbot, tmp_path):
    metrics = dict(ARENA_METRICS)
    metrics["fees_breakdown"] = {"commission": 50.004, "exchange": 50.004, "sec": 34.074, "total": 134.08}
    metrics["fees_total"] = 134.08
    folder = write_book(tmp_path / "arena", metrics)
    window = _window(qtbot, tmp_path)
    window.open_results(str(folder))
    banners = []
    for index in range(window.results_page.banners.count()):
        widget = window.results_page.banners.itemAt(index).widget()
        if widget is None:
            continue
        labels = widget.findChildren(QLabel)
        assert labels
        banners.extend(label.text() for label in labels)
    assert not any("equity.png" in text for text in banners)
    window.results_page.tabs.setCurrentIndex(4)
    assert "equity.png" in window.results_page.figure_note.text()
    headers = [
        window.results_page.activity.model().headerData(column, Qt.Orientation.Horizontal)
        for column in range(2)
    ]
    assert headers == ["Metric", "Value"]
    model = window.results_page.activity.model()
    values = [model.rows[row]["value"] for row in range(len(model.rows))]
    assert any(isinstance(value, float) for value in values)
    model.sort(1, Qt.SortOrder.AscendingOrder)
    ordered = [model.rows[model.source_position(row)]["value"] for row in range(model.rowCount())]
    numbers = [value for value in ordered if isinstance(value, (int, float)) and not isinstance(value, bool)]
    assert numbers == sorted(numbers)
    fee_model = window.results_page.fees.model()
    components = [str(fee_model.index(row, 0).data()) for row in range(fee_model.rowCount())]
    assert "total" not in [item.casefold() for item in components]
    assert window.results_page.fees_footer.text().endswith("$134.08")
    window.results_page.tabs.setCurrentIndex(0)
    window.results_page.chart.show_readout_at(1)
    readout = window.results_page.chart.readout.text()
    assert "$1,010,000.00" in readout
    assert "%" in readout
    assert "e+" not in readout.lower()


def test_run_page_hides_irrelevant_fields_and_paints_a_radio_ring(qtbot, tmp_path):
    window = _window(qtbot, tmp_path)
    window.show_page(2)
    window.strategies_page.set_profile("arena")
    window.show_page(3)
    page = window.run_page
    page.single.setChecked(True)
    page.named.setChecked(True)
    page.refresh()
    for widget in (
        page.design_day,
        page.design_date,
        page.design_fraction,
        page.warmup_sessions,
        page.warmup_days,
        page.pack_dir,
        page.sidecar,
        page.dividends,
        page.workers,
    ):
        assert widget.isHidden(), widget
    summary = page.summary_text().casefold()
    assert "profile default" in summary
    assert "not a number this window filled in" not in summary
    assert "batch workers" not in summary
    assert "pack" not in summary
    assert "design" not in summary
    page.walk.setChecked(True)
    page.refresh()
    assert page.design_date.isVisible()
    assert page.pack_dir.isHidden()
    assert page.sidecar.isHidden()
    page.single.setChecked(True)
    page.pack.setChecked(True)
    page.refresh()
    assert page.src.isHidden()
    assert page.minutes.isHidden()
    assert page.pack_dir.isVisible()
    assert page.sidecar.isVisible()
    page.single.setChecked(True)
    page.walk.setChecked(False)
    radio = page.walk
    image = radio.grab().toImage()
    mid = image.height() // 2
    ring = 0
    for x in range(1, 15):
        pixel = image.pixelColor(x, mid)
        if pixel.red() > 160 and pixel.green() > 160 and pixel.blue() > 160:
            ring += 1
    assert ring >= 1
