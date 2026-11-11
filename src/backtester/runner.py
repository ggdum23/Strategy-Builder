"""Run one strategy, or a design window and a locked tail.

Benchmarks go through the same engine and the same cost configuration as the
strategy. Arena financing stays at zero. Desk financing stays at the desk rate
(7% unless the run overrides it).

Phase 1 walk-forward isolation is the heap, class attributes, module globals,
the environment, argv, and cwd. It is not an OS jail. See docs/ISOLATION.md.
"""

from __future__ import annotations

import gc
import hashlib
import hmac
import os
import pickle
import shutil
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass
from pathlib import Path

from . import engine_arena, engine_desk
from .data import WalkSlices, frame_to_desk_bars, load_named, slice_walkforward
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
    "cost_basis",
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


def _cost_basis(profile: str, share_factor: float, *, anonymized: bool) -> dict:
    """What the invoice used. The strategy does not receive this dict."""
    schedule = get_profile(profile).fee_table
    name = get_profile(profile).name
    return {
        "share_scale": share_factor,
        "share_factor": share_factor,
        "anonymized": bool(anonymized),
        "per_share_fees": "real_share_counts",
        "value_fees": "unscaled_trade_value",
        "cost_schedule": schedule,
        "profile": name,
        "note": (
            f"Per-share fees use real share counts (share_scale={share_factor}). "
            f"Value-based fees use trade value. Cost schedule: {schedule}."
        ),
    }


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
    anonymized: bool = False,
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
        borrow = get_profile(name).borrow_rate_annual if borrow_rate_annual is None else borrow_rate_annual
        kwargs = {
            "initial_equity": initial_equity,
            "share_scale": share_factor,
            "reset_day": effective_reset,
            "borrow_rate_annual": borrow,
        }
        result = engine_arena.run(strategy, frame, **kwargs)
        metrics = enrich_arena_metrics(result, engine_arena.metrics(result))
        metrics["share_factor"] = share_factor
        metrics["borrow_rate_annual"] = borrow
        metrics["financing_rate_annual"] = 0.0
        metrics["cost_basis"] = _cost_basis(name, share_factor, anonymized=anonymized)
        return result, metrics
    if name == "desk":
        rate = engine_desk.D.FINANCING_RATE_ANNUAL if financing_rate_annual is None else financing_rate_annual
        borrow = get_profile(name).borrow_rate_annual if borrow_rate_annual is None else borrow_rate_annual
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
        metrics["cost_basis"] = _cost_basis(name, share_factor, anonymized=anonymized)
        return result, metrics
    raise ValueError(f"unknown profile {profile}")


def _benchmarks(profile: str):
    if profile == "arena":
        return (("buy_and_hold_1x", engine_arena.BuyAndHold),)
    if profile == "desk":
        return (("buy_and_hold_1x", DeskBuyAndHold), ("long_2x", TwoXLong))
    raise ValueError(profile)


def _strategy_spec(strategy_cls):
    """How the child should obtain the class.

    A class loaded from a file lives in a synthetic module the child cannot
    import. The child loads that file again. An importable class is pickled
    by module and name.
    """
    path = getattr(strategy_cls, "__strategy_path__", None)
    if path:
        return {"kind": "path", "path": str(path)}
    return {"kind": "class", "cls": strategy_cls}


def _load_strategy(spec):
    if spec["kind"] == "path":
        from .strategy_loader import load_strategy_class

        return load_strategy_class(spec["path"])
    if spec["kind"] == "class":
        return spec["cls"]
    raise ValueError(f"unknown strategy spec {spec.get('kind')!r}")


def _standalone_frame(frame):
    """A frame whose pickle contains only its own rows."""
    out = frame.copy(deep=True).reset_index(drop=True)
    out.attrs.clear()
    return out


def _plain_kwargs(kwargs):
    out = dict(kwargs)
    dividends = out.get("dividends")
    if dividends:
        out["dividends"] = {str(key): float(value) for key, value in dividends.items()}
    return out


def _tag_observer(result, strategy):
    tagged = dict(result)
    tagged["observer"] = strategy
    return tagged


def _picklable_books(books):
    """Drop an observer the child cannot send back. The books themselves must travel."""
    portable = {}
    for label, (result, metrics) in books.items():
        try:
            pickle.dumps((result, metrics))
        except Exception:
            plain = dict(result)
            plain.pop("observer", None)
            pickle.dumps((plain, metrics))
            portable[label] = (plain, metrics)
        else:
            portable[label] = (result, metrics)
    return portable


# Names a spawned book may inherit. Anything else, including a data-root
# variable, is dropped. PATH stays because the interpreter and native
# libraries may need it; a data directory placed on PATH is not stripped.
_ENV_KEEP = frozenset(
    {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "SYSTEMDRIVE",
        "TEMP",
        "TMP",
        "TMPDIR",
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONNOUSERSITE",
        "PYTHONHASHSEED",
        "PYTHONUTF8",
        "PYTHONIOENCODING",
        "PYTHONUNBUFFERED",
        "PYTHONSAFEPATH",
        "PYTHONLEGACYWINDOWSSTDIO",
        "PYTHONMALLOC",
        "VIRTUAL_ENV",
        "LD_LIBRARY_PATH",
        "LIBPATH",
        "DYLD_LIBRARY_PATH",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TZ",
    }
)
_CHILD_ARGV = ["backtester-design"]


def _child_environ(source) -> dict:
    kept = {}
    for key, value in source.items():
        upper = key.upper()
        if upper in _ENV_KEEP or upper.startswith("LC_"):
            kept[key] = value
    return kept


def _enter_child(job):
    """Scrub the book process before any strategy code runs.

    Returns the argv and cwd spawn copied in, so a test can see that the copy
    itself was already clean.
    """
    inherited_argv = list(sys.argv)
    inherited_cwd = os.getcwd()
    sys.argv = list(_CHILD_ARGV)
    os.chdir(job["cwd"])
    os.environ.clear()
    os.environ.update(job["env"])
    return inherited_argv, inherited_cwd


def _launch_book(ctx, queue, job):
    """Start one book. Spawn copies argv and cwd at start, so set them first.

    The parent directory, argv, and environment are restored before this
    returns, including when start fails.
    """
    cwd = tempfile.mkdtemp(prefix="bt-book-")
    job["cwd"] = cwd
    job["env"] = _child_environ(os.environ)
    proc = ctx.Process(target=_books_worker, args=(queue, job), name="walk-forward-book")
    old_argv = list(sys.argv)
    old_cwd = os.getcwd()
    old_env = dict(os.environ)
    sys.argv = list(_CHILD_ARGV)
    os.chdir(cwd)
    os.environ.clear()
    os.environ.update(job["env"])
    started = False
    try:
        proc.start()
        started = True
    finally:
        sys.argv = old_argv
        os.chdir(old_cwd)
        os.environ.clear()
        os.environ.update(old_env)
        if not started:
            shutil.rmtree(cwd, ignore_errors=True)
    return proc, cwd


def _set_dumpable(enabled: bool) -> int | None:
    """Set PR_SET_DUMPABLE on Linux. Returns the previous flag, or None elsewhere.

    A non-dumpable parent cannot be attached with ptrace, which is the cheap
    mitigation for a same-user /proc/<pid>/mem read. It is not a sandbox.
    Windows DACL work is out of scope for this phase.
    """
    if sys.platform != "linux":
        return None
    import ctypes

    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    previous = libc.prctl(3, 0, 0, 0, 0)
    if previous < 0:
        err = ctypes.get_errno()
        raise OSError(err, "prctl PR_GET_DUMPABLE failed")
    target = 1 if enabled else 0
    if previous != target:
        rc = libc.prctl(4, target, 0, 0, 0)
        if rc != 0:
            err = ctypes.get_errno()
            raise OSError(err, "prctl PR_SET_DUMPABLE failed")
    return int(previous)


def _restore_dumpable(previous: int | None) -> None:
    if previous is None:
        return
    _set_dumpable(bool(previous))


def _books_worker(queue, job):
    try:
        inherited_argv, inherited_cwd = _enter_child(job)
        gate = job.get("gate")
        if gate is not None:
            gate["started"].set()
            if not gate["release"].wait(timeout=180):
                raise RuntimeError("design parent did not release the walk-forward child")
        strategy_cls = _load_strategy(job["strategy"])
        books = run_books(strategy_cls, job["frame"], job["profile"], **job["kwargs"])
        _note_child(books, inherited_argv, inherited_cwd)
        queue.put(("ok", _picklable_books(books)))
    except BaseException:
        queue.put(("err", traceback.format_exc()))
    finally:
        queue.close()
        queue.join_thread()


def _note_child(books, inherited_argv, inherited_cwd) -> None:
    observer = books["strategy"][0].get("observer")
    if observer is None:
        return
    observer.inherited_argv = list(inherited_argv)
    observer.inherited_cwd = inherited_cwd


def _wait_until_started(proc, event, queue, timeout: float) -> None:
    import queue as queue_mod

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if event.wait(0.05):
            return
        if not proc.is_alive():
            proc.join(timeout=5)
            try:
                message = queue.get_nowait()
            except queue_mod.Empty:
                message = None
            if message is not None and message[0] == "err":
                raise RuntimeError(message[1])
            raise RuntimeError(f"walk-forward worker exited {proc.exitcode} before it was ready")
    raise RuntimeError("walk-forward worker did not start")


def _take_result(proc, queue, timeout: float):
    """Read the result, or raise as soon as the child exits without one.

    A dead child used to block on ``queue.get`` until the 600s timeout.
    The process sentinel and the queue are waited together.
    """
    import queue as queue_mod
    from multiprocessing.connection import wait

    deadline = time.monotonic() + timeout
    reader = queue._reader
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if proc.is_alive():
                proc.terminate()
            proc.join(timeout=30)
            raise RuntimeError(f"walk-forward worker timed out (exit {proc.exitcode})")
        ready = wait([reader, proc.sentinel], timeout=remaining)
        if reader in ready:
            try:
                return queue.get_nowait()
            except queue_mod.Empty:
                pass
        if proc.sentinel in ready or not proc.is_alive():
            proc.join(timeout=30)
            try:
                return queue.get_nowait()
            except queue_mod.Empty:
                raise RuntimeError(
                    f"walk-forward worker exited {proc.exitcode} without a result"
                ) from None


def _isolated_books(strategy_cls, frame, profile, *, during=None, **kwargs):
    """Run ``run_books`` in a fresh spawned interpreter.

    The child receives this frame, the strategy, and the run kwargs. Its argv
    is ``['backtester-design']``, its cwd is an empty temporary directory, and
    its environment is the allowlist in ``_child_environ``. ``during``, when
    given, runs in the parent after the child is up and before the child
    continues. A long-lived process is not reused.
    """
    import multiprocessing

    ctx = multiprocessing.get_context("spawn")
    result_queue = ctx.Queue()
    gate = None
    if during is not None:
        gate = {"started": ctx.Event(), "release": ctx.Event()}
    job = {
        "strategy": _strategy_spec(strategy_cls),
        "frame": _standalone_frame(frame),
        "profile": profile,
        "kwargs": _plain_kwargs(kwargs),
        "gate": gate,
    }
    proc = None
    cwd = None
    try:
        proc, cwd = _launch_book(ctx, result_queue, job)
        if gate is not None:
            _wait_until_started(proc, gate["started"], result_queue, 60)
            try:
                during()
            finally:
                gate["release"].set()
        message = _take_result(proc, result_queue, 600)
        proc.join(timeout=60)
        if proc.exitcode != 0:
            detail = "" if message[0] == "ok" else message[1]
            raise RuntimeError(f"walk-forward worker exited {proc.exitcode}\n{detail}")
        if message[0] == "ok":
            return message[1]
        raise RuntimeError(message[1])
    finally:
        if proc is not None and proc.is_alive():
            proc.terminate()
            proc.join(timeout=10)
        result_queue.close()
        result_queue.join_thread()
        if cwd is not None:
            shutil.rmtree(cwd, ignore_errors=True)


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
    anonymized: bool = False,
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
        anonymized=anonymized,
    )
    strategy = strategy_cls()
    result, metrics = execute(strategy, frame, profile, **common)
    books = {"strategy": (_tag_observer(result, strategy), metrics)}
    if with_benchmarks:
        for label, cls in _benchmarks(profile):
            bench = cls()
            bench_result, bench_metrics = execute(bench, frame, profile, **common)
            books[label] = (_tag_observer(bench_result, bench), bench_metrics)
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
    if not loaded.inspection.runnable:
        raise ValueError(
            f"{symbol} is {loaded.inspection.price_adjustment} and is not runnable. "
            "This phase executes raw prices only."
        )
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


@dataclass(frozen=True)
class _SliceMeta:
    design_end_day: int
    design_end_date: str
    locked_start_day: int
    locked_start_date: str
    warmup_days_requested: int
    warmup_sessions: int
    locked_day_max: int


def _keystream(key: bytes, nonce: bytes, nbytes: int) -> bytes:
    """HMAC-SHA256 keystream. ``key`` is the only secret; ``nonce`` is stored in the file."""
    chunks = []
    produced = 0
    counter = 0
    while produced < nbytes:
        block = hmac.new(key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest()
        chunks.append(block)
        produced += len(block)
        counter += 1
    return b"".join(chunks)[:nbytes]


def _xor(data: bytes, stream: bytes) -> bytes:
    import numpy as np

    left = np.frombuffer(data, dtype=np.uint8)
    right = np.frombuffer(stream, dtype=np.uint8)
    return np.bitwise_xor(left, right).tobytes()


def _seal(payload: bytes, key: bytes) -> bytes:
    """Encrypt ``payload``. The file holds a nonce and a MAC, not the key."""
    nonce = os.urandom(16)
    cipher = _xor(payload, _keystream(key, nonce, len(payload)))
    mac = hmac.new(key, b"BTLK" + nonce + cipher, hashlib.sha256).digest()
    return b"BTLK" + nonce + mac + cipher


def _open_sealed(blob: bytes, key: bytes) -> bytes:
    if len(blob) < 4 + 16 + 32 or blob[:4] != b"BTLK":
        raise ValueError("locked spill is not a sealed frame")
    nonce = blob[4:20]
    mac = blob[20:52]
    cipher = blob[52:]
    expected = hmac.new(key, b"BTLK" + nonce + cipher, hashlib.sha256).digest()
    if not hmac.compare_digest(mac, expected):
        raise ValueError("locked spill failed authentication")
    return _xor(cipher, _keystream(key, nonce, len(cipher)))


def _spill_frame(frame, key: bytes) -> str:
    """Write a sealed frame. The file is not a readable pickle, and ``key`` is not written."""
    payload = pickle.dumps(_standalone_frame(frame), protocol=pickle.HIGHEST_PROTOCOL)
    sealed = _seal(payload, key)
    del payload
    fd, path = tempfile.mkstemp(prefix="bt-locked-", suffix=".pkl")
    os.close(fd)
    with open(path, "wb") as handle:
        handle.write(sealed)
    return path


def _read_spilled(path: str, key: bytes):
    return pickle.loads(_open_sealed(Path(path).read_bytes(), key))


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
    inspection_notes=(),
    during_design=None,
):
    """Design run sees only design bars. The locked run may replay earlier bars as warm-up.

    The parent keeps the design slice and writes the locked window as a sealed
    blob. The key stays in this function: it is not put in the child job, the
    environment, argv, or any file. The caller's frame is dropped before the
    design child starts, and the locked window is opened only after that child
    has exited. Each book is a new spawned interpreter. See docs/ISOLATION.md.

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
    design = _standalone_frame(slices.design)
    meta = _SliceMeta(
        design_end_day=slices.design_end_day,
        design_end_date=slices.design_end_date,
        locked_start_day=slices.locked_start_day,
        locked_start_date=slices.locked_start_date,
        warmup_days_requested=slices.warmup_days_requested,
        warmup_sessions=slices.warmup_sessions,
        locked_day_max=int(slices.locked["day"].max()),
    )
    spill_key = os.urandom(32)
    spilled = _spill_frame(slices.locked_with_warmup, spill_key)
    del slices
    del frame
    gc.collect()
    gc.collect()
    common = dict(
        share_factor=share_factor,
        borrow_rate_annual=borrow_rate_annual,
        financing_rate_annual=financing_rate_annual,
        fee_mult=fee_mult,
        dividends=dividends,
        initial_equity=initial_equity,
        with_benchmarks=with_benchmarks,
    )
    notes = [str(note) for note in (inspection_notes or ())]
    previous_dumpable = _set_dumpable(False)
    try:
        try:
            design_books = _isolated_books(
                strategy_cls,
                design,
                profile,
                max_day=meta.design_end_day,
                warmup_sessions=0,
                during=during_design,
                **common,
            )
        finally:
            _restore_dumpable(previous_dumpable)
        locked_with = _standalone_frame(_read_spilled(spilled, spill_key))
        locked = _standalone_frame(
            locked_with.loc[locked_with["day"] >= meta.locked_start_day].reset_index(drop=True)
        )
        rebuilt = WalkSlices(
            design_end_day=meta.design_end_day,
            design_end_date=meta.design_end_date,
            locked_start_day=meta.locked_start_day,
            locked_start_date=meta.locked_start_date,
            warmup_days_requested=meta.warmup_days_requested,
            warmup_sessions=meta.warmup_sessions,
            design=design,
            locked=locked,
            locked_with_warmup=locked_with,
        )
        if profile == "arena":
            locked_books = _isolated_books(
                strategy_cls,
                rebuilt.locked_with_warmup,
                profile,
                reset_day=meta.locked_start_day if meta.warmup_sessions else None,
                max_day=meta.locked_day_max,
                warmup_sessions=0,
                **common,
            )
        elif profile == "desk":
            locked_books = _isolated_books(
                strategy_cls,
                rebuilt.locked_with_warmup,
                profile,
                warmup_sessions=meta.warmup_sessions,
                max_day=meta.locked_day_max,
                **common,
            )
        else:
            raise ValueError(profile)
    finally:
        os.unlink(spilled)
    out = Path(out_dir)
    design_payload = write_books(out / "design", profile, symbol, bar_minutes, rebuilt.design, design_books)
    locked_payload = write_books(
        out / "locked", profile, symbol, bar_minutes, rebuilt.locked_with_warmup, locked_books
    )
    summary = {
        "profile": profile,
        "symbol": symbol,
        "bar_minutes": bar_minutes,
        "design_end_day": rebuilt.design_end_day,
        "design_end_date": rebuilt.design_end_date,
        "locked_start_day": rebuilt.locked_start_day,
        "locked_start_date": rebuilt.locked_start_date,
        "warmup_days_requested": rebuilt.warmup_days_requested,
        "warmup_sessions": rebuilt.warmup_sessions,
        "design_days_max": int(rebuilt.design["day"].max()),
        "locked_days_min": int(rebuilt.locked["day"].min()),
        "design_sessions": int(rebuilt.design["day"].nunique()),
        "locked_sessions": int(rebuilt.locked["day"].nunique()),
        "warmup_rule": get_profile(profile).warmup,
        "inspection_notes": notes,
        "design": _summary(profile, design_payload),
        "locked": _summary(profile, locked_payload),
    }
    write_json(out / "walkforward.json", summary)
    return summary, rebuilt, design_books, locked_books


def run_pack(
    strategy_cls,
    pack_dir,
    sidecar_path,
    profile: str,
    out_dir,
    *,
    borrow_rate_annual: float | None = None,
    financing_rate_annual: float | None = None,
    fee_mult: float = 1.0,
    dividends: dict | None = None,
    warmup_sessions: int = 0,
    initial_equity: float = 1_000_000.0,
    with_benchmarks: bool = True,
    during=None,
):
    """Run a pack. The sidecar stays in this process.

    An anonymized pack's share_scale is the price factor k. It is the only
    sidecar field handed to the strategy process, and only as the engine's
    share_factor. The date map, ticker, volume factor, and operator note are
    not part of that job.
    """
    from .pack import load_pack, load_sidecar

    loaded = load_pack(pack_dir)
    secret = load_sidecar(sidecar_path)
    if secret["pack_id"] != loaded.pack_id:
        raise ValueError("sidecar does not match this pack")
    if bool(secret["anonymized"]) != loaded.anonymized:
        raise ValueError("sidecar anonymized flag does not match the pack")
    anonymized = bool(secret["anonymized"])
    share_scale = float(secret["share_scale"])
    symbol = str(secret["symbol"])
    if anonymized and dividends:
        raise ValueError(
            "An anonymized pack cannot take a raw dividend file. "
            "Dividend cash is shares times amount, and pack share counts are rescaled. "
            "Refusing to apply an unscaled dividend."
        )
    if borrow_rate_annual is None and secret["borrow_rate_source"] == "explicit":
        borrow_rate_annual = float(secret["borrow_rate_annual"])
    # The rest of the sidecar (date map, ticker, volume factor, note) is not
    # copied into the child job. Drop the document before the spawn.
    del secret
    gc.collect()
    books = _isolated_books(
        strategy_cls,
        loaded.frame,
        profile,
        share_factor=share_scale,
        borrow_rate_annual=borrow_rate_annual,
        financing_rate_annual=financing_rate_annual,
        fee_mult=fee_mult,
        dividends=dividends,
        warmup_sessions=warmup_sessions,
        initial_equity=initial_equity,
        with_benchmarks=with_benchmarks,
        anonymized=anonymized,
        during=during,
    )
    payload = write_books(
        out_dir,
        get_profile(profile).name,
        symbol,
        loaded.bar_minutes,
        loaded.frame,
        books,
        extra={
            "warnings": [],
            "pack_id": loaded.pack_id,
            "anonymized": anonymized,
        },
    )
    # Not written to metrics.json. A test reads what the child strategy observed.
    observer = books["strategy"][0].get("observer")
    if observer is not None:
        payload["strategy_observer"] = observer
    return payload


def run_parallel(*_args, **_kwargs):
    raise NotImplementedError(
        "Parallel scoring is a later phase. Each worker will be its own process, "
        "one strategy and one symbol at a time."
    )
