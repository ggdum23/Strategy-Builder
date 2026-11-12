"""Eleventh review. Strategy ids are not prices. Both gates read the stored id."""

from __future__ import annotations

import json
import random
from pathlib import Path

from backtester.gui.audience import (
    _scale_in_token,
    export_allowed,
    export_block_reason,
    scan_publication,
)
from backtester.parallel import assert_designer_clean as parallel_assert
from backtester.report import assert_designer_clean, designer_component_problem, read_leaderboard
from test_parallel import FLAT, _anon_pack, _strategy

_SCALES = (0.25, 0.5, 0.7, 2.0, 7.777)


def _run(tmp_path: Path, name: str, strategies, *, symbol: str, price_factor: float):
    from backtester.parallel import PackMarket, run_parallel

    pack, sidecar, _built = _anon_pack(tmp_path, symbol, name=name, price_factor=price_factor)
    out = tmp_path / name / "out"
    result = run_parallel(
        strategies,
        [PackMarket(str(pack), str(sidecar))],
        out,
        profile="arena",
        workers=1,
        timeout_s=60,
        with_benchmarks=False,
    )
    return result, out


def _statuses(rows) -> list[str]:
    return sorted(row["status"] for row in rows)


def test_decimal_strategy_id_is_not_a_price_and_the_batch_finishes(tmp_path):
    strategies = [
        _strategy(tmp_path / "strategies", name, FLAT)
        for name in ("opening_drive", "mean_reversion", "ema12", "vol_1.5", "qqq_momentum")
    ]
    result, out = _run(tmp_path, "five", strategies, symbol="QQQ", price_factor=1.1)
    assert (out / "run.json").is_file()
    document = json.loads((out / "run.json").read_text(encoding="utf-8"))
    jobs = document["jobs"]
    assert {job["strategy_id"] for job in jobs} == {
        "opening_drive",
        "mean_reversion",
        "ema12",
        "vol_1.5",
        "qqq_momentum",
    }
    assert {job["status"] for job in jobs} == {"ok"}
    operator = read_leaderboard(out / "leaderboard.csv")
    designer_root = out / "designer"
    designer = read_leaderboard(designer_root / "leaderboard.csv")
    assert _statuses(operator) == _statuses(designer) == _statuses(jobs)
    assert {row["strategy_id"] for row in operator} == {job["strategy_id"] for job in jobs}
    folders = {path.name for path in designer_root.iterdir() if path.is_dir()}
    assert folders == {row["strategy_id"] for row in designer}
    assert "vol_1.5" in folders
    assert "qqq_momentum" not in folders
    assert "RANDOM_momentum" in folders
    assert {row["strategy_id"] for row in designer} == {
        "opening_drive",
        "mean_reversion",
        "ema12",
        "vol_1.5",
        "RANDOM_momentum",
    }
    for row in designer:
        book = designer_root / row["strategy_id"] / row["source_id"]
        assert (book / "metrics.json").is_file()
        assert row["status"] == "ok"
    blob = "\n".join(
        path.read_text(encoding="utf-8")
        for path in designer_root.rglob("*")
        if path.is_file() and path.suffix.lower() != ".png"
    )
    assert "qqq_momentum" not in blob.casefold()
    assert_designer_clean(designer_root, symbol="QQQ", scale=1.1)
    assert result.failures == 0


def test_scale_in_a_strategy_id_fails_one_job_and_the_batch_finishes(tmp_path):
    strategies = [
        _strategy(tmp_path / "scale-strategies", "opening_drive", FLAT),
        _strategy(tmp_path / "scale-strategies", "note25e2", FLAT),
    ]
    _result, out = _run(tmp_path, "scale-id", strategies, symbol="QQQ", price_factor=0.25)
    assert (out / "run.json").is_file()
    jobs = json.loads((out / "run.json").read_text(encoding="utf-8"))["jobs"]
    by_id = {job["strategy_id"]: job["status"] for job in jobs}
    assert by_id == {"opening_drive": "ok", "note25e2": "error"}
    operator = read_leaderboard(out / "leaderboard.csv")
    designer_root = out / "designer"
    designer = read_leaderboard(designer_root / "leaderboard.csv")
    assert _statuses(operator) == _statuses(designer) == _statuses(jobs)
    assert {row["strategy_id"]: row["status"] for row in operator} == by_id
    shown = {row["strategy_id"]: row["status"] for row in designer}
    assert shown["opening_drive"] == "ok"
    assert "note25e2" not in shown
    failed = [row for row in designer if row["status"] == "error"]
    assert len(failed) == 1
    folder = designer_root / failed[0]["strategy_id"]
    assert folder.is_dir()
    assert folder.name != "note25e2"
    errors = list(folder.rglob("error.json"))
    assert errors
    for path in errors:
        document = json.loads(path.read_text(encoding="utf-8"))
        assert document["error"] == "the strategy failed"
        assert "note25e2" not in path.read_text(encoding="utf-8")
    blob = "\n".join(
        path.read_text(encoding="utf-8")
        for path in designer_root.rglob("*")
        if path.is_file() and path.suffix.lower() != ".png"
    )
    assert "note25e2" not in blob
    assert "25e2" not in blob
    kept = designer_root / "opening_drive"
    assert any(kept.rglob("metrics.json"))
    assert_designer_clean(designer_root, symbol="QQQ", scale=0.25)


def test_isolate_dirty_folders_does_not_abort_the_batch(tmp_path, monkeypatch):
    real = parallel_assert

    def boom(directory, **kwargs):
        path = Path(directory)
        if path.name == "opening_drive":
            raise RuntimeError("forced isolate failure")
        if path.name == "designer" and (path / "leaderboard.csv").is_file():
            raise RuntimeError("forced tree failure")
        return real(directory, **kwargs)

    monkeypatch.setattr("backtester.parallel.assert_designer_clean", boom)
    strategies = [
        _strategy(tmp_path / "isolate-strategies", "opening_drive", FLAT),
        _strategy(tmp_path / "isolate-strategies", "mean_reversion", FLAT),
    ]
    _result, out = _run(tmp_path, "isolate", strategies, symbol="QQQ", price_factor=1.1)
    assert (out / "run.json").is_file()
    jobs = json.loads((out / "run.json").read_text(encoding="utf-8"))["jobs"]
    by_id = {job["strategy_id"]: job["status"] for job in jobs}
    assert by_id["mean_reversion"] == "ok"
    assert by_id["opening_drive"] == "error"
    operator = read_leaderboard(out / "leaderboard.csv")
    designer = read_leaderboard(out / "designer" / "leaderboard.csv")
    assert _statuses(operator) == _statuses(designer) == _statuses(jobs)
    assert any((out / "designer" / "mean_reversion").rglob("metrics.json"))


def test_path_predicate_skips_price_and_keeps_scale():
    assert designer_component_problem("vol_1.5", symbols=("QQQ",), scale=2) is None
    assert designer_component_problem("vol_1.5", symbols=("QQQ",), scale=1.1) is None
    assert designer_component_problem("vol_1.5", symbols=("QQQ",), scale=1.5) == "the scale"
    assert designer_component_problem("note25e2", scale=0.25) == "the scale"
    assert designer_component_problem("2", symbols=("2",), scale=1.5) == "the ticker"
    assert designer_component_problem("qqq_momentum", symbols=("QQQ",), scale=1.1) == "the ticker"


def test_unsigned_exponent_matches_when_the_value_is_the_scale():
    assert _scale_in_token("note25e2", 0.25) is True
    assert _scale_in_token("pack_185e2abcdef", 2) is False
    assert _scale_in_token("pack_2e2abcdef0", 2) is True
    assert _scale_in_token("pack_25e2abcd", 0.25) is True
    assert _scale_in_token("build2_7.777e3", 7.777) is True
    assert scan_publication("note25e2", scale=0.25) == "the scale"
    assert export_allowed("note25e2", symbols=(), scale=0.25) is False
    assert export_block_reason("note25e2", symbols=(), scale=0.25) == "the scale"
    assert scan_publication("pack_2e2abcdef0", scale=2) == "the scale"
    assert export_block_reason("pack_2e2abcdef0", symbols=(), scale=2) == "the scale"
    assert scan_publication("4738e304", scale=2) is None


def test_both_gates_agree_on_forty_thousand_pack_ids():
    from backtester.designer_allow import designer_source_id

    rng = random.Random(11)
    pinned = (
        "pack_b2040abcde",
        "pack_c2011abcde",
        "pack_1950cabcde",
        "pack_185e2abcdef",
        "pack_4738e304ab",
    )
    ids = list(pinned)
    ids.extend("pack_" + "".join(rng.choice("0123456789abcdef") for _ in range(12)) for _ in range(40_000))
    scales = (0.25, 0.5, 0.7, 2.0)
    for ident in ids:
        stored = designer_source_id(ident)
        for scale in scales:
            allowed = export_allowed(stored, symbols=(), scale=scale)
            scanned = scan_publication(stored, scale=scale)
            assert allowed is True, (ident, stored, scale)
            assert scanned is None
            assert allowed == (scanned is None)


def test_formatted_leaderboard_uses_the_column_key_not_the_label():
    labeled = "sharpe|Sharpe,total_return|Total return\n-3.61,-6.92%\n"
    bare = "Sharpe,Total return\n-3.61,-6.92%\n"
    identified = "strategy_id,sharpe\nvol_1.5,-3.61\n"
    for scale in _SCALES:
        assert export_allowed(labeled, symbols=(), scale=scale)
        assert scan_publication(labeled, scale=scale) is None
        assert export_allowed(bare, symbols=(), scale=scale) is False
        assert scan_publication(bare, scale=scale) == "a price"
        assert export_allowed(identified, symbols=(), scale=scale)
        assert scan_publication(identified, scale=scale) is None
        assert export_allowed("close 450.12", symbols=(), scale=scale) is False
        assert export_allowed("note 2.5", symbols=(), scale=scale) is False
    assert export_allowed(identified, symbols=(), scale=1.5) is False
    assert scan_publication(identified, scale=1.5) == "the scale"
    reversed_header = "Sharpe|sharpe,Total return|total_return\n-3.61,-6.92%\n"
    assert export_allowed(reversed_header, symbols=(), scale=2) is False
    assert scan_publication(reversed_header, scale=2) == "a price"
