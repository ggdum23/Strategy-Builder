import os
import subprocess
import sys
import time
from pathlib import Path

import ci_process

ROOT = Path(__file__).resolve().parents[1]
SENTINEL = r"D:\strategy-builder-ci-upgrade"


def _destination() -> Path:
    value = os.environ.get("SB_INSTALL_DIR", "")
    if not value:
        raise SystemExit("SB_INSTALL_DIR is required")
    return Path(value)


def _user_paths() -> tuple[Path, Path, Path, Path]:
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        raise SystemExit("LOCALAPPDATA is not set")
    root = Path(local) / "StrategyBuilder"
    return root, root / "config.json", root / "runs", root / "runs" / "ci-preserve.txt"


def _invoke_guard(mode: str, log: Path, config: Path, user_root: Path) -> tuple[int, str]:
    guard = ROOT / "packaging" / "uninstall_guard.py"
    root_flag = "yes" if user_root.exists() else "no"
    completed = subprocess.run(
        [
            sys.executable,
            str(guard),
            "--mode",
            mode,
            "--sentinel",
            SENTINEL,
            "--log-file",
            str(log),
            "--config",
            str(config),
            "--root-exists",
            root_flag,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    lines = [line.strip() for line in (completed.stdout or "").splitlines() if line.strip()]
    text = lines[-1] if lines else ""
    return int(completed.returncode), text


def _wait_settled(mode: str, log: Path, config: Path, user_root: Path) -> None:
    deadline = time.monotonic() + 30
    while True:
        code, text = _invoke_guard(mode, log, config, user_root)
        if code != 0:
            if log.is_file():
                print(log.read_text(encoding="utf-8", errors="replace"))
            raise SystemExit(text)
        idle = not ci_process.uninstall_phase_running()
        if text == "ok" and idle:
            again, again_text = _invoke_guard(mode, log, config, user_root)
            if again != 0:
                if log.is_file():
                    print(log.read_text(encoding="utf-8", errors="replace"))
                raise SystemExit(again_text)
            if again_text == "ok":
                return
        if time.monotonic() >= deadline:
            if log.is_file():
                print(log.read_text(encoding="utf-8", errors="replace"))
            if mode == "delete":
                raise SystemExit(f"DELETEUSERDATA left {user_root}")
            raise SystemExit("uninstall keep did not see the keeping line within 30s")
        time.sleep(0.2)


def main() -> int:
    delete_user_data = "--delete-user-data" in sys.argv[1:]
    destination = _destination()
    user_root, config, runs, marker = _user_paths()
    unins = destination / "unins000.exe"
    if not unins.is_file():
        if delete_user_data:
            raise SystemExit(f"uninstaller not found: {unins}")
        print("uninstall skipped; nothing was installed")
        return 0
    if not delete_user_data:
        runs.mkdir(parents=True, exist_ok=True)
        marker.write_text("keep", encoding="ascii")
    temp = Path(os.environ.get("TEMP") or os.environ.get("TMP") or ".")
    log = temp / "strategy-builder-uninstall.log"
    log.unlink(missing_ok=True)
    flag = " /DELETEUSERDATA" if delete_user_data else ""
    mode = "delete" if delete_user_data else "keep"
    arguments = f'/VERYSILENT /SUPPRESSMSGBOXES /NORESTART{flag} /LOG="{log}"'
    code = ci_process.wait_gui_process(str(unins), arguments)
    print(f"uninstaller exit {code}")
    cli = destination / "backtester.exe"
    cli_deadline = time.monotonic() + 90
    while cli.exists() and time.monotonic() < cli_deadline:
        time.sleep(1)
    if cli.exists():
        if log.is_file():
            print(log.read_text(encoding="utf-8", errors="replace"))
        raise SystemExit(f"uninstall left {cli} (exit {code})")
    if code != 0:
        if log.is_file():
            print(log.read_text(encoding="utf-8", errors="replace"))
        raise SystemExit(f"uninstaller exited {code}")
    _wait_settled(mode, log, config, user_root)
    if delete_user_data:
        print(f"uninstalled {destination}")
        print(f"deleted {user_root}")
        return 0
    print(f"uninstalled {destination}")
    print(f"kept {user_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
