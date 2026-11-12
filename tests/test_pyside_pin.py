"""Python 3.11 must not resolve the GUI extra to PySide6 6.12."""

from __future__ import annotations

from pathlib import Path


def test_python_311_gui_extra_excludes_pyside_612():
    text = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    assert "PySide6>=6.6,<6.12; python_version < '3.12'" in text
    assert "PySide6>=6.6; python_version >= '3.12'" in text
