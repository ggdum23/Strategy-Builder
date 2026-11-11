"""What the frozen bundle picks up from the build environment.

PyInstaller already follows normal imports. This module adds the pieces that
are optional or easy to miss:

- every ``backtester`` submodule, so a later phase is collected by being
  installed, without a spec edit
- matplotlib's data files when matplotlib is installed
- PySide6, including the platforms, styles, and imageformats plugins, when
  PySide6 is installed
- pyqtgraph when it is installed (its own hook collects templates and data)

The Windows build installs the ``gui`` extra when ``pyproject.toml`` defines
one. Until then these Qt packages are absent and are not collected.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

QT_PLUGIN_KINDS = ("platforms", "styles", "imageformats")


def installed(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _unique(items: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        ordered.append(item)
    return ordered


def hiddenimports() -> list[str]:
    from PyInstaller.utils.hooks import collect_submodules

    hidden = [
        "backtester",
        "backtester.__main__",
        "backtester.runner",
        "backtester.data",
        "backtester.pack",
        "backtester.registry",
        "backtester.report",
        "backtester.engine_arena",
        "backtester.engine_desk",
        "backtester.costs",
        "backtester.costs_desk",
        "backtester.profiles",
        "backtester.strategy_loader",
        "backtester.arena_runner",
        "backtester.referee",
        "backtester.sandbox_child",
        "backtester.desktop",
        "strategy_builder_config",
        "multiprocessing",
        "multiprocessing.spawn",
        "multiprocessing.resource_tracker",
        "numpy",
        "pandas",
    ]
    if sys.platform == "win32":
        hidden.append("multiprocessing.popen_spawn_win32")
    else:
        hidden.append("multiprocessing.popen_spawn_posix")
    hidden.extend(collect_submodules("backtester"))
    if installed("matplotlib"):
        hidden.extend(
            [
                "matplotlib",
                "matplotlib.backends",
                "matplotlib.backends.backend_agg",
                "matplotlib.backends.backend_pdf",
                "matplotlib.figure",
                "matplotlib.pyplot",
            ]
        )
        if installed("PySide6"):
            hidden.append("matplotlib.backends.backend_qtagg")
    if installed("PySide6"):
        hidden.extend(
            [
                "PySide6",
                "PySide6.QtCore",
                "PySide6.QtGui",
                "PySide6.QtWidgets",
                "shiboken6",
            ]
        )
    if installed("pyqtgraph"):
        # The official hook collects submodules except examples (importing
        # examples constructs a Qt application). Name the package so that
        # hook runs; do not walk examples from here.
        hidden.append("pyqtgraph")
        hidden.append("pyqtgraph.multiprocess.bootstrap")
    return _unique(hidden)


def matplotlib_datas() -> list[tuple[str, str]]:
    if not installed("matplotlib"):
        return []
    import matplotlib

    data = matplotlib.get_data_path()
    if not data:
        raise SystemExit("matplotlib is installed but get_data_path() is empty")
    return [(data, "matplotlib/mpl-data")]


def qt_plugin_binaries() -> list[tuple[str, str]]:
    """platforms, styles, and imageformats, using PyInstaller's Qt layout.

    Hidden imports of QtGui and QtWidgets trigger the same official hooks.
    Collecting here as well keeps the three plugin directories explicit, and
    the archive dedupes a file that both paths name.
    """
    if not installed("PySide6"):
        return []
    from PyInstaller.utils.hooks.qt import pyside6_library_info

    if pyside6_library_info.version is None:
        raise SystemExit("PySide6 is installed but PyInstaller could not import it")
    binaries: list[tuple[str, str]] = []
    missing: list[str] = []
    for kind in QT_PLUGIN_KINDS:
        try:
            found = pyside6_library_info.collect_plugins(kind)
        except Exception as exc:
            missing.append(f"{kind} ({exc})")
            continue
        if not found:
            missing.append(kind)
            continue
        binaries.extend((str(src), str(Path(dest))) for src, dest in found)
    if missing:
        raise SystemExit(
            "PySide6 is installed but Qt plugins were not collected: "
            + ", ".join(missing)
            + ". The frozen GUI needs platforms, styles, and imageformats."
        )
    return binaries


def excludes() -> list[str]:
    # One Qt binding. PySide6 is the GUI extra. pytest is the source-checkout
    # test command and is not part of the frozen program.
    return [
        "PyQt5",
        "PyQt6",
        "PySide2",
        "pytest",
        "pyqtgraph.examples",
    ]
