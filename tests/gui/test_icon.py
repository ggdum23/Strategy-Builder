from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYTEST_QT_API", "pyside6")

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pytestqt")

from PySide6.QtWidgets import QApplication

from backtester.gui.app import apply_window_icon, icon_file

ROOT = Path(__file__).resolve().parents[2]


def test_application_icon_is_set(qtbot):
    app = QApplication.instance() or QApplication([])
    previous = app.windowIcon()
    apply_window_icon(app)
    icon = app.windowIcon()
    assert not icon.isNull()
    assert icon.availableSizes()
    assert icon_file() == ROOT / "packaging" / "assets" / "strategy_builder.ico"
    app.setWindowIcon(previous)


def test_frozen_bundle_icon_is_set(qtbot, monkeypatch, tmp_path):
    bundled = tmp_path / "assets"
    bundled.mkdir()
    target = bundled / "strategy_builder.ico"
    target.write_bytes((ROOT / "packaging" / "assets" / "strategy_builder.ico").read_bytes())
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    app = QApplication.instance() or QApplication([])
    previous = app.windowIcon()
    apply_window_icon(app)
    assert icon_file() == target
    assert not app.windowIcon().isNull()
    app.setWindowIcon(previous)
