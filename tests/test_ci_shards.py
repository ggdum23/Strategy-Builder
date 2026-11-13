"""The Windows jobs partition the suite. A new file has to be assigned."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_windows_shards_cover_the_suite():
    proc = subprocess.run(
        [sys.executable, str(ROOT / "tests" / "ci_shards.py"), "check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    assert any(line.startswith("fast ") for line in lines)
    assert any(line.startswith("accounting ") for line in lines)
    assert any(line.startswith("walkforward ") for line in lines)
    assert any(line.startswith("referee ") for line in lines)
    assert any(line.startswith("parallel-") for line in lines)
    assert not any(line.startswith("rest ") for line in lines)
