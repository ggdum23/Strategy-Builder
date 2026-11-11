"""Fourth review of 466bc44. Each test fails when its fix is reverted."""

from __future__ import annotations

import csv
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

from backtester.gui import backend as gui_backend
from backtester.gui.audience import designer_error, designer_surface_text, export_allowed, scrub_text
from synthetic import ARENA_METRICS, write_book
from test_review2 import _banner_texts
from test_review3 import _assert_previous_book_is_gone, _operator_book
from test_window import _window

_MONTHS = ("Jan", "January", "Feb", "March", "Sep", "September")
_DAYS = ("1", "4", "04", "28")
_YEARS = ("1999", "2024", "2026")


def _metrics(**extra) -> dict:
    payload = dict(ARENA_METRICS)
    payload.update(extra)
    return payload


def _load_operator(page, book: Path) -> None:
    page.load_path(str(book), audience="operator")
    QApplication.processEvents()
    assert "7.777" in page.cost_line.text()


def _failure_text(page) -> str:
    return " ".join(_banner_texts(page.banners))


def test_undecodable_bytes_clear_the_previous_book(qtbot, tmp_path, monkeypatch):
    book = _operator_book(tmp_path / "qqq-book")
    bad_metrics = tmp_path / "bad-metrics"
    bad_metrics.mkdir()
    (bad_metrics / "metrics.json").write_bytes(b"\xff")
    bad_equity = tmp_path / "bad-equity"
    bad_equity.mkdir()
    (bad_equity / "metrics.json").write_text(json.dumps(_metrics()), encoding="utf-8")
    (bad_equity / "equity_curve.csv").write_bytes(b"date,equity\n\xff\n")

    window = _window(qtbot, tmp_path)
    page = window.results_page

    def fails(target: Path) -> None:
        _load_operator(page, book)
        page.load_path(str(target), audience="operator")
        QApplication.processEvents()
        _assert_previous_book_is_gone(page)
        banner = _failure_text(page).casefold()
        assert "could not be read" in banner
        assert "unicodedecodeerror" in banner

    fails(bad_metrics)
    fails(bad_equity)

    _load_operator(page, book)
    original_table = gui_backend._load_table

    def raise_csv(*_args, **_kwargs):
        raise csv.Error("line contains NUL")

    monkeypatch.setattr(gui_backend, "_load_table", raise_csv)
    page.load_path(str(book), audience="operator")
    QApplication.processEvents()
    _assert_previous_book_is_gone(page)
    assert "nul" in _failure_text(page).casefold()
    monkeypatch.setattr(gui_backend, "_load_table", original_table)

    _load_operator(page, book)

    def raise_value(*_args, **_kwargs):
        raise ValueError("bad parse")

    monkeypatch.setattr(gui_backend, "_read_json", raise_value)
    page.load_path(str(book), audience="operator")
    QApplication.processEvents()
    _assert_previous_book_is_gone(page)
    assert "valueerror" in _failure_text(page).casefold()


def _curve(folder: Path, table: str) -> Path:
    folder.mkdir(parents=True)
    metrics = _metrics(anonymized=True, symbol="QQQ")
    (folder / "metrics.json").write_text(json.dumps(metrics), encoding="utf-8")
    (folder / "equity_curve.csv").write_text(table, encoding="utf-8")
    return folder


def _axis_labels(page) -> list[str]:
    axis = page.chart.equity_plot.getAxis("bottom")
    return [label for label in axis.tickStrings([0.0, 1.0], 1.0, 1) if label]


def test_designer_axis_and_crosshair_use_the_session_index(qtbot, tmp_path):
    day = _curve(
        tmp_path / "day",
        "day,date,equity\n7,2024-01-02,100\n8,2024-01-03,110\n",
    )
    dates = _curve(
        tmp_path / "dates",
        "date,equity\n2024-01-02,100\n2024-01-03,110\n",
    )
    window = _window(qtbot, tmp_path)
    window.resize(1200, 800)
    page = window.results_page
    page.load_path(str(day), audience="designer")
    QApplication.processEvents()
    assert page.chart._study["sessions"] == ["7", "8"]
    assert _axis_labels(page) == ["7", "8"]
    page.chart.show_readout_at(0)
    readout = page.chart.readout.text()
    assert readout.startswith("7")
    assert "2024" not in readout
    assert "2024-01-02" not in readout

    page.load_path(str(dates), audience="designer")
    QApplication.processEvents()
    assert page.chart._study["sessions"] == ["0", "1"]
    assert _axis_labels(page) == ["0", "1"]
    page.chart.show_readout_at(1)
    readout = page.chart.readout.text()
    assert readout.startswith("1")
    assert "2024" not in readout


def test_posix_paths_with_spaces_leave_the_banner(qtbot, tmp_path):
    sentence = "RuntimeError: failed to open /home/jeu/Strategy Builder/QQQ/data.csv"
    cleaned = designer_error(sentence)
    assert "RuntimeError" in cleaned
    for needle in ("Builder", "QQQ", "data.csv", "/home", "jeu"):
        assert needle not in cleaned, needle
    quoted = designer_error("OSError: missing '/home/jeu/Strategy Builder/QQQ/data.csv'")
    assert "OSError" in quoted
    assert "Builder" not in quoted and "data.csv" not in quoted
    paused = designer_error(
        "OSError: cannot open /home/jeu/Strategy Builder/QQQ/data.csv, permission denied"
    )
    assert "OSError" in paused
    assert "permission denied" not in paused
    assert "Builder" not in paused and "QQQ" not in paused

    folder = write_book(tmp_path / "book", _metrics(anonymized=True, symbol="QQQ"), benchmark=False)
    window = _window(qtbot, tmp_path)
    page = window.results_page
    page.load_path(str(folder), audience="designer")
    warning = "failed to open /home/jeu/Strategy Builder/QQQ/data.csv"
    page._report.warnings.append(warning)
    page.show_report(page._report)
    QApplication.processEvents()
    blob = _failure_text(page)
    assert "failed" in blob.casefold()
    for needle in ("Builder", "/home", "QQQ", "data.csv", "jeu"):
        assert needle not in blob, needle
    assert "\\" not in blob


def test_period_is_enough_to_separate_month_and_day():
    samples = ("Jan.4, 2024", "Jan.4 2024", "January.4, 2024", "January.4 2024")
    for sample in samples:
        cleaned = scrub_text(sample, ())
        assert "2024" not in cleaned, sample
        assert "jan" not in cleaned.casefold(), sample
        assert cleaned.strip(" ,.") == "", sample
        assert not export_allowed(sample, symbols=(), scale=None), sample
    assert scrub_text("Jan. 4, 2024", ()) == ""
    assert scrub_text("price 2024.50", ()) == "price 2024.50"


def test_bare_year_stays_only_after_the_word_day():
    assert scrub_text("day 2024", ()) == "day 2024"
    assert scrub_text("Day 2024", ()) == "Day 2024"
    assert "2024" not in scrub_text("today 2024", ())
    assert "today" in scrub_text("today 2024", ())
    assert "2024" not in scrub_text("holiday 2024", ())
    assert "holiday" in scrub_text("holiday 2024", ())
    kept = "price 2024.50 ratio 0.2024 day 2024"
    assert scrub_text(kept, ()) == kept
    assert export_allowed(kept, symbols=(), scale=None)


def test_scale_matches_only_a_standalone_number():
    assert scrub_text("long_2x", (), scale=2) == "long_2x"
    assert scrub_text("ratio 2.5", (), scale=2) == "ratio 2.5"
    assert scrub_text("count 12 and 20", (), scale=2) == "count 12 and 20"
    assert "2" not in scrub_text("held 2 shares", (), scale=2)
    assert scrub_text("scale 7.777", (), scale=7.777) == "scale "
    assert export_allowed("long_2x", symbols=(), scale=2)
    assert export_allowed("2.5", symbols=(), scale=2)
    assert export_allowed("12", symbols=(), scale=2)
    assert export_allowed("2024.50", symbols=(), scale=2)
    assert not export_allowed("held 2 shares", symbols=(), scale=2)
    assert not export_allowed("scale 2", symbols=(), scale=2)
    assert not export_allowed("share count 2.0", symbols=(), scale=2)


def _calendar_samples() -> list[str]:
    samples = [
        "01.04.2024",
        "1.4.2024",
        "04.01.2024",
        "2024-1-4",
        "2024-01-04",
        "2024/1/4",
        "2024.1.4",
        "Jan4/2024",
        "January.4,2024",
        "4th January 2024",
    ]
    for month in _MONTHS:
        for day in _DAYS:
            for year in _YEARS:
                samples.append(f"{month}.{day}, {year}")
                samples.append(f"{month}.{day} {year}")
                samples.append(f"{month} {day}, {year}")
                samples.append(f"{day}.{month}.{year}")
                samples.append(f"{day} {month} {year}")
    return samples


def _path_samples() -> list[str]:
    return [
        "/home/jeu/Strategy Builder/data.csv",
        "/home/jeu/Strategy Builder/QQQ/data.csv",
        "/tmp/Strategy Builder/book/notes.csv",
        r"C:\Users\Jeu\Desktop\Strategy Builder\QQQ\data.csv",
        r"C:\Users\Jeu\Desktop\Strategy Builder\book\notes.csv",
        r"\\server\share\Strategy Builder\QQQ\data.csv",
        r"\\server\share\Strategy Builder\book\notes.csv",
        "//server/share/Strategy Builder/QQQ/data.csv",
        "//server/share/Strategy Builder/book/notes.csv",
        "'/home/jeu/Strategy Builder/QQQ/data.csv'",
    ]


def test_designer_free_text_cannot_carry_dates_paths_or_scale(qtbot, tmp_path):
    # These survive the date regex. Only the token filter removes them.
    for glued in ("Jan2024", "January2024", "FY2024", "year2024"):
        shown = designer_surface_text(glued, ())
        assert glued not in shown and "2024" not in shown
        assert not export_allowed(glued, symbols=(), scale=None)
    for glued in ("scale7.777", "x7.777"):
        shown = designer_surface_text(glued, (), scale=7.777)
        assert glued not in shown and "7.777" not in shown
        assert not export_allowed(glued, symbols=(), scale=7.777)
    assert designer_surface_text("price 2024.50 day 2024", ()) == "price 2024.50 day 2024"
    dates = _calendar_samples()
    paths = _path_samples()
    ordinary = "long_2x 2.5 2024.50 0.2024 day 2024 count 12"
    for sample in dates:
        shown = designer_surface_text(f"note {sample} tail", ("QQQ",), scale=2)
        assert sample not in shown, sample
        assert "2024" not in shown and "1999" not in shown and "2026" not in shown, sample
        assert not export_allowed(sample, symbols=(), scale=2), sample
    for sample in ("today 2024", "holiday 2024"):
        shown = designer_surface_text(sample, (), scale=2)
        assert "2024" not in shown, sample
        assert sample.split()[0] in shown
    for sample in paths:
        shown = designer_surface_text(f"open {sample} please", ("QQQ",), scale=2)
        assert sample not in shown
        for needle in ("Builder", "QQQ", "data.csv", "notes.csv", "/", "\\"):
            assert needle not in shown, (sample, needle)
        assert not export_allowed(sample, symbols=(), scale=None), sample
    kept = designer_surface_text(ordinary, ("QQQ",), scale=2)
    for piece in ("long_2x", "2.5", "2024.50", "0.2024", "day 2024", "12"):
        assert piece in kept, piece
    assert export_allowed(ordinary, symbols=("QQQ",), scale=2)
    assert not export_allowed(f"held QQQ overnight {ordinary}", symbols=("QQQ",), scale=2)
    assert not export_allowed("scale 2", symbols=(), scale=2)

    # A path consumes the rest of the field, commas included. The ordinary
    # tokens sit after that path, so this line does not keep them. The check
    # above keeps the same tokens when the line has no path.
    comment = "held QQQ " + " ".join(dates[:12]) + ", " + " ".join(paths[:4]) + ", " + ordinary + " scale 2"
    metrics = _metrics(
        anonymized=True,
        symbol="QQQ",
        comment=comment,
        n_fills=9,
        cost_basis={"share_scale": 2, "anonymized": True, "cost_schedule": "arena"},
    )
    folder = write_book(tmp_path / "fuzz", metrics, benchmark=False)
    window = _window(qtbot, tmp_path)
    page = window.results_page
    page.load_path(str(folder), audience="designer")
    QApplication.processEvents()
    shown = str(page._report.metrics.get("comment") or "")
    blob = "\n".join(
        [
            shown,
            page.identity.text(),
            page.cost_line.text(),
            page.chart.readout.text(),
            " ".join(page.chart._study["sessions"]),
            _failure_text(page),
        ]
    )
    assert "qqq" not in blob.casefold()
    assert "Builder" not in blob
    assert "\\" not in blob and "/home" not in blob
    assert "scale 2" not in blob
    for sample in dates[:12] + paths[:4]:
        assert sample not in blob, sample
    assert "held" in shown and "RANDOM" in shown
    for piece in ("long_2x", "2.5", "2024.50", "0.2024", "day 2024"):
        assert piece not in shown, piece
    exported = tmp_path / "copy"
    written = page.export_to(exported)
    assert "metrics.json" in written
    sheet = (exported / "metrics.json").read_text(encoding="utf-8")
    assert export_allowed(sheet, symbols=("QQQ",), scale=2)
    for sample in dates[:12] + paths[:4]:
        assert sample not in sheet
    assert "QQQ" not in sheet
    for piece in ("long_2x", "2.5", "2024.50", "day 2024"):
        assert piece not in sheet
