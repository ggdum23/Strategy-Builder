"""Report files: curves, the chart, liquidation, and the designer copy."""

from __future__ import annotations

import json
import multiprocessing
import threading
from pathlib import Path

import pandas as pd
import pytest

from backtester.engine_desk import DeskConfig, metrics, run
from backtester.report import (
    REPORT_CONTRACT,
    assert_designer_clean,
    json_ready,
    load_report,
    read_leaderboard,
    redact_metrics,
    register_run,
    write_book,
    write_json,
    write_leaderboard,
)
from backtester.runner import execute


def _frame():
    rows = []
    for day in (0, 1):
        for minute in (570, 571):
            rows.append(
                {
                    "date": f"2026-03-0{2 + day}",
                    "day": day,
                    "weekday": 0,
                    "minute": minute,
                    "time": f"{minute // 60:02d}:{minute % 60:02d}",
                    "session": "rth",
                    "open": 100.0,
                    "high": 100.0,
                    "low": 100.0,
                    "close": 100.0,
                    "volume": 1000.0,
                    "wap": 100.0,
                    "count": 1,
                    "filled": 0,
                    "short_session": 0,
                }
            )
    return pd.DataFrame(rows)


class _Flat:
    def on_bar(self, _bar, _state):
        return None


def test_equity_csv_matches_the_curve_and_the_png_is_a_chart(tmp_path):
    result, reported = execute(_Flat(), _frame(), "arena")
    write_book(tmp_path, "arena", result, reported, _frame())
    assert (tmp_path / "equity.csv").read_bytes() == (tmp_path / "equity_curve.csv").read_bytes()
    png = (tmp_path / "equity.png").read_bytes()
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert b"NIK" not in png
    assert not (tmp_path / "liquidation.json").is_file()
    assert json_ready({"calmar": reported["calmar"]}) == {"calmar": None}


def _bar(session, time, phase, price, **kw):
    hour, minute = (int(part) for part in time.split(":"))
    close = float(kw.get("close", price))
    return {
        "session": session,
        "time": time,
        "minute": hour * 60 + minute,
        "phase": phase,
        "open": float(kw.get("open", price)),
        "high": float(kw.get("high", max(price, close))),
        "low": float(kw.get("low", min(price, close))),
        "close": close,
        "volume": 1000.0,
        "wap": close,
        "count": 1,
        "filled": 0,
        "new_session": False,
    }


def test_liquidation_json_is_written_only_on_a_knockout(tmp_path):
    bars = [
        _bar("S1", "09:30", "rth", 100),
        _bar("S1", "09:31", "rth", 100),
        _bar("S1", "15:59", "rth", 100),
        _bar("S1", "16:00", "post", 100, close=20, low=20),
        _bar("S2", "09:30", "rth", 20),
    ]

    class Long:
        def on_bar(self, row, _account):
            if row["time"] == "09:30" and row["session"] == "S1":
                return 1.0
            return None

    result = run(Long(), bars, DeskConfig())
    reported = metrics(result)
    assert reported["disqualified"] is True
    knocked = tmp_path / "knocked"
    write_book(knocked, "desk", result, reported, None)
    post = json.loads((knocked / "liquidation.json").read_text(encoding="utf-8"))
    for key in ("session", "time", "phase", "close", "equity_before", "hwm", "shares_before", "equity_after", "shares_after"):
        assert key in post
    assert post["phase"] == "post"
    assert post["shares_after"] == 0
    assert reported["disqualified"] is True

    flat = run(_Flat(), bars, DeskConfig())
    quiet = tmp_path / "quiet"
    write_book(quiet, "desk", flat, metrics(flat), None)
    assert not (quiet / "liquidation.json").is_file()
    assert (quiet / "equity.csv").read_bytes() == (quiet / "equity_curve.csv").read_bytes()


def test_redaction_drops_the_ticker_the_date_and_the_factor(tmp_path):
    payload = {
        "symbol": "BRK.B",
        "share_scale": 1.7,
        "share_factor": 1.7,
        "cost_basis": {"share_scale": 1.7, "note": "share_scale=1.7"},
        "inspection": {"source_root": "C:/hidden"},
        "note": "2024-01-02 BRK.B held",
        "total_return": 0.1,
    }
    cleaned = redact_metrics(payload, symbol="BRK.B")
    text = json.dumps(cleaned)
    for key in ("symbol", "share_scale", "share_factor", "cost_basis", "inspection"):
        assert key not in cleaned
    assert "2024-01-02" not in text
    assert "BRK.B" not in text
    assert "ASSET" in text
    assert cleaned["total_return"] == 0.1
    assert cleaned["audience"] == "designer"
    root = tmp_path / "designer"
    root.mkdir()
    (root / "metrics.json").write_text(text, encoding="utf-8")
    assert_designer_clean(root, symbol="BRK.B")
    (root / "planted.json").write_text('{"note": "share_scale"}', encoding="utf-8")
    with pytest.raises(RuntimeError, match="share_scale"):
        assert_designer_clean(root, symbol="BRK.B")


def test_leaderboard_uses_lf_and_writes_inf():
    import tempfile

    rows = [
        {
            "rank": 1,
            "strategy_id": "flat",
            "source_id": "book",
            "symbol": "NIK",
            "profile": "arena",
            "status": "ok",
            "total_return": 0.1,
            "excess_total_return": 0.05,
            "calmar": float("inf"),
            "disqualified": False,
            "error": "",
        }
    ]
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "leaderboard.csv"
        write_leaderboard(path, rows, audience="operator")
        raw = path.read_bytes()
        assert b"\r\n" not in raw
        assert raw.endswith(b"\n")
        loaded = read_leaderboard(path)
        assert loaded[0]["symbol"] == "NIK"
        assert loaded[0]["calmar"] == "inf"
        assert loaded[0]["disqualified"] == "false"
        designer = Path(directory) / "designer.csv"
        write_leaderboard(designer, rows, audience="designer")
        header = designer.read_text(encoding="utf-8").splitlines()[0]
        assert "symbol" not in header.split(",")
        assert "strategy_id" in header


def test_register_run_keeps_every_id_under_threads_and_processes(tmp_path):
    index = tmp_path / "runs.json"
    register_run(index, "seed", tmp_path / "seed", kind="book", profile="arena")
    ids = [f"t{number}" for number in range(8)] + ["p0", "p1"]

    def once(run_id: str) -> None:
        register_run(index, run_id, tmp_path / run_id, kind="parallel", profile="desk")

    threads = [threading.Thread(target=once, args=(run_id,)) for run_id in ids[:8]]
    ctx = multiprocessing.get_context("spawn")
    processes = [
        ctx.Process(
            target=register_run,
            args=(str(index), run_id, str(tmp_path / run_id)),
            kwargs={"kind": "book", "profile": "arena"},
        )
        for run_id in ids[8:]
    ]
    for thread in threads:
        thread.start()
    for proc in processes:
        proc.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive()
    for proc in processes:
        proc.join(timeout=60)
        assert proc.exitcode == 0
    document = json.loads(index.read_text(encoding="utf-8"))
    assert set(document["runs"]) == set(["seed", *ids])
    assert document["schema_version"] == 1
    assert document["runs"]["seed"]["kind"] == "book"


def test_load_report_types_parallel_cells(tmp_path):
    rows = [
        {
            "rank": 1,
            "strategy_id": "flat",
            "source_id": "book",
            "symbol": "NIK",
            "profile": "arena",
            "status": "ok",
            "total_return": 0.125,
            "excess_total_return": -0.5,
            "calmar": float("inf"),
            "n_fills": 4,
            "disqualified": False,
            "long_2x_total_return": None,
            "error": "",
        }
    ]
    write_json(tmp_path / "run.json", {"schema_version": 1, "id": "r1", "kind": "parallel", "profile": "arena"})
    write_leaderboard(tmp_path / "leaderboard.csv", rows, audience="operator")
    view = load_report(tmp_path)
    assert view["contract"] == REPORT_CONTRACT == "strategy-builder.report.v1"
    assert view["kind"] == "parallel"
    row = view["rows"][0]
    assert row["rank"] == 1 and isinstance(row["rank"], int)
    assert row["n_fills"] == 4 and isinstance(row["n_fills"], int)
    assert row["total_return"] == pytest.approx(0.125) and isinstance(row["total_return"], float)
    assert row["excess_total_return"] == pytest.approx(-0.5)
    assert row["calmar"] == float("inf")
    assert row["disqualified"] is False
    assert row["long_2x_total_return"] is None
    assert row["strategy_id"] == "flat"
    assert row["error"] is None
    raw = read_leaderboard(tmp_path / "leaderboard.csv")
    assert raw[0]["calmar"] == "inf"
    assert raw[0]["disqualified"] == "false"
