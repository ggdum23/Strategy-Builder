import hashlib
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from importlib.metadata import version
from pathlib import Path

import ci_process

ROOT = Path(__file__).resolve().parents[1]
INNO_VERSION = "6.7.4"
INNO_URL = "https://github.com/jrsoftware/issrc/releases/download/is-6_7_4/innosetup-6.7.4.exe"
INNO_SHA256 = "a549929319f9120716677c685535ca4671e4b937b5fdc87e2585f32d8eb30e8c"
PYINSTALLER_PIN = "6.22.3"


def _run_python(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, *args], cwd=ROOT, check=False, capture_output=True, text=True)


def _python_text(*args: str) -> str:
    completed = _run_python(*args)
    if completed.returncode != 0:
        raise SystemExit(completed.stderr.strip() or completed.stdout.strip() or "python command failed")
    return completed.stdout.strip()


def _iscc_version_text(path: str) -> str:
    probe = Path(os.environ.get("TEMP") or os.environ.get("TMP") or ".") / "strategy-builder-iscc-probe.iss"
    probe.write_bytes(b"; version probe\r\n")
    completed = subprocess.run([path, str(probe)], check=False, capture_output=True, text=True)
    output = completed.stdout + completed.stderr
    matched = re.search(r"Compiler engine version:\s+Inno Setup\s+(\d+\.\d+\.\d+(?:\.\d+)?)", output)
    if matched is None:
        return ""
    return matched.group(1)


def _exact_inno_version(text: str) -> bool:
    parts = [part for part in text.split(".") if part != ""]
    required = [part for part in INNO_VERSION.split(".") if part != ""]
    if len(parts) != len(required):
        return False
    for got, want in zip(parts, required):
        if got != want:
            return False
    return True


def _install_inno() -> str:
    temp = Path(os.environ.get("TEMP") or os.environ.get("TMP") or ".")
    installer = temp / f"innosetup-{INNO_VERSION}.exe"
    dest = ROOT / "packaging" / "build" / f"innosetup-{INNO_VERSION}"
    print(f"Downloading Inno Setup {INNO_VERSION}")
    request = urllib.request.Request(INNO_URL, headers={"User-Agent": "strategy-builder-build"})
    with urllib.request.urlopen(request) as response, installer.open("wb") as handle:
        shutil.copyfileobj(response, handle)
    digest = hashlib.sha256(installer.read_bytes()).hexdigest()
    if digest != INNO_SHA256:
        installer.unlink(missing_ok=True)
        raise SystemExit(f"Inno Setup download hash is {digest}, expected {INNO_SHA256}")
    print("Inno Setup download hash ok")
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    arguments = f'/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP- /DIR="{dest}"'
    code = ci_process.wait_gui_process(str(installer), arguments)
    if code not in (0, 3010):
        raise SystemExit(f"Inno Setup installer exited {code}")
    found = next(dest.rglob("ISCC.exe"), None)
    if found is not None:
        return str(found)
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    program_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    for fallback in (
        Path(program_x86) / "Inno Setup 6" / "ISCC.exe",
        Path(program_files) / "Inno Setup 6" / "ISCC.exe",
    ):
        if not fallback.is_file():
            continue
        if _exact_inno_version(_iscc_version_text(str(fallback))):
            return str(fallback)
    raise SystemExit(f"ISCC.exe was not installed to {dest}")


def _resolve_iscc() -> str:
    print(f"Installing pinned Inno Setup {INNO_VERSION} (preinstalled compilers are not used)")
    return _install_inno()


def main() -> int:
    os.chdir(ROOT)
    versions = ROOT / "packaging" / "versions.py"
    checked = _run_python(str(versions), "--check")
    if checked.returncode != 0:
        raise SystemExit("version check failed")
    project = _python_text(str(versions), "--print", "project")
    app_version = _python_text(str(versions), "--print", "inno")
    version_info = _python_text(str(versions), "--print", "version-info")
    pyi = version("pyinstaller")
    if pyi != PYINSTALLER_PIN:
        raise SystemExit(f"PyInstaller {pyi} is installed; this build uses {PYINSTALLER_PIN}")
    if _python_text(str(versions), "--has-extra", "gui") == "yes":
        print("Installing the gui extra")
        installed = subprocess.run([sys.executable, "-m", "pip", "install", ".[gui]"], cwd=ROOT, check=False)
        if installed.returncode != 0:
            raise SystemExit("pip install .[gui] failed")
    epoch = subprocess.run(["git", "-C", str(ROOT), "log", "-1", "--format=%ct"], check=False, capture_output=True, text=True)
    stamp = epoch.stdout.strip()
    if epoch.returncode != 0 or not stamp:
        raise SystemExit("SOURCE_DATE_EPOCH needs the current commit timestamp")
    os.environ["SOURCE_DATE_EPOCH"] = stamp
    os.environ["PYTHONHASHSEED"] = "0"
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    icon = subprocess.run([sys.executable, str(ROOT / "packaging" / "make_icon.py")], cwd=ROOT, check=False)
    if icon.returncode != 0:
        raise SystemExit("icon generation failed")
    dist = ROOT / "packaging" / "dist"
    work = ROOT / "packaging" / "build" / "pyinstaller"
    dist.mkdir(parents=True, exist_ok=True)
    print(f"PyInstaller {pyi} SOURCE_DATE_EPOCH={stamp}")
    frozen = subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--distpath",
            str(dist),
            "--workpath",
            str(work),
            str(ROOT / "packaging" / "strategy_builder.spec"),
        ],
        cwd=ROOT,
        check=False,
    )
    if frozen.returncode != 0:
        raise SystemExit("PyInstaller failed")
    bundle = dist / "StrategyBuilder"
    verified = subprocess.run([sys.executable, str(ROOT / "packaging" / "verify_bundle.py"), str(bundle)], cwd=ROOT, check=False)
    if verified.returncode != 0:
        raise SystemExit("bundle verification failed")
    iscc = _resolve_iscc()
    if not iscc or not Path(iscc).is_file():
        raise SystemExit(f"ISCC.exe not found after installing Inno Setup {INNO_VERSION}")
    iscc_version = _iscc_version_text(iscc)
    if not _exact_inno_version(iscc_version):
        raise SystemExit(f"ISCC {iscc} is version {iscc_version}; this build requires {INNO_VERSION}")
    print(f"ISCC {iscc} version {iscc_version}")
    compiled = subprocess.run(
        [
            iscc,
            f"/DAppVersion={app_version}",
            f"/DVersionInfoVersion={version_info}",
            f"/DSetupName={project}",
            f"/DPayloadDir={bundle}",
            f"/DRepoRoot={ROOT}",
            f"/DOutputDir={dist}",
            str(ROOT / "packaging" / "strategy_builder.iss"),
        ],
        cwd=ROOT,
        check=False,
    )
    if compiled.returncode != 0:
        raise SystemExit("ISCC failed")
    setup = dist / f"StrategyBuilder-Setup-{project}.exe"
    if not setup.is_file():
        raise SystemExit(f"setup executable was not written: {setup}")
    signed = subprocess.run([sys.executable, str(ROOT / "packaging" / "sign_setup.py"), str(setup)], cwd=ROOT, check=False)
    if signed.returncode != 0:
        raise SystemExit("signing step failed")
    digest = hashlib.sha256(setup.read_bytes()).hexdigest()
    size = setup.stat().st_size
    (dist / "SHA256SUMS").write_text(f"{digest}  {setup.name}\n", encoding="ascii")
    print(f"SHA256 {digest}")
    print(f"SIZE {size}")
    print(f"SETUP {setup}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        python_version = sys.version.split()[0]
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(
                "\n".join(
                    [
                        "### Installer",
                        "",
                        f"- File: ``{setup.name}``",
                        f"- Size: {size} bytes",
                        f"- SHA256: ``{digest}``",
                        f"- PyInstaller: {pyi}",
                        f"- Inno Setup: {INNO_VERSION}",
                        f"- Python: {python_version}",
                        f"- SOURCE_DATE_EPOCH: {stamp}",
                        "",
                    ]
                )
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
