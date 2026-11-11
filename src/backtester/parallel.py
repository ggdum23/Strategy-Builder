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
from dataclasses import dataclass
from pathlib import Path

from .profiles import get_profile
from .report import (
    OPERATOR_LEADERBOARD_COLUMNS,
    _scrub_text,
    assert_designer_clean,
    redact_metrics,
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

    out.mkdir(parents=True, exist_ok=True)
    assignments = []
    for ref, market in pairs:
        if isinstance(market, _Prepared):
            frame = runner_mod._standalone_frame(market.frame)
        else:
            frame = None
        assignments.append((ref, market, frame))

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
        if stop.cancelled():
            emit(ref.strategy_id, source_id, "cancelled", "cancelled")
            return _job_result(ref, source_id, "cancelled", out, None, "cancelled", timeout_s)
        if not isinstance(market, _Prepared):
            emit(ref.strategy_id, source_id, "error", _first_line(market[1]))
            _write_failure(
                out / "jobs" / ref.strategy_id / source_id,
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
            return _job_result(ref, source_id, "error", out, None, market[1], timeout_s)
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
                out / "jobs" / ref.strategy_id / source_id,
                name,
                market.symbol,
                market.bar_minutes,
                frame,
                books,
                anonymized=market.anonymized,
                extra=extra,
            )
            emit(ref.strategy_id, source_id, "ok", "")
            return _job_result(ref, source_id, "ok", out, payload, None, timeout_s)
        except Exception as exc:
            status = _classify(exc)
            text = _clip(str(exc))
            tail = getattr(exc, "tail", "") or ""
            if tail:
                scrubbed = _scrub_text(str(tail), market.symbol if market.symbol else "")
                scrubbed = scrubbed.strip()
                if scrubbed:
                    text = _clip(text + "\n" + scrubbed[:2000])
            try:
                _write_failure(
                    out / "jobs" / ref.strategy_id / source_id,
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
            return _job_result(ref, source_id, status, out, None, text, timeout_s)

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
        if fut.cancelled() or not fut.done():
            results.append(_job_result(ref, source_id, "cancelled", out, None, "cancelled", timeout_s))
            continue
        try:
            results.append(fut.result())
        except Exception as exc:
            results.append(_job_result(ref, source_id, "error", out, None, _clip(str(exc)), timeout_s))

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


def _job_result(ref, source_id, status, out, payload, error, timeout_s) -> JobResult:
    snap = _snapshot(payload)
    total = None if snap is None else snap.get("total_return")
    excess = None
    if snap is not None and total is not None and snap.get("bh_total_return") is not None:
        excess = float(total) - float(snap["bh_total_return"])
    return JobResult(
        strategy_id=ref.strategy_id,
        source_id=source_id,
        status=status,
        out_dir=str(out / "jobs" / ref.strategy_id / source_id),
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


def _designer_component(value: str, symbol: str, used: set) -> str:
    """A designer path component. A case-variant of the ticker becomes ASSET."""
    text = _scrub_text(str(value), symbol) if symbol else str(value)
    if not text or text in {".", ".."}:
        text = "ASSET"
    candidate = text
    suffix = 2
    while candidate in used:
        candidate = f"{text}-{suffix}"
        suffix += 1
    used.add(candidate)
    return _safe_component(candidate)


def _bundle_designer(out, ranked_rows, assignments, results) -> None:
    """One directory a designer can be handed. Named jobs stay out of it."""
    selected = []
    symbols = []
    for row, assignment in _rows_with_markets(ranked_rows, assignments):
        market = assignment[1]
        anonymized = not isinstance(market, _Prepared) or market.anonymized
        if not anonymized:
            continue
        selected.append(row)
        if isinstance(market, _Prepared) and market.symbol:
            symbols.append(market.symbol)
    if not selected:
        return
    designer = out / "designer"
    if designer.exists():
        shutil.rmtree(designer)
    designer.mkdir(parents=True)
    by_job = {(item.strategy_id, item.source_id): item for item in results}
    cleaned = []
    used_names = set()
    for row, assignment in _rows_with_markets(selected, assignments):
        item = by_job[(row["strategy_id"], row["source_id"])]
        market = assignment[1]
        symbol = market.symbol if isinstance(market, _Prepared) else ""
        # The directory suffix and the leaderboard id are the same string.
        # Scrubbing alone turns both ZzZx and zzzx into ASSET.
        folder = _designer_component(row["strategy_id"], symbol, used_names)
        source_name = _designer_component(row["source_id"], symbol, set())
        source = Path(item.out_dir) / "designer"
        if source.is_dir():
            dest = designer / folder / source_name
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(source, dest)
        copied = {}
        for key, value in row.items():
            if key == "symbol":
                continue
            if isinstance(value, str):
                copied[key] = _scrub_text(value, symbol)
            else:
                copied[key] = value
        copied["strategy_id"] = folder
        copied["source_id"] = source_name
        copied["error"] = _scrub_text(_designer_error_text(item), symbol)
        cleaned.append(copied)
    for index, row in enumerate(cleaned, start=1):
        row["rank"] = index
    write_leaderboard(designer / "leaderboard.csv", cleaned, audience="designer")
    assert_designer_clean(designer, symbol="")
    for symbol in symbols:
        assert_designer_clean(designer, symbol=symbol)


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
