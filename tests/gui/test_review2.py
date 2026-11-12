"""Second review of ef4b7c4. Each test fails on that tree."""

from __future__ import annotations

import json
import os
import struct
import zlib
from decimal import Decimal
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYTEST_QT_API", "pyside6")

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")
pytest.importorskip("pytestqt")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLabel, QPushButton

from backtester.gui.audience import export_allowed, export_text, scrub_text
from backtester.gui.backend import load_run
from backtester.gui.charts import ChartPanel
from backtester.gui.format import format_number
from synthetic import ARENA_METRICS, write_book, write_parallel_run
from test_window import _visible_blob, _window

ROOT = Path(__file__).resolve().parents[2]


def _png_itxt(secret: bytes) -> bytes:
    """1×1 PNG whose iTXt text is zlib-compressed."""
    raw = b"\x00\x00\x00\x00\x00"
    compressed = zlib.compress(raw)
    payload = zlib.compress(secret)
    # keyword, flag=1, method=0, language, translated keyword, text
    data = b"Comment\x00\x01\x00en\x00\x00" + payload

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    return b"".join(
        [
            b"\x89PNG\r\n\x1a\n",
            chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)),
            chunk(b"iTXt", data),
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


def _banner_texts(layout) -> list[str]:
    texts = []
    for index in range(layout.count()):
        widget = layout.itemAt(index).widget()
        if widget is None:
            continue
        labels = widget.findChildren(QLabel)
        assert labels, "a banner with no label cannot be checked"
        texts.extend(label.text() for label in labels)
        tip = widget.toolTip()
        if tip:
            texts.append(tip)
    return texts


def test_peek_keeps_a_one_letter_ticker(tmp_path):
    from backtester.gui.audience import peek_symbols

    folder = tmp_path / "book"
    folder.mkdir()
    (folder / "metrics.json").write_text(json.dumps({"symbol": "V"}), encoding="utf-8")
    assert peek_symbols(folder) == ("V",)


def test_scrub_keeps_numbers_and_uses_one_ticker_rule():
    assert "2024.50" in scrub_text("price 2024.50", ())
    assert "0.2024" in scrub_text("ratio 0.2024", ())
    assert "2000.50" in scrub_text("level 2000.50", ())
    assert "2024" in scrub_text("day 2024", ())
    assert "2024" not in scrub_text("2024", ())
    assert "2024" not in scrub_text("January 2024", ()).casefold()
    assert "jan" not in scrub_text("04.Jan.2024", ()).casefold()
    assert scrub_text("held QQQ1", ("QQQ",)) == "held RANDOM1"
    assert scrub_text("ticker V1", ("V",)) == "ticker V1"
    assert not export_allowed("held V overnight", symbols=("V",), scale=None)
    cleaned = scrub_text("applied share_scale 7.777 for QQQ1", ("QQQ",), scale=7.777)
    folded = cleaned.casefold()
    assert "share_scale" not in folded
    assert "7.777" not in folded
    assert "qqq" not in folded
    assert not export_allowed("max_drawdown 0.2 and price 2024.50 and day 2024", symbols=("MA",), scale=None)
    assert not export_allowed("held MA overnight", symbols=("MA",), scale=None)
    assert not export_allowed("QQQ1", symbols=("QQQ",), scale=None)


def test_compressed_itxt_fails_the_allow_list(tmp_path):
    path = tmp_path / "equity.png"
    path.write_bytes(_png_itxt(b"share_scale 7.777 QQQ"))
    text = export_text(path)
    assert "share_scale" in text.casefold()
    assert not export_allowed(text, symbols=("QQQ",), scale=7.777)


def test_designer_strings_periods_and_rewritten_export(qtbot, tmp_path):
    metrics = dict(ARENA_METRICS)
    metrics["symbol"] = "ZZZX"
    metrics["ticker"] = "QQQ"
    metrics["anonymized"] = True
    metrics["comment"] = "applied share_scale 7.777 for QQQ1 on 2024/01/04"
    metrics["note"] = "04.Jan.2024 and January 2024"
    metrics["cost_basis"] = {"share_scale": 7.777, "anonymized": True, "cost_schedule": "arena"}
    metrics["period_returns"] = {
        "S01": 0.04,
        "monthly": {"2024-01": 0.01, "2024-02": 0.22},
        "yearly": {"2024": 0.4, "2025": 0.6},
    }
    folder = tmp_path / "ZZZX" / "book"
    write_book(folder, metrics, benchmark=False)
    window = _window(qtbot, tmp_path)
    window.results_page.load_path(str(folder))
    page = window.results_page
    assert page._report.audience == "designer"
    blob = _visible_blob(window).casefold()
    for needle in ("share_scale", "7.777", "qqq", "2024/01/04", "january", "04.jan"):
        assert needle not in blob, needle
    heat = page.periods.findChildren(QLabel)
    labels = " ".join(label.text() for label in heat)
    assert "M1" in labels and "M2" in labels
    assert "Y1" in labels and "Y2" in labels
    assert "1.00%" in labels and "22.00%" in labels
    assert "40.00%" in labels and "60.00%" in labels
    exported = tmp_path / "copy"
    written = page.export_to(exported)
    sheet = (exported / "metrics.json").read_text(encoding="utf-8")
    payload = json.loads(sheet)
    periods = payload["period_returns"]
    assert periods["monthly"] == {"M1": 0.01, "M2": 0.22}
    assert periods["yearly"] == {"Y1": 0.4, "Y2": 0.6}
    assert "metrics.json" in written
    for name in ("metrics.json", "displayed_metrics.csv"):
        text = (exported / name).read_text(encoding="utf-8")
        assert export_allowed(text, symbols=("QQQ", "ZZZX"), scale=7.777), name
        assert "share_scale" not in text.casefold()
        assert "7.777" not in text


def test_rewritten_export_is_withheld_when_the_allow_list_fails(qtbot, tmp_path, monkeypatch):
    metrics = dict(ARENA_METRICS)
    metrics["anonymized"] = True
    metrics["symbol"] = "QQQ"
    folder = write_book(tmp_path / "book", metrics, benchmark=False)
    window = _window(qtbot, tmp_path)
    window.results_page.load_path(str(folder))
    monkeypatch.setattr(
        "backtester.gui.pages.results_page.export_allowed",
        lambda *args, **kwargs: False,
    )
    exported = tmp_path / "copy"
    written = window.results_page.export_to(exported)
    for name in ("metrics.json", "displayed_metrics.csv"):
        assert name not in written
        assert not (exported / name).exists()


def test_load_uses_the_real_directory(qtbot, tmp_path):
    first = dict(ARENA_METRICS)
    first["symbol"] = "ZZZX"
    first["anonymized"] = True
    first["total_return"] = 0.04
    second = dict(ARENA_METRICS)
    second["symbol"] = "IBM"
    second["anonymized"] = False
    second["total_return"] = 0.25
    book_a = write_book(tmp_path / "ZZZX" / "book-a", first, benchmark=False)
    book_b = write_book(tmp_path / "other" / "book-b", second, benchmark=False)
    window = _window(qtbot, tmp_path)
    page = window.results_page
    page.load_path(str(book_a))
    assert page._source_path == str(book_a)
    assert "ZZZX" not in page.path.text()
    assert page.metric_text("total_return") == "4.00%"
    page.path.set_text(str(book_b))
    page.load_current()
    assert page._source_path == str(book_b)
    assert page.metric_text("total_return") == "25.00%"
    page.load_path(str(book_a))
    index = page.audience.findData("operator")
    page.audience.setCurrentIndex(index)
    QApplication.processEvents()
    assert page._source_path == str(book_a)
    assert page.path.text() == str(book_a)
    assert page._report.kind == "book"
    assert page.metric_text("total_return") == "4.00%"


def test_fee_footer_clears_on_a_batch(qtbot, tmp_path):
    metrics = dict(ARENA_METRICS)
    metrics["fees_breakdown"] = {"commission": 50.004, "exchange": 50.004, "sec": 34.074, "total": 134.08}
    metrics["fees_total"] = 134.08
    book = write_book(tmp_path / "fees", metrics, benchmark=False)
    batch = write_parallel_run(tmp_path / "batch", anonymized=False)
    window = _window(qtbot, tmp_path)
    page = window.results_page
    page.load_path(str(book))
    window.show_page(4)
    assert page.fees_footer.text().endswith("$134.08")
    assert not page.fees_footer.isHidden()
    page.load_path(str(batch), audience="operator")
    QApplication.processEvents()
    assert page.fees_footer.isHidden()
    assert page.fees_footer.text() == ""


def test_fee_rows_add_back_to_the_stored_total(qtbot, tmp_path):
    metrics = dict(ARENA_METRICS)
    metrics["fees_breakdown"] = {"commission": 50.004, "exchange": 50.004, "sec": 34.074, "total": 134.08}
    metrics["fees_total"] = 134.08
    folder = write_book(tmp_path / "fees", metrics, benchmark=False)
    window = _window(qtbot, tmp_path)
    window.results_page.load_path(str(folder))
    model = window.results_page.fees.model()
    shown = []
    for row in range(model.rowCount()):
        text = str(model.index(row, 1).data()).replace("$", "").replace(",", "")
        shown.append(Decimal(text))
    assert Decimal("50.004") in shown
    total = Decimal(window.results_page.fees_footer.text().split("$")[-1].replace(",", ""))
    assert sum(shown).quantize(Decimal("0.01")) == total


def test_tight_log_axis_has_several_dollar_ticks(qtbot):
    panel = _panel(qtbot)
    panel.set_study(
        equity=[("Strategy", [0, 1, 2, 3], [1_000_000, 1_050_000, 980_000, 1_100_000], "#8eb7f0")],
        drawdown=[("drawdown", [0, 1, 2, 3], [0, -0.01, -0.04, -0.02], "#e06c75")],
        sessions=["d0", "d1", "d2", "d3"],
        equity_note="equity",
        drawdown_note="drawdown",
    )
    panel.log_box.setChecked(True)
    QApplication.processEvents()
    axis = panel.equity_plot.getAxis("left")
    _x, y_range = panel.equity_plot.viewRange()
    labels = []
    for spacing, values in axis.tickValues(y_range[0], y_range[1], 420):
        labels.extend(label for label in axis.tickStrings(values, 1.0, spacing) if label)
    assert len(set(labels)) >= 4


def test_session_ticks_sit_on_integer_sessions_once(qtbot):
    panel = _panel(qtbot)
    sessions = ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"]
    panel.set_study(
        equity=[("Strategy", [0, 1, 2, 3], [10, 11, 9, 12], "#8eb7f0")],
        drawdown=[("drawdown", [0, 1, 2, 3], [0, -0.1, -0.2, -0.05], "#e06c75")],
        sessions=sessions,
        equity_note="equity",
        drawdown_note="drawdown",
    )
    QApplication.processEvents()
    axis = panel.equity_plot.getAxis("bottom")
    x_range = panel.equity_plot.viewRange()[0]
    labels = []
    for spacing, values in axis.tickValues(x_range[0], x_range[1], 800):
        for value in values:
            assert abs(float(value) - round(float(value))) < 1e-6
        labels.extend(axis.tickStrings(values, 1.0, spacing))
    shown = [label for label in labels if label]
    assert shown
    assert len(shown) == len(set(shown))
    assert axis.tickStrings([1.5], 1.0, 1) == [""]
    assert "2024-01-04" in shown


def test_walk_forward_clears_pack(qtbot, tmp_path):
    window = _window(qtbot, tmp_path)
    window.show_page(2)
    window.strategies_page.set_profile("arena")
    window.show_page(3)
    page = window.run_page
    page.pack.setChecked(True)
    page.walk.setChecked(True)
    QApplication.processEvents()
    assert page.named.isChecked()
    assert not page.pack.isChecked()
    assert page.draft()["source"] == "named"
    assert "Named files" in page.summary_text()
    assert "Pack" not in page.summary_text()


def test_designer_errors_drop_absolute_paths(qtbot, tmp_path):
    fixture = ROOT / "tests" / "gui" / "fixtures" / "phase3_desk_batch"
    window = _window(qtbot, tmp_path)
    window.results_page.load_path(str(fixture), audience="designer")
    page = window.results_page
    crash = next(child for child in page._report.children if "crash" in str(child.get("name")))
    assert "RuntimeError" in str(crash.get("error"))
    assert "boom" in str(crash.get("error"))
    assert "/tmp" not in str(crash.get("error"))
    assert "\\" not in str(crash.get("error"))
    window.show_page(4)
    QApplication.processEvents()
    model = page.jobs.model()
    cells = [str(model.index(row, 2).data() or "") for row in range(model.rowCount())]
    assert any("boom" in cell for cell in cells)
    assert all("/tmp" not in cell for cell in cells)
    tips = " ".join(_banner_texts(page.banners))
    assert "boom" in tips
    assert "/tmp" not in tips
    page.load_path(str(fixture), audience="operator")
    operator = next(child for child in page._report.children if "crash" in str(child.get("name")))
    assert "/tmp" in str(operator.get("error"))


def test_tiny_fraction_is_not_eight_zeros():
    text = format_number("fraction", 1e-12)
    assert text != "0.00000000%"
    assert "e" in text.lower() or text.startswith("<")


def test_benchmark_row_uses_a_human_label(qtbot, tmp_path):
    folder = write_book(tmp_path / "book", dict(ARENA_METRICS))
    window = _window(qtbot, tmp_path)
    window.results_page.load_path(str(folder), audience="operator")
    model = window.results_page.benchmarks.model()
    labels = [str(model.index(row, 0).data() or "") for row in range(model.rowCount())]
    assert "1× buy and hold" in labels
    assert "buy_and_hold_1x" not in labels


def test_run_page_does_not_stretch_a_blank_region(qtbot, tmp_path):
    window = _window(qtbot, tmp_path)
    window.resize(1680, 1180)
    window.show_page(3)
    QApplication.processEvents()
    button = next(item for item in window.run_page.findChildren(QPushButton) if item.text() == "Copy last pack paths")
    form = button.parentWidget()
    gap = form.height() - button.geometry().bottom()
    assert gap < 160
    assert window.run_page.log.height() >= window.run_page.summary.height()
