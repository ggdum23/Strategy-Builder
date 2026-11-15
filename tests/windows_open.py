
from __future__ import annotations

import sys

import pytest
from backtester.sandbox import WINDOWS_JAIL_STARTUP_S as JAIL_BUDGET_S

OWNER_PC = pytest.mark.xfail(
    sys.platform == "win32",
    strict=False,
    reason="verify live on owner Windows PC",
)



_TIMER_CAP_S = 18 * 60


def windows_jails(count: int):
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise ValueError("jail count must be a positive integer")

    def decorate(fn):
        marked = pytest.mark.windows_jails(count)(fn)
        if sys.platform == "win32":
            marked = pytest.mark.timeout(min(JAIL_BUDGET_S * count, _TIMER_CAP_S), method="thread")(marked)
        return marked

    return decorate


def runner_timeout(jails: int, timeout_s: float, *, deadline: bool = False) -> float:
    if isinstance(jails, bool) or not isinstance(jails, int) or jails < 1:
        raise ValueError("jail count must be a positive integer")
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)) or not timeout_s > 0:
        raise ValueError("timeout_s must be positive")
    if deadline or sys.platform != "win32":
        return float(timeout_s)
    return max(float(timeout_s), float(JAIL_BUDGET_S * jails))


def windows_limit(seconds: float, jails: int = 1) -> float:
    if isinstance(jails, bool) or not isinstance(jails, int) or jails < 1:
        raise ValueError("jail count must be a positive integer")
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not seconds > 0:
        raise ValueError("seconds must be positive")
    if sys.platform != "win32":
        return float(seconds)
    return float(seconds) + float(JAIL_BUDGET_S * jails)
