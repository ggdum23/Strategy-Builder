"""Price basis, cash interest, and financing. Defaults stay bit-identical."""

from __future__ import annotations

import hashlib
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from backtester import costs as arena_costs
from backtester import engine_arena, engine_desk
from backtester.accounting import (
    borrowed_base,
    dividend_cash,
    idle_base,
    refuse_mixed_rows,
    require_annual_rate,
    require_withholding_rate,
    resolve_accounting,
    session_accrual,
)
from backtester.engine_desk import DeskConfig
from backtester.report import assert_designer_clean, write_json, write_leaderboard

ROOT = Path(__file__).resolve().parents[1]
ET = timezone(timedelta(hours=-4))
COSTS_SHA256 = "a3b0a522d8bf469001e75aaf274b0c5d3cbef624a68ed9fb94d29298110fe281"


class Flat:
    def on_bar(self, _bar, _state):
        return None


class Always:
    def __init__(self, target):
        self.target = target

    def on_bar(self, _bar, _state):
        return self.target


def _frame(days=2):
    rows = []
    for day in range(days):
        for minute in (570, 571):
            rows.append(
                {
                    "date": f"2026-03-{2 + day:02d}",
                    "day": day,
                    "weekday": day,
                    "minute": minute,
                    "time": f"{minute // 60:02d}:{minute % 60:02d}",
                    "session": "rth",
                    "open": 100.0,
                    "high": 100.5,
                    "low": 99.5,
                    "close": 100.25,
                    "volume": 1000.0,
                    "wap": 100.0,
                    "count": 10,
                    "filled": 0,
                    "short_session": 0,
                }
            )
    return pd.DataFrame(rows)


def _desk_bars():
    from test_desk import bar

    return [
        bar("S1", "09:30", "rth", 100),
        bar("S1", "09:31", "rth", 100),
        bar("S1", "15:59", "rth", 100),
        bar("S2", "09:30", "rth", 100),
        bar("S2", "15:59", "rth", 100),
    ]


def test_costs_file_is_the_pinned_reference():
    digest = hashlib.sha256((ROOT / "src" / "backtester" / "costs.py").read_bytes()).hexdigest()
    assert digest == COSTS_SHA256
    assert digest == hashlib.sha256((ROOT / "tests" / "reference" / "arena" / "costs.py").read_bytes()).hexdigest()


def test_default_arena_book_matches_the_engine_and_explicit_zeros():
    from backtester.runner import execute

    frame = _frame()
    result, metrics = execute(Flat(), frame, "arena")
    bare = engine_arena.run(Flat(), frame)
    assert result["daily"] == bare["daily"]
    assert result["fills"] == bare["fills"]
    assert result["fees"] == bare["fees"]
    assert result["borrow"] == bare["borrow"]
    assert "financing" not in result
    explicit, explicit_metrics = execute(
        Flat(),
        frame,
        "arena",
        price_basis="raw",
        cash_rate_annual=0.0,
        financing_rate_annual=0.0,
        withholding_rate=0.0,
    )
    assert explicit["daily"] == bare["daily"]
    assert explicit_metrics["final_equity"] == metrics["final_equity"]
    assert metrics["price_basis"] == "raw"
    assert metrics["series_kind"] == "raw"
    assert metrics["total_return_method"] == "none"
    assert metrics["cash_rate_annual"] == 0.0
    assert metrics["financing_rate_annual"] == 0.0
    assert metrics["withholding_rate"] == 0.0
    assert metrics["accrual"] == "session_boundary_1_252"
    assert metrics["sessions_per_year"] == 252
    assert metrics["dividends_applied"] is False
    assert metrics["accounting"]["price_basis"] == "raw"
    assert metrics["cash_interest"] == 0.0
    assert metrics["share_factor"] == 1.0
    assert metrics["cost_basis"]["per_share_fees"] == "real_share_counts"


def test_default_desk_book_matches_explicit_profile_defaults():
    from backtester.runner import execute

    frame = _frame()
    result, metrics = execute(Flat(), frame, "desk")
    again, again_metrics = execute(
        Flat(),
        frame,
        "desk",
        financing_rate_annual=0.07,
        cash_rate_annual=0.0,
        withholding_rate=0.0,
        price_basis="raw",
    )
    assert result["daily"] == again["daily"]
    assert result["cash"] == again["cash"]
    assert result["financing"] == again["financing"]
    assert result["dividends"] == again["dividends"]
    assert result["fees"] == again["fees"]
    assert result["borrow"] == again["borrow"]
    assert metrics["final_equity"] == again_metrics["final_equity"]
    assert metrics["financing_rate_annual"] == 0.07
    assert metrics["cash_rate_annual"] == 0.0
    assert metrics["total_return_method"] == "none"
    assert metrics["price_basis"] == "raw"
    assert metrics["dividends_applied"] is False
    bars = _desk_bars()
    plain = engine_desk.run(Always(2.0), bars)
    explicit = engine_desk.run(
        Always(2.0),
        bars,
        DeskConfig(financing_rate_annual=0.07, cash_rate_annual=0.0, withholding_rate=0.0, dividends={}),
    )
    assert plain["daily"] == explicit["daily"]
    assert plain["cash"] == explicit["cash"]
    assert plain["financing"] == explicit["financing"]
    assert plain["financing"] > 0.0


def test_arena_default_matches_the_reference_loop():
    from test_engine import make_bars
    from test_parity import load_reference

    ref = load_reference()
    bars = make_bars(days=4, price=lambda _i: 100.0)
    left = ref.run(ref.BuyAndHold(), bars)
    right = engine_arena.run(engine_arena.BuyAndHold(), bars)
    assert left["daily"] == right["daily"]
    assert left["fills"] == right["fills"]
    assert left["fees"] == right["fees"]
    assert left["borrow"] == right["borrow"]
    assert left["episodes"] == right["episodes"]
    zeros = engine_arena.run(
        engine_arena.BuyAndHold(),
        bars,
        cash_rate_annual=0.0,
        financing_rate_annual=0.0,
        withholding_rate=0.0,
        dividends=None,
    )
    assert zeros["daily"] == right["daily"]
    assert "financing" not in zeros


def test_rates_reject_negative_absurd_and_non_numeric_input():
    assert require_annual_rate("cash_rate_annual", 0.0) == 0.0
    assert require_annual_rate("cash_rate_annual", 1.0) == 1.0
    assert require_withholding_rate("withholding_rate", 1.0) == 1.0
    with pytest.raises(ValueError, match="cannot be negative"):
        require_annual_rate("cash_rate_annual", -0.01)
    with pytest.raises(ValueError, match="above 1.0"):
        require_annual_rate("financing_rate_annual", 1.0000001)
    with pytest.raises(ValueError, match="not bool"):
        require_annual_rate("cash_rate_annual", True)
    with pytest.raises(ValueError, match="not text"):
        require_annual_rate("financing_rate_annual", "7%")
    with pytest.raises(ValueError, match="above 1.0"):
        require_withholding_rate("withholding_rate", 1.01)
    with pytest.raises(ValueError, match="finite"):
        require_annual_rate("cash_rate_annual", math.nan)
    with pytest.raises(ValueError, match="does not credit dividends"):
        resolve_accounting("arena", dividends={"2026-03-02": 1.0})
    with pytest.raises(ValueError, match="needs a dividends file"):
        resolve_accounting("arena", price_basis="total_return")
    with pytest.raises(ValueError, match="not runnable"):
        resolve_accounting("desk", series_kind="total_return")


def test_bases_are_mutually_exclusive_and_the_accrual_is_one_252nd():
    assert borrowed_base(10, 100.0, 500.0) == 500.0
    assert idle_base(10, 100.0, 500.0) == 0.0
    assert borrowed_base(10, 100.0, 2000.0) == 0.0
    assert idle_base(10, 100.0, 2000.0) == 1000.0
    assert borrowed_base(-10, 100.0, 500.0) == 500.0
    assert idle_base(0, 100.0, 1_000_000.0) == 1_000_000.0
    assert session_accrual(0.10, 1_000_000.0) == 0.10 / 252.0 * 1_000_000.0
    assert dividend_cash(10, 1.5, 0.0) == 15.0
    assert dividend_cash(10, 1.5, 0.15) == 10 * 1.5 * 0.85
    assert dividend_cash(-10, 1.5, 0.15) == -15.0
    assert dividend_cash(10, -1.5, 0.15) == -15.0


def test_flat_cash_interest_is_one_252nd_per_boundary():
    rate = 0.10
    equity = 1_000_000.0
    first = session_accrual(rate, equity)
    second = session_accrual(rate, equity + first)
    arena = engine_arena.run(Flat(), _frame(days=3), cash_rate_annual=rate)
    assert arena["daily"][0][1] == equity
    assert arena["daily"][1][1] == equity + first
    assert arena["daily"][2][1] == equity + first + second
    assert arena["cash_interest"] == first + second
    assert arena["financing"] == 0.0
    desk = engine_desk.run(Flat(), _desk_bars(), DeskConfig(cash_rate_annual=rate))
    assert desk["cash_interest"] == first
    assert desk["cash"] == equity + first
    assert desk["financing"] == 0.0
    untouched = engine_desk.run(Flat(), _desk_bars(), DeskConfig())
    assert untouched["cash"] == equity
    assert untouched["cash_interest"] == 0.0


def test_arena_financing_matches_the_precharge_snap():
    from test_engine import make_bars

    bars = make_bars(days=2, price=lambda _i: 100.0)
    base = engine_arena.run(Always(2.0), bars)
    rate = 0.08
    charged = engine_arena.run(Always(2.0), bars, financing_rate_annual=rate)
    _day, equity, _exposure, shares, close = base["daily"][0]
    borrowed = borrowed_base(shares, close, equity)
    assert borrowed > 0.0
    due = session_accrual(rate, borrowed, arena_costs.SESSIONS_PER_YEAR)
    assert charged["daily"][0] == base["daily"][0]
    assert charged["financing"] == due
    _session, _minute, _phase, fill_shares, _decision, fill_px, fee, _wanted = base["fills"][0]
    hand_cash = 1_000_000.0 - fill_shares * fill_px - fee
    assert hand_cash + shares * close == equity
    assert charged["daily"][1][1] == hand_cash - due + shares * close
    assert charged["cash_interest"] == 0.0


def test_withholding_hits_long_dividends_and_not_shorts():
    bars = _desk_bars()
    amount = 2.0
    dividends = {"S2": amount}
    long_gross = engine_desk.run(Always(1.0), bars, DeskConfig(dividends=dividends))
    long_net = engine_desk.run(
        Always(1.0), bars, DeskConfig(dividends=dividends, withholding_rate=0.15)
    )
    shares = long_gross["daily"][0]["shares"]
    assert shares > 0
    assert long_gross["dividends"] == shares * amount
    assert long_net["dividends"] == shares * amount * (1.0 - 0.15)
    assert long_gross["cash"] - long_net["cash"] == shares * amount * 0.15
    short_gross = engine_desk.run(Always(-1.0), bars, DeskConfig(dividends=dividends))
    short_net = engine_desk.run(
        Always(-1.0), bars, DeskConfig(dividends=dividends, withholding_rate=0.15)
    )
    short_shares = short_gross["daily"][0]["shares"]
    assert short_shares < 0
    assert short_gross["dividends"] == short_shares * amount
    assert short_net["dividends"] == short_gross["dividends"]


def test_leaderboard_refuses_mixed_accounting_and_accepts_a_legacy_board(tmp_path):
    from backtester.report import read_leaderboard

    left = resolve_accounting("arena").leaderboard_fields()
    right = resolve_accounting("arena", cash_rate_annual=0.02).leaderboard_fields()
    rows = [
        {"strategy_id": "a", "price_basis": left["price_basis"], **left},
        {"strategy_id": "b", "price_basis": right["price_basis"], **right},
    ]
    with pytest.raises(ValueError, match="refuses rows"):
        write_leaderboard(tmp_path / "mixed.csv", rows)
    same = [dict(rows[0]), dict(rows[0])]
    write_leaderboard(tmp_path / "same.csv", same)
    assert read_leaderboard(tmp_path / "same.csv")[0]["price_basis"] == "raw"
    assert read_leaderboard(tmp_path / "same.csv")[0]["accrual"] == "session_boundary_1_252"
    legacy = [{"strategy_id": "old", "total_return": 0.1, "status": "ok"}]
    write_leaderboard(tmp_path / "legacy.csv", legacy)
    refuse_mixed_rows(legacy)


def test_accounting_vocabulary_is_not_treated_as_a_ticker(tmp_path):
    root = tmp_path / "designer"
    root.mkdir()
    write_json(
        root / "metrics.json",
        {
            "audience": "designer",
            "profile": "arena",
            "price_basis": "raw",
            "total_return_method": "none",
            "series_kind": "total_return",
            "accrual": "session_boundary_1_252",
            "dividends_applied": False,
            "cash_rate_annual": 0.0,
            "financing_rate_annual": 0.07,
            "withholding_rate": 0.0,
        },
    )
    assert_designer_clean(root, symbol="RAW")
    assert_designer_clean(root, symbol="NONE")
    assert_designer_clean(root, symbol="RETURN")


def _write_filled(path: Path, rows: list[dict]) -> None:
    import gzip

    path.parent.mkdir(parents=True, exist_ok=True)
    header = [
        "timestamp_et",
        "epoch_utc",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "wap",
        "count",
        "filled",
        "session",
    ]
    lines = [",".join(header)]
    for row in rows:
        lines.append(",".join(str(row[name]) for name in header))
    with gzip.open(path, "wt", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines) + "\n")


def _filled_row(day: datetime, minute: int, price: float) -> dict:
    stamp = day.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(minutes=minute)
    stamp = stamp.replace(tzinfo=ET)
    return {
        "timestamp_et": stamp.isoformat(),
        "epoch_utc": int(stamp.timestamp()),
        "open": price,
        "high": price + 0.4,
        "low": price - 0.4,
        "close": price + 0.1,
        "volume": 10,
        "wap": price,
        "count": 3,
        "filled": 0,
        "session": "rth",
    }


def test_registry_total_return_runs_only_as_an_explicit_basis(tmp_path):
    from backtester.data import load_named
    from backtester.registry import entry_from_inspection, save_registry, upsert_series
    from backtester.runner import run_named

    day = datetime(2026, 5, 4)
    nxt = datetime(2026, 5, 5)
    rows = [_filled_row(day, minute, 50.0) for minute in (9 * 60 + 30, 9 * 60 + 31, 15 * 60 + 59)]
    rows += [_filled_row(nxt, minute, 50.0) for minute in (9 * 60 + 30, 15 * 60 + 59)]
    src = tmp_path / "src"
    path = src / "NIK_1min_filled_2026-05.csv.gz"
    _write_filled(path, rows)
    loaded = load_named(src, "NIK", price_kind="total_return")
    entry = entry_from_inspection(loaded.inspection, source_root=str(src), profile="arena")
    assert entry["runnable"] is False
    registry = tmp_path / "registry.json"
    save_registry(registry, upsert_series({"schema_version": 1, "series": []}, entry))
    with pytest.raises(ValueError, match="not runnable"):
        run_named(Flat, src, "NIK", "arena", tmp_path / "raw", registry_path=registry, with_benchmarks=False)
    payload = run_named(
        Flat,
        src,
        "NIK",
        "arena",
        tmp_path / "tr",
        price_basis="total_return",
        registry_path=registry,
        with_benchmarks=False,
    )
    assert payload["price_basis"] == "total_return"
    assert payload["series_kind"] == "total_return"
    assert payload["total_return_method"] == "registry_series"
    assert payload["dividends_applied"] is False
    assert payload["share_factor"] == 1.0
    with pytest.raises(ValueError, match="dividends file"):
        run_named(
            Flat,
            src,
            "NIK",
            "arena",
            tmp_path / "both",
            price_basis="total_return",
            registry_path=registry,
            dividends={"2026-05-05": 1.0},
            with_benchmarks=False,
        )


def test_cash_dividends_are_the_total_return_method_on_a_raw_series(tmp_path):
    from backtester.runner import execute

    frame = _frame()
    with pytest.raises(ValueError, match="does not credit dividends"):
        execute(Flat(), frame, "arena", dividends={"2026-03-03": 1.25})
    _result, metrics = execute(
        Always(1.0),
        frame,
        "arena",
        price_basis="total_return",
        dividends={"2026-03-03": 1.25},
        withholding_rate=0.0,
    )
    assert metrics["total_return_method"] == "cash_dividends"
    assert metrics["series_kind"] == "raw"
    assert metrics["dividends_applied"] is True
    assert metrics["dividends_net"] != 0.0
    desk_result, desk_metrics = execute(
        Always(1.0),
        frame,
        "desk",
        dividends={"2026-03-03": 1.25},
    )
    assert desk_metrics["total_return_method"] == "profile_dividends"
    assert desk_result["dividends"] != 0.0


def test_parallel_refuses_a_mixed_series_batch(tmp_path, monkeypatch):
    from backtester.parallel import FrameMarket, StrategyRef, run_parallel

    def boom(*_args, **_kwargs):
        raise AssertionError("spawned")

    monkeypatch.setattr("backtester.runner._isolated_books", boom)
    path = tmp_path / "strategy.py"
    path.write_text("class Strategy:\n    def on_bar(self, bar, state):\n        return None\n", encoding="utf-8")
    frame = _frame()
    markets = [
        FrameMarket("raw-book", "AAA", frame, series_kind="raw"),
        FrameMarket("tr-book", "BBB", frame, series_kind="total_return"),
    ]
    with pytest.raises(ValueError, match="mixes"):
        run_parallel(
            [StrategyRef("flat", str(path))],
            markets,
            tmp_path / "out",
            profile="desk",
            workers=1,
            price_basis="raw",
        )
