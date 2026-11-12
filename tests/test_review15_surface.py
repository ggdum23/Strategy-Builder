"""Fifteenth review. Job status comes from the run record, not from metrics.json."""

from __future__ import annotations

import json

from backtester.gui.backend import load_run


def _job(root, strategy_id: str, *, metrics: bool, error: str | None = None) -> None:
    job = root / "jobs" / strategy_id / "QQQ"
    job.mkdir(parents=True)
    if metrics:
        (job / "metrics.json").write_text("{}\n", encoding="utf-8")
    if error is not None:
        (job / "error.json").write_text(json.dumps({"error": error}) + "\n", encoding="utf-8")


def test_operator_job_status_follows_run_json_and_the_leaderboard(tmp_path):
    root = tmp_path / "batch"
    _job(root, "alpha", metrics=True)
    _job(root, "beta", metrics=True, error="RuntimeError: boom")
    _job(root, "gamma", metrics=True)
    _job(root, "delta", metrics=True, error="leaderboard says error")
    _job(root, "epsilon", metrics=True, error="run.json says error")
    _job(root, "zeta", metrics=False, error="no record")
    (root / "leaderboard.csv").write_text(
        "strategy_id,source_id,status,total_return\n"
        "alpha,QQQ,ok,0.1\n"
        "beta,QQQ,error,\n"
        "delta,QQQ,error,0.2\n"
        "epsilon,QQQ,ok,0.3\n",
        encoding="utf-8",
    )
    (root / "run.json").write_text(
        json.dumps(
            {
                "kind": "parallel",
                "jobs": [
                    {"strategy_id": "alpha", "source_id": "QQQ", "status": "ok"},
                    {"strategy_id": "beta", "source_id": "QQQ", "status": "error"},
                    {"strategy_id": "delta", "source_id": "QQQ", "status": "ok"},
                    {"strategy_id": "epsilon", "source_id": "QQQ", "status": "error"},
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    report = load_run(root, audience="operator")
    states = {child["strategy_id"]: child for child in report.children}
    assert states["alpha"]["state"] == "ok"
    assert states["alpha"]["error"] is None
    assert states["beta"]["state"] == "error"
    assert states["beta"]["error"] == "RuntimeError: boom"
    assert states["gamma"]["state"] == "ok"
    assert states["delta"]["state"] == "error"
    assert states["epsilon"]["state"] == "error"
    assert states["zeta"]["state"] == "failed"
    assert states["zeta"]["error"] == "no record"

    designer_beta = root / "designer" / "beta" / "QQQ"
    designer_beta.mkdir(parents=True)
    (designer_beta / "error.json").write_text(
        json.dumps({"error": "the strategy failed"}) + "\n",
        encoding="utf-8",
    )
    (root / "designer" / "leaderboard.csv").write_text(
        "rank,strategy_id,source_id,total_return\n2,beta,QQQ,\n",
        encoding="utf-8",
    )
    designer = load_run(root)
    assert designer.audience == "designer"
    shown = {child["strategy_id"]: child for child in designer.children}
    assert shown["beta"]["state"] == "failed"
    assert shown["beta"]["error"] == "the strategy failed"
    blob = json.dumps(shown).casefold()
    assert "boom" not in blob
    assert "runtimeerror" not in blob
