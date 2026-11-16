
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
