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
    assert metrics["financing_cost"] == 0.0
    assert metrics["dividends_net"] == 0.0
    from backtester.runner import _summary

    headline = _summary("arena", metrics)
    assert headline["cash_interest"] == 0.0
    assert headline["financing_cost"] == 0.0
    assert headline["dividends_net"] == 0.0
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
    from backtester.runner import _summary

    headline = _summary("desk", metrics)
    assert headline["cash_interest"] == 0.0
    assert headline["financing_cost"] == metrics["financing_cost"]
    assert headline["dividends_net"] == metrics["dividends_net"]
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
    equity = 1_000_000.0
    # Literal session charges. This does not call session_accrual.
    first = 0.10 / 252.0 * equity
    second = 0.10 / 252.0 * (equity + first)
    arena = engine_arena.run(Flat(), _frame(days=3), cash_rate_annual=0.10)
    assert arena["daily"][0][1] == equity
    assert arena["daily"][1][1] == equity + first
    assert arena["daily"][2][1] == equity + first + second
    assert arena["cash_interest"] == first + second
    assert arena["financing"] == 0.0
    desk = engine_desk.run(Flat(), _desk_bars(), DeskConfig(cash_rate_annual=0.10))
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


def test_accounting_vocabulary_is_reserved_as_a_whole_symbol(tmp_path):
    from backtester.accounting import EXACT_RESERVED
    from backtester.report import require_anonymizable_symbol, reserved_symbol_conflict

    blocked = (
        "RAW",
        "NONE",
        "TOTAL_RETURN",
        "CASH_DIVIDENDS",
        "PROFILE_DIVIDENDS",
        "REGISTRY_SERIES",
        "SESSION_BOUNDARY_1_252",
        "PRICE_BASIS",
        "CASH_INTEREST",
        "ACCOUNTING",
    )
    for symbol in blocked:
        assert symbol.casefold() in EXACT_RESERVED
        assert reserved_symbol_conflict(symbol) == symbol.casefold()
        with pytest.raises(ValueError, match="reserved report name"):
            require_anonymizable_symbol(symbol)
    assert reserved_symbol_conflict("1") is None
    assert reserved_symbol_conflict("SESSION") is None
    assert reserved_symbol_conflict("RETURN") is None
    assert reserved_symbol_conflict("252") is None
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
    assert_designer_clean(root, symbol="ZZZX")


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


def test_arena_warmup_charges_only_live_sessions(tmp_path):
    from backtester.runner import walk_forward

    frame = _frame(days=6)
    _summary, slices, _design, locked = walk_forward(
        Flat,
        frame,
        "arena",
        tmp_path / "out",
        symbol="TEST",
        bar_minutes=1,
        design_end_day=3,
        warmup_days=2,
        cash_rate_annual=0.10,
        with_benchmarks=False,
    )
    assert slices.warmup_sessions >= 2
    result, metrics = locked["strategy"]
    assert len(result["daily"]) == 2
    one = 0.10 / 252.0 * 1_000_000.0
    assert result["daily"][0][1] == 1_000_000.0
    assert result["cash_interest"] == one
    assert result["daily"][1][1] == 1_000_000.0 + one
    # Flat book: the only cash change is the live charge, added onto the reset equity.
    assert metrics["final_equity"] == metrics["initial_equity"] + metrics["cash_interest"]
    assert metrics["cash_interest"] == one


class _HoldThroughPost:
    WANTS_EXTENDED_HOURS = True

    def on_bar(self, bar, _state):
        if bar["day"] == 0 and bar["minute"] == 570 and bar["session"] == "rth":
            return 2.0
        return None


class _FlattenInPost(_HoldThroughPost):
    def on_bar(self, bar, state):
        if bar["day"] == 0 and bar["minute"] == 1000 and bar["session"] == "post":
            return 0.0
        return super().on_bar(bar, state)


def test_arena_post_flatten_is_in_the_overnight_snap():
    from test_engine import make_bars

    bars = make_bars(days=2, rth_minutes=(570, 571), post=(1000, 1001), price=lambda _i: 100.0)
    held = engine_arena.run(_HoldThroughPost(), bars, financing_rate_annual=0.08, dividends={1: 2.0})
    flat = engine_arena.run(_FlattenInPost(), bars, financing_rate_annual=0.08, dividends={1: 2.0})
    assert held["financing"] != 0.0
    assert held["dividends"] != 0.0
    assert any(fill[2] == "post" and fill[7] == 0 for fill in flat["fills"])
    assert flat["financing"] == 0.0
    assert flat["dividends"] == 0.0
    # Borrow stays on the last regular bar. A long pays none.
    assert flat["borrow"] == 0.0
    assert held["borrow"] == 0.0


class _DeskHold:
    def on_bar(self, bar, _account):
        if bar["session"] == "S1" and bar["time"] == "09:30" and bar["phase"] == "rth":
            return 1.0
        return None


class _DeskFlattenRth(_DeskHold):
    def on_bar(self, bar, account):
        if bar["session"] == "S1" and bar["time"] == "09:31" and bar["phase"] == "rth":
            return 0.0
        return super().on_bar(bar, account)


class _DeskFlattenPost(_DeskHold):
    def on_bar(self, bar, account):
        if bar["session"] == "S1" and bar["phase"] == "post":
            return 0.0
        return super().on_bar(bar, account)


def test_desk_snap_includes_an_rth_flatten_and_not_a_post_target():
    from test_desk import bar

    bars = [
        bar("S1", "09:30", "rth", 100),
        bar("S1", "09:31", "rth", 100),
        bar("S1", "09:32", "rth", 100),
        bar("S1", "15:59", "rth", 100),
        bar("S1", "16:00", "post", 100),
        bar("S1", "16:01", "post", 100),
        bar("S2", "09:30", "rth", 100),
        bar("S2", "15:59", "rth", 100),
    ]
    config = DeskConfig(financing_rate_annual=0.08, dividends={"S2": 2.0})
    held = engine_desk.run(_DeskHold(), bars, config)
    flattened = engine_desk.run(_DeskFlattenRth(), bars, config)
    posted = engine_desk.run(_DeskFlattenPost(), bars, config)
    assert held["financing"] > 0.0
    assert held["dividends"] != 0.0
    assert flattened["financing"] == 0.0
    assert flattened["dividends"] == 0.0
    assert posted["financing"] > 0.0
    assert posted["dividends"] != 0.0
    closed = [fill for fill in posted["fills"] if fill["shares_after"] == 0]
    assert closed
    assert closed[0]["session"] == "S2"
    assert closed[0]["phase"] == "rth"


class _DividendSpy:
    """Records dividend maps visible on the call stack. Holds one times equity."""

    def __init__(self):
        self.seen = []

    def on_bar(self, _bar, _state):
        import inspect

        frame = inspect.currentframe()
        while frame is not None:
            local = frame.f_locals
            for name, value in local.items():
                if "dividend" not in name or not isinstance(value, dict):
                    continue
                self.seen.append({str(key): value[key] for key in value})
            frame = frame.f_back
        return 1.0


def _dated_frame(dates):
    rows = []
    for day, date in enumerate(dates):
        for minute in (570, 571, 959):
            rows.append(
                {
                    "date": date,
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


def _shares_at_first_mark(profile, result):
    if profile == "arena":
        return result["daily"][0][3]
    return result["daily"][0]["shares"]


@pytest.mark.parametrize("profile", ["arena", "desk"])
def test_walk_forward_hides_a_locked_ex_date(tmp_path, profile):
    from backtester.runner import execute, walk_forward

    dates = ["2024-06-03", "2024-06-04", "2024-06-05", "2024-06-06"]
    frame = _dated_frame(dates)
    design_amount = 1.25
    locked_amount = 19.19
    locked_date = "2024-06-06"
    dividends = {"2024-06-04": design_amount, locked_date: locked_amount}
    basis = "total_return" if profile == "arena" else "raw"
    summary, slices, design_books, locked_books = walk_forward(
        _DividendSpy,
        frame,
        profile,
        tmp_path / profile,
        symbol="NIK",
        bar_minutes=1,
        design_end_day=1,
        warmup_days=1,
        dividends=dividends,
        price_basis=basis,
        with_benchmarks=False,
    )
    assert summary["design_sessions"] == 2
    assert slices.warmup_sessions == 1
    design_result, design_metrics = design_books["strategy"]
    locked_result, _locked_metrics = locked_books["strategy"]
    design_shares = _shares_at_first_mark(profile, design_result)
    locked_shares = _shares_at_first_mark(profile, locked_result)
    assert design_shares > 0
    assert locked_shares > 0
    assert design_result["dividends"] == design_shares * design_amount
    assert locked_result["dividends"] == locked_shares * locked_amount
    assert design_metrics["dividends_net"] == design_shares * design_amount
    seen = design_result["observer"].seen
    assert seen
    for mapping in seen:
        assert locked_date not in mapping
        assert locked_amount not in mapping.values()
        assert all(locked_date not in str(key) for key in mapping)
    locked_seen = locked_result["observer"].seen
    assert any(locked_amount in mapping.values() for mapping in locked_seen)
    assert any(locked_date in mapping for mapping in locked_seen)
    with pytest.raises(ValueError, match="dividend date 1999-01-01 is not in this sample"):
        execute(
            Flat(),
            frame,
            "arena",
            price_basis="total_return",
            dividends={locked_date: locked_amount, "1999-01-01": 1.0},
        )


def test_walkforward_cli_keeps_a_locked_ex_date_and_run_rejects_a_typo(tmp_path, capsys):
    from backtester.__main__ import main

    days = [datetime(2024, 6, 3), datetime(2024, 6, 4), datetime(2024, 6, 5)]
    rows = []
    for day in days:
        for minute in (9 * 60 + 30, 9 * 60 + 31, 15 * 60 + 59):
            rows.append(_filled_row(day, minute, 50.0))
    src = tmp_path / "src"
    _write_filled(src / "NIK_1min_filled_2024-06.csv.gz", rows)
    dividends = tmp_path / "dividends.csv"
    dividends.write_text(
        "session,amount\n2024-06-03,1.0\n2024-06-04,1.25\n2024-06-05,19.19\n",
        encoding="utf-8",
    )
    strategy = tmp_path / "strategy.py"
    strategy.write_text(
        "class Strategy:\n    def on_bar(self, bar, state):\n        return 1.0\n",
        encoding="utf-8",
    )
    arena = main(
        [
            "walkforward",
            "--profile",
            "arena",
            "--ticker",
            "NIK",
            "--strategy",
            str(strategy),
            "--src",
            str(src),
            "--out",
            str(tmp_path / "arena"),
            "--price-basis",
            "total_return",
            "--dividends",
            str(dividends),
            "--design-end-day",
            "1",
            "--warmup-days",
            "1",
        ]
    )
    assert arena == 0
    desk = main(
        [
            "walkforward",
            "--profile",
            "desk",
            "--ticker",
            "NIK",
            "--strategy",
            str(strategy),
            "--src",
            str(src),
            "--out",
            str(tmp_path / "desk"),
            "--dividends",
            str(dividends),
            "--design-end-day",
            "1",
            "--warmup-days",
            "1",
        ]
    )
    assert desk == 0
    typo = tmp_path / "typo.csv"
    typo.write_text("session,amount\n1999-01-01,1.0\n2024-06-04,1.25\n", encoding="utf-8")
    rejected = main(
        [
            "run",
            "--profile",
            "arena",
            "--ticker",
            "NIK",
            "--strategy",
            str(strategy),
            "--src",
            str(src),
            "--out",
            str(tmp_path / "run"),
            "--price-basis",
            "total_return",
            "--dividends",
            str(typo),
        ]
    )
    assert rejected == 2
    assert "dividend date 1999-01-01 is not in this sample" in capsys.readouterr().err
