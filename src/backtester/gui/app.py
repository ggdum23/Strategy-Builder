
from __future__ import annotations

import multiprocessing
import sys
from pathlib import Path


def icon_file() -> Path:
    if getattr(sys, "frozen", False):
        root = getattr(sys, "_MEIPASS", None)
        base = Path(root) if root else Path(sys.executable).resolve().parent
        return base / "assets" / "strategy_builder.ico"
    return Path(__file__).resolve().parents[3] / "packaging" / "assets" / "strategy_builder.ico"


def apply_window_icon(app) -> None:
    from PySide6.QtGui import QIcon

    app.setWindowIcon(QIcon(str(icon_file())))


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
    apply_window_icon(app)
    from .window import MainWindow

    window = MainWindow()
    window.show()
    return int(app.exec())
