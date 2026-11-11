"""Third review of d1a6958. Each test fails when its fix is reverted."""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYTEST_QT_API", "pyside6")

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")
pytest.importorskip("pytestqt")

from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import QApplication, QWidget

from backtester.gui.audience import designer_error, export_allowed, export_text, scrub_text
from backtester.gui.format import format_number
from synthetic import ARENA_METRICS, write_book
from test_review2 import _png_itxt
from test_window import _window

ESZETT = "ß"
LIGATURE_FI = "ﬁ"


def _page_text(page) -> str:
    chunks = []
    for widget in page.findChildren(QWidget):
        for method in ("text", "toolTip", "placeholderText"):
            fn = getattr(widget, method, None)
            if not callable(fn):
                continue
            try:
                value = fn()
            except Exception:
                continue
            if isinstance(value, str) and value:
                chunks.append(value)
        plain = getattr(widget, "toPlainText", None)
        if callable(plain):
            try:
                chunks.append(plain())
            except Exception:
                pass
        model_fn = getattr(widget, "model", None)
        if not callable(model_fn):
            continue
        try:
            model = model_fn()
        except Exception:
            continue
        if model is None:
            continue
        for row in range(model.rowCount()):
            for column in range(model.columnCount()):
                value = model.data(model.index(row, column), Qt.ItemDataRole.DisplayRole)
                if isinstance(value, str) and value:
                    chunks.append(value)
    return "\n".join(chunks)


def _operator_book(directory: Path) -> Path:
    metrics = dict(ARENA_METRICS)
    metrics["symbol"] = "QQQ"
    metrics["cost_basis"] = {
        "share_scale": 7.777,
        "cost_schedule": "arena",
        "per_share_fees": "real",
    }
    metrics["fees_breakdown"] = {"commission": 50.004, "exchange": 50.004, "sec": 34.074, "total": 134.08}
    metrics["fees_total"] = 134.08
    return write_book(directory, metrics)


def _assert_previous_book_is_gone(page) -> None:
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    QApplication.processEvents()
    blob = _page_text(page)
    assert "7.777" not in blob
    assert "2024-01-02" not in blob
    assert "50.004" not in blob
    assert "from the report files" not in blob.casefold()
    assert page.cost_line.text() == ""
    assert page.cost_line.toolTip() == ""
    assert page.chart.isHidden()
    assert page.chart.caption.text() == ""
    assert page.chart.dd_caption.text() == ""
    assert "2024" not in page.chart.readout.text()
    assert page.chart._study["sessions"] == []
    assert page.chart._study["equity"] == []
    assert list(page.chart._x_axis.labels) == []
    bottom = page.chart.equity_plot.getAxis("bottom")
    assert list(getattr(bottom, "labels", []) or []) == []
    assert page.activity.isHidden()
    assert page.activity.model().rowCount() == 0
    assert page.benchmarks.isHidden()
    assert page.benchmarks.model().rowCount() == 0
    assert page.fees.isHidden()
    assert page.fees.model().rowCount() == 0
    assert page.fees_footer.isHidden()
    assert page.fees_footer.text() == ""
    assert page.periods.isHidden()
    heat = " ".join(label.text() for label in page.periods.findChildren(type(page.identity)))
    assert "2024" not in heat
    assert "2.10%" not in heat
    assert page.fills.model().rowCount() == 0
    assert page.fills_note.text() == ""
    assert "QQQ" not in page.identity.text()


def test_failed_load_clears_the_previous_book(qtbot, tmp_path):
    book = _operator_book(tmp_path / "qqq-book")
    missing = tmp_path / "missing-dir"
    broken = tmp_path / "broken-json"
    broken.mkdir()
    (broken / "metrics.json").write_text("{", encoding="utf-8")
    listed = tmp_path / "json-list"
    listed.mkdir()
    (listed / "metrics.json").write_text("[1, 2, 3]", encoding="utf-8")
    empty = tmp_path / "empty-dir"
    empty.mkdir()
    walk = tmp_path / "no-leg"
    walk.mkdir()
    (walk / "walkforward.json").write_text("{}", encoding="utf-8")

    window = _window(qtbot, tmp_path)
    window.show_page(4)
    page = window.results_page
    for folder in (missing, broken, listed, empty, walk):
        page.load_path(str(book), audience="operator")
        QApplication.processEvents()
        assert "7.777" in page.cost_line.text()
        assert "from the report files" in page.chart.caption.text()
        page.chart.show_readout_at(0)
        assert "2024-01-02" in page.chart.readout.text()
        assert page.fees.model().rowCount() > 0
        assert not page.chart.isHidden()
        page.path.set_text(str(folder))
        page.load_current()
        QApplication.processEvents()
        _assert_previous_book_is_gone(page)


def test_dotted_unpadded_and_ordinal_dates_are_scrubbed_in_both_places():
    spelled = (
        "01.04.2024",
        "04.01.2024",
        "1.4.2024",
        "Jan. 4, 2024",
        "January 4th, 2024",
        "2024-1-4",
        "04.Jan.2024",
        "January 2024",
    )
    for sample in spelled:
        cleaned = scrub_text(sample, ())
        folded = cleaned.casefold()
        assert "2024" not in folded, sample
        assert "jan" not in folded, sample
        assert "-1-4" not in cleaned, sample
        assert "4th" not in folded, sample
        assert cleaned.strip(" ,.") == "", sample
        assert not export_allowed(sample, symbols=(), scale=None), sample
    kept = "price 2024.50 ratio 0.2024 day 2024"
    assert scrub_text(kept, ()) == kept
    assert export_allowed(kept, symbols=(), scale=None)
    sentence = scrub_text("on 2024-1-4.", ())
    assert "2024" not in sentence
    assert "-1-4" not in sentence
    assert scrub_text("Jan. 4, 2024", ()) == ""
    assert scrub_text("January 4th, 2024", ()) == ""


def test_share_factor_is_withheld_like_share_scale(qtbot, tmp_path):
    assert not export_allowed("share_factor 0.42", symbols=(), scale=None)
    assert not export_allowed("SHARE_FACTOR 0.42", symbols=(), scale=None)
    assert export_allowed("max_drawdown 0.2", symbols=(), scale=None)
    metrics = dict(ARENA_METRICS)
    metrics["anonymized"] = True
    metrics["symbol"] = "QQQ"
    folder = write_book(tmp_path / "book", metrics, benchmark=False)
    (folder / "fills.csv").write_text("note\nshare_factor 0.42\n", encoding="utf-8")
    (folder / "trades.csv").write_text("note\nplain row\n", encoding="utf-8")
    (folder / "equity.png").write_bytes(_png_itxt(b"share_factor 0.42"))
    window = _window(qtbot, tmp_path)
    page = window.results_page
    page.load_path(str(folder))
    assert page._report.audience == "designer"
    exported = tmp_path / "copy"
    written = page.export_to(exported)
    assert "trades.csv" in written
    assert "fills.csv" not in written
    assert not (exported / "fills.csv").exists()
    assert "equity.png" not in written
    assert not (exported / "equity.png").exists()
    assert "share_factor" in export_text(folder / "equity.png").casefold()
    assert not export_allowed(export_text(folder / "equity.png"), symbols=(), scale=None)


def test_windows_paths_with_spaces_do_not_leak_the_tail():
    cleaned = designer_error(
        "RuntimeError: failed to open C:\\Users\\Jeu\\Desktop\\Strategy Builder\\QQQ\\data.csv"
    )
    assert "RuntimeError" in cleaned
    for needle in ("Builder", "QQQ", "data.csv", "Jeu", "Users", "Desktop"):
        assert needle not in cleaned, needle
    unc = designer_error(
        "RuntimeError: failed to open \\\\server\\share\\Strategy Builder\\QQQ\\data.csv"
    )
    assert "RuntimeError" in unc
    for needle in ("Builder", "QQQ", "data.csv", "server", "share"):
        assert needle not in unc, needle
    forward = designer_error(
        "RuntimeError: failed to open //server/share/Strategy Builder/QQQ/data.csv"
    )
    assert "Builder" not in forward and "QQQ" not in forward and "data.csv" not in forward
    quoted = designer_error(
        "FileNotFoundError: [Errno 2] No such file or directory: "
        "'C:\\Users\\Jeu\\Desktop\\Strategy Builder\\QQQ\\data.csv'"
    )
    assert "FileNotFoundError" in quoted
    assert "Builder" not in quoted and "data.csv" not in quoted
    comma = designer_error(
        "OSError: cannot open C:\\Users\\Jeu\\Desktop\\Strategy Builder\\QQQ\\data.csv, permission denied"
    )
    assert "OSError" in comma
    assert "permission denied" in comma
    assert "Builder" not in comma and "QQQ" not in comma
    unix = designer_error("RuntimeError: boom /tmp/strategy/QQQ/data.csv")
    assert "RuntimeError" in unix
    assert "/tmp" not in unix


def test_negative_money_puts_the_sign_before_the_dollar():
    assert format_number("money", -340.25) == "-$340.25"
    assert format_number("money_full", -340.25) == "-$340.25"
    assert format_number("money", -1234.5) == "-$1,234.50"
    assert format_number("money", 340.25) == "$340.25"
    assert format_number("money", 5848.14) == "$5,848.14"
    assert format_number("money_full", -50.004) == "-$50.004"
    assert format_number("money_full", 50.004) == "$50.004"
    assert not str(format_number("money", -340.25)).startswith("$-")
    assert not str(format_number("money_full", -50.004)).startswith("$-")


def test_casefold_tickers_match_on_screen_and_in_export():
    assert scrub_text(f"held {ESZETT} overnight", ("SS",)) == "held RANDOM overnight"
    assert scrub_text("held SS overnight", (ESZETT,)) == "held RANDOM overnight"
    assert scrub_text(f"held {LIGATURE_FI} overnight", ("FI",)) == "held RANDOM overnight"
    assert scrub_text("held FI overnight", (LIGATURE_FI,)) == "held RANDOM overnight"
    assert scrub_text("maß", ("SS",)) == "maß"
    assert scrub_text(f"{LIGATURE_FI}le", ("FI",)) == f"{LIGATURE_FI}le"
    assert scrub_text("max_drawdown", ("MA",)) == "max_drawdown"
    assert scrub_text("QQQ1", ("QQQ",)) == "RANDOM1"
    assert scrub_text("held V overnight", ("V",)) == "held RANDOM overnight"
    assert not export_allowed(f"held {ESZETT} overnight", symbols=("SS",), scale=None)
    assert not export_allowed(f"held {LIGATURE_FI} overnight", symbols=("FI",), scale=None)
    assert not export_allowed("held SS overnight", symbols=(ESZETT,), scale=None)
    assert not export_allowed("held FI overnight", symbols=(LIGATURE_FI,), scale=None)
    assert export_allowed("max_drawdown 0.2", symbols=("MA",), scale=None)
    assert export_allowed("max_drawdown 0.2", symbols=("SS",), scale=None)
    assert export_allowed(f"{LIGATURE_FI}le", symbols=("FI",), scale=None)
    assert not export_allowed("QQQ1", symbols=("QQQ",), scale=None)


def test_designer_status_and_root_chip_drop_the_ticker(qtbot, tmp_path):
    metrics = dict(ARENA_METRICS)
    metrics["anonymized"] = True
    metrics["symbol"] = "QQQ"
    folder = write_book(tmp_path / "book", metrics, benchmark=False)
    window = _window(qtbot, tmp_path)
    page = window.results_page
    page.load_path(str(folder), audience="designer")
    window.data_page.set_root(str(tmp_path / "market" / "QQQ"))
    window.data_page.set_ticker("QQQ")
    window.set_status("Inspected QQQ: 22 sessions, tick 1m")
    window._refresh_chip()
    QApplication.processEvents()
    status = window.statusBar().currentMessage()
    assert "Inspected" in status
    assert "qqq" not in status.casefold()
    assert "qqq" not in window.root_chip.text().casefold()
    assert "qqq" not in window.root_chip.toolTip().casefold()
    assert "QQQ" in window.data_page.root_path.text()
    page.audience.setCurrentIndex(page.audience.findData("operator"))
    QApplication.processEvents()
    assert page._report.audience == "operator"
    assert "QQQ" in window.statusBar().currentMessage()
    assert "QQQ" in window.root_chip.text()


def test_results_title_follows_the_audience(qtbot, tmp_path):
    metrics = dict(ARENA_METRICS)
    metrics["anonymized"] = True
    folder = write_book(tmp_path / "book", metrics, benchmark=False)
    window = _window(qtbot, tmp_path)
    page = window.results_page
    assert page.header.heading.text() == "Operator report"
    page.load_path(str(folder))
    assert page._report.audience == "designer"
    assert page.header.heading.text() == "Designer report"
    page.audience.setCurrentIndex(page.audience.findData("operator"))
    QApplication.processEvents()
    assert page.header.heading.text() == "Operator report"
    page.audience.setCurrentIndex(page.audience.findData("designer"))
    QApplication.processEvents()
    assert page.header.heading.text() == "Designer report"

