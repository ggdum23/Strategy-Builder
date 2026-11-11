"""Smoke-test a frozen backtester executable.

Runs version, ingest, a short arena run, and a walk-forward that has to
spawn children. The walk-forward strategy records the scrubbed argv and
environment. Sample bars are read from ``sample_data`` when that tree
exists, otherwise from ``assets/sample``.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path


DESIGN_END = "2026-10-16"

# User-profile and data-root names the walk-forward child must not see.
# The full contract is runner._ENV_KEEP: every other key is also a failure.
FORBIDDEN_ENV = (
    "HOME",
    "USERPROFILE",
    "LOCALAPPDATA",
    "APPDATA",
    "HOMEDRIVE",
    "HOMEPATH",
    "HOMESHARE",
    "USERNAME",
    "USERDOMAIN",
    "USERDOMAIN_ROAMINGPROFILE",
    "ONEDRIVE",
    "ONEDRIVECONSUMER",
    "ONEDRIVECOMMERCIAL",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_CACHE_HOME",
    "MARKET_DATA_ROOT",
    "STRATEGY_BUILDER_SENTINEL",
    "STRATEGY_BUILDER_DEFAULTS",
    "STRATEGY_BUILDER_HEADLESS",
)


def project_version(repo: Path) -> str:
    with (repo / "pyproject.toml").open("rb") as handle:
        return str(tomllib.load(handle)["project"]["version"])


def find_filled(repo: Path, symbol: str) -> Path:
    candidates = [
        repo / "sample_data" / symbol / "filled",
        repo / "assets" / "sample" / symbol / "filled",
    ]
    for path in candidates:
        if path.is_dir() and any(path.glob("*.csv.gz")):
            return path
    raise SystemExit(f"no filled sample files for {symbol}")


def _run(exe: Path, args: list[str], *, cwd: Path, env: dict, timeout: int) -> subprocess.CompletedProcess[str]:
    try:
        proc = subprocess.run(
            [str(exe), *args],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise SystemExit(f"timed out after {timeout}s: {exe} {args}\n{exc}") from exc
    if proc.returncode != 0:
        raise SystemExit(
            f"command failed ({proc.returncode}): {exe.name} {args}\n"
            f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
        )
    return proc


def _clean_probes(directory: Path) -> None:
    for path in directory.glob("probe_*.json"):
        path.unlink(missing_ok=True)


def _child_env(src: Path) -> dict:
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    env["STRATEGY_BUILDER_SENTINEL"] = "frozen-smoke-sentinel"
    env["MARKET_DATA_ROOT"] = str(src)
    return env


def assert_child_surface(payload: dict) -> None:
    """The strategy saw only the runner allowlist.

    The working directory starts empty. The one file the runner itself
    creates there is ``stdio.log``, the capped copy of the child's stdout
    and stderr. Any other name is a file the strategy could see.
    """
    from backtester.runner import _ENV_KEEP

    reported = payload.get("env_keys")
    if not reported:
        raise SystemExit("child did not report env_keys")
    keys = [str(key).upper() for key in reported]
    leaked = [name for name in FORBIDDEN_ENV if name in keys]
    if leaked:
        raise SystemExit(f"user-path variables leaked into the child: {leaked}")
    extra = sorted({name for name in keys if name not in _ENV_KEEP and not name.startswith("LC_")})
    if extra:
        raise SystemExit(f"child environment is outside the allowlist: {extra}")
    if "cwd_entries" not in payload or not isinstance(payload.get("cwd_entries"), list):
        raise SystemExit(f"child cwd entries are not a list: {payload.get('cwd_entries')!r}")
    entries = payload["cwd_entries"]
    unexpected = [name for name in entries if name != "stdio.log"]
    if unexpected:
        raise SystemExit(f"child cwd was not empty: {entries}")


def windows_file_version(path: Path) -> str:
    """Fixed file and product version from a PE version resource."""
    import ctypes
    from ctypes import wintypes

    version = ctypes.WinDLL("version", use_last_error=True)
    get_size = version.GetFileVersionInfoSizeW
    get_size.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    get_size.restype = wintypes.DWORD
    get_info = version.GetFileVersionInfoW
    get_info.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID]
    get_info.restype = wintypes.BOOL
    query = version.VerQueryValueW
    query.argtypes = [
        wintypes.LPCVOID,
        wintypes.LPCWSTR,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.UINT),
    ]
    query.restype = wintypes.BOOL

    ignored = wintypes.DWORD(0)
    size = get_size(str(path), ctypes.byref(ignored))
    if not size:
        raise SystemExit(f"no version resource on {path}")
    buffer = ctypes.create_string_buffer(size)
    if not get_info(str(path), 0, size, buffer):
        raise SystemExit(f"could not read the version resource on {path}")
    pointer = ctypes.c_void_p()
    length = wintypes.UINT()
    if not query(buffer, "\\", ctypes.byref(pointer), ctypes.byref(length)):
        raise SystemExit(f"version resource has no fixed info: {path}")

    class VS_FIXEDFILEINFO(ctypes.Structure):
        _fields_ = [
            ("dwSignature", wintypes.DWORD),
            ("dwStrucVersion", wintypes.DWORD),
            ("dwFileVersionMS", wintypes.DWORD),
            ("dwFileVersionLS", wintypes.DWORD),
            ("dwProductVersionMS", wintypes.DWORD),
            ("dwProductVersionLS", wintypes.DWORD),
            ("dwFileFlagsMask", wintypes.DWORD),
            ("dwFileFlags", wintypes.DWORD),
            ("dwFileOS", wintypes.DWORD),
            ("dwFileType", wintypes.DWORD),
            ("dwFileSubtype", wintypes.DWORD),
            ("dwFileDateMS", wintypes.DWORD),
            ("dwFileDateLS", wintypes.DWORD),
        ]

    info = ctypes.cast(pointer, ctypes.POINTER(VS_FIXEDFILEINFO)).contents
    if info.dwSignature != 0xFEEF04BD:
        raise SystemExit(f"unexpected version signature on {path}")

    def _quad(high: int, low: int) -> str:
        return f"{high >> 16}.{high & 0xFFFF}.{low >> 16}.{low & 0xFFFF}"

    file_version = _quad(info.dwFileVersionMS, info.dwFileVersionLS)
    product_version = _quad(info.dwProductVersionMS, info.dwProductVersionLS)
    if file_version != product_version:
        raise SystemExit(f"{path.name} file version {file_version} != product version {product_version}")
    return file_version


def _assert_pe_versions(exe: Path, expected: str) -> None:
    if sys.platform != "win32":
        return
    gui = _gui_executable(exe)
    for path in (exe, gui):
        found = windows_file_version(path)
        if found != expected:
            raise SystemExit(f"{path.name} version resource is {found}, expected {expected}")


def _gui_executable(cli: Path) -> Path:
    name = "Strategy Builder.exe" if cli.suffix.lower() == ".exe" else "Strategy Builder"
    return cli.with_name(name)


def smoke(exe: Path, repo: Path, work: Path) -> None:
    exe = exe.resolve()
    repo = repo.resolve()
    work.mkdir(parents=True, exist_ok=True)
    if not exe.is_file():
        raise SystemExit(f"frozen executable not found: {exe}")
    expected = project_version(repo)
    src = find_filled(repo, "QQQ")
    strategy = repo / "examples" / "strategies" / "D02" / "strategy.py"
    probe = repo / "packaging" / "smoke" / "probe_strategy.py"
    if not strategy.is_file() or not probe.is_file():
        raise SystemExit("sample strategy or probe is missing")
    env = _child_env(src)
    cwd = work

    version = _run(exe, ["--version"], cwd=cwd, env=env, timeout=120)
    printed = version.stdout.strip()
    if printed != expected:
        raise SystemExit(f"--version printed {printed!r}, expected {expected!r}")

    gui = _gui_executable(exe)
    if not gui.is_file():
        raise SystemExit(f"windowed launcher not found: {gui}")
    report = work / "launcher-check.txt"
    gui_proc = _run(
        gui,
        ["--launcher-check", "--launcher-report", str(report)],
        cwd=cwd,
        env=env,
        timeout=60,
    )
    if not report.is_file():
        raise SystemExit(f"launcher check wrote no report\n{gui_proc.stdout}\n{gui_proc.stderr}")
    report_text = report.read_text(encoding="utf-8").strip()
    if report_text not in {"launcher-check: placeholder", "launcher-check: gui"}:
        raise SystemExit(f"unexpected launcher report {report_text!r}")
    packaging = str(repo / "packaging")
    if packaging not in sys.path:
        sys.path.insert(0, packaging)
    import versions

    _assert_pe_versions(exe, versions.version_info(expected))

    inspect_path = work / "inspect.json"
    _run(
        exe,
        ["ingest", "--ticker", "QQQ", "--src", str(src), "--out", str(inspect_path)],
        cwd=cwd,
        env=env,
        timeout=180,
    )
    inspection = json.loads(inspect_path.read_text(encoding="utf-8"))
    if inspection.get("n_sessions") != 22 or inspection.get("share_factor") != 1.0:
        raise SystemExit(f"unexpected inspection: {inspection.get('n_sessions')} {inspection.get('share_factor')}")

    run_dir = work / "run results"
    _run(
        exe,
        [
            "run",
            "--profile",
            "arena",
            "--ticker",
            "QQQ",
            "--strategy",
            str(strategy),
            "--src",
            str(src),
            "--out",
            str(run_dir),
        ],
        cwd=cwd,
        env=env,
        timeout=600,
    )
    metrics = json.loads((run_dir / "metrics.json").read_text(encoding="utf-8"))
    if metrics.get("profile") != "arena" or metrics.get("symbol") != "QQQ":
        raise SystemExit("short run wrote the wrong profile or symbol")
    if not metrics.get("final_equity") or metrics.get("n_fills", 0) < 1:
        raise SystemExit(f"short run did not trade: {metrics.get('final_equity')} fills={metrics.get('n_fills')}")

    probe_dir = probe.parent
    _clean_probes(probe_dir)
    walk_dir = work / "walk forward"
    try:
        _run(
            exe,
            [
                "walkforward",
                "--profile",
                "arena",
                "--ticker",
                "QQQ",
                "--strategy",
                str(probe),
                "--src",
                str(src),
                "--out",
                str(walk_dir),
                "--design-end-date",
                DESIGN_END,
                "--warmup-days",
                "3",
            ],
            cwd=cwd,
            env=env,
            timeout=900,
        )
        summary = json.loads((walk_dir / "walkforward.json").read_text(encoding="utf-8"))
        if summary.get("design_end_date") != DESIGN_END:
            raise SystemExit(f"walk-forward design end is {summary.get('design_end_date')}")
        if summary.get("design_sessions") != 12 or summary.get("locked_sessions") != 10:
            raise SystemExit(
                f"walk-forward sessions design={summary.get('design_sessions')} locked={summary.get('locked_sessions')}"
            )
        if not (summary.get("design_days_max", 1) < summary.get("locked_days_min", 0)):
            raise SystemExit("walk-forward design window is not before the locked window")
        if not (walk_dir / "design" / "metrics.json").is_file() or not (walk_dir / "locked" / "metrics.json").is_file():
            raise SystemExit("walk-forward did not write design and locked metrics")
        probes = sorted(probe_dir.glob("probe_*.json"))
        if len(probes) != 2:
            details = "\n".join(path.read_text(encoding="utf-8") for path in probes)
            raise SystemExit(f"expected two walk-forward children, found {len(probes)} probe files\n{details}")
        src_text = str(src)
        for path in probes:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("frozen") is not True:
                raise SystemExit(f"child was not the frozen executable: {payload.get('executable')}")
            if payload.get("argv") != ["backtester-design"]:
                raise SystemExit(f"child argv was not scrubbed: {payload.get('argv')}")
            if payload.get("sentinel") not in (None, ""):
                raise SystemExit("sentinel environment leaked into the child")
            if payload.get("market_data_root") not in (None, ""):
                raise SystemExit("MARKET_DATA_ROOT leaked into the child")
            command = str(payload.get("os_command_line") or "")
            if "multiprocessing-fork" not in command:
                raise SystemExit(
                    "child was not a spawn worker: "
                    f"{command!r} argv={payload.get('argv')} exe={payload.get('executable')}"
                )
            if "--src" in command or src_text in command:
                raise SystemExit(f"child command line still carries the data path: {command}")
            if not str(payload.get("cwd_name") or "").startswith("bt-book-"):
                raise SystemExit(f"child cwd was not the isolated directory: {payload.get('cwd')}")
            assert_child_surface(payload)
            executable_name = Path(str(payload.get("executable"))).name.casefold()
            if executable_name not in {"backtester", "backtester.exe"}:
                raise SystemExit(f"child executable was {executable_name}")
    finally:
        _clean_probes(probe_dir)
    print("frozen smoke ok")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Smoke-test a frozen Strategy Builder bundle")
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    args = parser.parse_args(argv)
    smoke(args.exe, args.repo, args.work)
    return 0


if __name__ == "__main__":
    sys.exit(main())
