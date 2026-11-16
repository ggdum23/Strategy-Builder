import os
import subprocess
import sys
from pathlib import Path


def sign(setup_exe: Path) -> None:
    pfx = os.environ.get("SIGN_PFX_PATH", "")
    if not pfx:
        print("code signing skipped (SIGN_PFX_PATH is not set)")
        return
    if not Path(pfx).is_file():
        raise SystemExit("SIGN_PFX_PATH does not exist")
    password = os.environ.get("SIGN_PFX_PASSWORD", "")
    if not password:
        raise SystemExit("SIGN_PFX_PASSWORD is required when SIGN_PFX_PATH is set")
    program_files = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    kits = Path(program_files) / "Windows Kits" / "10" / "bin"
    matches = [path for path in kits.rglob("signtool.exe") if path.parent.name.casefold() == "x64"]
    if not matches:
        raise SystemExit(f"signtool.exe was not found under {kits}")
    signtool = sorted(matches, key=lambda path: str(path), reverse=True)[0]
    timestamp = os.environ.get("SIGN_TIMESTAMP_URL") or "http://timestamp.digicert.com"
    completed = subprocess.run(
        [str(signtool), "sign", "/fd", "SHA256", "/td", "SHA256", "/tr", timestamp, "/f", pfx, "/p", password, str(setup_exe)],
        check=False,
    )
    if completed.returncode != 0:
        raise SystemExit("signtool failed")
    print(f"signed {setup_exe}")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        raise SystemExit("usage: sign_setup.py SETUP")
    sign(Path(argv[1]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
