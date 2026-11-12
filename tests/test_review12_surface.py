"""Twelfth review. A digit does not glue to a longer ticker, and a raw scale id fails."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backtester.designer_allow import designer_source_id
from backtester.gui.audience import (
    _scale_in_token,
    _scrub_obj,
    _ticker_spans,
    designer_surface_text,
    export_allowed,
    export_block_reason,
    peek_scale,
    scan_publication,
)
from backtester.report import assert_designer_clean, designer_component_problem, read_leaderboard, scrub_symbols, ticker_token_spans
from test_parallel import FLAT, _anon_pack, _strategy


def _same(text: str, symbol: str) -> None:
    assert _ticker_spans(text, symbol) == ticker_token_spans(text, symbol)


def test_longer_tickers_use_a_letter_boundary_and_one_character_does_not():
    for text in ("QQQ1", "QQQ2momentum", "1QQQ", "held QQQ1", "my_V_v2", "v2", "S00002", "RANDOM-2"):
        for symbol in ("QQQ", "V", "2", "SS"):
            _same(text, symbol)
    assert ticker_token_spans("QQQ1", "QQQ") == [(0, 3)]
    assert ticker_token_spans("QQQ2momentum", "QQQ") == [(0, 3)]
    assert ticker_token_spans("1QQQ", "QQQ") == [(1, 4)]
    assert ticker_token_spans("v2", "V") == []
    assert ticker_token_spans("my_V_v2", "V") == [(3, 4)]
    assert ticker_token_spans("S00002", "2") == []
    assert ticker_token_spans("S00010", "10") == []
    assert ticker_token_spans("100", "10") == []
    assert ticker_token_spans("RANDOM-2", "2") == [(7, 8)]
    assert scrub_symbols("QQQ1", ["QQQ"]) == "RANDOM1"
    assert scrub_symbols("QQQ2momentum", ["QQQ"]) == "RANDOM2momentum"
    assert scrub_symbols("1QQQ", ["QQQ"]) == "1RANDOM"
    assert designer_component_problem("QQQ1", symbols=("QQQ",), scale=1.1) == "the ticker"
    assert designer_component_problem("RANDOM1", symbols=("QQQ",), scale=1.1) is None
    assert designer_component_problem("1RANDOM", symbols=("QQQ",), scale=1.1) == "an unsafe name"
    assert designer_component_problem("v2", symbols=("V",), scale=1.1) is None


def test_glued_ticker_ids_publish_and_a_leftover_fails(tmp_path):
    from backtester.parallel import PackMarket, run_parallel

    names = ("QQQ1", "QQQ2momentum", "1QQQ")
    strategies = [_strategy(tmp_path / "strategies", name, FLAT) for name in names]
    pack, sidecar, _built = _anon_pack(tmp_path, "QQQ", name="glued", price_factor=1.1)
    out = tmp_path / "out"
    result = run_parallel(
        strategies,
        [PackMarket(str(pack), str(sidecar))],
        out,
        profile="arena",
        workers=1,
        timeout_s=60,
        with_benchmarks=False,
    )
    assert result.failures == 1
    assert (out / "run.json").is_file()
    jobs = json.loads((out / "run.json").read_text(encoding="utf-8"))["jobs"]
    assert {job["strategy_id"]: job["status"] for job in jobs} == {
        "QQQ1": "ok",
        "QQQ2momentum": "ok",
        "1QQQ": "error",
    }
    designer = out / "designer"
    board = read_leaderboard(designer / "leaderboard.csv")
    operator = read_leaderboard(out / "leaderboard.csv")
    assert sorted(row["status"] for row in board) == sorted(row["status"] for row in operator)
    assert sorted(row["status"] for row in board) == sorted(job["status"] for job in jobs)
    published = {row["strategy_id"] for row in board if row["status"] == "ok"}
    assert published == {"RANDOM1", "RANDOM2momentum"}
    assert "1RANDOM" not in {row["strategy_id"] for row in board}
    failed = [row for row in board if row["status"] == "error"]
    assert len(failed) == 1
    assert failed[0]["strategy_id"] not in names
    errors = list((designer / failed[0]["strategy_id"]).rglob("error.json"))
    assert errors
    document = json.loads(errors[0].read_text(encoding="utf-8"))
    assert document["error"] == "the strategy failed"
    assert document["strategy_id"] == failed[0]["strategy_id"]
    for row in board:
        if row["status"] == "ok":
            assert (designer / row["strategy_id"] / row["source_id"] / "metrics.json").is_file()
    blob = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in designer.rglob("*")
        if path.is_file() and path.suffix.lower() != ".png"
    )
    assert "QQQ" not in blob
    assert "1QQQ" not in blob
    assert "1RANDOM" not in blob
    assert "RANDOM1" in blob and "RANDOM2momentum" in blob
    assert_designer_clean(designer, symbol="QQQ", scale=1.1)
    board_text = (designer / "leaderboard.csv").read_text(encoding="utf-8")
    assert export_allowed(board_text, symbols=("QQQ",), scale=1.1)
    assert scan_publication(board_text, symbols=("QQQ",), scale=1.1) is None
    leftover = tmp_path / "leftover"
    leftover.mkdir()
    (leftover / "QQQ1").mkdir()
    with pytest.raises(RuntimeError, match="ticker"):
        assert_designer_clean(leftover, symbol="QQQ", scale=1.1)


def test_raw_scale_id_fails_one_job(tmp_path):
    from backtester.parallel import PackMarket, run_parallel

    cases = (("pack_2e2abcdef0", 2.0), ("pack_25e2abcd", 0.25))
    for ident, scale in cases:
        root = tmp_path / ident
        strategies = [
            _strategy(root / "strategies", "opening_drive", FLAT),
            _strategy(root / "strategies", ident, FLAT),
        ]
        pack, sidecar, _built = _anon_pack(root, "QQQ", name="pack", price_factor=scale)
        out = root / "out"
        run_parallel(
            strategies,
            [PackMarket(str(pack), str(sidecar))],
            out,
            profile="arena",
            workers=1,
            timeout_s=60,
            with_benchmarks=False,
        )
        assert (out / "run.json").is_file()
        jobs = json.loads((out / "run.json").read_text(encoding="utf-8"))["jobs"]
        by_id = {job["strategy_id"]: job["status"] for job in jobs}
        assert by_id == {"opening_drive": "ok", ident: "error"}
        operator = read_leaderboard(out / "leaderboard.csv")
        designer_root = out / "designer"
        designer = read_leaderboard(designer_root / "leaderboard.csv")
        assert sorted(row["status"] for row in operator) == sorted(row["status"] for row in designer)
        assert sorted(row["status"] for row in designer) == sorted(job["status"] for job in jobs)
        shown = {row["strategy_id"] for row in designer}
        assert "opening_drive" in shown
        assert ident not in shown
        failed = [row for row in designer if row["status"] == "error"]
        assert len(failed) == 1
        folder = designer_root / failed[0]["strategy_id"]
        assert folder.is_dir()
        assert folder.name != ident
        errors = list(folder.rglob("error.json"))
        assert errors
        for path in errors:
            document = json.loads(path.read_text(encoding="utf-8"))
            assert document["error"] == "the strategy failed"
        blob = "\n".join(
            path.read_text(encoding="utf-8", errors="replace")
            for path in designer_root.rglob("*")
            if path.is_file() and path.suffix.lower() != ".png"
        )
        assert ident not in blob
        assert any((designer_root / "opening_drive").rglob("metrics.json"))
        assert_designer_clean(designer_root, symbol="QQQ", scale=scale)


def test_raw_scale_ids_fail_both_gates_and_the_stored_id_does_not():
    raw = (
        ("pack_2e2abcdef0", 2),
        ("pack_25e2abcd", 0.25),
    )
    for ident, scale in raw:
        assert _scale_in_token(ident, scale) is True
        assert scan_publication(ident, scale=scale) == "the scale"
        assert export_block_reason(ident, symbols=(), scale=scale) == "the scale"
        assert export_allowed(ident, symbols=(), scale=scale) is False
        stored = designer_source_id(ident)
        assert stored != ident
        assert ident not in stored
        for other in (0.25, 0.5, 0.7, 2.0):
            assert export_allowed(stored, symbols=(), scale=other)
            assert scan_publication(stored, scale=other) is None
    assert _scale_in_token("pack_185e2abcdef", 2) is False
    assert scan_publication("4738e304", scale=2) == "a date"


def test_signed_exponent_identifiers_agree():
    cases = (
        ("pack_2e+2abcdef", 1.1, None),
        ("note25e+2", 1.1, None),
        ("pack_2e+2abcdef", 2, "the scale"),
        ("pack_2e+2abcdef", 200, "the scale"),
        ("note25e+2", 0.25, "the scale"),
        ("note25e+2", 2, "the scale"),
        ("1RANDOM", 1.1, None),
        ("2momentum", 1.1, None),
    )
    for text, scale, reason in cases:
        assert scan_publication(text, scale=scale) == reason
        assert export_block_reason(text, symbols=(), scale=scale) == reason
        assert export_allowed(text, symbols=(), scale=scale) is (reason is None)
    board = "strategy_id,sharpe\n1RANDOM,1.2\n"
    assert export_allowed(board, symbols=(), scale=1.1)
    assert scan_publication(board, scale=1.1) is None


def test_id_columns_keep_a_leading_digit_and_a_name_does_not():
    cleaned = _scrub_obj(
        {"strategy_id": "1RANDOM", "source_id": "1RANDOM", "name": "1RANDOM"},
        ("QQQ",),
        drop_prices=True,
        scale=1.1,
        allow_list=True,
    )
    assert cleaned["strategy_id"] == "1RANDOM"
    assert cleaned["source_id"] == "1RANDOM"
    assert cleaned["name"] == ""
    assert designer_surface_text("1RANDOM", ("QQQ",), scale=1.1) == ""
    assert export_allowed("note 2.5", symbols=(), scale=1.1) is False


def test_peek_scale_reads_operator_job_metrics(tmp_path: Path):
    out = tmp_path / "out"
    job = out / "jobs" / "opening_drive" / "src"
    job.mkdir(parents=True)
    (job / "metrics.json").write_text(
        json.dumps({"symbol": "QQQ", "cost_basis": {"share_scale": 1.1}}) + "\n",
        encoding="utf-8",
    )
    assert peek_scale(out) == 1.1
    designer = out / "designer"
    designer.mkdir()
    assert peek_scale(designer) == 1.1
    assert peek_scale(tmp_path / "missing") is None
