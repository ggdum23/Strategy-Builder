"""Split the Windows suite into jobs that finish inside the 25-minute cap.

The fast files share a shard. The walk-forward file and the accounting file
each have their own, because a single walk is the slow call. A test in a
file shard whose Windows budget is above 60 seconds is lifted into a
``jail-`` shard and deselected from the file run, so the 60-second cap does
not cover a multi-jail test. Parallel tests and those lifted tests are
packed so the sum of their Windows timeouts stays at or under 12 minutes,
which is well under the 25-minute job cap. A test marked ``windows_jails(n)``
is budgeted at 90 seconds per jail. Every other test is budgeted at 60.
``tests/test_jailed_examples.py`` stays in the jailed-examples job.
"""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PARALLEL_CHUNK = 12
DEFAULT_TEST_S = 60
JAIL_BUDGET_S = 90
# 12 minutes. The job cap is 25, so a shard that burns its whole budget
# still has room to start and to print the summary.
SHARD_BUDGET_S = 12 * 60
SHARD_HARD_S = 18 * 60

FILE_SHARDS = {
    "fast": (
        "tests/test_ci_shards.py",
        "tests/test_cli_e2e.py",
        "tests/test_data.py",
        "tests/test_desk.py",
        "tests/test_engine.py",
        "tests/test_file_jail.py",
        "tests/test_isolation.py",
        "tests/test_metrics.py",
        "tests/test_mutations.py",
        "tests/test_pack.py",
        "tests/test_pack_fees.py",
        "tests/test_packaging.py",
        "tests/test_parity.py",
        "tests/test_paths.py",
        "tests/test_reports.py",
        "tests/test_registry.py",
    ),
    "accounting": (
        "tests/test_accounting.py",
        "tests/test_designer_jail.py",
    ),
    "designer": ("tests/test_designer_costs.py",),
    "walkforward": ("tests/test_walkforward.py",),
    "referee": (
        "tests/test_referee.py",
        "tests/test_reserved.py",
    ),
    "gui": (
        "tests/gui/test_charts.py",
        "tests/gui/test_contract.py",
        "tests/gui/test_jobs.py",
        "tests/gui/test_review.py",
        "tests/gui/test_review10.py",
        "tests/gui/test_review11.py",
        "tests/gui/test_review12.py",
        "tests/gui/test_review13.py",
        "tests/gui/test_review15.py",
        "tests/gui/test_review16.py",
        "tests/gui/test_review2.py",
        "tests/gui/test_review3.py",
        "tests/gui/test_review4.py",
        "tests/gui/test_review5.py",
        "tests/gui/test_review6.py",
        "tests/gui/test_review7.py",
        "tests/gui/test_review9.py",
        "tests/gui/test_table.py",
        "tests/gui/test_window.py",
    ),
    "surface": (
        "tests/test_pyside_pin.py",
        "tests/test_redacted_surface.py",
        "tests/test_review10_surface.py",
        "tests/test_review11_surface.py",
        "tests/test_review12_surface.py",
        "tests/test_review13_surface.py",
        "tests/test_review14_surface.py",
        "tests/test_review15_surface.py",
        "tests/test_review16_surface.py",
    ),
}

_FILE_TO_SHARD = {path: name for name, paths in FILE_SHARDS.items() for path in paths}


def _windows_budget(item) -> int:
    """Seconds this test may use on a Windows runner.

    The mark's count is the number of jails. The budget is 90 seconds each,
    and it stops at the shard hard limit so one long mark is still a job
    the 25-minute runner can finish.
    """
    mark = item.get_closest_marker("windows_jails")
    if mark is None or not mark.args:
        return DEFAULT_TEST_S
    return min(JAIL_BUDGET_S * int(mark.args[0]), SHARD_HARD_S)


def collect() -> list[tuple[str, int]]:
    """Node ids and Windows budgets, without the jailed-examples job."""
    import pytest

    found: list[tuple[str, int]] = []

    class Collect:
        def pytest_collection_finish(self, session) -> None:
            for item in session.items:
                found.append((item.nodeid, _windows_budget(item)))

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
        code = pytest.main(
            ["--collect-only", "-q", "-m", "not jailed_sample", str(ROOT / "tests")],
            plugins=[Collect()],
        )
    if code not in (0, 5):
        sys.stderr.write(buffer.getvalue())
        raise SystemExit(code)
    return found


def _pack(pairs: list[tuple[str, int]], prefix: str) -> dict[str, list[str]]:
    """Pack tests so each shard stays within the 12-minute budget.

    A test that cannot share a shard is pulled out first. Otherwise a
    60-second test is left sitting alone between two long ones.
    """
    heavy = [(nodeid, seconds) for nodeid, seconds in pairs if seconds + DEFAULT_TEST_S > SHARD_BUDGET_S]
    light = [(nodeid, seconds) for nodeid, seconds in pairs if seconds + DEFAULT_TEST_S <= SHARD_BUDGET_S]
    shards: dict[str, list[str]] = {}
    bucket: list[str] = []
    used = 0
    number = 1

    def flush() -> None:
        nonlocal bucket, used, number
        if not bucket:
            return
        shards[f"{prefix}-{number}"] = bucket
        number += 1
        bucket = []
        used = 0

    for nodeid, seconds in light:
        if bucket and (used + seconds > SHARD_BUDGET_S or len(bucket) >= PARALLEL_CHUNK):
            flush()
        bucket.append(nodeid)
        used += seconds
    flush()
    for nodeid, _seconds in heavy:
        shards[f"{prefix}-{number}"] = [nodeid]
        number += 1
    return shards


def assign(items: list[tuple[str, int]]) -> dict[str, list[str]]:
    """Each collected test in exactly one shard. Unknown files land in ``rest``."""
    shards: dict[str, list[str]] = {name: [] for name in FILE_SHARDS}
    parallel: list[tuple[str, int]] = []
    # A multi-jail test does not belong in a file shard whose other tests
    # are budgeted at 60 seconds. It is packed on its own.
    lifted: list[tuple[str, int]] = []
    rest: list[str] = []
    for nodeid, seconds in items:
        path = nodeid.split("::", 1)[0].replace("\\", "/")
        if path == "tests/test_parallel.py":
            parallel.append((nodeid, seconds))
        elif path in _FILE_TO_SHARD:
            if seconds > DEFAULT_TEST_S:
                lifted.append((nodeid, seconds))
            else:
                shards[_FILE_TO_SHARD[path]].append(nodeid)
        else:
            rest.append(nodeid)
    shards.update(_pack(parallel, "parallel"))
    shards.update(_pack(lifted, "jail"))
    if rest:
        shards["rest"] = rest
    return {name: nodes for name, nodes in shards.items() if nodes}


def problems(items: list[tuple[str, int]], shards: dict[str, list[str]]) -> list[str]:
    errors = []
    budgets = {nodeid: seconds for nodeid, seconds in items}
    nodeids = [nodeid for nodeid, _seconds in items]
    if "rest" in shards:
        sample = ", ".join(shards["rest"][:5])
        errors.append(f"unassigned tests: {sample}")
    seen: dict[str, str] = {}
    for name, nodes in shards.items():
        if name.startswith("parallel-") or name.startswith("jail-"):
            if len(nodes) > PARALLEL_CHUNK:
                errors.append(f"{name} has {len(nodes)} tests")
            cost = sum(budgets.get(nodeid, DEFAULT_TEST_S) for nodeid in nodes)
            if cost > SHARD_HARD_S:
                errors.append(f"{name} budget is {cost}s")
            if cost > SHARD_BUDGET_S and len(nodes) > 1:
                errors.append(f"{name} packs {cost}s into {len(nodes)} tests")
        for nodeid in nodes:
            if nodeid in seen:
                errors.append(f"{nodeid} is in {seen[nodeid]} and {name}")
            seen[nodeid] = name
    missing = [nodeid for nodeid in nodeids if nodeid not in seen]
    if missing:
        errors.append(f"missing {len(missing)} tests, first {missing[0]}")
    return errors


def _targets(name: str, nodeids: list[str]) -> list[str]:
    """File paths for a whole-file shard. Node ids would exceed the Windows command limit."""
    if name in FILE_SHARDS:
        return list(FILE_SHARDS[name])
    return nodeids


def _deselect(name: str, items: list[tuple[str, int]]) -> list[str]:
    """Node ids pulled out of a file shard. The file path would run them again."""
    if name not in FILE_SHARDS:
        return []
    files = set(FILE_SHARDS[name])
    chosen = []
    for nodeid, seconds in items:
        path = nodeid.split("::", 1)[0].replace("\\", "/")
        if seconds > DEFAULT_TEST_S and path in files:
            chosen.append(nodeid)
    return chosen


def _pytest(targets: list[str], deselect: list[str] | None = None) -> int:
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "--timeout=60",
        "--timeout-method=thread",
        *targets,
    ]
    for nodeid in deselect or ():
        command.append(f"--deselect={nodeid}")
    return subprocess.call(command, cwd=ROOT)


def _load() -> tuple[list[tuple[str, int]], dict[str, list[str]], list[str]]:
    items = collect()
    shards = assign(items)
    return items, shards, problems(items, shards)


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[1] == "plan":
        _items, shards, found = _load()
        if found:
            print("\n".join(found), file=sys.stderr)
            return 2
        print("shards=" + json.dumps(list(shards)))
        return 0
    if len(argv) == 2 and argv[1] == "check":
        _items, shards, found = _load()
        if found:
            print("\n".join(found), file=sys.stderr)
            return 2
        for name, nodes in shards.items():
            print(f"{name} {len(nodes)}")
        return 0
    if len(argv) == 3 and argv[1] == "run":
        items, shards, found = _load()
        if found:
            print("\n".join(found), file=sys.stderr)
            return 2
        chosen = shards.get(argv[2])
        if not chosen:
            print(f"unknown shard {argv[2]}", file=sys.stderr)
            return 2
        return _pytest(_targets(argv[2], chosen), _deselect(argv[2], items))
    print("usage: ci_shards.py plan|check|run <shard>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
