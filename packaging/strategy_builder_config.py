
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

APP_DIR_NAME = "StrategyBuilder"
SCHEMA = 1

USER_README = """\
Strategy Builder keeps settings, the series registry, and backtest runs in this folder.
Uninstall leaves this folder in place unless you ask it to delete saved results.
Market data is not stored here. data_root in config.json points at the filled files.
"""


def user_data_dir(environ: dict | None = None, *, platform: str | None = None) -> Path:
    env = os.environ if environ is None else environ
    system = sys.platform if platform is None else platform
    if system == "win32":
        local = env.get("LOCALAPPDATA")
        if not local:
            profile = env.get("USERPROFILE")
            if not profile:
                raise RuntimeError("LOCALAPPDATA is not set")
            local = str(Path(profile) / "AppData" / "Local")
        return Path(local) / APP_DIR_NAME
    xdg = env.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / APP_DIR_NAME
    home = env.get("HOME")
    if not home:
        raise RuntimeError("HOME is not set")
    return Path(home) / ".local" / "share" / APP_DIR_NAME


def config_path(environ: dict | None = None, *, platform: str | None = None) -> Path:
    return user_data_dir(environ, platform=platform) / "config.json"


def console_executable(executable: str | Path | None = None) -> Path:
    exe = Path(sys.executable if executable is None else executable)
    name = "backtester.exe" if exe.suffix.lower() == ".exe" or sys.platform == "win32" else "backtester"
    if exe.suffix.lower() == ".exe":
        name = "backtester.exe"
    return exe.with_name(name)


def _read_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return loaded


def _defaults_file(environ: dict, defaults_path: Path | None) -> Path | None:
    if defaults_path is not None:
        return defaults_path
    override = environ.get("STRATEGY_BUILDER_DEFAULTS")
    if override:
        return Path(override)
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "defaults.json"
    return None


def ensure_user_config(
    environ: dict | None = None,
    defaults_path: Path | None = None,
    *,
    platform: str | None = None,
) -> dict:
    env = os.environ if environ is None else environ
    directory = user_data_dir(env, platform=platform)
    runs = directory / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    registry = directory / "registry.json"
    path = directory / "config.json"
    current = _read_json(path) if path.is_file() else {}
    defaults_file = _defaults_file(env, defaults_path)
    defaults = _read_json(defaults_file) if defaults_file is not None else {}
    if not str(current.get("data_root") or "").strip():
        current["data_root"] = str(defaults.get("data_root") or "")
    current["schema"] = SCHEMA
    current["runs_dir"] = str(runs)
    current["registry_path"] = str(registry)
    if getattr(sys, "frozen", False):
        current["install_dir"] = str(Path(sys.executable).resolve().parent)
    elif "install_dir" not in current:
        current["install_dir"] = ""
    path.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")
    readme = directory / "README.txt"
    if not readme.exists():
        readme.write_text(USER_README, encoding="utf-8")
    return current
