import os
import subprocess
import sys
from pathlib import Path

PACKAGES = (
    "libegl1 libgl1 libglib2.0-0 libdbus-1-3 libxkbcommon0 libfontconfig1 "
    "libx11-xcb1 libxcb-cursor0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 "
    "libxcb-randr0 libxcb-render-util0 libxcb-shape0 libxcb-xfixes0 "
    "libxcb-xinerama0 libxrender1 libxi6 libsm6 libice6 fonts-dejavu-core"
).split()


def show_sources() -> None:
    print("--- apt sources ---")
    mirrors = Path("/etc/apt/apt-mirrors.txt")
    if mirrors.is_file():
        print("apt-mirrors.txt:")
        print(mirrors.read_text(errors="replace"), end="")
    else:
        print("no /etc/apt/apt-mirrors.txt")
    subprocess.run(
        ["grep", "-R", "-n", "-E", "URIs:|^deb ", "/etc/apt/sources.list", "/etc/apt/sources.list.d"],
        check=False,
    )


def use_direct_mirror(mirror: str) -> None:
    script = Path(os.environ["GITHUB_WORKSPACE"]) / ".github" / "rewrite_apt_sources.py"
    subprocess.run(["sudo", sys.executable, str(script), mirror], check=True)


def use_mirror_file() -> None:
    text = (
        "https://mirrors.edge.kernel.org/ubuntu/\tpriority:1\n"
        "https://ftp.osuosl.org/pub/ubuntu/\tpriority:2\n"
    )
    subprocess.run(["sudo", "tee", "/etc/apt/apt-mirrors.txt"], input=text, text=True, stdout=subprocess.DEVNULL, check=True)
    subprocess.run(["sudo", "rm", "-rf", "/var/lib/apt/lists"], check=True)
    subprocess.run(["sudo", "mkdir", "-p", "/var/lib/apt/lists/partial"], check=True)


def install_qt() -> bool:
    updated = subprocess.run(["sudo", "timeout", "180", "apt-get", "update"], check=False)
    if updated.returncode != 0:
        return False
    installed = subprocess.run(
        ["sudo", "timeout", "180", "apt-get", "install", "-y", "--no-install-recommends", *PACKAGES],
        check=False,
    )
    return installed.returncode == 0


def main() -> int:
    show_sources()
    ok = False
    resolved = subprocess.run(
        ["timeout", "8", "getent", "hosts", "archive.ubuntu.com"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if resolved.returncode == 0:
        if install_qt():
            ok = True
        else:
            print("apt via image sources failed")
    else:
        print("archive.ubuntu.com did not resolve")
    if not ok:
        print("switching /etc/apt/apt-mirrors.txt to non-Canonical mirrors")
        use_mirror_file()
        show_sources()
        if install_qt():
            ok = True
        else:
            print("apt via apt-mirrors.txt failed")
    if not ok:
        for mirror in ("https://mirrors.edge.kernel.org/ubuntu", "https://ftp.osuosl.org/pub/ubuntu"):
            print(f"retrying with a direct URI {mirror}")
            use_direct_mirror(mirror)
            show_sources()
            if install_qt():
                ok = True
                break
            print(f"apt via {mirror} failed")
    if not ok:
        return 1
    tesseract = subprocess.run(
        ["sudo", "timeout", "180", "apt-get", "install", "-y", "--no-install-recommends", "tesseract-ocr"],
        check=False,
    )
    if tesseract.returncode != 0:
        print("tesseract unavailable; the OCR check will skip")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
