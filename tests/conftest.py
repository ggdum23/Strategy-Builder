"""Session hooks. The Windows jail grant runs once, before any timed test."""

from __future__ import annotations

import sys


def pytest_sessionstart(session) -> None:
    if sys.platform != "win32":
        return
    from backtester.sandbox import warm_windows_profile

    warm_windows_profile()
