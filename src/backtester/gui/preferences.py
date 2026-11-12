"""Remembered paths. Appearance only, plus the folders the operator already chose.

The default data root is the Windows tree the tool is meant to open. It is
written into the field the operator can see. A missing folder is an error at
inspect time. It is not replaced with a sample.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

DEFAULT_DATA_ROOT = r"C:\Users\Jeu\Desktop\Backtester"

DEFAULTS = {
    "data_root": DEFAULT_DATA_ROOT,
    "theme": "dark",
    "registry_path": "",
    "runs_root": "",
    "last_out": "",
    "last_src": "",
    "last_ticker": "",
    "last_profile": "",
    "last_strategy_dir": "",
    "last_pack": "",
    "last_sidecar": "",
}


def default_store_path() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming")))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    return base / "strategy-builder" / "preferences.json"


def as_path(text: str) -> Path:
    """One path. Surrounding quotes are removed. Interior spaces stay intact."""
    raw = str(text).strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {'"', "'"}:
        raw = raw[1:-1]
    return Path(raw)


class Preferences:
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else default_store_path()
        self.data = dict(DEFAULTS)
        self.load()

    def load(self) -> dict:
        if self.path.is_file():
            try:
                document = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                document = {}
            if isinstance(document, dict):
                for key in DEFAULTS:
                    if key in document and isinstance(document[key], str):
                        self.data[key] = document[key]
        return self.data

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {key: str(self.data.get(key, DEFAULTS[key])) for key in DEFAULTS}
        text = json.dumps(payload, indent=2) + "\n"
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(self.path)

    def set(self, key: str, value: str) -> None:
        if key not in DEFAULTS:
            raise KeyError(key)
        self.data[key] = "" if value is None else str(value)
