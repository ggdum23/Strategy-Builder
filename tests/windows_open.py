"""Windows-only open items. The same assertions still run on Linux."""

from __future__ import annotations

import sys

import pytest

# A pass on the owner machine is an xpass. A failure stays visible as xfail
# and does not fail the job. Linux never takes this mark.
OWNER_PC = pytest.mark.xfail(
    sys.platform == "win32",
    strict=False,
    reason="verify live on owner Windows PC",
)

# One AppContainer jail on a GitHub-hosted runner is about 16–20 seconds of
# startup before the result pipe has bytes. 90 seconds is that cost with room
# for the strategy. The suite default stays 60 seconds.
JAIL_BUDGET_S = 90


def windows_jails(count: int):
    """Windows pytest-timeout of ``JAIL_BUDGET_S`` per AppContainer jail.

    Linux does not take the timeout mark, so the suite default is unchanged.
    The ``windows_jails`` mark is present on every platform so the shard
    planner can budget the same groups.
    """
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise ValueError("jail count must be a positive integer")

    def decorate(fn):
        marked = pytest.mark.windows_jails(count)(fn)
        if sys.platform == "win32":
            marked = pytest.mark.timeout(JAIL_BUDGET_S * count, method="thread")(marked)
        return marked

    return decorate
