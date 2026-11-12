"""Chart ranges. The view has to contain every series, including the trough."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYTEST_QT_API", "pyside6")

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")
pytest.importorskip("pytestqt")

from PySide6.QtWidgets import QApplication

from backtester.gui.charts import ChartPanel


def _panel(qtbot) -> ChartPanel:
    panel = ChartPanel()
    qtbot.addWidget(panel)
    panel.resize(900, 640)
    panel.show()
    qtbot.waitExposed(panel)
    QApplication.processEvents()
    return panel


def test_view_contains_benchmark_and_drawdown_extrema(qtbot):
    panel = _panel(qtbot)
    panel.set_study(
        equity=[
            ("Strategy", [0, 1, 2, 3], [100, 110, 105, 108], "#8eb7f0"),
            ("1× buy and hold", [0, 1, 2, 3], [80, 40, 20, 90], "#b7aea2"),
        ],
        drawdown=[("drawdown", [0, 1, 2, 3], [0, -0.2, -0.95, -0.4], "#e06c75")],
        sessions=["d0", "d1", "d2", "d3"],
        equity_note="equity",
        drawdown_note="running peak picture",
    )
    QApplication.processEvents()
    assert panel.covered() == []


def test_log_scale_keeps_positive_extrema_and_refuses_a_non_positive_point(qtbot):
    panel = _panel(qtbot)
    panel.set_study(
        equity=[
            ("Strategy", [0, 1, 2, 3], [10, 50, 20, 80], "#8eb7f0"),
            ("1× buy and hold", [0, 1, 2, 3], [5, 12, 9, 30], "#b7aea2"),
        ],
        drawdown=[("drawdown", [0, 1, 2], [0, -0.4, -0.1], "#e06c75")],
        sessions=["d0", "d1", "d2", "d3"],
        equity_note="equity",
        drawdown_note="running peak picture",
    )
    panel.log_box.setChecked(True)
    QApplication.processEvents()
    assert panel.log_box.isChecked()
    assert panel.covered() == []
    panel.set_study(
        equity=[("Strategy", [0, 1, 2], [10, 0, 12], "#8eb7f0")],
        drawdown=[("drawdown", [0, 1, 2], [0, -1, -0.2], "#e06c75")],
        sessions=["d0", "d1", "d2"],
        equity_note="equity",
        drawdown_note="running peak picture",
    )
    panel.log_box.setChecked(True)
    QApplication.processEvents()
    assert panel.log_box.isChecked() is False
    assert "not above zero" in panel.readout.text()
