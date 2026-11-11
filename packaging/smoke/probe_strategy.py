"""Walk-forward probe for the frozen executable.

Records what the strategy process can see after the runner scrubs argv, the
working directory, and the environment. The file is written beside this
module. The smoke driver deletes it.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _os_command_line() -> str:
    if sys.platform == "win32":
        import ctypes

        return str(ctypes.windll.kernel32.GetCommandLineW())
    proc = Path("/proc/self/cmdline")
    if proc.is_file():
        parts = proc.read_bytes().split(b"\0")
        return " ".join(part.decode("utf-8", "replace") for part in parts if part)
    return ""


class Strategy:
    WANTS_EXTENDED_HOURS = False

    def __init__(self):
        self._recorded = False

    def on_bar(self, _bar, _state):
        if not self._recorded:
            self._recorded = True
            self._record()
        return None

    def _record(self) -> None:
        here = Path(__file__).resolve().parent
        destination = here / f"probe_{os.getpid()}.json"
        payload = {
            "argv": list(sys.argv),
            "cwd": os.getcwd(),
            "cwd_name": Path(os.getcwd()).name,
            "sentinel": os.environ.get("STRATEGY_BUILDER_SENTINEL"),
            "market_data_root": os.environ.get("MARKET_DATA_ROOT"),
            "os_command_line": _os_command_line(),
            "frozen": bool(getattr(sys, "frozen", False)),
            "executable": sys.executable,
        }
        destination.write_text(json.dumps(payload), encoding="utf-8")
