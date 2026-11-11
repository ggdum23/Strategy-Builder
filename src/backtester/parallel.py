"""Score many strategies, each in its own process.

The worker is the same book process as a pack run: spawn, argv
``['backtester-design']``, an empty working directory, and the environment
allowlist. The sidecar, the date map, and the unscaled dividend amounts stay
in the parent. ``share_scale`` is passed the same way a single pack run passes
it. Desk dividends are remapped here before the child starts.

Results do not depend on how many workers ran or which job finished first.
A strategy that crashes, hangs, or exceeds the resident-set cap is one failed
row. The other jobs keep running. ``CancelToken.cancel`` stops work that has
not finished. It is safe to call from another thread. ``on_progress`` runs on
the thread that called ``run_parallel``.

The leaderboard sorts by excess total return against the 1x book. An anonymized
job also lands in ``designer/``, which is the tree to hand a strategy designer.
"""

from __future__ import annotations

import gc
import hashlib
import json
import math
import queue
import secrets
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor, wait
from concurrent.futures import FIRST_COMPLETED
from dataclasses import dataclass, replace
from pathlib import Path

from .profiles import get_profile
from . import report as report_mod
from .report import (
    OPERATOR_LEADERBOARD_COLUMNS,
    _designer_value_error,
    assert_designer_clean,
    redact_metrics,
    require_anonymizable_symbol,
    scrub_designer_tree,
    scrub_symbols,
    register_run,
    write_json,
    write_leaderboard,
)


@dataclass(frozen=True)
class StrategyRef:
    """One ``strategy.py``. ``strategy_id`` is the label on the leaderboard."""

    strategy_id: str
    path: str


@dataclass(frozen=True)
class NamedMarket:
    """Raw files. Share factor is 1. ``load_named`` runs in the parent."""

    src: str
    symbol: str
    bar_minutes: int = 1
    source_id: str | None = None


@dataclass(frozen=True)
class PackMarket:
    """A pack plus the operator sidecar. The child never receives the sidecar."""

    pack_dir: str
    sidecar_path: str
    source_id: str | None = None


@dataclass(frozen=True)
class FrameMarket:
    """A frame the parent already loaded. Named data only, share factor 1.

    Anonymized bars belong in a ``PackMarket`` so the sidecar scale is applied
    in the parent. This object does not carry a scale.
    """

    source_id: str
    symbol: str
    frame: object
    bar_minutes: int = 1


@dataclass(frozen=True)
class CancelToken:
    """Thread-safe cancel. ``cancel`` unblocks workers that are still running."""

    def __post_init__(self) -> None:
        object.__setattr__(self, "_flag", threading.Event())

    def cancel(self) -> None:
        self._flag.set()

    def cancelled(self) -> bool:
        return self._flag.is_set()


@dataclass(frozen=True)
class ProgressEvent:
    """One progress notice. ``completed`` counts jobs that have finished."""

    completed: int
    total: int
    strategy_id: str
    source_id: str
    status: str
    message: str = ""


@dataclass(frozen=True)
class JobResult:
    strategy_id: str
    source_id: str
    status: str
    out_dir: str
    total_return: float | None
    excess_total_return: float | None
    error: str | None
    timeout_s: float
    snapshot: dict | None = None


@dataclass(frozen=True)
class ParallelResult:
    run_id: str
    out_dir: str
    profile: str
    results: tuple[JobResult, ...]
    leaderboard_path: str
    cancelled: bool
    failures: int


@dataclass
class _Prepared:
    source_id: str
    symbol: str
    frame: object
    bar_minutes: int
    share_factor: float
    anonymized: bool
    dividends: dict | None
    borrow_rate_annual: float | None
    pack_id: str | None
    inspection: dict | None


class _Stop:
    def __init__(self, external: CancelToken | None):
        self._abort = threading.Event()
        self._external = external

    def abort(self) -> None:
        self._abort.set()

    def cancelled(self) -> bool:
        if self._abort.is_set():
            return True
        return self._external is not None and self._external.cancelled()

    def user_cancelled(self) -> bool:
        return self._external is not None and self._external.cancelled()


def discover_strategies(directory) -> tuple[StrategyRef, ...]:
    """``strategy.py`` in ``directory`` and in each immediate child directory."""
    root = Path(directory)
    if not root.is_dir():
        raise FileNotFoundError(f"strategies directory not found: {root}")
    found = []
    if (root / "strategy.py").is_file():
        found.append(root / "strategy.py")
    children = [path for path in root.iterdir() if path.is_dir() and not path.name.startswith(".")]
    for child in sorted(children, key=lambda path: path.name):
        if child.name == "__pycache__":
            continue
        if (child / "strategy.py").is_file():
            found.append(child / "strategy.py")
    if not found:
        raise ValueError(f"no strategy.py under {root}")
    used = set()
    refs = []
    for path in found:
        base = path.parent.name
        candidate = base
        suffix = 2
        while candidate in used:
            candidate = f"{base}-{suffix}"
            suffix += 1
        used.add(candidate)
        _safe_component(candidate)
        refs.append(StrategyRef(strategy_id=candidate, path=str(path.resolve())))
    return tuple(refs)


def run_parallel(
    strategies,
    markets,
    out_dir,
    *,
    profile: str,
    workers: int,
    timeout_s: float = 600.0,
    memory_bytes: int | None = None,
    on_progress=None,
    cancel: CancelToken | None = None,
    borrow_rate_annual: float | None = None,
    financing_rate_annual: float | None = None,
    fee_mult: float = 1.0,
    dividends: dict | None = None,
    warmup_sessions: int = 0,
    initial_equity: float = 1_000_000.0,
    with_benchmarks: bool = True,
    index_path=None,
) -> ParallelResult:
    """Cartesian product of strategies and markets. See the module docstring."""
    from . import runner as runner_mod

    name = get_profile(profile).name
    _check_batch(name, workers, timeout_s, memory_bytes, fee_mult, financing_rate_annual, dividends)
    strategy_list = tuple(strategies)
    market_list = tuple(markets)
    if not strategy_list or not market_list:
        raise ValueError("parallel needs at least one strategy and one market")
    resolved = []
    seen_ids = set()
    for ref in strategy_list:
        _safe_component(ref.strategy_id)
        if ref.strategy_id in seen_ids:
            raise ValueError(f"duplicate strategy_id {ref.strategy_id}")
        seen_ids.add(ref.strategy_id)
        path = Path(ref.path).resolve()
        if not path.is_file():
            raise FileNotFoundError(f"strategy file not found: {ref.path}")
        resolved.append(StrategyRef(strategy_id=ref.strategy_id, path=str(path)))
    strategy_list = tuple(resolved)
    out = Path(out_dir).resolve()
    for market in market_list:
        if isinstance(market, PackMarket) and runner_mod._report_inside_pack(market.pack_dir, out):
            raise ValueError("the report must not live inside the pack directory")
    if index_path is not None:
        index_path = Path(index_path).resolve()

    prepared, prepare_failures = _prepare_markets(
        market_list, dividends, borrow_rate_annual, runner_mod
    )
    gc.collect()
    pairs = []
    for ref in strategy_list:
        for market in prepared:
            pairs.append((ref, market))
        for failure in prepare_failures:
            pairs.append((ref, failure))
    pairs.sort(key=lambda item: (item[0].strategy_id, _pair_source(item[1])))
    seen = set()
    for ref, market in pairs:
        key = (ref.strategy_id, _pair_source(market))
        if key in seen:
            raise ValueError(f"duplicate job {ref.strategy_id} / {_pair_source(market)}")
        seen.add(key)

    for market in prepared:
        if market.anonymized:
            require_anonymizable_symbol(market.symbol)

    out.mkdir(parents=True, exist_ok=True)
    assignments = []
    for ref, market in pairs:
        if isinstance(market, _Prepared):
            frame = runner_mod._standalone_frame(market.frame)
        else:
            frame = None
        assignments.append((ref, market, frame))
    # Windows folds ZzZx and zzzx onto one directory. Reserve names before the
    # workers start so two threads never write the same equity.png.
    job_dirs = _job_directories(out, assignments)

    total = len(assignments)
    progress_q = queue.Queue()
    stop = _Stop(cancel)
    state = {"completed": 0}
    error = None

    def emit(strategy_id, source_id, status, message=""):
        progress_q.put((strategy_id, source_id, status, message))

    def drain():
        while True:
            try:
                strategy_id, source_id, status, message = progress_q.get_nowait()
            except queue.Empty:
                return
            completed = state["completed"]
            if status != "started":
                completed += 1
                state["completed"] = completed
            if on_progress is None:
                continue
            on_progress(
                ProgressEvent(
                    completed=completed,
                    total=total,
                    strategy_id=strategy_id,
                    source_id=source_id,
                    status=status,
                    message=message,
                )
            )

    def one(assignment):
        ref, market, frame = assignment
        source_id = _pair_source(market)
        directory = job_dirs[(ref.strategy_id, source_id)]
        if stop.cancelled():
            emit(ref.strategy_id, source_id, "cancelled", "cancelled")
            return _job_result(ref, source_id, "cancelled", directory, None, "cancelled", timeout_s)
        if not isinstance(market, _Prepared):
            emit(ref.strategy_id, source_id, "error", _first_line(market[1]))
            _write_failure(
                directory,
                strategy_id=ref.strategy_id,
                source_id=source_id,
                profile=name,
                status="error",
                error=market[1],
                designer_error=market[2],
                timeout_s=timeout_s,
                anonymized=True,
                symbol=market[3],
            )
            return _job_result(ref, source_id, "error", directory, None, market[1], timeout_s)
        emit(ref.strategy_id, source_id, "started", "")
        try:
            books = runner_mod._isolated_books(
                None,
                frame,
                name,
                during=_ready,
                timeout=timeout_s,
                memory_bytes=memory_bytes,
                stop=stop,
                strategy_path=ref.path,
                share_factor=market.share_factor,
                borrow_rate_annual=market.borrow_rate_annual,
                financing_rate_annual=financing_rate_annual,
                fee_mult=fee_mult,
                dividends=market.dividends,
                warmup_sessions=warmup_sessions,
                initial_equity=initial_equity,
                with_benchmarks=with_benchmarks,
                anonymized=market.anonymized,
            )
            extra = {
                "warnings": [],
                "anonymized": market.anonymized,
                "strategy_id": ref.strategy_id,
                "source_id": source_id,
            }
            if market.pack_id:
                extra["pack_id"] = market.pack_id
            if market.inspection is not None:
                extra["inspection"] = market.inspection
            payload = runner_mod.write_books(
                directory,
                name,
                market.symbol,
                market.bar_minutes,
                frame,
                books,
                anonymized=market.anonymized,
                extra=extra,
            )
            emit(ref.strategy_id, source_id, "ok", "")
            return _job_result(ref, source_id, "ok", directory, payload, None, timeout_s)
        except Exception as exc:
            status = _classify(exc)
            text = _clip(str(exc))
            tail = getattr(exc, "tail", "") or ""
            if tail:
                # The operator copy keeps the original ticker. The designer
                # sentence below does not include this tail.
                shown = str(tail).strip()
                if shown:
                    text = _clip(text + "\n" + shown[:2000])
            try:
                _write_failure(
                    directory,
                    strategy_id=ref.strategy_id,
                    source_id=source_id,
                    profile=name,
                    status=status,
                    error=text,
                    designer_error="the strategy failed" if market.anonymized else text,
                    timeout_s=timeout_s,
                    anonymized=market.anonymized,
                    symbol=market.symbol,
                )
            except Exception as write_exc:
                status = "error"
                text = _clip(text + "\n" + str(write_exc))
            emit(ref.strategy_id, source_id, status, _first_line(text))
            return _job_result(ref, source_id, status, directory, None, text, timeout_s)

    executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="bt-parallel")
    futures = []
    gathered = {}
    try:
        for assignment in assignments:
            futures.append((assignment, executor.submit(one, assignment)))
        pending = {fut for _assignment, fut in futures}
        while pending:
            done, pending = wait(pending, timeout=0.05, return_when=FIRST_COMPLETED)
            drain()
            for fut in done:
                gathered[fut] = fut.result()
        drain()
    except Exception as exc:
        error = exc
        stop.abort()
    finally:
        if error is not None:
            stop.abort()
        executor.shutdown(wait=True, cancel_futures=error is not None)

    results = []
    for assignment, fut in futures:
        if fut in gathered:
            results.append(gathered[fut])
            continue
        ref, market, _frame = assignment
        source_id = _pair_source(market)
        directory = job_dirs[(ref.strategy_id, source_id)]
        if fut.cancelled() or not fut.done():
            results.append(_job_result(ref, source_id, "cancelled", directory, None, "cancelled", timeout_s))
            continue
        try:
            results.append(fut.result())
        except Exception as exc:
            results.append(_job_result(ref, source_id, "error", directory, None, _clip(str(exc)), timeout_s))

    ranked_rows = _rank_rows(results, assignments, name, timeout_s)
    for index, row in enumerate(ranked_rows, start=1):
        row["rank"] = index
    leaderboard = out / "leaderboard.csv"
    write_leaderboard(leaderboard, ranked_rows, audience="operator")
    _bundle_designer(out, ranked_rows, assignments, results)

    run_id = "r" + secrets.token_hex(8)
    jobs = []
    for row in ranked_rows:
        jobs.append(
            {
                "strategy_id": row["strategy_id"],
                "source_id": row["source_id"],
                "symbol": row.get("symbol") or "",
                "status": row["status"],
                "anonymized": _row_anonymized(row, assignments),
            }
        )
    write_json(
        out / "run.json",
        {
            "schema_version": 1,
            "id": run_id,
            "kind": "parallel",
            "profile": name,
            "rank_key": "excess_total_return",
            "cancelled": stop.user_cancelled(),
            "timeout_s": timeout_s,
            "workers": workers,
            "jobs": jobs,
        },
    )
    if index_path is not None:
        register_run(index_path, run_id, out, kind="parallel", profile=name)
    ordered = _order_results(results, ranked_rows)
    failures = sum(1 for item in ordered if item.status != "ok")
    outcome = ParallelResult(
        run_id=run_id,
        out_dir=str(out),
        profile=name,
        results=tuple(ordered),
        leaderboard_path=str(leaderboard),
        cancelled=stop.user_cancelled(),
        failures=failures,
    )
    if error is not None:
        raise error
    return outcome


def _ready() -> None:
    return None


def _check_batch(name, workers, timeout_s, memory_bytes, fee_mult, financing_rate_annual, dividends) -> None:
    if isinstance(workers, bool) or not isinstance(workers, int) or workers < 1:
        raise ValueError("workers must be a positive integer")
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)) or not timeout_s > 0:
        raise ValueError("timeout_s must be positive")
    if memory_bytes is not None and (
        isinstance(memory_bytes, bool) or not isinstance(memory_bytes, int) or memory_bytes <= 0
    ):
        raise ValueError("memory_bytes must be a positive number of bytes")
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


def _prepare_markets(markets, dividends, borrow_rate_annual, runner_mod):
    prepared = []
    failures = []
    for market in markets:
        try:
            prepared.append(_prepare_one(market, dividends, borrow_rate_annual, runner_mod))
        except Exception as exc:
            if isinstance(market, PackMarket):
                source_id = market.source_id or _path_source_id(market.pack_dir)
                try:
                    _safe_component(source_id)
                except ValueError:
                    source_id = _path_source_id(market.pack_dir)
                failures.append((source_id, _clip(str(exc)), "pack could not be prepared", ""))
            else:
                raise
    return prepared, failures


def _prepare_one(market, dividends, borrow_rate_annual, runner_mod) -> _Prepared:
    if isinstance(market, FrameMarket):
        source_id = _safe_component(market.source_id)
        return _Prepared(
            source_id=source_id,
            symbol=str(market.symbol),
            frame=market.frame,
            bar_minutes=int(market.bar_minutes),
            share_factor=1.0,
            anonymized=False,
            dividends=None if dividends is None else dict(dividends),
            borrow_rate_annual=borrow_rate_annual,
            pack_id=None,
            inspection=None,
        )
    if isinstance(market, NamedMarket):
        from .data import load_named

        loaded = load_named(market.src, market.symbol, bar_minutes=market.bar_minutes)
        if not loaded.inspection.runnable:
            raise ValueError(
                f"{market.symbol} is {loaded.inspection.price_adjustment} and is not runnable. "
                "This phase executes raw prices only."
            )
        factor = loaded.inspection.share_factor
        if factor != 1.0:
            raise ValueError(
                f"share_factor {factor} is not 1.0. Raw runs use 1.0. "
                "0.9 is not a kit constant."
            )
        source_id = _safe_component(market.source_id or market.symbol)
        return _Prepared(
            source_id=source_id,
            symbol=str(market.symbol),
            frame=loaded.frame,
            bar_minutes=int(market.bar_minutes),
            share_factor=1.0,
            anonymized=False,
            dividends=None if dividends is None else dict(dividends),
            borrow_rate_annual=borrow_rate_annual,
            pack_id=None,
            inspection=loaded.inspection.as_dict(),
        )
    if isinstance(market, PackMarket):
        return _prepare_pack(market, dividends, borrow_rate_annual, runner_mod)
    raise TypeError(f"unknown market {type(market).__name__}")


def _prepare_pack(market: PackMarket, dividends, borrow_rate_annual, runner_mod) -> _Prepared:
    from .pack import load_pack, load_sidecar

    loaded = load_pack(market.pack_dir)
    secret = load_sidecar(market.sidecar_path)
    try:
        if secret["pack_id"] != loaded.pack_id:
            raise ValueError("sidecar does not match this pack")
        if bool(secret["anonymized"]) != loaded.anonymized:
            raise ValueError("sidecar anonymized flag does not match the pack")
        anonymized = bool(secret["anonymized"])
        share_scale = float(secret["share_scale"])
        symbol = str(secret["symbol"])
        child_dividends = runner_mod._pack_dividends(
            dividends,
            anonymized=anonymized,
            share_scale=share_scale,
            date_map=secret["date_map"],
        )
        borrow = borrow_rate_annual
        if borrow is None and secret["borrow_rate_source"] == "explicit":
            borrow = float(secret["borrow_rate_annual"])
        pack_id = str(loaded.pack_id)
        if anonymized:
            source_id = f"pack_{pack_id[:12]}"
        else:
            source_id = market.source_id or symbol
        source_id = _safe_component(source_id)
        frame = loaded.frame
        bar_minutes = int(loaded.bar_minutes)
    finally:
        secret = None
    return _Prepared(
        source_id=source_id,
        symbol=symbol,
        frame=frame,
        bar_minutes=bar_minutes,
        share_factor=share_scale,
        anonymized=anonymized,
        dividends=child_dividends,
        borrow_rate_annual=borrow,
        pack_id=pack_id,
        inspection=None,
    )


def _pair_source(market) -> str:
    if isinstance(market, _Prepared):
        return market.source_id
    return market[0]


def _path_source_id(path) -> str:
    digest = hashlib.sha256(str(Path(path).resolve()).encode("utf-8")).hexdigest()[:12]
    return f"pack_{digest}"


def _safe_component(value: str) -> str:
    text = str(value)
    if not text or text in {".", ".."}:
        raise ValueError(f"unsafe id {value!r}")
    if any(ch in text for ch in '\\/:*?"<>|'):
        raise ValueError(f"unsafe id {value!r}")
    if text.endswith(" ") or text.endswith("."):
        raise ValueError(f"unsafe id {value!r}")
    return text


def _reserve_case_component(value: str, used: set[str]) -> str:
    """A directory name that stays distinct when the disk ignores case.

    ``used`` holds casefolded names already reserved. ``ZzZx`` and ``zzzx``
    would be one folder on Windows, so the second becomes ``zzzx-2``.
    """
    text = _safe_component(value)
    candidate = text
    suffix = 2
    while candidate.casefold() in used:
        candidate = f"{text}-{suffix}"
        suffix += 1
    used.add(candidate.casefold())
    return candidate


def _job_directories(out: Path, assignments) -> dict[tuple[str, str], Path]:
    """One real directory per job, including ids that differ only by case."""
    strategy_names: dict[str, str] = {}
    used_strategies: set[str] = set()
    used_sources: dict[str, set[str]] = {}
    paths: dict[tuple[str, str], Path] = {}
    for ref, market, _frame in assignments:
        strategy_id = ref.strategy_id
        if strategy_id not in strategy_names:
            strategy_names[strategy_id] = _reserve_case_component(strategy_id, used_strategies)
        folder = strategy_names[strategy_id]
        source_id = _pair_source(market)
        sources = used_sources.setdefault(folder, set())
        source_name = _reserve_case_component(source_id, sources)
        paths[(strategy_id, source_id)] = out / "jobs" / folder / source_name
    return paths


def _classify(exc: BaseException) -> str:
    text = str(exc)
    if "walk-forward worker cancelled" in text:
        return "cancelled"
    if "exceeded memory limit" in text or "MemoryError" in text:
        return "memory"
    if "walk-forward worker timed out" in text:
        return "timeout"
    if "walk-forward worker exited" in text:
        return "crash"
    return "error"


def _clip(text: str, limit: int = 8000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n..."


def _first_line(text: str) -> str:
    line = text.splitlines()[0] if text else ""
    if len(line) > 200:
        return line[:200]
    return line


# Columns copied from the strategy metrics. Rank, identity, and the benchmark
# excess are filled by the batch, not by the engine.
_PAYLOAD_COLUMNS = (
    "total_return",
    "cagr",
    "ann_vol",
    "sharpe",
    "sortino",
    "max_drawdown",
    "max_dd_duration_sessions",
    "calmar",
    "n_fills",
    "round_trips",
    "hit_rate",
    "avg_holding_sessions",
    "turnover_per_year",
    "fees_total",
    "best_day",
    "worst_day",
    "percent_up_days",
    "avg_abs_exposure_close",
    "frac_sessions_long_at_close",
    "frac_sessions_short_at_close",
    "long_round_trips",
    "short_round_trips",
    "financing_cost",
    "borrow_cost",
    "dividends_net",
    "margin_calls_filled",
    "max_gross_leverage",
    "avg_gross_leverage",
    "worst_equity_over_hwm",
    "disqualified",
)


def _plain(value):
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, bool):
        return value
    if hasattr(value, "item"):
        try:
            value = value.item()
        except Exception:
            return value
    if isinstance(value, bool):
        return bool(value)
    if isinstance(value, int) and not isinstance(value, bool):
        return int(value)
    if isinstance(value, float):
        return float(value)
    return value


def _snapshot(payload) -> dict | None:
    if not payload:
        return None
    snap = {key: _plain(payload.get(key)) for key in _PAYLOAD_COLUMNS if key in payload}
    benches = payload.get("benchmarks") or {}
    bh = (benches.get("buy_and_hold_1x") or {}).get("total_return")
    two = (benches.get("long_2x") or {}).get("total_return")
    snap["bh_total_return"] = None if bh is None else _plain(bh)
    snap["long_2x_total_return"] = None if two is None else _plain(two)
    return snap


def _job_result(ref, source_id, status, directory, payload, error, timeout_s) -> JobResult:
    snap = _snapshot(payload)
    total = None if snap is None else snap.get("total_return")
    excess = None
    if snap is not None and total is not None and snap.get("bh_total_return") is not None:
        excess = float(total) - float(snap["bh_total_return"])
    return JobResult(
        strategy_id=ref.strategy_id,
        source_id=source_id,
        status=status,
        out_dir=str(directory),
        total_return=None if total is None else float(total),
        excess_total_return=excess,
        error=error,
        timeout_s=float(timeout_s),
        snapshot=snap,
    )


def _rank_rows(results, assignments, profile, timeout_s) -> list[dict]:
    by_key = {(item.strategy_id, item.source_id): item for item in results}
    rows = []
    for ref, market, _frame in assignments:
        source_id = _pair_source(market)
        item = by_key[(ref.strategy_id, source_id)]
        symbol = market.symbol if isinstance(market, _Prepared) else ""
        row = {column: None for column in OPERATOR_LEADERBOARD_COLUMNS}
        row.update(
            {
                "strategy_id": ref.strategy_id,
                "source_id": source_id,
                "symbol": symbol,
                "profile": profile,
                "status": item.status,
                "total_return": item.total_return,
                "excess_total_return": item.excess_total_return,
                "timeout_s": timeout_s,
                "error": "" if item.status == "ok" else (item.error or item.status),
            }
        )
        if item.snapshot:
            for key, value in item.snapshot.items():
                if key in row:
                    row[key] = value
            if item.excess_total_return is not None:
                row["excess_total_return"] = item.excess_total_return
        rows.append(row)
    rows.sort(key=_rank_key)
    return rows


def _rank_key(row):
    ok = row["status"] == "ok"
    excess = row.get("excess_total_return")
    if ok and isinstance(excess, (int, float)) and not isinstance(excess, bool) and math.isfinite(excess):
        return (0, -float(excess), row["strategy_id"], row["source_id"])
    if ok:
        return (1, 0.0, row["strategy_id"], row["source_id"])
    return (2, 0.0, row["strategy_id"], row["source_id"])


def _rows_with_markets(rows, assignments):
    by_key = {}
    for assignment in assignments:
        by_key[(assignment[0].strategy_id, _pair_source(assignment[1]))] = assignment
    for row in rows:
        yield row, by_key[(row["strategy_id"], row["source_id"])]


def _row_anonymized(row, assignments) -> bool:
    for assignment in assignments:
        if assignment[0].strategy_id == row["strategy_id"] and _pair_source(assignment[1]) == row["source_id"]:
            market = assignment[1]
            if isinstance(market, _Prepared):
                return bool(market.anonymized)
            return True
    return False


def _designer_error_text(item: JobResult) -> str:
    """The sentence written under designer/, not the operator traceback."""
    if item.status == "ok":
        return ""
    path = Path(item.out_dir) / "designer" / "error.json"
    if path.is_file():
        document = json.loads(path.read_text(encoding="utf-8"))
        text = document.get("error")
        if isinstance(text, str) and text:
            return text
    return "the strategy failed"


def _name_dirty(candidate: str, symbols) -> bool:
    """True when this path component would fail the designer audit."""
    if not isinstance(candidate, str) or candidate == "":
        return True
    try:
        _safe_component(candidate)
    except ValueError:
        return True
    seen: set[str] = set()
    for probe in [*(symbols or ()), ""]:
        if probe in seen:
            continue
        seen.add(probe)
        if _designer_value_error("caption", candidate, probe):
            return True
    return False


def _screened_sentence(symbols) -> str:
    """An error sentence that is not a ticker for any symbol in the batch."""
    for phrase in ("the strategy failed", "withheld", "not published", "stopped"):
        if not _name_dirty(phrase, symbols):
            return phrase
    for length in range(2, 40):
        phrase = "q" * length
        if not _name_dirty(phrase, symbols):
            return phrase
    return "q"


def _designer_component(value: str, symbols, used: set) -> str:
    """A designer path component scrubbed against every symbol in the batch.

    ``used`` stores casefolded names, the same reservation as job directories.
    ``My_RANDOM_v2`` and ``my_RANDOM_v2`` would be one folder on a
    case-insensitive disk, so the second becomes ``my_RANDOM_v2-2``. A
    candidate whose base or suffix is a ticker for any batch symbol is
    skipped, including ``RANDOM-2`` when the symbol is ``2``. The stand-in
    is :data:`report_mod.TICKER_STAND_IN`. The same strategy id always gets
    the same component, so the caller passes one shared ``used`` set.
    """
    symbol_list = tuple(dict.fromkeys(symbol for symbol in (symbols or ()) if symbol))
    scrubbed = scrub_symbols(str(value), symbol_list) if symbol_list else str(value)
    stand_in = report_mod.TICKER_STAND_IN
    stems: list[str] = []
    if scrubbed and not _name_dirty(scrubbed, symbol_list):
        stems.append(scrubbed)
    if stand_in not in stems and not _name_dirty(stand_in, symbol_list):
        stems.append(stand_in)
    if not stems:
        shown = symbol_list[0] if symbol_list else value
        raise ValueError(
            f"Symbol {shown!r} collides with a reserved report name and cannot be anonymized; "
            "run it as an operator (raw) book or rename the symbol in the data."
        )
    for stem in stems:
        bare = True
        suffix = 2
        for _attempt in range(20000):
            candidate = stem if bare else f"{stem}-{suffix}"
            if bare:
                bare = False
            else:
                suffix += 1
            if _name_dirty(candidate, symbol_list):
                continue
            if candidate.casefold() in used:
                continue
            used.add(candidate.casefold())
            return candidate
    raise RuntimeError(f"no screened designer name for {value!r}")


def _result_for(results, row) -> JobResult:
    for item in results:
        if item.strategy_id == row["strategy_id"] and item.source_id == row["source_id"]:
            return item
    raise KeyError((row["strategy_id"], row["source_id"]))


def _fail_published_row(results, row, message: str) -> None:
    """One job's designer tree failed. The batch still finishes."""
    row["status"] = "error"
    row["error"] = message
    for index, item in enumerate(results):
        if item.strategy_id == row["strategy_id"] and item.source_id == row["source_id"]:
            results[index] = replace(item, status="error", error=message)
            return


def _audit_job_tree(dest, symbols) -> None:
    probes = list(dict.fromkeys([*symbols, ""]))
    for probe in probes:
        assert_designer_clean(dest, symbol=probe or "")


def _cell_violation(copied: dict, symbols) -> str | None:
    probes = list(dict.fromkeys([*symbols, ""]))
    for key, value in copied.items():
        if not isinstance(value, str):
            continue
        for probe in probes:
            reason = _designer_value_error(key, value, probe)
            if reason:
                return f"{key} contains {reason}"
    return None


def _write_clean_designer_error(dest, folder: str, source_name: str, profile: str, symbols) -> None:
    """Replace ``dest`` with an error file that passes the audit for every symbol.

    The directory is removed again when the audit fails, so a ticker suffix is
    never left on disk. ``folder`` and ``source_name`` must already be screened.
    """
    if _name_dirty(folder, symbols) or _name_dirty(source_name, symbols):
        raise RuntimeError(f"unscreened designer name {folder}/{source_name}")
    phrase = _screened_sentence(symbols)
    payload = {
        "strategy_id": folder,
        "source_id": source_name,
        "status": "error",
        "error": phrase,
    }
    if profile and not _name_dirty(str(profile), symbols):
        payload["profile"] = str(profile)
    if not _name_dirty("designer", symbols):
        payload["audience"] = "designer"
    dest = Path(dest)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)
    write_json(dest / "error.json", payload)
    try:
        _audit_job_tree(dest, symbols)
    except Exception:
        shutil.rmtree(dest, ignore_errors=True)
        raise


def _bundle_designer(out, ranked_rows, assignments, results) -> None:
    """One directory a designer can be handed. Named jobs stay out of it.

    A scrub or audit failure marks that job ``error`` and stays inside this
    function. The other jobs are still published, and the caller still writes
    ``run.json``.
    """
    selected = []
    symbols = []
    for row, assignment in _rows_with_markets(ranked_rows, assignments):
        market = assignment[1]
        anonymized = not isinstance(market, _Prepared) or market.anonymized
        if not anonymized:
            continue
        selected.append((row, assignment))
        if isinstance(market, _Prepared) and market.symbol:
            symbols.append(market.symbol)
    if not selected:
        return
    unique_symbols = list(dict.fromkeys(symbols))
    designer = out / "designer"
    if designer.exists():
        shutil.rmtree(designer)
    designer.mkdir(parents=True)
    mutated = False
    placed = {}
    # One folder per strategy id. A second id that scrubs to the same text
    # becomes RANDOM-2. Both markets of one strategy share the folder.
    used_folders: set[str] = set()
    folders: dict[str, str] = {}
    used_sources: dict[str, set[str]] = {}
    for row, assignment in selected:
        item = _result_for(results, row)
        market = assignment[1]
        symbol = market.symbol if isinstance(market, _Prepared) else ""
        strategy_id = row["strategy_id"]
        if strategy_id not in folders:
            folders[strategy_id] = _designer_component(strategy_id, unique_symbols, used_folders)
        folder = folders[strategy_id]
        source_used = used_sources.setdefault(folder, set())
        source_name = _designer_component(row["source_id"], unique_symbols, source_used)
        placed[(strategy_id, row["source_id"])] = (folder, source_name, symbol)
        source = Path(item.out_dir) / "designer"
        if not source.is_dir():
            continue
        dest = designer / folder / source_name
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            shutil.rmtree(dest)
        try:
            shutil.copytree(source, dest)
            scrub_designer_tree(
                dest,
                unique_symbols,
                strategy_id=strategy_id,
                folder=folder,
                source_id=row["source_id"],
                source_name=source_name,
            )
            _audit_job_tree(dest, unique_symbols)
        except Exception as exc:
            mutated = True
            names_dirty = _name_dirty(folder, unique_symbols) or _name_dirty(source_name, unique_symbols)
            if names_dirty:
                # Keep the book. A later pass renames the directory when the
                # files themselves are clean, so a bad suffix does not fail
                # the row that ran.
                continue
            _fail_published_row(results, row, _clip(f"designer audit failed: {exc}"))
            try:
                _write_clean_designer_error(
                    dest, folder, source_name, row.get("profile") or "", unique_symbols
                )
            except Exception:
                if dest.exists():
                    shutil.rmtree(dest, ignore_errors=True)

    def designer_rows():
        nonlocal mutated
        cleaned = []
        for row in ranked_rows:
            key = (row["strategy_id"], row["source_id"])
            if key not in placed:
                continue
            folder, source_name, _symbol = placed[key]
            item = _result_for(results, row)
            copied = {}
            for column, value in row.items():
                if column == "symbol":
                    continue
                if isinstance(value, str):
                    copied[column] = scrub_symbols(value, unique_symbols, key=column)
                else:
                    copied[column] = value
            # The leaderboard id is the directory name. Do not substitute a
            # different token; a repair pass renames the directory to match.
            copied["strategy_id"] = folder
            copied["source_id"] = source_name
            if row["status"] != "ok":
                copied["status"] = "error"
                copied["error"] = scrub_symbols(
                    _designer_error_text(item), unique_symbols, key="error"
                )
            other = {
                column: value
                for column, value in copied.items()
                if column not in {"strategy_id", "source_id"} and isinstance(value, str)
            }
            if _cell_violation(other, unique_symbols):
                mutated = True
                _fail_published_row(results, row, _clip("designer audit failed: leaderboard cell"))
                copied["status"] = "error"
                copied["error"] = _screened_sentence(unique_symbols)
                for column, value in list(copied.items()):
                    if column in {"strategy_id", "source_id", "status"}:
                        continue
                    if isinstance(value, str) and _cell_violation({column: value}, unique_symbols):
                        copied[column] = _screened_sentence(unique_symbols)
            cleaned.append(copied)
        for index, copied in enumerate(cleaned, start=1):
            copied["rank"] = index
        return cleaned

    def republish_operator() -> None:
        if not mutated:
            return
        ranked_rows.sort(key=_rank_key)
        for index, row in enumerate(ranked_rows, start=1):
            row["rank"] = index
        write_leaderboard(out / "leaderboard.csv", ranked_rows, audience="operator")

    def assert_tree() -> None:
        assert_designer_clean(designer, symbol="")
        for symbol in unique_symbols:
            assert_designer_clean(designer, symbol=symbol)

    def _rows_for(folder: str) -> list:
        found = []
        for row in ranked_rows:
            key = (row["strategy_id"], row["source_id"])
            if key in placed and placed[key][0] == folder:
                found.append(row)
        return found

    def _rename_clean(folder: str) -> str:
        """Give a dirty directory a screened name and keep the books."""
        rows_here = _rows_for(folder)
        representative = rows_here[0]["strategy_id"] if rows_here else folder
        new_folder = folder
        if _name_dirty(folder, unique_symbols):
            new_folder = _designer_component(representative, unique_symbols, used_folders)
            old_path = designer / folder
            new_path = designer / new_folder
            if old_path.exists():
                old_path.rename(new_path)
        source_used = used_sources.setdefault(new_folder, set())
        for row in rows_here:
            key = (row["strategy_id"], row["source_id"])
            _previous, old_source, symbol = placed[key]
            if _name_dirty(old_source, unique_symbols) or old_source.casefold() in source_used:
                new_source = _designer_component(row["source_id"], unique_symbols, source_used)
                src = designer / new_folder / old_source
                dst = designer / new_folder / new_source
                if src.exists() and src != dst:
                    src.rename(dst)
            else:
                new_source = old_source
                source_used.add(new_source.casefold())
            placed[key] = (new_folder, new_source, symbol)
            dest = designer / new_folder / new_source
            if dest.is_dir():
                scrub_designer_tree(
                    dest,
                    unique_symbols,
                    strategy_id=folder,
                    folder=new_folder,
                    source_id=old_source,
                    source_name=new_source,
                )
        return new_folder

    def _fail_folder(folder: str, message: str) -> None:
        """Replace one strategy folder with screened error trees, then re-audit."""
        rows_here = _rows_for(folder)
        old_path = designer / folder
        if old_path.exists():
            shutil.rmtree(old_path)
        if not rows_here:
            return
        if not _name_dirty(folder, unique_symbols):
            new_folder = folder
            source_used: set[str] = set()
            used_sources[new_folder] = source_used
        else:
            new_folder = _designer_component(rows_here[0]["strategy_id"], unique_symbols, used_folders)
            source_used = used_sources.setdefault(new_folder, set())
        for row in rows_here:
            key = (row["strategy_id"], row["source_id"])
            _previous, old_source, _symbol = placed[key]
            if not _name_dirty(old_source, unique_symbols) and old_source.casefold() not in source_used:
                source_name = old_source
                source_used.add(source_name.casefold())
            else:
                source_name = _designer_component(row["source_id"], unique_symbols, source_used)
            placed[key] = (new_folder, source_name, _symbol)
            _fail_published_row(results, row, message)
            _write_clean_designer_error(
                designer / new_folder / source_name,
                new_folder,
                source_name,
                row.get("profile") or "",
                unique_symbols,
            )
        try:
            _audit_job_tree(designer / new_folder, unique_symbols)
        except Exception:
            if (designer / new_folder).exists():
                shutil.rmtree(designer / new_folder, ignore_errors=True)
            raise

    def isolate_dirty_folders() -> None:
        """A failure outside one job directory fails that strategy's rows only.

        The published tree is not replaced by a single error file, and a clean
        strategy keeps its directory. A directory whose name is a ticker is
        renamed to a screened name. When the files are clean the row stays
        ok. A failed audit is removed before a screened tree is written, so
        the dirty name does not remain on disk.
        """
        nonlocal mutated
        dirty: list[tuple[str, Exception]] = []
        seen_folders: list[str] = []
        for folder, _source_name, _symbol in placed.values():
            if folder not in seen_folders:
                seen_folders.append(folder)
        for folder in seen_folders:
            folder_path = designer / folder
            if not folder_path.exists():
                continue
            try:
                _audit_job_tree(folder_path, unique_symbols)
            except Exception as exc:
                dirty.append((folder, exc))
        if not dirty:
            return
        mutated = True
        for folder, exc in dirty:
            message = _clip(f"designer audit failed: {exc}")
            names_dirty = _name_dirty(folder, unique_symbols) or any(
                _name_dirty(placed[(row["strategy_id"], row["source_id"])][1], unique_symbols)
                for row in _rows_for(folder)
            )
            if names_dirty:
                new_folder = _rename_clean(folder)
                try:
                    _audit_job_tree(designer / new_folder, unique_symbols)
                    continue
                except Exception as exc2:
                    message = _clip(f"designer audit failed: {exc2}")
                    folder = new_folder
            _fail_folder(folder, message)

    def publish_designer_board() -> None:
        republish_operator()
        write_leaderboard(designer / "leaderboard.csv", designer_rows(), audience="designer")

    def drop_remaining_dirty() -> None:
        """A directory that still fails the audit is deleted, not published."""
        nonlocal mutated
        mutated = True
        seen: list[str] = []
        for folder, _source_name, _symbol in list(placed.values()):
            if folder not in seen:
                seen.append(folder)
        for folder in seen:
            folder_path = designer / folder
            if not folder_path.exists():
                continue
            try:
                _audit_job_tree(folder_path, unique_symbols)
            except Exception:
                shutil.rmtree(folder_path, ignore_errors=True)
                for row in ranked_rows:
                    key = (row["strategy_id"], row["source_id"])
                    if key in placed and placed[key][0] == folder:
                        _fail_published_row(results, row, "designer audit failed")
                        placed.pop(key, None)

    designer_rows()
    publish_designer_board()
    try:
        assert_tree()
    except Exception:
        mutated = True
        isolate_dirty_folders()
        publish_designer_board()
        try:
            assert_tree()
        except Exception:
            isolate_dirty_folders()
            publish_designer_board()
            try:
                assert_tree()
            except Exception:
                drop_remaining_dirty()
                publish_designer_board()


def _order_results(results, ranked_rows) -> list[JobResult]:
    by_key = {(item.strategy_id, item.source_id): item for item in results}
    return [by_key[(row["strategy_id"], row["source_id"])] for row in ranked_rows]


def _write_failure(
    directory,
    *,
    strategy_id,
    source_id,
    profile,
    status,
    error,
    designer_error,
    timeout_s,
    anonymized,
    symbol,
) -> None:
    directory = Path(directory)
    if (directory / "designer").exists():
        shutil.rmtree(directory / "designer")
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "strategy_id": strategy_id,
        "source_id": source_id,
        "profile": profile,
        "status": status,
        "error": error,
        "timeout_s": timeout_s,
        "anonymized": bool(anonymized),
    }
    if symbol and not anonymized:
        payload["symbol"] = symbol
    write_json(directory / "error.json", payload)
    if anonymized:
        designer = directory / "designer"
        designer.mkdir(parents=True, exist_ok=True)
        cleaned = redact_metrics(
            {
                "strategy_id": strategy_id,
                "source_id": source_id,
                "profile": profile,
                "status": status,
                "error": designer_error,
                "timeout_s": timeout_s,
            },
            symbol=symbol or "",
        )
        write_json(designer / "error.json", cleaned)
        assert_designer_clean(designer, symbol=symbol or "")
