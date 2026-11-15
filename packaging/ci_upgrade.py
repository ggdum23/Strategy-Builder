import json
import os
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
    if not (destination / "backtester.exe").is_file():
        raise SystemExit(f"upgrade requires an existing install: {destination}")
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        raise SystemExit("LOCALAPPDATA is not set")
    user_root = Path(local) / "StrategyBuilder"
    config_path = user_root / "config.json"
    if not config_path.is_file():
        raise SystemExit(f"upgrade requires {config_path}")
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    payload["data_root"] = SENTINEL
    payload["ci_marker"] = "upgrade-keep"
    config_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    marker = user_root / "ci-upgrade.txt"
    marker.write_text("upgrade-keep", encoding="ascii")
    temp = Path(os.environ.get("TEMP") or os.environ.get("TMP") or ".")
    log = temp / "strategy-builder-upgrade.log"
    log.unlink(missing_ok=True)
    print(f"Upgrading {destination}")
    arguments = f'/VERYSILENT /CURRENTUSER /SUPPRESSMSGBOXES /NORESTART /DIR="{destination}" /LOG="{log}"'
    code = ci_process.wait_gui_process(str(setup), arguments)
    print(f"upgrade exit {code}")
    if code != 0:
        if log.is_file():
            print(log.read_text(encoding="utf-8", errors="replace"))
        raise SystemExit(f"upgrade exited {code}")
    after = json.loads(config_path.read_text(encoding="utf-8"))
    if after.get("data_root") != SENTINEL:
        raise SystemExit(f"upgrade rewrote data_root to {after.get('data_root')}")
    if after.get("ci_marker") != "upgrade-keep":
        raise SystemExit("upgrade dropped ci_marker")
    if marker.read_text(encoding="ascii").strip() != "upgrade-keep":
        raise SystemExit(f"upgrade removed {marker}")
    for name in ("backtester.exe", "Strategy Builder.exe", "unins000.exe"):
        if not (destination / name).is_file():
            raise SystemExit(f"upgrade removed {name}")
    if Path(SENTINEL).exists():
        raise SystemExit(f"upgrade created the sentinel data folder {SENTINEL}")
    print("upgrade preserved config.json")
    print(f"data_root {SENTINEL}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
