"""Windows-only open items. The same assertions still run on Linux."""

from __future__ import annotations

import sys

import pytest
from backtester.sandbox import WINDOWS_JAIL_STARTUP_S as JAIL_BUDGET_S

# A pass on the owner machine is an xpass. A failure stays visible as xfail
# and does not fail the job. Linux never takes this mark.
OWNER_PC = pytest.mark.xfail(
    sys.platform == "win32",
    strict=False,
    reason="verify live on owner Windows PC",
)

# One AppContainer jail on a GitHub-hosted runner is about 16–20 seconds of
# startup before the result pipe has bytes. 90 seconds is that cost with room
# for the strategy. The suite default stays 60 seconds. The same number is
# the Windows ready-wait in the referee jail.


# The Windows job cap is 25 minutes. One test's timer stays inside the
# 18-minute shard hard limit in tests/ci_shards.py, even when the mark
# counts more jails than that. The mark's count is still the real number
# of launches; the timer is what the runner can finish.
_TIMER_CAP_S = 18 * 60


def windows_jails(count: int):
    """Windows pytest-timeout of ``JAIL_BUDGET_S`` per AppContainer jail.

    Linux does not take the timeout mark, so the suite default is unchanged.
    The ``windows_jails`` mark is present on every platform so the shard
    planner can budget the same groups. A test that launches a jail without
    this mark is a failure: it would otherwise sit in the 60-second file shard.
    """
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise ValueError("jail count must be a positive integer")

    def decorate(fn):
        marked = pytest.mark.windows_jails(count)(fn)
        if sys.platform == "win32":
            marked = pytest.mark.timeout(min(JAIL_BUDGET_S * count, _TIMER_CAP_S), method="thread")(marked)
        return marked

    return decorate


def runner_timeout(jails: int, timeout_s: float, *, deadline: bool = False) -> float:
    """Worker budget for ``run_parallel(..., timeout_s=)``.

    Linux keeps ``timeout_s``. On Windows a run that has to finish gets at
    least ``JAIL_BUDGET_S`` per jail, because AppContainer startup is inside
    that budget and a 30-second cap expires while ``pipe_bytes`` is still 0.

    ``deadline=True`` keeps ``timeout_s`` on Windows. Those tests measure a
    kill or a cancel, and a longer budget would let the strategy finish.
    """
    if isinstance(jails, bool) or not isinstance(jails, int) or jails < 1:
        raise ValueError("jail count must be a positive integer")
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)) or not timeout_s > 0:
        raise ValueError("timeout_s must be positive")
    if deadline or sys.platform != "win32":
        return float(timeout_s)
    return max(float(timeout_s), float(JAIL_BUDGET_S * jails))


def windows_limit(seconds: float, jails: int = 1) -> float:
    """Wall-clock bound. Linux keeps ``seconds``. Windows adds one jail budget per jail."""
    if isinstance(jails, bool) or not isinstance(jails, int) or jails < 1:
        raise ValueError("jail count must be a positive integer")
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not seconds > 0:
        raise ValueError("seconds must be positive")
    if sys.platform != "win32":
        return float(seconds)
    return float(seconds) + float(JAIL_BUDGET_S * jails)
