"""Fifth review of ef0efdf, plus the RANDOM stand-in. Each test fails when its fix is reverted."""

from __future__ import annotations

import json
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYTEST_QT_API", "pyside6")

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")
pytest.importorskip("pytestqt")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from backtester.gui import backend as gui_backend
from backtester.gui.audience import designer_surface_text, export_allowed, scrub_text
from backtester.gui.backend import load_run
from synthetic import ARENA_METRICS, write_book
from test_review2 import _banner_texts
from test_review3 import _assert_previous_book_is_gone, _operator_book
from test_window import _window

_WHOLE_NUMBERS = (
    "-1.5e-3",
    "-340.25",
    "-$340.25",
    "-12.5%",
    "1.23e+4",
    "1,250,000.50",
    "pre-market",
)
_GLUED_YEARS = ("Jan2024", "January2024", "FY2024", "year2024")
_GLUED_SCALES = ("scale7.777", "x7.777")
_PATH_KEY = r"C:\Users\Jeu\Desktop\Strategy Builder\QQQ\data.csv"
_TRACE = """Traceback (most recent call last):
  File "worker.py", line 3, in run
    raise RuntimeError("worker exited 1")
ValueError: anonymization rejected symbol 'metrics': reserved vocabulary
RuntimeError: worker exited 1
"""


def _banners(page) -> str:
    return " ".join(_banner_texts(page.banners))


def _headers(table) -> list[str]:
    model = table.model()
    return [
        str(model.headerData(column, Qt.Orientation.Horizontal, Qt.ItemDataRole.DisplayRole) or "")
        for column in range(model.columnCount())
    ]


def _table_text(table) -> str:
    model = table.model()
    chunks = _headers(table)
    for row in range(model.rowCount()):
        for column in range(model.columnCount()):
            value = model.data(model.index(row, column), Qt.ItemDataRole.DisplayRole)
            if value not in (None, ""):
                chunks.append(str(value))
    return "\n".join(chunks)


def test_signed_numbers_and_hyphen_words_stay_one_token():
    prices = {"-340.25", "-$340.25", "-12.5%", "1.23e+4"}
    for sample in _WHOLE_NUMBERS:
        assert designer_surface_text(sample, ()) == sample
        assert export_allowed(sample, symbols=(), scale=None) is (sample not in prices)


def test_glued_year_and_scale_tokens_fail_on_screen_and_in_export():
    for glued in _GLUED_YEARS:
        shown = designer_surface_text(glued, ())
        assert glued not in shown
        assert "2024" not in shown
        assert not export_allowed(glued, symbols=(), scale=None)
    for glued in _GLUED_SCALES:
        shown = designer_surface_text(glued, (), scale=7.777)
        assert glued not in shown
        assert "7.777" not in shown
        assert not export_allowed(glued, symbols=(), scale=7.777)
    assert designer_surface_text("price 2024.50 day 2024", ()) == "price 2024.50 day 2024"
    assert not export_allowed("price 2024.50 day 2024", symbols=(), scale=None)
    assert designer_surface_text("long_2x and 2.5", (), scale=2) == "long_2x and 2.5"
    assert not export_allowed("long_2x and 2.5", symbols=(), scale=2)
    assert "2" not in designer_surface_text("scale 2", (), scale=2).split()


def test_headers_and_keys_use_the_same_stripper(qtbot, tmp_path):
    metrics = dict(ARENA_METRICS)
    metrics["anonymized"] = True
    metrics["symbol"] = "QQQ"
    metrics["QQQ"] = "see QQQ"
    metrics["comment"] = "see QQQ"
    metrics["strategy_id"] = "QQQ"
    metrics[_PATH_KEY] = 1
    folder = write_book(tmp_path / "book", metrics, benchmark=False)
    (folder / "fills.csv").write_text(
        "day,date,minute,session,shares_delta,open,fill_price,fees,shares_after,QQQ\n"
        "0,2024-01-02,570,rth,10,100,100.01,0.35,10,1\n",
        encoding="utf-8",
    )
    window = _window(qtbot, tmp_path)
    page = window.results_page
    page.load_path(str(folder), audience="designer")
    QApplication.processEvents()
    headers = _headers(page.fills)
    assert "QQQ" not in headers
    assert "RANDOM" not in headers
    assert "ASSET" not in headers
    assert "date" not in headers
    assert "open" not in headers
    assert "day" in headers and "session" in headers
    other = _table_text(page.other)
    assert "RANDOM" in other
    assert "strategy_id" in page._report.other_fields
    assert "comment" not in page._report.metrics
    assert "QQQ" not in page._report.metrics
    for needle in ("QQQ", "ASSET", "Users", "Builder", "data.csv", _PATH_KEY):
        assert needle not in other
    page.load_path(str(folder), audience="operator")
    QApplication.processEvents()
    assert "QQQ" in _headers(page.fills)
    operator = _table_text(page.other)
    assert "QQQ" in operator
    assert _PATH_KEY in operator
    assert "see QQQ" in operator


def test_truncated_leg_and_job_error_reach_the_banner(qtbot, tmp_path):
    book = _operator_book(tmp_path / "qqq-book")
    walk = tmp_path / "walk"
    walk.mkdir()
    (walk / "walkforward.json").write_text("{}\n", encoding="utf-8")
    design = walk / "design"
    design.mkdir()
    (design / "metrics.json").write_text("{", encoding="utf-8")
    parallel = tmp_path / "parallel"
    job = parallel / "jobs" / "alpha" / "src"
    job.mkdir(parents=True)
    (job / "error.json").write_text("{", encoding="utf-8")
    (parallel / "run.json").write_text(json.dumps({"kind": "parallel", "jobs": []}), encoding="utf-8")

    window = _window(qtbot, tmp_path)
    page = window.results_page
    for folder in (walk, parallel):
        page.load_path(str(book), audience="operator")
        QApplication.processEvents()
        assert "7.777" in page.cost_line.text()
        page.load_path(str(folder), audience="operator")
        QApplication.processEvents()
        assert "could not be read" in _banners(page).casefold()
        _assert_previous_book_is_gone(page)


def test_status_and_chip_drop_paths_and_use_random(qtbot, tmp_path):
    metrics = dict(ARENA_METRICS)
    metrics["anonymized"] = True
    metrics["symbol"] = "QQQ"
    folder = write_book(tmp_path / "book", metrics, benchmark=False)
    window = _window(qtbot, tmp_path)
    page = window.results_page
    page.load_path(str(folder), audience="designer")
    root = "/home/jeu/Strategy Builder/QQQ/data.csv"
    window.data_page.set_root(root)
    window.set_status(f"Opened {root}")
    window._refresh_chip()
    QApplication.processEvents()
    status = window.statusBar().currentMessage()
    chip = window.root_chip.text()
    tip = window.root_chip.toolTip()
    for surface in (status, chip, tip):
        folded = surface.casefold()
        assert "builder" not in folded
        assert "/home" not in folded
        assert "data.csv" not in folded
        assert "qqq" not in folded
    window.set_status("Inspected QQQ")
    QApplication.processEvents()
    inspected = window.statusBar().currentMessage()
    assert "Inspected" in inspected
    assert "RANDOM" in inspected
    assert "qqq" not in inspected.casefold()
    assert "ASSET" not in inspected
    page.audience.setCurrentIndex(page.audience.findData("operator"))
    QApplication.processEvents()
    assert "QQQ" in window.statusBar().currentMessage()
    assert root in window.root_chip.text()
    assert root in window.root_chip.toolTip()


def test_run_page_shows_anonymization_rejection_and_ignores_renamed_files(qtbot, tmp_path):
    window = _window(qtbot, tmp_path)
    window.run_page.on_failed(_TRACE)
    QApplication.processEvents()
    banner = _banners(window.run_page)
    assert "reserved vocabulary" in banner
    assert "metrics" in banner
    assert banner != "RuntimeError: worker exited 1"

    renamed = tmp_path / "renamed"
    renamed.mkdir()
    (renamed / "metrics_QQQ.json").write_text(json.dumps(ARENA_METRICS), encoding="utf-8")
    (renamed / "equity_curve_QQQ.csv").write_text("day,date,equity\n0,2024-01-02,10\n", encoding="utf-8")
    report = load_run(renamed)
    assert report.kind == "incomplete"
    assert report.curves == []


def test_memory_error_and_oversize_json_clear_the_book(qtbot, tmp_path, monkeypatch):
    book = _operator_book(tmp_path / "qqq-book")
    window = _window(qtbot, tmp_path)
    page = window.results_page
    page.load_path(str(book), audience="operator")
    QApplication.processEvents()
    assert "7.777" in page.cost_line.text()

    def boom(_path):
        raise MemoryError("metrics.json")

    monkeypatch.setattr(gui_backend, "_read_json", boom)
    page.load_path(str(book), audience="operator")
    QApplication.processEvents()
    banner = _banners(page).casefold()
    assert "could not be read" in banner
    assert "memoryerror" in banner
    _assert_previous_book_is_gone(page)

    monkeypatch.undo()
    monkeypatch.setattr(gui_backend, "JSON_READ_LIMIT", 8)
    page.load_path(str(book), audience="operator")
    QApplication.processEvents()
    limited = _banners(page).casefold()
    assert "could not be read" in limited
    assert "byte" in limited
    _assert_previous_book_is_gone(page)


def test_null_figure_does_not_say_saved(qtbot, tmp_path):
    folder = write_book(tmp_path / "book", dict(ARENA_METRICS), benchmark=False)
    (folder / "equity.png").write_bytes(b"not a png")
    window = _window(qtbot, tmp_path)
    page = window.results_page
    page.load_path(str(folder), audience="operator")
    QApplication.processEvents()
    note = page.figure_note.text()
    assert note == "figure could not be read"
    assert "Saved figure" not in note
    assert page.figure_label.text() == ""


def test_designer_stand_in_is_random_and_operator_keeps_the_ticker():
    assert scrub_text("held QQQ overnight", ("QQQ",)) == "held RANDOM overnight"
    assert scrub_text("QQQ1", ("QQQ",)) == "RANDOM1"
    assert "ASSET" not in scrub_text("held QQQ overnight", ("QQQ",))
    shown = designer_surface_text("see QQQ", ("QQQ",))
    assert "RANDOM" in shown
    assert "QQQ" not in shown
    assert "ASSET" not in shown
