"""Eleventh review. A parallel designer export keeps the formatted leaderboard."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_parallel import FLAT, LONG, _anon_pack, _strategy
from test_window import _window

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from backtester.gui.audience import export_allowed
from backtester.gui.backend import TableData
from backtester.parallel import PackMarket, run_parallel
from windows_open import windows_jails


_SCALES = (0.25, 0.5, 0.7, 2.0, 7.777)


@windows_jails(4)
def test_parallel_designer_export_writes_the_leaderboard_at_every_scale(qtbot, tmp_path, monkeypatch):
    _export_leaderboard(qtbot, tmp_path, monkeypatch)


def _export_leaderboard(qtbot, tmp_path, monkeypatch):
    pack, sidecar, _built = _anon_pack(tmp_path, "QQQ", name="board", price_factor=1.1)
    out = tmp_path / "out"
    run_parallel(
        [
            _strategy(tmp_path / "strategies", "mean_rev_3", LONG),
            _strategy(tmp_path / "strategies", "opening_drive", FLAT),
        ],
        [PackMarket(str(pack), str(sidecar))],
        out,
        profile="arena",
        workers=1,
        timeout_s=60,
        with_benchmarks=False,
    )
    window = _window(qtbot, tmp_path)
    window.show_page(4)
    page = window.results_page
    page.load_path(str(out), audience="designer")
    QApplication.processEvents()
    assert page._report is not None
    assert page._report.audience == "designer"
    assert page._report.leaderboard is not None
    assert page.leaderboard.isVisible()
    model = page.leaderboard.model()
    assert model is not None and model.columnCount()
    for scale in _SCALES:
        monkeypatch.setattr("backtester.gui.pages.results_page.peek_scale", lambda _folder, _scale=scale: _scale)
        exported = tmp_path / f"export-{scale}"
        written = page.export_to(exported)
        assert "leaderboard_view.csv" in written
        board = (exported / "leaderboard_view.csv").read_text(encoding="utf-8")
        assert export_allowed(board, symbols=(), scale=scale)
        header = board.splitlines()[0]
        keys = [cell.split("|", 1)[0] for cell in header.split(",")]
        assert "sharpe" in keys
        assert "total_return" in keys
        assert "Sharpe" not in keys
        assert "mean_rev_3" in board
        status = window._status.currentMessage()
        assert "leaderboard_view.csv" in status
        assert status != "Nothing to export"

    board = page._report.leaderboard
    rows = [dict(row) for row in board.rows]
    rows[0]["error"] = "close 450.12"
    page._report.leaderboard = TableData(board.columns, tuple(rows), board.source)
    page._fill_leaderboard(page._report)
    QApplication.processEvents()
    withheld = tmp_path / "withheld"
    written = page.export_to(withheld)
    assert "leaderboard_view.csv" not in written
    assert not (withheld / "leaderboard_view.csv").exists()
    status = window._status.currentMessage()
    assert "leaderboard_view.csv" in status
    assert "price" in status.casefold()
    assert status != "Nothing to export"
