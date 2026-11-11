"""The worker calls the public runner and does not return sidecar fields."""

from __future__ import annotations

import json
from pathlib import Path

from backtester.gui.backend import load_run, read_designer_manifest
from backtester.gui.jobs import build_pack_job, execute_run, inspect_series, record_series

from synthetic import write_filled, write_strategy

ROOT = Path(__file__).resolve().parents[2]


def _series(tmp_path, *, gap=False):
    folder = tmp_path / "Strategy Builder" / "QQQ" / "filled"
    return write_filled(folder, gap=gap)


def test_inspect_reports_gaps_and_does_not_guess(tmp_path):
    src = _series(tmp_path, gap=True)
    payload = inspect_series(src, "QQQ", bar_minutes=1, price_kind="raw")
    info = payload["inspection"]
    assert info["share_factor"] == 1.0
    assert info["native_tick"] == 0.01
    assert info["timezone_offsets"] == ["-05:00"]
    assert payload["gaps"]
    assert any("hole" in gap or "not the modal" in gap for gap in payload["gaps"])
    rth = next(segment for segment in info["session_segments"] if segment["label"] == "rth")
    assert rth["gapped_sessions"] >= 1
    try:
        inspect_series(src, "QQQ", bar_minutes=1, price_kind="")
    except ValueError as exc:
        assert "guess" in str(exc).lower() or "raw" in str(exc)
    else:
        raise AssertionError("blank price kind was accepted")


def test_pack_result_has_no_sidecar_fields(tmp_path):
    src = _series(tmp_path)
    registry = tmp_path / "registry.json"
    record_series(src, "QQQ", registry, profile="arena", price_kind="raw", bar_minutes=1)
    pack = tmp_path / "pack"
    sidecar = tmp_path / "operator" / "sidecar.json"
    result = build_pack_job(
        src,
        "QQQ",
        pack,
        sidecar,
        registry,
        anonymize=True,
        profile="arena",
        bar_minutes=1,
        price_kind="raw",
    )
    forbidden = {"price_factor", "volume_factor", "date_map", "share_scale", "operator_note", "symbol"}
    assert forbidden.isdisjoint(result)
    assert result["anonymized"] is True
    assert result["sidecar_displayed"] is False
    assert result["manifest"]["asset_name"] == "RANDOM"
    manifest = read_designer_manifest(pack)
    assert manifest["asset_name"] == "RANDOM"
    blob = json.dumps(result)
    assert "price_factor" not in blob
    assert "volume_factor" not in blob
    text = sidecar.read_text(encoding="utf-8")
    assert "price_factor" in text
    assert text not in blob


def test_named_run_display_matches_the_report_file(tmp_path):
    src = _series(tmp_path)
    strategy = write_strategy(tmp_path / "Strategy Builder" / "book" / "strategy.py", 1.0)
    out = tmp_path / "Strategy Builder" / "out" / "one"
    result = execute_run(
        {
            "mode": "single",
            "profile": "arena",
            "source": "named",
            "strategies": [str(strategy)],
            "src": str(src),
            "ticker": "QQQ",
            "out": str(out),
            "bar_minutes": 1,
            "initial_equity": 1_000_000,
            "borrow_rate_annual": None,
            "financing_rate_annual": None,
            "fee_mult": 1,
            "dividends": "",
            "warmup_sessions": 0,
            "warmup_days": 0,
            "design_end_day": None,
            "design_end_date": "",
            "design_fraction": None,
        }
    )
    stored = json.loads((out / "metrics.json").read_text(encoding="utf-8"))
    assert result["headline"]["total_return"] == stored["total_return"]
    assert result["headline"]["final_equity"] == stored["final_equity"]
    report = load_run(out)
    assert report.metrics["total_return"] == stored["total_return"]
    assert report.metrics["sharpe"] == stored["sharpe"]
    assert report.metrics["max_drawdown"] == stored["max_drawdown"]
    assert report.benchmarks["buy_and_hold_1x"]["total_return"] == stored["benchmarks"]["buy_and_hold_1x"]["total_return"]
    config = json.loads((out / "run_config.json").read_text(encoding="utf-8"))
    assert config["api"] == "backtester.runner.run_named"
    assert config["arguments"]["borrow_rate_annual"] is None
    assert config["arguments"]["share_factor"] is None
    assert "Strategy Builder" in config["arguments"]["src"]
    assert "price_factor" not in json.dumps(config)


def test_walkforward_writes_two_legs(tmp_path):
    src = _series(tmp_path)
    strategy = write_strategy(tmp_path / "hold.py", 1.0)
    out = tmp_path / "walk"
    result = execute_run(
        {
            "mode": "walkforward",
            "profile": "arena",
            "source": "named",
            "strategies": [str(strategy)],
            "src": str(src),
            "ticker": "QQQ",
            "out": str(out),
            "bar_minutes": 1,
            "initial_equity": 1_000_000,
            "borrow_rate_annual": None,
            "financing_rate_annual": None,
            "fee_mult": 1,
            "dividends": "",
            "warmup_sessions": 0,
            "warmup_days": 0,
            "design_end_day": None,
            "design_end_date": "2024-01-02",
            "design_fraction": None,
        }
    )
    assert result["design_end_date"] == "2024-01-02"
    assert result["locked_start_date"] == "2024-01-03"
    report = load_run(out)
    assert report.kind == "walkforward"
    assert {child["leg"] for child in report.children} == {"design", "locked"}
    summary = json.loads((out / "walkforward.json").read_text(encoding="utf-8"))
    design = json.loads((out / "design" / "metrics.json").read_text(encoding="utf-8"))
    assert report.walkforward["design_sessions"] == summary["design_sessions"]
    assert design["total_return"] == summary["design"]["total_return"]


def test_batch_calls_run_parallel_with_progress_and_cancel(tmp_path, monkeypatch):
    src = _series(tmp_path)
    good = write_strategy(tmp_path / "strategies" / "good.py", 1.0)
    bad = tmp_path / "strategies" / "bad.py"
    bad.write_text("raise RuntimeError('boom')\n", encoding="utf-8")
    out = tmp_path / "batch"
    status = tmp_path / "status.json"
    seen = {}

    def fake(strategies, markets, out_dir, **kwargs):
        seen["strategies"] = list(strategies)
        seen["markets"] = list(markets)
        seen["kwargs"] = kwargs
        alpha = Path(out_dir) / "jobs" / strategies[1].strategy_id / "QQQ"
        alpha.mkdir(parents=True)
        (alpha / "metrics.json").write_text(
            json.dumps({"profile": "arena", "symbol": "QQQ", "total_return": 0.01}) + "\n",
            encoding="utf-8",
        )
        beta = Path(out_dir) / "jobs" / strategies[0].strategy_id / "QQQ"
        beta.mkdir(parents=True)
        (beta / "error.json").write_text(
            json.dumps({"strategy_id": strategies[0].strategy_id, "source_id": "QQQ", "status": "error", "error": "boom"})
            + "\n",
            encoding="utf-8",
        )
        (Path(out_dir) / "run.json").write_text(json.dumps({"kind": "parallel", "jobs": []}) + "\n", encoding="utf-8")
        (Path(out_dir) / "leaderboard.csv").write_text(
            "rank,strategy_id,total_return\n1," + strategies[1].strategy_id + ",0.01\n",
            encoding="utf-8",
        )
        kwargs["on_progress"](
            type("E", (), {"completed": 1, "total": 2, "strategy_id": "bad", "source_id": "QQQ", "status": "error", "message": "boom"})()
        )
        Path(str(status) + ".cancel").write_text("cancel\n", encoding="utf-8")
        for _ in range(40):
            if kwargs["cancel"].cancelled():
                break
            import time

            time.sleep(0.05)
        assert kwargs["cancel"].cancelled()
        return type(
            "R",
            (),
            {
                "out_dir": str(out_dir),
                "failures": 1,
                "results": (
                    type("J", (), {"strategy_id": strategies[0].strategy_id, "status": "error", "out_dir": str(beta), "error": "boom"})(),
                    type("J", (), {"strategy_id": strategies[1].strategy_id, "status": "ok", "out_dir": str(alpha), "error": None})(),
                ),
            },
        )()

    monkeypatch.setattr("backtester.runner.run_parallel", fake)
    result = execute_run(
        {
            "mode": "batch",
            "profile": "arena",
            "source": "named",
            "strategies": [str(bad), str(good)],
            "src": str(src),
            "ticker": "QQQ",
            "out": str(out),
            "bar_minutes": 1,
            "initial_equity": 1_000_000,
            "borrow_rate_annual": None,
            "financing_rate_annual": None,
            "fee_mult": 1,
            "dividends": "",
            "warmup_sessions": 0,
            "warmup_days": 0,
            "design_end_day": None,
            "design_end_date": "",
            "design_fraction": None,
            "workers": 1,
        },
        status_path=status,
    )
    ids = [item.strategy_id for item in seen["strategies"]]
    assert len(ids) == 2
    assert seen["markets"][0].symbol == "QQQ"
    assert seen["kwargs"]["workers"] == 1
    assert seen["kwargs"]["on_progress"] is not None
    assert result["failed"] == 1
    payload = json.loads(status.read_text(encoding="utf-8"))
    assert payload["count"] == 2
    assert "boom" in payload["message"]
    report = load_run(out, audience="operator")
    assert report.kind == "batch"
    failed = [child for child in report.children if child["state"] == "failed"]
    assert failed and "boom" in (failed[0]["error"] or "")
    assert any(child["state"] == "ok" for child in report.children)
