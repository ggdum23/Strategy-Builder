"""GUI tests. Qt is offscreen. The core suite skips these when PySide6 is absent."""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYTEST_QT_API", "pyside6")
os.environ.setdefault("MPLBACKEND", "Agg")
