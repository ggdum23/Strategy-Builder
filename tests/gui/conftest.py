"""GUI tests. Qt is offscreen. The core suite skips these when PySide6 is absent."""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYTEST_QT_API", "pyside6")
os.environ.setdefault("MPLBACKEND", "Agg")


def pytest_sessionfinish(session, exitstatus):
    """Leave before PySide tears down on Linux.

    Python 3.11 on Linux finishes the suite and then aborts in the
    interpreter finalizer (``bool_dealloc`` while garbage-collecting).
    The tests have already reported. Ending the process here keeps that
    abort from failing the job. Windows raises an access violation inside
    ``os._exit`` while Qt is still loaded, and it does not hit the Linux
    abort, so it returns normally.
    """
    if sys.platform == "win32":
        return
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        return
    if QApplication.instance() is None:
        return
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(int(exitstatus))
