
from __future__ import annotations

from pathlib import Path


def test_python_311_gui_extra_excludes_pyside_612():
    text = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    reason = (
        "PySide6 6.12.0 drops references to True during widget teardown. "
        "Python 3.11 still reference-counts that singleton, so the process aborts. "
        "Python 3.12 and newer may use 6.12. The Windows installer stays on Python 3.12."
    )
    assert "PySide6>=6.6,<6.12; python_version < '3.12'" in text, reason
    assert "PySide6>=6.6; python_version >= '3.12'" in text, reason
