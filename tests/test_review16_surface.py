"""Sixteenth review. Status files and job rows, without Qt."""

from __future__ import annotations

import json

from backtester.gui.backend import load_run
from backtester.gui.jobs import process_entry


def _job(root, strategy_id: str, *, metrics: bool, error: str | None = None) -> None:
    job = root / "jobs" / strategy_id / "QQQ"
    job.mkdir(parents=True)
    if metrics:
        (job / "metrics.json").write_text("{}\n", encoding="utf-8")
    if error is not None:
        (job / "error.json").write_text(json.dumps({"error": error}) + "\n", encoding="utf-8")


def test_error_json_wins_over_metrics_when_the_run_does_not_name_the_job(tmp_path):
    root = tmp_path / "batch"
    _job(root, "gamma", metrics=True)
    _job(root, "eta", metrics=True, error="metrics must not hide this")
    _job(root, "kept", metrics=True, error="recorded ok still wins")
    (root / "leaderboard.csv").write_text(
        "strategy_id,source_id,status,total_return\nkept,QQQ,ok,0.1\n",
        encoding="utf-8",
    )
    (root / "run.json").write_text(
        json.dumps(
            {
                "kind": "parallel",
                "jobs": [{"strategy_id": "kept", "source_id": "QQQ", "status": "ok"}],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    report = load_run(root, audience="operator")
    states = {child["strategy_id"]: child for child in report.children}
    assert states["gamma"]["state"] == "ok"
    assert states["gamma"]["error"] is None
    assert states["eta"]["state"] == "failed"
    assert states["eta"]["error"] == "metrics must not hide this"
    assert states["kept"]["state"] == "ok"
    assert states["kept"]["error"] is None


def test_leaderboard_error_message_fills_a_job_with_no_error_file(tmp_path):
    root = tmp_path / "batch"
    _job(root, "theta", metrics=True)
    sentence = "RuntimeError: the bar was rejected"
    (root / "leaderboard.csv").write_text(
        "strategy_id,source_id,status,error\n"
        f"theta,QQQ,error,{sentence}\n",
        encoding="utf-8",
    )
    (root / "run.json").write_text(
        json.dumps(
            {
                "kind": "parallel",
                "jobs": [{"strategy_id": "theta", "source_id": "QQQ", "status": "error"}],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    report = load_run(root, audience="operator")
    child = report.children[0]
    assert child["state"] == "error"
    assert child["error"] == sentence

    designer = root / "designer" / "theta" / "QQQ"
    designer.mkdir(parents=True)
    (designer / "metrics.json").write_text("{}\n", encoding="utf-8")
    (root / "designer" / "leaderboard.csv").write_text(
        "strategy_id,source_id,status,error\n"
        "theta,QQQ,error,the strategy failed\n",
        encoding="utf-8",
    )
    shown = load_run(root)
    assert shown.audience == "designer"
    row = shown.children[0]
    assert row["error"] == "the strategy failed"
    blob = json.dumps(shown.children).casefold()
    assert "rejected" not in blob
    assert "runtimeerror" not in blob


def test_process_entry_records_base_exception(tmp_path, monkeypatch):
    def interrupt(config, status_path=None):
        raise KeyboardInterrupt()

    monkeypatch.setattr("backtester.gui.jobs.execute_run", interrupt)
    status = tmp_path / "interrupt.json"
    process_entry("single", {"mode": "single"}, str(status))
    payload = json.loads(status.read_text(encoding="utf-8"))
    assert payload["state"] == "error"
    assert payload["failure"]["type"] == "KeyboardInterrupt"
    assert payload["state"] != "running"

    class Halt(BaseException):
        pass

    def halt(config, status_path=None):
        raise Halt("stop")

    monkeypatch.setattr("backtester.gui.jobs.execute_run", halt)
    halted = tmp_path / "halt.json"
    process_entry("single", {"mode": "single"}, str(halted))
    body = json.loads(halted.read_text(encoding="utf-8"))
    assert body["state"] == "error"
    assert body["failure"]["type"] == "Halt"
    assert body["failure"]["message"] == "stop"

    def leave(config, status_path=None):
        raise SystemExit(3)

    monkeypatch.setattr("backtester.gui.jobs.execute_run", leave)
    exited = tmp_path / "exit.json"
    process_entry("single", {"mode": "single"}, str(exited))
    gone = json.loads(exited.read_text(encoding="utf-8"))
    assert gone["state"] == "error"
    assert gone["failure"]["type"] == "SystemExit"
