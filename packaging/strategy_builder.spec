# -*- mode: python ; coding: utf-8 -*-
"""One-folder bundle: console backtester.exe and windowed Strategy Builder.exe.

One-file mode is intentionally not used. Startup is faster and antivirus
products are less likely to flag a directory of ordinary DLLs than a
self-extracting executable.

Both executables embed this spec's entry script and share _internal.
The entry dispatches on the executable name. Spawn children re-enter
whichever executable called them; freeze_support runs before that dispatch.
"""

import sys
from pathlib import Path

from PyInstaller.building.api import COLLECT, EXE, PYZ
from PyInstaller.building.build_main import Analysis

sys.path.insert(0, SPECPATH)
import collect_optional
import versions

ROOT = Path(SPECPATH).resolve().parent
ENTRY = str(ROOT / "packaging" / "frozen_entry.py")
ICON = str(ROOT / "packaging" / "assets" / "strategy_builder.ico")
# EXE(version=) loads a VSVersionInfo file. versions.version_info() is the
# four-part string (0.1.0.0); the file is that string turned into a resource.
VERSION_FILE = ROOT / "packaging" / "build" / "file_version_info.txt"
VERSION_FILE.parent.mkdir(parents=True, exist_ok=True)
VERSION_FILE.write_text(versions.version_info_text(), encoding="utf-8")

datas = collect_optional.matplotlib_datas()
binaries = collect_optional.qt_plugin_binaries()
hidden = collect_optional.hiddenimports()

a = Analysis(
    [ENTRY],
    pathex=[str(ROOT / "src"), str(ROOT / "packaging")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    # Agg only. Automatic discovery selects QtAgg once PySide6 is installed,
    # and report workers must stay headless. The GUI can add QtAgg here if
    # it calls matplotlib.use("QtAgg") before importing pyplot.
    hooksconfig={"matplotlib": {"backends": ["Agg"]}},
    runtime_hooks=[],
    excludes=collect_optional.excludes(),
    noarchive=False,
)
pyz = PYZ(a.pure)

_exe_common = dict(
    exclude_binaries=True,
    upx=False,
    strip=False,
    disable_windowed_traceback=False,
    contents_directory="_internal",
    icon=ICON,
    bootloader_ignore_signals=False,
    version=str(VERSION_FILE),
)

exe_cli = EXE(
    pyz,
    a.scripts,
    [],
    name="backtester",
    console=True,
    **_exe_common,
)
exe_gui = EXE(
    pyz,
    a.scripts,
    [],
    name="Strategy Builder",
    console=False,
    **_exe_common,
)

coll = COLLECT(
    exe_cli,
    exe_gui,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="StrategyBuilder",
)
