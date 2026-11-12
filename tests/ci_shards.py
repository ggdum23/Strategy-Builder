"""Split the Windows suite into jobs that finish inside the 25-minute cap.

The fast files share a shard. The walk-forward file and the accounting file
each have their own, because a single walk is the slow call. Parallel tests
are chunks of 12, so a chunk still finishes if every test uses the whole
60-second cap. ``tests/test_jailed_examples.py`` stays in the jailed-examples
job.
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
}

_FILE_TO_SHARD = {path: name for name, paths in FILE_SHARDS.items() for path in paths}


def collect() -> list[str]:
    """Node ids for the suite, without the separate jailed-examples job."""
    import pytest

    found: list[str] = []

    class Collect:
        def pytest_collection_finish(self, session) -> None:
            found.extend(item.nodeid for item in session.items)

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


def assign(nodeids: list[str]) -> dict[str, list[str]]:
    """Each collected test in exactly one shard. Unknown files land in ``rest``."""
    shards: dict[str, list[str]] = {name: [] for name in FILE_SHARDS}
    parallel: list[str] = []
    rest: list[str] = []
    for nodeid in nodeids:
        path = nodeid.split("::", 1)[0].replace("\\", "/")
        if path == "tests/test_parallel.py":
            parallel.append(nodeid)
        elif path in _FILE_TO_SHARD:
            shards[_FILE_TO_SHARD[path]].append(nodeid)
        else:
            rest.append(nodeid)
    for index in range(0, len(parallel), PARALLEL_CHUNK):
        shards[f"parallel-{index // PARALLEL_CHUNK + 1}"] = parallel[index : index + PARALLEL_CHUNK]
    if rest:
        shards["rest"] = rest
    return {name: nodes for name, nodes in shards.items() if nodes}


def problems(nodeids: list[str], shards: dict[str, list[str]]) -> list[str]:
    errors = []
    if "rest" in shards:
        sample = ", ".join(shards["rest"][:5])
        errors.append(f"unassigned tests: {sample}")
    seen: dict[str, str] = {}
    for name, nodes in shards.items():
        if name.startswith("parallel-") and len(nodes) > PARALLEL_CHUNK:
            errors.append(f"{name} has {len(nodes)} tests")
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


def _pytest(targets: list[str]) -> int:
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "--timeout=60",
        "--timeout-method=thread",
        *targets,
    ]
    return subprocess.call(command, cwd=ROOT)


def _load() -> tuple[list[str], dict[str, list[str]], list[str]]:
    nodeids = collect()
    shards = assign(nodeids)
    return nodeids, shards, problems(nodeids, shards)


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[1] == "plan":
        _nodeids, shards, found = _load()
        if found:
            print("\n".join(found), file=sys.stderr)
            return 2
        print("shards=" + json.dumps(list(shards)))
        return 0
    if len(argv) == 2 and argv[1] == "check":
        _nodeids, shards, found = _load()
        if found:
            print("\n".join(found), file=sys.stderr)
            return 2
        for name, nodes in shards.items():
            print(f"{name} {len(nodes)}")
        return 0
    if len(argv) == 3 and argv[1] == "run":
        _nodeids, shards, found = _load()
        if found:
            print("\n".join(found), file=sys.stderr)
            return 2
        chosen = shards.get(argv[2])
        if not chosen:
            print(f"unknown shard {argv[2]}", file=sys.stderr)
            return 2
        return _pytest(_targets(argv[2], chosen))
    print("usage: ci_shards.py plan|check|run <shard>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
