"""Every D example, jailed, matches an in-process run on the October sample.

This is the ``jailed_sample`` job. The six-way matrix skips the marker.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backtester.data import load_named
from backtester.runner import _isolated_books, run_books
from backtester.strategy_loader import load_strategy_class

ROOT = Path(__file__).resolve().parents[1]
QQQ = ROOT / "assets" / "sample" / "QQQ" / "filled"
STRATEGIES = ROOT / "examples" / "strategies"
NAMES = [f"D{number:02d}" for number in range(1, 21)]


@pytest.fixture(scope="module")
def october():
    loaded = load_named(QQQ, "QQQ")
    return loaded.frame


def _same_failure(local: BaseException, remote: BaseException) -> None:
    text = str(remote)
    assert type(local).__name__ in text
    assert str(local) in text


@pytest.mark.jailed_sample
@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("profile", ["arena", "desk"])
def test_jailed_example_matches_in_process(october, name, profile):
    path = STRATEGIES / name / "strategy.py"
    cls = load_strategy_class(path)
    try:
        local = run_books(cls, october, profile, with_benchmarks=False)
    except Exception as exc:
        local_error = exc
        local = None
    else:
        local_error = None
    try:
        jailed = _isolated_books(
            None,
            october,
            profile,
            strategy_path=str(path),
            with_benchmarks=False,
        )
    except Exception as exc:
        remote_error = exc
        jailed = None
    else:
        remote_error = None
    if local_error is not None or remote_error is not None:
        assert local_error is not None and remote_error is not None
        _same_failure(local_error, remote_error)
        return
    remote_result, remote_metrics = jailed["strategy"]
    local_result, local_metrics = local["strategy"]
    for key in ("fills", "daily", "fees", "n_fills", "busted"):
        assert remote_result.get(key) == local_result.get(key), name
    assert remote_metrics == local_metrics
