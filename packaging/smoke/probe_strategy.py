"""Walk-forward probe for the frozen executable.

Records what the strategy process can see after the runner scrubs argv, the
working directory, and the environment. The file is written beside this
module. The smoke driver deletes it.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _os_command_line() -> str:
    if sys.platform == "win32":
        import ctypes

        # The default restype is a pointer-sized integer, which is the address
        # of the command line rather than the text.
        ctypes.windll.kernel32.GetCommandLineW.restype = ctypes.c_wchar_p
        return ctypes.windll.kernel32.GetCommandLineW() or ""
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
        cwd = os.getcwd()
        # The jail cannot write beside this file. The worker copies ``surface``
        # into the operator directory after the engine finishes.
        self.surface = {
            "argv": list(sys.argv),
            "cwd": cwd,
            "cwd_name": Path(cwd).name,
            "cwd_entries": sorted(os.listdir(cwd)),
            "env_keys": sorted(os.environ),
            "sentinel": os.environ.get("STRATEGY_BUILDER_SENTINEL"),
            "market_data_root": os.environ.get("MARKET_DATA_ROOT"),
            "home": os.environ.get("HOME"),
            "userprofile": os.environ.get("USERPROFILE"),
            "localappdata": os.environ.get("LOCALAPPDATA"),
            "os_command_line": _os_command_line(),
            "frozen": bool(getattr(sys, "frozen", False)),
            "executable": sys.executable,
        }
