"""Seventeenth review. Recorded rows, case-suffixed folders, and the exception line."""

from __future__ import annotations

import json

from backtester.gui.backend import load_run
from backtester.gui.jobs import failure_view
from backtester.parallel import _reserve_case_component


def _metrics(path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "metrics.json").write_text("{}\n", encoding="utf-8")


def test_failure_view_shows_the_exception_line_and_keeps_the_traceback():
    text = (
        "Traceback (most recent call last):\n"
        '  File "strategy.py", line 2, in on_bar\n'
        "    raise RuntimeError('inner cause')\n"
        "RuntimeError: inner cause\n"
        "\n"
        "The above exception was the direct cause of the following exception:\n"
        "\n"
        "Traceback (most recent call last):\n"
        '  File "strategy.py", line 4, in on_bar\n'
        "    raise ValueError('book rejected\\nthe bar')\n"
        "ValueError: book rejected\n"
        "the bar\n"
    )
    shown, detail = failure_view(text)
    assert shown == "ValueError: book rejected"
    assert "Traceback (most recent call last):" in detail
    assert "the bar" in detail
    assert "inner cause" in detail
    plain, full = failure_view("first line of the failure\nsecond line stays in the tip")
    assert plain == "first line of the failure"
    assert "second line stays in the tip" in full


def test_leaderboard_row_without_a_directory_stays_in_the_jobs_list(tmp_path):
    root = tmp_path / "batch"
    _metrics(root / "jobs" / "alpha" / "QQQ")
    (root / "leaderboard.csv").write_text(
        "strategy_id,source_id,status,error\n"
        "alpha,QQQ,ok,\n"
        "missing,QQQ,error,no directory for this row\n",
        encoding="utf-8",
    )
    (root / "run.json").write_text(
        json.dumps(
            {
                "kind": "parallel",
                "jobs": [
                    {"strategy_id": "alpha", "source_id": "QQQ", "status": "ok"},
                    {"strategy_id": "missing", "source_id": "QQQ", "status": "error"},
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    report = load_run(root, audience="operator")
    states = {child["strategy_id"]: child for child in report.children}
    assert set(states) == {"alpha", "missing"}
    assert states["alpha"]["state"] == "ok"
    assert states["missing"]["state"] == "error"
    assert states["missing"]["error"] == "no directory for this row"
    assert states["missing"]["path"] == ""


def test_case_suffixed_folder_uses_the_recorded_id(tmp_path):
    used: set[str] = set()
    first = _reserve_case_component("Momentum", used)
    second = _reserve_case_component("momentum", used)
    root = tmp_path / "batch"
    _metrics(root / "jobs" / first / "QQQ")
    _metrics(root / "jobs" / second / "QQQ")
    message = "the strategy failed"
    (root / "leaderboard.csv").write_text(
        "strategy_id,source_id,status,error\n"
        f"Momentum,QQQ,ok,\n"
        f"momentum,QQQ,error,{message}\n",
        encoding="utf-8",
    )
    (root / "run.json").write_text(
        json.dumps(
            {
                "kind": "parallel",
                "jobs": [
                    {"strategy_id": "Momentum", "source_id": "QQQ", "status": "ok"},
                    {"strategy_id": "momentum", "source_id": "QQQ", "status": "error", "error": message},
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    report = load_run(root, audience="operator")
    states = {child["strategy_id"]: child for child in report.children}
    assert second == "momentum-2"
    assert states[first]["state"] == "ok"
    assert states[first]["error"] is None
    assert states[second]["state"] == "error"
    assert states[second]["error"] == message
    assert len(states) == 2


def test_suffixed_folder_joins_when_only_the_base_id_is_recorded(tmp_path):
    root = tmp_path / "batch"
    _metrics(root / "jobs" / "momentum-2" / "QQQ")
    (root / "leaderboard.csv").write_text(
        "strategy_id,source_id,status,error\n"
        "momentum,QQQ,error,the strategy failed\n",
        encoding="utf-8",
    )
    (root / "run.json").write_text(
        json.dumps(
            {
                "kind": "parallel",
                "jobs": [{"strategy_id": "momentum", "source_id": "QQQ", "status": "error"}],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    report = load_run(root, audience="operator")
    child = report.children[0]
    assert child["strategy_id"] == "momentum-2"
    assert child["state"] == "error"
    assert child["error"] == "the strategy failed"
    assert len(report.children) == 1


def test_three_ids_keep_the_literal_suffix_on_its_own_folder(tmp_path):
    keys = [("Momentum", "QQQ"), ("momentum", "QQQ"), ("momentum-2", "QQQ")]
    used: set[str] = set()
    folders: dict[str, str] = {}
    for strategy_id, _source in sorted(keys):
        if strategy_id not in folders:
            folders[strategy_id] = _reserve_case_component(strategy_id, used)
    root = tmp_path / "batch"
    for folder in folders.values():
        _metrics(root / "jobs" / folder / "QQQ")
    (root / "leaderboard.csv").write_text(
        "strategy_id,source_id,status,error\n"
        "Momentum,QQQ,ok,\n"
        "momentum,QQQ,error,the strategy failed\n"
        "momentum-2,QQQ,ok,\n",
        encoding="utf-8",
    )
    report = load_run(root, audience="operator")
    states = {child["strategy_id"]: child for child in report.children}
    assert folders["momentum"] == "momentum-2"
    assert folders["momentum-2"] == "momentum-2-2"
    assert states["momentum-2"]["state"] == "error"
    assert states["momentum-2"]["error"] == "the strategy failed"
    assert states["momentum-2-2"]["state"] == "ok"
    assert states["momentum-2-2"]["error"] is None
    assert states["Momentum"]["state"] == "ok"


def test_case_suffixed_source_folder_uses_the_recorded_source(tmp_path):
    used: set[str] = set()
    first = _reserve_case_component("QQQ", used)
    second = _reserve_case_component("qqq", used)
    root = tmp_path / "batch"
    _metrics(root / "jobs" / "momentum" / first)
    _metrics(root / "jobs" / "momentum" / second)
    (root / "leaderboard.csv").write_text(
        "strategy_id,source_id,status,error\n"
        "momentum,QQQ,ok,\n"
        "momentum,qqq,error,the strategy failed\n",
        encoding="utf-8",
    )
    report = load_run(root, audience="operator")
    states = {child["source_id"]: child for child in report.children}
    assert second == "qqq-2"
    assert states[first]["state"] == "ok"
    assert states[second]["state"] == "error"
    assert states[second]["error"] == "the strategy failed"


def test_literal_suffixed_id_does_not_inherit_the_shorter_id(tmp_path):
    root = tmp_path / "batch"
    _metrics(root / "jobs" / "momentum" / "QQQ")
    _metrics(root / "jobs" / "momentum-2" / "QQQ")
    (root / "leaderboard.csv").write_text(
        "strategy_id,source_id,status,error\n"
        "momentum,QQQ,error,the strategy failed\n"
        "momentum-2,QQQ,ok,\n",
        encoding="utf-8",
    )
    report = load_run(root, audience="operator")
    states = {child["strategy_id"]: child for child in report.children}
    assert states["momentum"]["state"] == "error"
    assert states["momentum"]["error"] == "the strategy failed"
    assert states["momentum-2"]["state"] == "ok"
    assert states["momentum-2"]["error"] is None


def test_designer_case_suffix_stays_opaque(tmp_path):
    root = tmp_path / "batch"
    _metrics(root / "jobs" / "momentum-2" / "QQQ")
    (root / "jobs" / "momentum-2" / "QQQ" / "error.json").write_text(
        json.dumps({"error": "Traceback (most recent call last):\nValueError: share_scale"}) + "\n",
        encoding="utf-8",
    )
    (root / "leaderboard.csv").write_text(
        "strategy_id,source_id,status,error\n"
        "momentum,QQQ,error,the strategy failed\n",
        encoding="utf-8",
    )
    designer = root / "designer" / "momentum-2" / "QQQ"
    _metrics(designer)
    (root / "designer" / "leaderboard.csv").write_text(
        "strategy_id,source_id,status,error\n"
        "momentum,QQQ,error,the strategy failed\n",
        encoding="utf-8",
    )
    shown = load_run(root)
    assert shown.audience == "designer"
    child = shown.children[0]
    assert child["state"] == "error"
    assert child["error"] == "the strategy failed"
    blob = json.dumps(shown.children)
    assert "Traceback" not in blob
    assert "share_scale" not in blob
    assert "ValueError" not in blob
