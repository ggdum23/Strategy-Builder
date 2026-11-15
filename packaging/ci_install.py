import json
import os
import re
import sys
from pathlib import Path

import ci_process

ROOT = Path(__file__).resolve().parents[1]


def _setup_exe() -> Path:
    matches = sorted((ROOT / "packaging" / "dist").glob("StrategyBuilder-Setup-*.exe"))
    if len(matches) != 1:
        raise SystemExit(f"expected one setup executable, found {len(matches)}")
    return matches[0]


def _destination() -> Path:
    value = os.environ.get("SB_INSTALL_DIR", "")
    if not value:
        raise SystemExit("SB_INSTALL_DIR is required")
    return Path(value)


def main() -> int:
    destination = _destination()
    setup = _setup_exe()
    if " " not in str(destination):
        raise SystemExit(f"install directory must contain a space: {destination}")
    if not setup.is_file():
        raise SystemExit(f"setup executable not found: {setup}")
    iss = (ROOT / "packaging" / "strategy_builder.iss").read_text(encoding="utf-8")
    matched = re.search(r'#define DefaultDataRoot "([^"]+)"', iss)
    if matched is None:
        raise SystemExit("DefaultDataRoot is not defined in strategy_builder.iss")
    expected_root = matched.group(1)
    root_existed = Path(expected_root).exists()
    temp = Path(os.environ.get("TEMP") or os.environ.get("TMP") or ".")
    log = temp / "strategy-builder-install.log"
    log.unlink(missing_ok=True)
    print(f"Installing to {destination}")
    arguments = f'/VERYSILENT /CURRENTUSER /SUPPRESSMSGBOXES /NORESTART /DIR="{destination}" /LOG="{log}"'
    code = ci_process.wait_gui_process(str(setup), arguments)
    print(f"installer exit {code}")
    if code != 0:
        if log.is_file():
            print(log.read_text(encoding="utf-8", errors="replace"))
        raise SystemExit(f"installer exited {code}")
    required = [
        destination / "backtester.exe",
        destination / "Strategy Builder.exe",
        destination / "unins000.exe",
        destination / "defaults.json",
        destination / "README.md",
        destination / "LICENSE.txt",
    ]
    for path in required:
        if not path.is_file():
            if log.is_file():
                print(log.read_text(encoding="utf-8", errors="replace"))
            raise SystemExit(f"install did not write {path}")
    defaults = json.loads((destination / "defaults.json").read_text(encoding="utf-8"))
    if defaults.get("data_root") != expected_root:
        raise SystemExit(f"defaults.json data_root is {defaults.get('data_root')}, expected {expected_root}")
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        raise SystemExit("LOCALAPPDATA is not set")
    config_path = Path(local) / "StrategyBuilder" / "config.json"
    if not config_path.is_file():
        raise SystemExit(f"per-user config was not written: {config_path}")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if config.get("data_root") != expected_root:
        raise SystemExit(f"config.json data_root is {config.get('data_root')}, expected {expected_root}")
    runs = Path(local) / "StrategyBuilder" / "runs"
    if not runs.is_dir():
        raise SystemExit("runs directory was not created")
    if not root_existed and Path(expected_root).exists():
        raise SystemExit(f"installer created the market-data folder {expected_root}")
    print(f"installed {destination}")
    print(f"data_root {expected_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
