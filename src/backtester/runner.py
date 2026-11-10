"""Run one strategy, or a design window and a locked tail.

Benchmarks go through the same engine and the same cost configuration as the
strategy. Arena financing stays at zero. Desk financing stays at the desk rate
(7% unless the run overrides it).
"""

from __future__ import annotations

from pathlib import Path

from . import engine_arena, engine_desk
from .data import frame_to_desk_bars, load_named, slice_walkforward
from .engine_desk import BuyAndHold as DeskBuyAndHold
from .engine_desk import DeskConfig, TwoXLong
from .profiles import get_profile
from .report import enrich_arena_metrics, write_book, write_json

_ARENA_KEYS = (
    "total_return",
    "cagr",
    "ann_vol",
    "sharpe",
    "sortino",
    "max_drawdown",
    "max_dd_duration_sessions",
    "calmar",
    "final_equity",
    "n_fills",
    "fees_total",
    "borrow_cost",
    "percent_up_days",
)

_DESK_KEYS = _ARENA_KEYS + (
    "financing_cost",
    "dividends_net",
    "margin_calls_queued",
    "margin_calls_filled",
    "max_gross_leverage",
    "avg_gross_leverage",
    "worst_equity_over_hwm",
    "disqualified",
    "borrow_rate_annual",
    "financing_rate_annual",
    "share_factor",
    "fee_mult",
)


def _summary(profile: str, metrics: dict) -> dict:
    keys = _DESK_KEYS if profile == "desk" else _ARENA_KEYS
    return {key: metrics.get(key) for key in keys}


def execute(
    strategy,
    frame,
    profile: str,
    *,
    share_factor: float = 1.0,
    borrow_rate_annual: float | None = None,
    financing_rate_annual: float | None = None,
    fee_mult: float = 1.0,
    dividends: dict | None = None,
    warmup_sessions: int = 0,
    initial_equity: float = 1_000_000.0,
    reset_day: int | None = None,
    max_day: int | None = None,
):
    """Run one strategy instance on ``frame``.

    ``max_day``, when set, refuses a frame whose last session is later than
    that day. Walk-forward uses it so a design run cannot be handed the tail.
    """
    name = get_profile(profile).name
    if max_day is not None and len(frame) and int(frame["day"].max()) > int(max_day):
        raise RuntimeError(
            f"refusing to run: frame contains day {int(frame['day'].max())} after declared end {int(max_day)}"
        )
    if name == "arena":
        if fee_mult != 1.0:
            raise ValueError(
                "fee-mult is a desk cost-stress switch. An arena run uses the unmodified Arena schedule."
            )
        if dividends:
            raise ValueError("The arena profile does not credit dividends.")
        if financing_rate_annual not in (None, 0.0):
            raise ValueError(
                "Arena financing is 0 in this phase so the reference loop stays unchanged. "
                "A financing switch is later work."
            )
        effective_reset = reset_day
        if warmup_sessions and reset_day is None:
            days = list(dict.fromkeys(int(day) for day in frame["day"].tolist()))
            if warmup_sessions >= len(days):
                raise ValueError("arena warmup covers the whole sample")
            effective_reset = days[warmup_sessions]
        kwargs = {
            "initial_equity": initial_equity,
            "share_scale": share_factor,
            "reset_day": effective_reset,
        }
        if borrow_rate_annual is not None:
            kwargs["borrow_rate_annual"] = borrow_rate_annual
        result = engine_arena.run(strategy, frame, **kwargs)
        metrics = enrich_arena_metrics(result, engine_arena.metrics(result))
        metrics["share_factor"] = share_factor
        metrics["borrow_rate_annual"] = (
            engine_arena.C.BORROW_RATE_ANNUAL if borrow_rate_annual is None else borrow_rate_annual
        )
        metrics["financing_rate_annual"] = 0.0
        return result, metrics
    if name == "desk":
        rate = engine_desk.D.FINANCING_RATE_ANNUAL if financing_rate_annual is None else financing_rate_annual
        borrow = engine_desk.D.BORROW_RATE_ANNUAL if borrow_rate_annual is None else borrow_rate_annual
        config = DeskConfig(
            initial_equity=initial_equity,
            share_factor=share_factor,
            borrow_rate_annual=borrow,
            financing_rate_annual=rate,
            fee_mult=fee_mult,
            live_start_ordinal=warmup_sessions,
            dividends=dict(dividends or {}),
        )
        rows = frame_to_desk_bars(frame)
        result = engine_desk.run(strategy, rows, config)
        metrics = engine_desk.metrics(result)
        return result, metrics
    raise ValueError(f"unknown profile {profile}")


def _benchmarks(profile: str):
    if profile == "arena":
        return (("buy_and_hold_1x", engine_arena.BuyAndHold),)
    if profile == "desk":
        return (("buy_and_hold_1x", DeskBuyAndHold), ("long_2x", TwoXLong))
    raise ValueError(profile)


def run_books(
    strategy_cls,
    frame,
    profile: str,
    *,
    share_factor: float = 1.0,
    borrow_rate_annual: float | None = None,
    financing_rate_annual: float | None = None,
    fee_mult: float = 1.0,
    dividends: dict | None = None,
    warmup_sessions: int = 0,
    initial_equity: float = 1_000_000.0,
    reset_day: int | None = None,
    max_day: int | None = None,
    with_benchmarks: bool = True,
):
    """Fresh instance per book. Benchmarks reuse the strategy's cost configuration."""
    common = dict(
        share_factor=share_factor,
        borrow_rate_annual=borrow_rate_annual,
        financing_rate_annual=financing_rate_annual,
        fee_mult=fee_mult,
        dividends=dividends,
        warmup_sessions=warmup_sessions,
        initial_equity=initial_equity,
        reset_day=reset_day,
        max_day=max_day,
    )
    result, metrics = execute(strategy_cls(), frame, profile, **common)
    books = {"strategy": (result, metrics)}
    if with_benchmarks:
        for label, cls in _benchmarks(profile):
            books[label] = execute(cls(), frame, profile, **common)
    return books


def write_books(out_dir, profile: str, symbol: str, bar_minutes: int, frame, books, *, extra: dict | None = None) -> dict:
    """Write the strategy curves at the top of ``out_dir`` and benchmarks underneath."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    strategy_result, strategy_metrics = books["strategy"]
    write_book(out, profile, strategy_result, strategy_metrics, frame)
    benchmark_payload = {}
    for label, (result, metrics) in books.items():
        if label == "strategy":
            continue
        write_book(out / "benchmarks" / label, profile, result, metrics, frame)
        benchmark_payload[label] = _summary(profile, metrics)
    payload = dict(strategy_metrics)
    payload["profile"] = profile
    payload["symbol"] = symbol
    payload["bar_minutes"] = bar_minutes
    payload["benchmarks"] = benchmark_payload
    if extra:
        payload["run"] = extra
    write_json(out / "metrics.json", payload)
    return payload


def run_named(
    strategy_cls,
    src,
    symbol: str,
    profile: str,
    out_dir,
    *,
    bar_minutes: int = 1,
    share_factor: float | None = None,
    borrow_rate_annual: float | None = None,
    financing_rate_annual: float | None = None,
    fee_mult: float = 1.0,
    dividends: dict | None = None,
    warmup_sessions: int = 0,
    initial_equity: float = 1_000_000.0,
    splits=(),
    with_benchmarks: bool = True,
):
    loaded = load_named(src, symbol, bar_minutes=bar_minutes, splits=splits)
    factor = loaded.inspection.share_factor if share_factor is None else share_factor
    warnings = []
    if factor != 1.0:
        warnings.append(
            f"share_factor is {factor} on raw {symbol} files. Raw prints use 1.0; "
            "0.9 is only for an anonymized desk kit whose prices were rescaled."
        )
    books = run_books(
        strategy_cls,
        loaded.frame,
        profile,
        share_factor=factor,
        borrow_rate_annual=borrow_rate_annual,
        financing_rate_annual=financing_rate_annual,
        fee_mult=fee_mult,
        dividends=dividends,
        warmup_sessions=warmup_sessions,
        initial_equity=initial_equity,
        with_benchmarks=with_benchmarks,
    )
    payload = write_books(
        out_dir,
        profile,
        symbol,
        bar_minutes,
        loaded.frame,
        books,
        extra={"warnings": warnings, "inspection": loaded.inspection.as_dict()},
    )
    return payload


def walk_forward(
    strategy_cls,
    frame,
    profile: str,
    out_dir,
    *,
    symbol: str,
    bar_minutes: int,
    design_end_day: int | None = None,
    design_end_date: str | None = None,
    design_fraction: float | None = None,
    warmup_days: int = 0,
    share_factor: float = 1.0,
    borrow_rate_annual: float | None = None,
    financing_rate_annual: float | None = None,
    fee_mult: float = 1.0,
    dividends: dict | None = None,
    initial_equity: float = 1_000_000.0,
    with_benchmarks: bool = True,
):
    """Design run sees only design bars. The locked run may replay earlier bars as warm-up.

    Arena warm-up trades, then rescales to the starting equity and keeps exposure.
    Desk warm-up is flat (``live`` false) and goes live flat at the starting equity.
    Those are different engines. This function does not merge them.
    """
    slices = slice_walkforward(
        frame,
        design_end_day=design_end_day,
        design_end_date=design_end_date,
        design_fraction=design_fraction,
        warmup_days=warmup_days,
    )
    common = dict(
        share_factor=share_factor,
        borrow_rate_annual=borrow_rate_annual,
        financing_rate_annual=financing_rate_annual,
        fee_mult=fee_mult,
        dividends=dividends,
        initial_equity=initial_equity,
        with_benchmarks=with_benchmarks,
    )
    design_books = run_books(
        strategy_cls,
        slices.design,
        profile,
        max_day=slices.design_end_day,
        warmup_sessions=0,
        **common,
    )
    if profile == "arena":
        locked_books = run_books(
            strategy_cls,
            slices.locked_with_warmup,
            profile,
            reset_day=slices.locked_start_day if slices.warmup_sessions else None,
            max_day=int(slices.locked["day"].max()),
            warmup_sessions=0,
            **common,
        )
    elif profile == "desk":
        locked_books = run_books(
            strategy_cls,
            slices.locked_with_warmup,
            profile,
            warmup_sessions=slices.warmup_sessions,
            max_day=int(slices.locked["day"].max()),
            **common,
        )
    else:
        raise ValueError(profile)
    out = Path(out_dir)
    design_payload = write_books(out / "design", profile, symbol, bar_minutes, slices.design, design_books)
    locked_frame = slices.locked_with_warmup
    locked_payload = write_books(out / "locked", profile, symbol, bar_minutes, locked_frame, locked_books)
    summary = {
        "profile": profile,
        "symbol": symbol,
        "bar_minutes": bar_minutes,
        "design_end_day": slices.design_end_day,
        "design_end_date": slices.design_end_date,
        "locked_start_day": slices.locked_start_day,
        "locked_start_date": slices.locked_start_date,
        "warmup_days_requested": slices.warmup_days_requested,
        "warmup_sessions": slices.warmup_sessions,
        "design_days_max": int(slices.design["day"].max()),
        "locked_days_min": int(slices.locked["day"].min()),
        "design_sessions": int(slices.design["day"].nunique()),
        "locked_sessions": int(slices.locked["day"].nunique()),
        "warmup_rule": get_profile(profile).warmup,
        "design": _summary(profile, design_payload),
        "locked": _summary(profile, locked_payload),
    }
    write_json(out / "walkforward.json", summary)
    return summary, slices, design_books, locked_books


def run_parallel(*_args, **_kwargs):
    raise NotImplementedError(
        "Parallel scoring is a later phase. Each worker will be its own process, "
        "one strategy and one symbol at a time."
    )
