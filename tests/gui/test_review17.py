"""Seventeenth review. Failure banners, field names, and the results sentence."""

from __future__ import annotations

import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYTEST_QT_API", "pyside6")

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pytestqt")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLabel

from backtester.gui.jobs import JobFailure, failure_line
from test_review5 import _banners, _table_text
from test_window import _window

_TRACE = (
    "Traceback (most recent call last):\n"
    '  File "strategy.py", line 3, in on_bar\n'
    "    raise ValueError('book rejected\\nthe bar')\n"
    "ValueError: book rejected\n"
    "the bar\n"
)


def _labels(page) -> str:
    found = []
    for index in range(page.banners.count()):
        widget = page.banners.itemAt(index).widget()
        if widget is None:
            continue
        found.extend(label.text() for label in widget.findChildren(QLabel))
    return "\n".join(found)


def _tips(page) -> str:
    found = []
    for index in range(page.banners.count()):
        widget = page.banners.itemAt(index).widget()
        if widget is not None and widget.toolTip():
            found.append(widget.toolTip())
    return "\n".join(found)


def _error_tip(table, job: str) -> str:
    model = table.model()
    headers = [
        str(model.headerData(column, Qt.Orientation.Horizontal, Qt.ItemDataRole.DisplayRole) or "")
        for column in range(model.columnCount())
    ]
    job_column = headers.index("Job")
    error_column = headers.index("Error")
    for row in range(model.rowCount()):
        name = str(model.data(model.index(row, job_column), Qt.ItemDataRole.DisplayRole) or "")
        if job in name:
            return str(model.data(model.index(row, error_column), Qt.ItemDataRole.ToolTipRole) or "")
    return ""


def test_returning_to_run_keeps_the_failure_banner(qtbot, tmp_path):
    window = _window(qtbot, tmp_path)
    page = window.run_page
    failure = JobFailure(
        "ValueError",
        "book rejected the bar",
        "Traceback (most recent call last):\nValueError: book rejected the bar\n",
        True,
    )
    page.on_failed(failure)
    sentence = failure_line("ValueError", "book rejected the bar")
    window.show_page(0)
    window.show_page(3)
    QApplication.processEvents()
    assert sentence in _banners(page)
    assert "Traceback" in page.log.toPlainText()
    page.refresh()
    assert sentence in _banners(page)
    page.set_running(True)
    assert sentence not in _banners(page)


def test_system_exit_banners_a_sentence(qtbot, tmp_path):
    window = _window(qtbot, tmp_path)
    failure = JobFailure("SystemExit", "3", "Traceback (most recent call last):\nSystemExit: 3\n")
    window.run_page.on_failed(failure)
    QApplication.processEvents()
    assert _banners(window.run_page) == "SystemExit: 3"


def test_designer_left_blank_banner_names_the_field(qtbot, tmp_path):
    folder = tmp_path / "book"
    folder.mkdir()
    (folder / "metrics.json").write_text(
        json.dumps({"anonymized": True, "profile": "arena", "initial_equity": 1_000_000}) + "\n",
        encoding="utf-8",
    )
    window = _window(qtbot, tmp_path)
    page = window.results_page
    page.load_path(str(folder), audience="designer")
    QApplication.processEvents()
    banners = _banners(page)
    assert "benchmarks" in banners
    assert "left blank" in banners


def test_results_cells_show_the_exception_line_not_the_traceback(qtbot, tmp_path):
    root = tmp_path / "batch"
    alpha = root / "jobs" / "alpha" / "QQQ"
    beta = root / "jobs" / "beta" / "QQQ"
    alpha.mkdir(parents=True)
    beta.mkdir(parents=True)
    (alpha / "metrics.json").write_text("{}\n", encoding="utf-8")
    (beta / "metrics.json").write_text("{}\n", encoding="utf-8")
    (alpha / "error.json").write_text(json.dumps({"error": _TRACE}) + "\n", encoding="utf-8")
    (beta / "error.json").write_text(
        json.dumps({"error": "first line of the failure\nsecond line stays in the tip"}) + "\n",
        encoding="utf-8",
    )
    (root / "leaderboard.csv").write_text(
        "strategy_id,source_id,status,error\n"
        "alpha,QQQ,error,x\n"
        "beta,QQQ,error,x\n",
        encoding="utf-8",
    )
    (root / "run.json").write_text(
        json.dumps(
            {
                "kind": "parallel",
                "jobs": [
                    {"strategy_id": "alpha", "source_id": "QQQ", "status": "error"},
                    {"strategy_id": "beta", "source_id": "QQQ", "status": "error"},
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    designer = root / "designer" / "alpha" / "QQQ"
    designer.mkdir(parents=True)
    (designer / "error.json").write_text(json.dumps({"error": "the strategy failed"}) + "\n", encoding="utf-8")
    (root / "designer" / "leaderboard.csv").write_text(
        "strategy_id,source_id,status,error\nalpha,QQQ,error,the strategy failed\n",
        encoding="utf-8",
    )
    window = _window(qtbot, tmp_path)
    page = window.results_page
    page.load_path(str(root), audience="operator")
    QApplication.processEvents()
    table = _table_text(page.jobs)
    labels = _labels(page)
    tips = _tips(page) + "\n" + _error_tip(page.jobs, "alpha")
    assert "ValueError: book rejected" in table
    assert "ValueError: book rejected" in labels
    assert "first line of the failure" in table
    assert "first line of the failure" in labels
    assert "Traceback (most recent call last):" not in table
    assert "Traceback (most recent call last):" not in labels
    assert "the bar" not in table
    assert "the bar" not in labels
    assert "second line stays in the tip" not in table
    assert "second line stays in the tip" not in labels
    assert "Traceback (most recent call last):" in tips
    assert "the bar" in tips
    assert "second line stays in the tip" in _error_tip(page.jobs, "beta")
    page.load_path(str(root), audience="designer")
    QApplication.processEvents()
    shown = _banners(page) + "\n" + _table_text(page.jobs) + "\n" + _tips(page)
    assert "the strategy failed" in shown
    assert "Traceback" not in shown
    assert "book rejected" not in shown.casefold()
    assert "share_scale" not in shown.casefold()
