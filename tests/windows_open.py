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
