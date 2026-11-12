"""Sort order and the display-string cache. Qt is optional for the core suite."""

from __future__ import annotations

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYTEST_QT_API", "pyside6")

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pytestqt")

from PySide6.QtCore import Qt

from backtester.gui.widgets import DictModel


def test_numeric_sort_order_is_locked(qtbot):
    rows = [
        {"name": "a", "v": -10, "text": "10"},
        {"name": "b", "v": float("nan"), "text": "100"},
        {"name": "c", "v": 2, "text": "2"},
        {"name": "d", "v": 100, "text": "9"},
        {"name": "e", "v": 2, "text": "10"},
        {"name": "f", "v": -10, "text": "9"},
        {"name": "g", "v": float("-inf"), "text": "2"},
        {"name": "h", "v": float("inf"), "text": "9"},
    ]
    model = DictModel(["v", "name"], rows, {"v": "v", "name": "name"})
    names = [rows[model.source_position(row)]["name"] for row in range(model.rowCount())]
    assert names == ["b", "g", "a", "f", "c", "e", "d", "h"]
    model.sort(0, Qt.SortOrder.DescendingOrder)
    names = [rows[model.source_position(row)]["name"] for row in range(model.rowCount())]
    assert names == ["h", "d", "c", "e", "a", "f", "g", "b"]
    shown = model.data(model.index(0, 0), Qt.ItemDataRole.DisplayRole)
    assert shown == "∞"
    words = DictModel(["text"], [{"text": "10"}, {"text": "100"}, {"text": "2"}, {"text": "9"}], {}, {})
    lexical = [words.rows[words.source_position(row)]["text"] for row in range(words.rowCount())]
    assert lexical == ["10", "100", "2", "9"]


def test_filter_uses_cached_display_strings(qtbot):
    rows = [{"a": index, "b": index * 0.5, "c": "row", "d": None} for index in range(100_000)]
    model = DictModel(["a", "b", "c", "d"], rows, {}, {"b": "money"})
    started = time.perf_counter()
    model.set_filter("row")
    elapsed = time.perf_counter() - started
    assert elapsed < 0.5
    assert model.rowCount() == 100_000
    model.set_filter("no-such-token")
    assert model.rowCount() == 0
