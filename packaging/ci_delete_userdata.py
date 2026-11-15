import os
import re
import subprocess
import sys
from pathlib import Path

import ci_process

ROOT = Path(__file__).resolve().parents[1]
SENTINEL = r"D:\strategy-builder-ci-upgrade"


def _destination() -> Path:
    value = os.environ.get("SB_INSTALL_DIR", "")
    if not value:
        raise SystemExit("SB_INSTALL_DIR is required")
    return Path(value)


def _setup_exe() -> Path:
    matches = sorted((ROOT / "packaging" / "dist").glob("StrategyBuilder-Setup-*.exe"))
    if len(matches) != 1:
        raise SystemExit(f"expected one setup executable, found {len(matches)}")
    return matches[0]


def main() -> int:
    destination = _destination()
    setup = _setup_exe()
    if " " not in str(destination):
        raise SystemExit(f"install directory must contain a space: {destination}")
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        raise SystemExit("LOCALAPPDATA is not set")
    user_root = Path(local) / "StrategyBuilder"
    config_path = user_root / "config.json"
    upgrade_marker = user_root / "ci-upgrade.txt"
    if not config_path.is_file():
        raise SystemExit(f"keep-uninstall removed {config_path}")
    if not upgrade_marker.is_file():
        raise SystemExit(f"keep-uninstall removed {upgrade_marker}")
    iss = (ROOT / "packaging" / "strategy_builder.iss").read_text(encoding="utf-8")
    matched = re.search(r'#define DefaultDataRoot "([^"]+)"', iss)
    if matched is None:
        raise SystemExit("DefaultDataRoot is not defined in strategy_builder.iss")
    default_root = matched.group(1)
    default_existed = Path(default_root).exists()
    temp = Path(os.environ.get("TEMP") or os.environ.get("TMP") or ".")
    log = temp / "strategy-builder-reinstall.log"
    log.unlink(missing_ok=True)
    print(f"Reinstalling {destination} before DELETEUSERDATA")
    arguments = f'/VERYSILENT /CURRENTUSER /SUPPRESSMSGBOXES /NORESTART /DIR="{destination}" /LOG="{log}"'
    code = ci_process.wait_gui_process(str(setup), arguments)
    print(f"reinstall exit {code}")
    if code != 0:
        if log.is_file():
            print(log.read_text(encoding="utf-8", errors="replace"))
        raise SystemExit(f"reinstall exited {code}")
    sentinel = Path(SENTINEL)
    sentinel_file = sentinel / "ci-sentinel-keep.txt"
    sentinel.mkdir(parents=True, exist_ok=True)
    sentinel_file.write_text("survive", encoding="ascii")
    temp_root = temp / "strategy-builder-data-root"
    temp_file = temp_root / "ci-data-root-keep.txt"
    temp_root.mkdir(parents=True, exist_ok=True)
    temp_file.write_text("survive", encoding="ascii")
    completed = subprocess.run([sys.executable, str(ROOT / "packaging" / "ci_uninstall.py"), "--delete-user-data"], cwd=ROOT, check=False)
    if completed.returncode != 0:
        raise SystemExit(f"delete uninstall exited {completed.returncode}")
    if user_root.exists():
        raise SystemExit(f"user data still exists: {user_root}")
    if not default_existed and Path(default_root).exists():
        raise SystemExit(f"DELETEUSERDATA created the market-data folder {default_root}")
    for planted in (sentinel_file, temp_file):
        if not planted.is_file():
            raise SystemExit(f"DELETEUSERDATA removed {planted}")
        if planted.read_text(encoding="ascii").strip() != "survive":
            raise SystemExit(f"DELETEUSERDATA changed {planted}")
    print("deleted user data")
    print("kept market data")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
