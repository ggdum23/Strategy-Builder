"""Twelfth review. Leading-digit ids stay visible, and a job row opens its designer book."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from synthetic import write_book
from test_window import _window

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QLabel

from backtester.gui.audience import peek_scale


def _batch(tmp_path: Path) -> Path:
    out = tmp_path / "out"
    metrics = {
        "profile": "arena",
        "initial_equity": 1_000_000,
        "final_equity": 1_040_000,
        "total_return": 0.04,
        "sharpe": 1.2,
        "anonymized": True,
    }
    published = {
        "1RANDOM": "1QQQ",
        "RANDOM1": "QQQ1",
        "RANDOM2momentum": "QQQ2momentum",
    }
    for folder, raw in published.items():
        write_book(out / "designer" / folder / "src", metrics, benchmark=False)
        job = out / "jobs" / raw / "src"
        job.mkdir(parents=True)
        (job / "metrics.json").write_text(
            json.dumps({"symbol": "QQQ", "cost_basis": {"share_scale": 1.1}}) + "\n",
            encoding="utf-8",
        )
    rows = ["rank,strategy_id,source_id,total_return,sharpe"]
    for index, folder in enumerate(published, start=1):
        rows.append(f"{index},{folder},src,0.04,1.2")
    (out / "designer" / "leaderboard.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
    (out / "run.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "parallel",
                "jobs": [
                    {"strategy_id": raw, "source_id": "src", "symbol": "QQQ", "status": "ok"}
                    for raw in published.values()
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return out


def _column(model, column: int) -> list[str]:
    return [str(model.data(model.index(row, column)) or "") for row in range(model.rowCount())]


def test_leading_digit_ids_show_and_double_click_opens_the_designer_book(qtbot, tmp_path):
    out = _batch(tmp_path)
    assert peek_scale(out) == 1.1
    assert peek_scale(out / "designer") == 1.1
    window = _window(qtbot, tmp_path)
    window.show_page(4)
    page = window.results_page
    page.load_path(str(out), audience="designer")
    QApplication.processEvents()
    assert page._report is not None
    assert page._report.audience == "designer"
    labels = [widget.text() for widget in page.findChildren(QLabel)]
    blob = "\n".join(labels)
    assert "share_scale" not in blob
    assert "scale 1.1" not in blob
    jobs = page.jobs.model()
    assert jobs is not None
    shown = _column(jobs, 0)
    assert any("1RANDOM" in text for text in shown)
    assert any("RANDOM1" in text for text in shown)
    assert any("RANDOM2momentum" in text for text in shown)
    assert all("QQQ" not in text for text in shown)
    assert all(text != "—" for text in shown)
    board = page.leaderboard.model()
    assert board is not None
    cells = [text for column in range(board.columnCount()) for text in _column(board, column)]
    assert "1RANDOM" in cells
    assert "RANDOM1" in cells
    assert "RANDOM2momentum" in cells
    target = None
    for row in range(jobs.rowCount()):
        if "1RANDOM" in str(jobs.data(jobs.index(row, 0)) or ""):
            target = jobs.index(row, 0)
            break
    assert target is not None
    page.jobs.doubleClicked.emit(target)
    QApplication.processEvents()
    report = page._report
    assert report is not None
    assert report.kind == "book"
    assert report.directory is not None
    assert report.directory.name == "src"
    assert report.directory.parent.name == "1RANDOM"
    assert "designer" in report.directory.parts
    assert "jobs" not in report.directory.parts
    assert report.curves
    assert "No equity" not in page.chart.caption.text()
