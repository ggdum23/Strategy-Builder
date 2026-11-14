"""Prepared-data table on the data page."""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from backtester.data import load_named
from backtester.gui.pages.data_page import DataPage
from backtester.prepared import list_entries
from backtester.synthetic import write_series


class _Host:
    def __init__(self):
        self.status = ""

    def start_task(self, fn, on_success, on_error):
        try:
            on_success(fn())
        except Exception as exc:
            on_error(exc)

    def set_status(self, text):
        self.status = text

    def remember(self, **_kwargs):
        return None


def test_prepared_table_lists_and_deletes(tmp_path, monkeypatch):
    cache = tmp_path / "prepared"
    cache.mkdir()
    monkeypatch.setenv("STRATEGY_BUILDER_PREPARED_DIR", str(cache))
    source = tmp_path / "src"
    write_series(source, "SYN", sessions=3)
    loaded = load_named(source, "SYN")
    key = os.path.basename(loaded.prepared)
    app = QApplication.instance() or QApplication([])
    page = DataPage(_Host())
    page.refresh_prepared()
    rows = page.prepared_table.raw_rows()
    assert [row["key"] for row in rows] == [key]
    assert rows[0]["symbol"] == "SYN"
    page.prepared_table.selectRow(0)
    page.delete_prepared()
    assert page.prepared_table.raw_rows() == []
    assert list_entries() == []
    app.processEvents()
