"""Open the research terminal.

High-DPI scaling is left to Qt 6. The rounding policy is pass-through so a
Windows display at 150% does not snap the window to a blurrier step.
"""

from __future__ import annotations

import multiprocessing
import sys


def main(argv=None) -> int:
    multiprocessing.freeze_support()
    try:
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QGuiApplication
        from PySide6.QtWidgets import QApplication
    except ImportError:
        print(
            'error: the desktop window needs the gui extra: py -3 -m pip install -e ".[gui]"',
            file=sys.stderr,
        )
        return 2
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("StrategyBuilder.Backtester")
        except (AttributeError, OSError):
            pass
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication.instance() or QApplication(list(argv) if argv is not None else sys.argv)
    app.setApplicationName("Strategy Builder")
    app.setOrganizationName("StrategyBuilder")
    app.setStyle("Fusion")
    from .window import MainWindow

    window = MainWindow()
    window.show()
    return int(app.exec())
