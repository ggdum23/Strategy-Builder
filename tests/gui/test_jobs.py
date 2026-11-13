"""The worker calls the public runner and does not return sidecar fields."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from backtester.gui.backend import load_run, read_designer_manifest
from backtester.gui.jobs import build_pack_job, execute_run, inspect_series, record_series
from windows_open import windows_jails

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
    from backtester.pack import load_sidecar

    secret = load_sidecar(sidecar)
    assert len(secret["factor_mac"]) == 64
    assert len(secret["factor_key"]) == 64
    assert "factor_key" not in blob
    assert "factor_mac" not in blob
    assert "borrow_rate_source" not in blob
    document = json.loads(text)
    document.pop("factor_mac", None)
    document.pop("factor_key", None)
    sidecar.write_text(json.dumps(document), encoding="utf-8")
    strategy = write_strategy(tmp_path / "strategy.py", 1.0)
    try:
        execute_run(
            {
                "mode": "single",
                "profile": "arena",
                "source": "pack",
                "strategies": [str(strategy)],
                "pack": str(pack),
                "sidecar": str(sidecar),
                "out": str(tmp_path / "out"),
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
    except ValueError as exc:
        assert "rebuild the pack" in str(exc)
    else:
        raise AssertionError("a sidecar with no factor MAC was accepted")


@windows_jails(1)
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


@windows_jails(1)
def test_execute_run_single_on_a_file_strategy_launches_the_jail(tmp_path):
    from backtester import sandbox

    src = _series(tmp_path)
    strategy = write_strategy(tmp_path / "Strategy Builder" / "book" / "strategy.py", 1.0)
    out = tmp_path / "Strategy Builder" / "out" / "jailed"
    seen = []
    previous = sandbox._LAUNCH_OBSERVER

    def observer():
        seen.append(1)
        if previous is not None:
            previous()

    sandbox.set_launch_observer(observer)
    try:
        execute_run(
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
    finally:
        sandbox.set_launch_observer(previous)
    assert seen


@windows_jails(2)
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


def _write_probe(path: Path, out: Path) -> Path:
    """Module-level code writes a pid marker beside the file it actually executed.

    The GUI worker passes the source path into the jail. The jail copies the
    file, so a marker beside this path exists only when the caller imported it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    out_literal = json.dumps(str(out))
    path.write_text(
        "import json\n"
        "import os\n"
        "from pathlib import Path\n"
        f"\n_OUT = Path({out_literal})\n"
        "\n"
        "def _facts():\n"
        "    saw = False\n"
        "    try:\n"
        "        import gc\n"
        "        for obj in gc.get_objects():\n"
        "            if not isinstance(obj, dict) or 'frame' not in obj:\n"
        "                continue\n"
        "            frame = obj.get('frame')\n"
        "            try:\n"
        "                closes = [round(float(value), 2) for value in list(frame['close'])[:3]]\n"
        "            except Exception:\n"
        "                continue\n"
        "            if closes == [100.01, 100.02, 100.03]:\n"
        "                saw = True\n"
        "                break\n"
        "    except Exception:\n"
        "        saw = False\n"
        "    leaked = None\n"
        "    try:\n"
        "        record = json.loads((_OUT / 'run_config.json').read_text(encoding='utf-8'))\n"
        "        side = (record.get('arguments') or {}).get('sidecar_path')\n"
        "        if side:\n"
        "            document = json.loads(Path(side).read_text(encoding='utf-8'))\n"
        "            leaked = document.get('symbol') or document.get('price_factor')\n"
        "    except Exception:\n"
        "        leaked = None\n"
        "    return {'pid': int(os.getpid()), 'saw_holder_frame': bool(saw), 'sidecar': leaked}\n"
        "\n"
        "_FACTS = _facts()\n"
        "try:\n"
        "    Path(__file__).with_name('import-marker.json').write_text(\n"
        "        json.dumps(_FACTS), encoding='utf-8'\n"
        "    )\n"
        "except Exception:\n"
        "    pass\n"
        "\n"
        "class Strategy:\n"
        "    def __init__(self):\n"
        "        self.surface = dict(_FACTS)\n"
        "\n"
        "    def on_bar(self, bar, state):\n"
        "        return 0\n",
        encoding="utf-8",
    )
    return path


def _run_config(**overrides) -> dict:
    config = {
        "mode": "single",
        "profile": "arena",
        "source": "named",
        "strategies": [],
        "src": "",
        "ticker": "QQQ",
        "out": "",
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
    config.update(overrides)
    return config


def _assert_caller_did_not_exec(strategy: Path, *surfaces: Path) -> None:
    """The caller's pid is not the process that executed the strategy file."""
    caller = os.getpid()
    marker = strategy.with_name("import-marker.json")
    assert not marker.is_file()
    assert str(strategy.resolve().parent) not in sys.path
    assert surfaces
    for path in surfaces:
        body = json.loads(path.read_text(encoding="utf-8"))
        assert int(body["pid"]) != caller
        assert body["saw_holder_frame"] is False
        assert body["sidecar"] is None


def test_an_in_process_load_writes_the_caller_pid_and_drops_sys_path(tmp_path):
    """The marker mechanism sees this process. The GUI tests require that it does not."""
    out = tmp_path / "out"
    out.mkdir()
    sidecar = tmp_path / "sidecar.json"
    sidecar.write_text(json.dumps({"symbol": "QQQ", "price_factor": 1.25}), encoding="utf-8")
    (out / "run_config.json").write_text(
        json.dumps({"arguments": {"sidecar_path": str(sidecar)}}) + "\n",
        encoding="utf-8",
    )
    strategy = _write_probe(tmp_path / "book" / "strategy.py", out)
    holder = {"frame": {"close": [100.01, 100.02, 100.03]}, "factor": 1.0}
    from backtester.strategy_loader import load_strategy_class

    parent = str(strategy.resolve().parent)
    assert parent not in sys.path
    load_strategy_class(strategy)
    assert parent not in sys.path
    body = json.loads(strategy.with_name("import-marker.json").read_text(encoding="utf-8"))
    assert int(body["pid"]) == os.getpid()
    assert body["saw_holder_frame"] is True
    assert body["sidecar"] == "QQQ"
    del holder


@windows_jails(1)
def test_single_run_does_not_execute_the_strategy_in_the_caller(tmp_path):
    src = _series(tmp_path)
    out = tmp_path / "out"
    strategy = _write_probe(tmp_path / "book" / "strategy.py", out)
    execute_run(_run_config(strategies=[str(strategy)], src=str(src), out=str(out)))
    _assert_caller_did_not_exec(strategy, out / "child_surface.json")


@windows_jails(1)
def test_pack_run_does_not_execute_the_strategy_in_the_caller(tmp_path):
    src = _series(tmp_path)
    registry = tmp_path / "registry.json"
    record_series(src, "QQQ", registry, profile="arena", price_kind="raw", bar_minutes=1)
    pack = tmp_path / "pack"
    sidecar = tmp_path / "operator" / "sidecar.json"
    build_pack_job(
        src,
        "QQQ",
        pack,
        sidecar,
        registry,
        anonymize=False,
        profile="arena",
        bar_minutes=1,
        price_kind="raw",
    )
    out = tmp_path / "out"
    strategy = _write_probe(tmp_path / "book" / "strategy.py", out)
    execute_run(
        _run_config(
            source="pack",
            strategies=[str(strategy)],
            pack=str(pack),
            sidecar=str(sidecar),
            out=str(out),
        )
    )
    _assert_caller_did_not_exec(strategy, out / "child_surface.json")
    # Import-time code that ran here could open the sidecar. The caller did not.
    secret = sidecar.read_text(encoding="utf-8")
    assert "QQQ" in secret
    marker = strategy.with_name("import-marker.json")
    assert not marker.is_file()
    surface = json.loads((out / "child_surface.json").read_text(encoding="utf-8"))
    assert surface["sidecar"] is None
    assert secret not in json.dumps(surface)


@windows_jails(2)
def test_walk_forward_does_not_execute_the_strategy_in_the_caller(tmp_path):
    src = _series(tmp_path)
    out = tmp_path / "walk"
    strategy = _write_probe(tmp_path / "book" / "strategy.py", out)
    execute_run(
        _run_config(
            mode="walkforward",
            strategies=[str(strategy)],
            src=str(src),
            out=str(out),
            design_end_date="2024-01-02",
        )
    )
    _assert_caller_did_not_exec(
        strategy,
        out / "design" / "child_surface.json",
        out / "locked" / "child_surface.json",
    )
