"""Designer-facing books.

Canonical equity, the flat fee in ``costs_designer``, and the profile's
slippage and rate accruals. This module does not read a scale factor and
does not call the per-share fee schedules. The operator invoice is a
different run.
"""

from __future__ import annotations

from . import engine_arena, engine_desk
from .accounting import stamp_accounting
from .costs_designer import DESIGNER_INITIAL_EQUITY, flat_bps, for_profile, label
from .data import frame_to_desk_bars
from .engine_desk import BuyAndHold as DeskBuyAndHold
from .engine_desk import DeskConfig, TwoXLong
from .profiles import get_profile
from .report import enrich_arena_metrics


def session_dividends(dividends, date_map):
    """Calendar amounts keyed by pack session id.

    The amount is the cash figure on the dividend file. It is not restated
    by a price factor. ``None`` stays ``None`` (no file). An empty map means
    the file was present and this window has no ex-date only when the caller
    already filtered it.
    """
    if not dividends:
        return dividends
    if not isinstance(date_map, dict) or not date_map:
        raise ValueError("a pack dividend remap needs the session date map")
    by_date = {}
    for ordinal, date in date_map.items():
        day = str(date)
        if day in by_date:
            raise ValueError(f"date map repeats {day}")
        by_date[day] = str(ordinal)
    out = {}
    for key, amount in dividends.items():
        day = str(key)
        if day not in by_date:
            raise ValueError(f"dividend date {day} is not in this pack")
        session_id = f"S{int(by_date[day]):05d}"
        value = float(amount)
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError(f"dividend for {day} is not finite")
        if session_id in out:
            raise ValueError(f"dividend date {day} maps to {session_id} twice")
        out[session_id] = value
    return out


def spawn_kwargs(
    *,
    borrow_rate_annual=None,
    financing_rate_annual=None,
    fee_mult=1.0,
    dividends=None,
    warmup_sessions=0,
    reset_day=None,
    max_day=None,
    with_benchmarks=True,
    price_basis="raw",
    series_kind="raw",
    cash_rate_annual=None,
    withholding_rate=None,
    accounting=None,
):
    """Keyword arguments for one designer child. Equity is not an argument."""
    return {
        "borrow_rate_annual": borrow_rate_annual,
        "financing_rate_annual": financing_rate_annual,
        "fee_mult": fee_mult,
        "dividends": dividends,
        "warmup_sessions": warmup_sessions,
        "reset_day": reset_day,
        "max_day": max_day,
        "with_benchmarks": with_benchmarks,
        "price_basis": price_basis,
        "series_kind": series_kind,
        "cash_rate_annual": cash_rate_annual,
        "withholding_rate": withholding_rate,
        "accounting": accounting,
    }


def _benchmarks(profile: str):
    name = get_profile(profile).name
    if name == "arena":
        return (("buy_and_hold_1x", engine_arena.BuyAndHold),)
    if name == "desk":
        return (("buy_and_hold_1x", DeskBuyAndHold), ("long_2x", TwoXLong))
    raise ValueError(profile)


def _accounting(profile, kwargs):
    from .runner import _accounting_for

    return _accounting_for(profile, kwargs)


def _metrics(profile: str, result: dict, accounting, borrow_rate_annual):
    name = get_profile(profile).name
    if name == "arena":
        metrics = enrich_arena_metrics(result, engine_arena.metrics(result))
        borrow = get_profile(name).borrow_rate_annual if borrow_rate_annual is None else borrow_rate_annual
        metrics["borrow_rate_annual"] = borrow
    elif name == "desk":
        metrics = engine_desk.metrics(result)
    else:
        raise ValueError(f"unknown profile {profile}")
    metrics.pop("share_factor", None)
    metrics["cost_model"] = label(name)
    metrics["flat_fee_bps"] = flat_bps(name)
    stamp_accounting(metrics, accounting)
    metrics.pop("cost_basis", None)
    return metrics


def execute(strategy, frame, profile: str, **kwargs):
    """One designer book. Starting equity is the canonical index."""
    from .runner import _arena_dividend_days

    name = get_profile(profile).name
    fee_mult = float(kwargs.get("fee_mult", 1.0))
    if name == "arena" and fee_mult != 1.0:
        raise ValueError(
            "fee-mult is a desk cost-stress switch. An arena run uses the unmodified Arena schedule."
        )
    max_day = kwargs.get("max_day")
    if max_day is not None and len(frame) and int(frame["day"].max()) > int(max_day):
        raise RuntimeError(
            f"refusing to run: frame contains day {int(frame['day'].max())} after declared end {int(max_day)}"
        )
    accounting = _accounting(name, kwargs)
    model = for_profile(name, fee_mult=fee_mult)
    borrow = kwargs.get("borrow_rate_annual")
    if name == "arena":
        effective_reset = kwargs.get("reset_day")
        warmup = int(kwargs.get("warmup_sessions") or 0)
        if warmup and effective_reset is None:
            days = list(dict.fromkeys(int(day) for day in frame["day"].tolist()))
            if warmup >= len(days):
                raise ValueError("arena warmup covers the whole sample")
            effective_reset = days[warmup]
        arena_borrow = get_profile(name).borrow_rate_annual if borrow is None else borrow
        dividends = kwargs.get("dividends")
        arena_dividends = _arena_dividend_days(frame, dividends) if accounting.dividends_applied else None
        result = engine_arena.run(
            strategy,
            frame,
            initial_equity=DESIGNER_INITIAL_EQUITY,
            reset_day=effective_reset,
            borrow_rate_annual=arena_borrow,
            cash_rate_annual=accounting.cash_rate_annual,
            financing_rate_annual=accounting.financing_rate_annual,
            withholding_rate=accounting.withholding_rate,
            dividends=arena_dividends,
            cost_model=model,
        )
        return result, _metrics(name, result, accounting, borrow)
    if name == "desk":
        desk_borrow = get_profile(name).borrow_rate_annual if borrow is None else borrow
        rows = frame_to_desk_bars(frame)
        result = engine_desk.run(
            strategy,
            rows,
            DeskConfig(
                initial_equity=DESIGNER_INITIAL_EQUITY,
                borrow_rate_annual=desk_borrow,
                financing_rate_annual=accounting.financing_rate_annual,
                cash_rate_annual=accounting.cash_rate_annual,
                withholding_rate=accounting.withholding_rate,
                fee_mult=fee_mult,
                live_start_ordinal=int(kwargs.get("warmup_sessions") or 0),
                dividends=dict(kwargs.get("dividends") or {}),
                cost_model=model,
            ),
        )
        return result, _metrics(name, result, accounting, borrow)
    raise ValueError(f"unknown profile {profile}")


def run_books(strategy_cls, frame, profile: str, **kwargs):
    """Strategy and benchmarks, each a new instance, on the designer model."""
    with_benchmarks = kwargs.get("with_benchmarks", True)
    strategy = strategy_cls()
    result, metrics = execute(strategy, frame, profile, **kwargs)
    books = {"strategy": (_tag(result, strategy), metrics)}
    if with_benchmarks:
        for label_name, cls in _benchmarks(profile):
            bench = cls()
            bench_result, bench_metrics = execute(bench, frame, profile, **kwargs)
            books[label_name] = (_tag(bench_result, bench), bench_metrics)
    return books


def _tag(result, strategy):
    tagged = dict(result)
    tagged["observer"] = strategy
    return tagged


def accept_books(profile: str, frame, child_books: dict, kwargs: dict) -> dict:
    """Recompute designer statistics in the parent. Benchmarks run again here."""
    from .runner import _metric_delta

    if "strategy" not in child_books:
        raise RuntimeError("strategy process returned no strategy book")
    strategy_result, child_metrics = child_books["strategy"]
    parent_metrics = _metrics(
        profile,
        strategy_result,
        _accounting(profile, kwargs),
        kwargs.get("borrow_rate_annual"),
    )
    delta = _metric_delta(child_metrics, parent_metrics)
    if delta:
        shown = ", ".join(delta[:12])
        raise RuntimeError(f"strategy metrics do not match the parent recomputation ({shown})")
    books = {"strategy": (strategy_result, parent_metrics)}
    if not kwargs.get("with_benchmarks", True):
        return books
    for label_name, cls in _benchmarks(profile):
        bench_result, bench_metrics = execute(cls(), frame, profile, **kwargs)
        child = child_books.get(label_name)
        if child is None:
            raise RuntimeError(f"{label_name} benchmark was not returned by the strategy process")
        delta = _metric_delta(child[1], bench_metrics)
        if delta:
            shown = ", ".join(delta[:12])
            raise RuntimeError(f"{label_name} benchmark does not match the parent recomputation ({shown})")
        books[label_name] = (bench_result, bench_metrics)
    return books
