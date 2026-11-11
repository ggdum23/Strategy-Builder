"""Run one strategy, or a design window and a locked tail.

Benchmarks go through the same engine and the same cost configuration as the
strategy. Arena financing stays at zero. Desk financing stays at the desk rate
(7% unless the run overrides it).

Phase 1 walk-forward isolation is the heap, class attributes, module globals,
the environment, argv, and cwd. It is not an OS jail. See docs/ISOLATION.md.
"""

from __future__ import annotations

import errno
import gc
import hashlib
import hmac
import math
import os
import pickle
import shutil
import signal
import sys
import tempfile
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path

from . import engine_arena, engine_desk
from .data import WalkSlices, frame_to_desk_bars, load_named, slice_walkforward
from .engine_desk import BuyAndHold as DeskBuyAndHold
from .engine_desk import DeskConfig, TwoXLong
from .profiles import get_profile
from .report import enrich_arena_metrics, publish_designer, write_book, write_json

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


def _release(holder: list):
    """Hand the billing value to the callee and leave this frame without it.

    The engine still has the scale for the duration of ``on_bar``. The runner
    frames above that call do not keep a local named ``share_scale`` or
    ``share_factor``. Post-fee cash still determines k. See docs/ISOLATION.md.
    """
    if len(holder) != 1:
        raise RuntimeError("billing handoff is empty")
    return holder.pop()


def _report_inside_pack(pack_dir, out_dir) -> bool:
    """True when the resolved report path is the pack or a path inside it."""
    pack = Path(pack_dir).resolve()
    out = Path(out_dir).resolve()
    try:
        out.relative_to(pack)
    except ValueError:
        return False
    return True


def _pack_dividends(dividends, *, anonymized: bool, share_scale: float, date_map: dict):
    """Real $/real-share amounts become the dict the child engine is given.

    An anonymized pack's session id is ``S#####``. The calendar date and the
    unscaled amount stay in this process.
    """
    if not dividends:
        return None
    if not anonymized:
        return {str(key): float(value) for key, value in dividends.items()}
    by_date = {}
    for ordinal, date in date_map.items():
        day = str(date)
        if day in by_date:
            raise ValueError(f"sidecar date map repeats {day}")
        by_date[day] = str(ordinal)
    scaled = {}
    for key, amount in dividends.items():
        day = str(key)
        if day not in by_date:
            raise ValueError(f"dividend date {day} is not in this pack")
        session_id = f"S{int(by_date[day]):05d}"
        value = float(amount) * float(share_scale)
        if not math.isfinite(value):
            raise ValueError(f"dividend for {day} is not finite")
        if session_id in scaled:
            raise ValueError(f"dividend date {day} maps to {session_id} twice")
        scaled[session_id] = value
    return scaled


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
    # Drop the parameter name before on_bar. The engine call receives the value.
    billing = [share_factor]
    del share_factor
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
        result = engine_arena.run(
            strategy,
            frame,
            initial_equity=initial_equity,
            share_scale=_release(billing),
            reset_day=effective_reset,
            borrow_rate_annual=borrow,
        )
        metrics = enrich_arena_metrics(result, engine_arena.metrics(result))
        scale = float(result["share_scale"])
        metrics["share_factor"] = scale
        metrics["borrow_rate_annual"] = borrow
        metrics["financing_rate_annual"] = 0.0
        metrics["cost_basis"] = _cost_basis(name, scale, anonymized=anonymized)
        return result, metrics
    if name == "desk":
        rate = engine_desk.D.FINANCING_RATE_ANNUAL if financing_rate_annual is None else financing_rate_annual
        borrow = get_profile(name).borrow_rate_annual if borrow_rate_annual is None else borrow_rate_annual
        rows = frame_to_desk_bars(frame)
        result = engine_desk.run(
            strategy,
            rows,
            DeskConfig(
                initial_equity=initial_equity,
                share_factor=_release(billing),
                borrow_rate_annual=borrow,
                financing_rate_annual=rate,
                fee_mult=fee_mult,
                live_start_ordinal=warmup_sessions,
                dividends=dict(dividends or {}),
            ),
        )
        metrics = engine_desk.metrics(result)
        metrics["cost_basis"] = _cost_basis(name, metrics["share_factor"], anonymized=anonymized)
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


class _ObserverView:
    """Picklable copy of a strategy instance loaded from a file.

    The child imports that file under a generated module name. The parent has
    not imported it, so the instance itself cannot cross the queue. The
    attributes can. A class the parent already imported is left as it is.
    """

    def __init__(self, values: dict):
        self.__dict__.update(values)


def _observer_view(observer):
    module = getattr(type(observer), "__module__", "")
    if not str(module).startswith("backtester_user_strategy_"):
        return observer
    values = {}
    for key, value in getattr(observer, "__dict__", {}).items():
        try:
            pickle.dumps(value)
        except Exception:
            continue
        values[key] = value
    return _ObserverView(values)


def _picklable_books(books):
    """Send books the parent can unpickle. A file strategy comes back as data."""
    portable = {}
    for label, (result, metrics) in books.items():
        plain = dict(result)
        if plain.get("observer") is not None:
            plain["observer"] = _observer_view(plain["observer"])
        try:
            pickle.dumps((plain, metrics))
        except Exception:
            plain.pop("observer", None)
            pickle.dumps((plain, metrics))
        portable[label] = (plain, metrics)
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
# Spawn copies the parent argv, cwd, and environment at Process.start.
# Two books starting together must not interleave that scrub.
_LAUNCH_LOCK = threading.Lock()
# The child keeps at most this much of what a strategy prints. The rest is
# discarded so a large print cannot fill the pipe the parent is reading, and
# cannot fill the disk. The tail attached to an operator error is shorter.
_STDIO_CAP_BYTES = 256 * 1024
_STDIO_NAME = "stdio.log"


class WorkerFailure(RuntimeError):
    """A book process failed. ``tail`` is its own stdout/stderr, not a path."""

    def __init__(self, message: str, *, tail: str = ""):
        super().__init__(message)
        self.tail = tail or ""


class _StdioCapture:
    """Redirect fd 1 and fd 2 into a capped file in the worker's directory.

    The redirect is on the descriptors, so a C extension and a subprocess see
    it. The reader keeps draining after the file is full, which is what stops
    a large print from blocking the strategy.
    """

    def __init__(self, directory: str, limit: int = _STDIO_CAP_BYTES):
        self.path = Path(directory) / _STDIO_NAME
        self.limit = int(limit)
        self._stored = 0
        self._ring = bytearray()
        self._ring_limit = 4096
        self._lock = threading.Lock()
        self._file = open(self.path, "wb")
        read_fd, write_fd = os.pipe()
        try:
            os.set_inheritable(write_fd, True)
        except OSError:
            pass
        try:
            sys.stdout.flush()
        except Exception:
            pass
        try:
            sys.stderr.flush()
        except Exception:
            pass
        os.dup2(write_fd, 1)
        os.dup2(write_fd, 2)
        os.close(write_fd)
        self._read = read_fd
        self._thread = threading.Thread(target=self._drain, name="bt-stdio", daemon=True)
        self._thread.start()

    def _drain(self) -> None:
        while True:
            try:
                chunk = os.read(self._read, 65536)
            except OSError:
                break
            if not chunk:
                break
            with self._lock:
                self._ring.extend(chunk)
                extra = len(self._ring) - self._ring_limit
                if extra > 0:
                    del self._ring[:extra]
                room = self.limit - self._stored
                if room > 0:
                    piece = chunk[:room]
                    self._file.write(piece)
                    self._stored += len(piece)
                    self._file.flush()

    def tail(self, limit: int = 2000) -> str:
        try:
            sys.stdout.flush()
        except Exception:
            pass
        try:
            sys.stderr.flush()
        except Exception:
            pass
        deadline = time.monotonic() + 0.5
        previous = -1
        stable = 0
        while time.monotonic() < deadline and stable < 2:
            with self._lock:
                size = len(self._ring)
            if size == previous:
                stable += 1
            else:
                stable = 0
                previous = size
            time.sleep(0.02)
        with self._lock:
            data = bytes(self._ring[-limit:])
        return data.decode("utf-8", "replace").replace("\x00", "")


def _new_session() -> None:
    """Make this worker a session leader so a group signal reaches its children.

    Windows has no ``setsid``. The parent puts that worker in a job object
    instead, before it releases the gate.
    """
    if os.name == "nt":
        return
    os.setsid()


def _drop_directory(entries, banned: str) -> list:
    """Remove one directory from a path list. Install paths stay."""
    if not banned:
        return list(entries)
    try:
        target = os.path.normcase(os.path.abspath(banned))
    except OSError:
        return list(entries)
    kept = []
    for entry in entries:
        if not entry:
            kept.append(entry)
            continue
        try:
            resolved = os.path.normcase(os.path.abspath(entry))
        except OSError:
            kept.append(entry)
            continue
        if resolved == target:
            continue
        kept.append(entry)
    return kept


def _child_environ(source) -> dict:
    kept = {}
    for key, value in source.items():
        upper = key.upper()
        if upper in _ENV_KEEP or upper.startswith("LC_"):
            kept[key] = value
    return kept


def _pythonpath_to_drop(source) -> list[str]:
    """PYTHONPATH entries the child must not keep on ``sys.path``.

    The variable itself is not inherited. An entry that is the installed
    package root stays, because an editable or ``PYTHONPATH=src`` layout
    imports ``backtester`` from there. Every other entry is removed, including
    one under the operator's directory that holds another job's report.
    """
    raw = source.get("PYTHONPATH", "")
    if not raw:
        return []
    package_root = os.path.normcase(str(Path(__file__).resolve().parent.parent))
    dropped = []
    for part in raw.split(os.pathsep):
        if not part:
            continue
        try:
            resolved = os.path.normcase(os.path.abspath(part))
        except OSError:
            continue
        if resolved == package_root and os.path.isfile(
            os.path.join(resolved, "backtester", "__init__.py")
        ):
            continue
        dropped.append(resolved)
    return dropped


def _enter_child(job):
    """Scrub the book process before any strategy code runs.

    Returns the argv and cwd spawn copied in, so a test can see that the copy
    itself was already clean. The scrub runs again here even when spawn already
    copied a clean argv and environment: a child that inherited the operator
    process still drops them.

    ``multiprocessing.process.ORIGINAL_DIR`` is the operator's directory from
    when this interpreter first imported multiprocessing, and spawn puts that
    directory on ``sys.path``. After the working directory is the empty temp
    dir, both point there instead. Install paths stay on ``sys.path``.
    ``PYTHONPATH`` is not in the child environment. Entries from the parent's
    ``PYTHONPATH`` that are not the installed package are removed here too,
    because spawn already copied them onto ``sys.path``.
    """
    inherited_argv = list(sys.argv)
    inherited_cwd = os.getcwd()
    import multiprocessing.process as mp_process

    operator_dir = mp_process.ORIGINAL_DIR or inherited_cwd
    sys.argv = list(_CHILD_ARGV)
    os.chdir(job["cwd"])
    os.environ.clear()
    os.environ.update(job["env"])
    mp_process.ORIGINAL_DIR = os.path.abspath(os.getcwd())
    if operator_dir and os.path.normcase(os.path.abspath(operator_dir)) != os.path.normcase(
        os.getcwd()
    ):
        sys.path[:] = _drop_directory(sys.path, operator_dir)
    for banned in job.get("drop_sys_path") or []:
        sys.path[:] = _drop_directory(sys.path, banned)
    return inherited_argv, inherited_cwd


def _launch_book(ctx, queue, job):
    """Start one book. Spawn copies argv and cwd at start, so set them first.

    The parent directory, argv, and environment are restored before this
    returns, including when start fails. The scrub is serialized: a second
    book must not snapshot the environment while it is cleared.
    """
    cwd = tempfile.mkdtemp(prefix="bt-book-")
    proc = ctx.Process(target=_books_worker, args=(queue, job), name="walk-forward-book")
    with _LAUNCH_LOCK:
        job["cwd"] = cwd
        job["drop_sys_path"] = _pythonpath_to_drop(os.environ)
        job["env"] = _child_environ(os.environ)
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
    _new_session()
    spool = None
    try:
        inherited_argv, inherited_cwd = _enter_child(job)
        spool = _StdioCapture(job["cwd"])
        gate = job.get("gate")
        if gate is not None:
            gate["started"].set()
            if not gate["release"].wait(timeout=180):
                raise RuntimeError("design parent did not release the walk-forward child")
        strategy_cls = _load_strategy(job["strategy"])
        frame = job["frame"]
        profile = job["profile"]
        kwargs = job["kwargs"]
        job.clear()
        books = run_books(
            strategy_cls,
            frame,
            profile,
            share_factor=kwargs.pop("share_factor"),
            **kwargs,
        )
        _note_child(books, inherited_argv, inherited_cwd)
        queue.put(("ok", _picklable_books(books)))
    except BaseException:
        tail = ""
        if spool is not None:
            try:
                tail = spool.tail()
            except Exception:
                tail = ""
        queue.put(("err", traceback.format_exc(), tail))
    finally:
        queue.close()
        queue.join_thread()


def _note_child(books, inherited_argv, inherited_cwd) -> None:
    observer = books["strategy"][0].get("observer")
    if observer is None:
        return
    observer.inherited_argv = list(inherited_argv)
    observer.inherited_cwd = inherited_cwd


def _kill_group(pgid: int, sig: int) -> None:
    """Signal a saved process group. The leader may already have been reaped.

    ``os.getpgid`` fails with ``ESRCH`` once the leader is gone, so the caller
    passes the id it recorded when the worker became the session leader. A
    missing group is the process already being dead, not an error.
    """
    try:
        os.killpg(int(pgid), sig)
    except ProcessLookupError:
        return
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return
        raise


def _stop_process(proc, job=None, pgid=None) -> None:
    """End a worker and the processes it started.

    ``pgid`` is the worker pid recorded after ``setsid``. Linux signals that
    group with SIGTERM and then SIGKILL even when the leader has already
    exited, so a grandchild is not left with ``ppid`` 1. SIGKILL is not
    conditional on the leader still being alive: a child that ignores SIGTERM
    would otherwise survive. Windows terminates the job object on this path,
    including a normal return. Both waits are bounded.
    """
    if job is not None:
        try:
            job.terminate()
        except OSError:
            pass
    if pgid and os.name != "nt":
        _kill_group(int(pgid), signal.SIGTERM)
    elif proc is not None and proc.is_alive():
        try:
            proc.terminate()
        except OSError:
            pass
    if proc is not None:
        proc.join(timeout=2)
    if pgid and os.name != "nt":
        _kill_group(int(pgid), signal.SIGKILL)
    elif proc is not None and proc.is_alive():
        try:
            proc.kill()
        except OSError:
            pass
    if proc is not None:
        proc.join(timeout=5)


def _cancelled(stop) -> bool:
    if stop is None:
        return False
    fn = getattr(stop, "cancelled", None)
    if fn is None:
        return False
    return bool(fn())


def _wait_until_started(proc, event, queue, timeout: float, stop=None, job=None, pgid=None) -> None:
    import queue as queue_mod

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _cancelled(stop):
            _stop_process(proc, job, pgid)
            raise RuntimeError("walk-forward worker cancelled")
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


def _take_result(proc, queue, timeout: float, *, memory_bytes: int | None = None, stop=None, job=None, pgid=None):
    """Read the result, or raise as soon as the child exits without one.

    A dead child used to block on ``queue.get`` until the 600s timeout.
    The process sentinel and the queue are waited together. ``memory_bytes``
    and ``stop`` are watched in this process. They are not sent to the child.
    """
    import queue as queue_mod
    from multiprocessing.connection import wait

    from .limits import process_tree_rss_bytes

    deadline = time.monotonic() + timeout
    reader = queue._reader
    while True:
        if wait([reader], timeout=0):
            try:
                return queue.get_nowait()
            except queue_mod.Empty:
                pass
        if not proc.is_alive():
            proc.join(timeout=30)
            try:
                return queue.get_nowait()
            except queue_mod.Empty:
                raise RuntimeError(
                    f"walk-forward worker exited {proc.exitcode} without a result"
                ) from None
        if _cancelled(stop):
            _stop_process(proc, job, pgid)
            raise RuntimeError("walk-forward worker cancelled")
        if memory_bytes is not None:
            rss = process_tree_rss_bytes(proc.pid)
            if rss is not None and rss > int(memory_bytes):
                _stop_process(proc, job, pgid)
                raise RuntimeError(
                    f"walk-forward worker exceeded memory limit ({rss} > {int(memory_bytes)})"
                )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _stop_process(proc, job, pgid)
            raise RuntimeError(f"walk-forward worker timed out (exit {proc.exitcode})")
        wait([reader, proc.sentinel], timeout=min(0.2, remaining))


def _metric_delta(child, parent) -> list[str]:
    """Top-level keys whose JSON forms differ. Empty means the books agree."""
    from .report import json_ready

    left = json_ready(child)
    right = json_ready(parent)
    if not isinstance(left, dict) or not isinstance(right, dict):
        return ["metrics"]
    bad = []
    for key in sorted(set(left) | set(right)):
        if left.get(key) != right.get(key):
            bad.append(str(key))
    return bad


def _metrics_from_result(profile: str, result: dict, *, anonymized: bool, borrow_rate_annual):
    """The statistics ``execute`` would attach, computed in this process."""
    name = get_profile(profile).name
    if name == "arena":
        metrics = enrich_arena_metrics(result, engine_arena.metrics(result))
        scale = float(result["share_scale"])
        borrow = get_profile(name).borrow_rate_annual if borrow_rate_annual is None else borrow_rate_annual
        metrics["share_factor"] = scale
        metrics["borrow_rate_annual"] = borrow
        metrics["financing_rate_annual"] = 0.0
        metrics["cost_basis"] = _cost_basis(name, scale, anonymized=anonymized)
        return metrics
    if name == "desk":
        metrics = engine_desk.metrics(result)
        metrics["cost_basis"] = _cost_basis(name, float(metrics["share_factor"]), anonymized=anonymized)
        return metrics
    raise ValueError(f"unknown profile {profile}")


def _accept_child_books(profile: str, frame, child_books: dict, kwargs: dict) -> dict:
    """Recompute statistics here. A child that rewrote them fails the book.

    The strategy's bars ran in the child, so a patched cost constant can still
    change that book's fills. The numbers published for the book are computed
    again from those fills in this process. Benchmarks are run again here, on
    this process's copy of the engine. A mismatch fails the row instead of
    publishing the child's copy. See docs/ISOLATION.md.
    """
    if "strategy" not in child_books:
        raise RuntimeError("strategy process returned no strategy book")
    strategy_result, child_metrics = child_books["strategy"]
    anonymized = bool(kwargs.get("anonymized", False))
    parent_metrics = _metrics_from_result(
        profile,
        strategy_result,
        anonymized=anonymized,
        borrow_rate_annual=kwargs.get("borrow_rate_annual"),
    )
    delta = _metric_delta(child_metrics, parent_metrics)
    if delta:
        shown = ", ".join(delta[:12])
        raise RuntimeError(f"strategy metrics do not match the parent recomputation ({shown})")
    books = {"strategy": (strategy_result, parent_metrics)}
    if not kwargs.get("with_benchmarks", True):
        return books
    common = dict(
        borrow_rate_annual=kwargs.get("borrow_rate_annual"),
        financing_rate_annual=kwargs.get("financing_rate_annual"),
        fee_mult=kwargs.get("fee_mult", 1.0),
        dividends=kwargs.get("dividends"),
        warmup_sessions=kwargs.get("warmup_sessions", 0),
        initial_equity=kwargs.get("initial_equity", 1_000_000.0),
        reset_day=kwargs.get("reset_day"),
        max_day=kwargs.get("max_day"),
        anonymized=anonymized,
    )
    billing = [float(kwargs.get("share_factor", 1.0))]
    for label, cls in _benchmarks(get_profile(profile).name):
        bench_result, bench_metrics = execute(
            cls(), frame, profile, share_factor=_release(billing), **common
        )
        billing.append(bench_metrics["share_factor"])
        child = child_books.get(label)
        if child is None:
            raise RuntimeError(f"{label} benchmark was not returned by the strategy process")
        delta = _metric_delta(child[1], bench_metrics)
        if delta:
            shown = ", ".join(delta[:12])
            raise RuntimeError(f"{label} benchmark does not match the parent recomputation ({shown})")
        books[label] = (bench_result, bench_metrics)
    return books


def _failure_from_message(message) -> WorkerFailure:
    detail = message[1] if len(message) > 1 else ""
    tail = message[2] if len(message) > 2 and isinstance(message[2], str) else ""
    return WorkerFailure(str(detail), tail=tail)


def _isolated_books(
    strategy_cls,
    frame,
    profile,
    *,
    during=None,
    timeout=600,
    memory_bytes=None,
    stop=None,
    strategy_path=None,
    **kwargs,
):
    """Run ``run_books`` in a fresh spawned interpreter.

    The child receives this frame, the strategy, and the run kwargs. Its argv
    is ``['backtester-design']``, its cwd is an empty temporary directory, and
    its environment is the allowlist in ``_child_environ``. ``during``, when
    given, runs in the parent after the child is up and before the child
    continues. A long-lived process is not reused.

    ``timeout`` starts after the child has entered, so interpreter startup is
    not part of the strategy budget. ``memory_bytes`` and ``stop`` stay in
    the parent. ``strategy_path``, when set, is loaded in the child so an
    import error belongs to that strategy.

    The gate is always used. On Windows the job object is armed before the
    gate opens, so a process the strategy starts is in that job. If that
    assignment fails, the run fails instead of falling back to signaling only
    the worker pid. On Linux the worker pid is the process-group id once the
    child has entered, and every exit path signals that group. Statistics
    and benchmarks are recomputed in this process from the child's result.
    """
    import multiprocessing

    from .limits import arm_worker_job

    if strategy_path is not None:
        spec = {"kind": "path", "path": str(strategy_path)}
    elif strategy_cls is None:
        raise ValueError("strategy is missing")
    else:
        spec = _strategy_spec(strategy_cls)
    ctx = multiprocessing.get_context("spawn")
    result_queue = ctx.Queue()
    gate = {"started": ctx.Event(), "release": ctx.Event()}
    job = {
        "strategy": spec,
        "frame": _standalone_frame(frame),
        "profile": profile,
        "kwargs": _plain_kwargs(kwargs),
        "gate": gate,
    }
    proc = None
    cwd = None
    guard = None
    pgid = None
    try:
        proc, cwd = _launch_book(ctx, result_queue, job)
        guard = arm_worker_job(proc.pid, None if memory_bytes is None else int(memory_bytes))
        if os.name == "nt" and guard is None:
            raise RuntimeError("the worker could not be placed in a job object")
        _wait_until_started(proc, gate["started"], result_queue, 60, stop, guard)
        # setsid has run: the started event is set after it. The group id is
        # the worker pid. Keep it after the leader is reaped; getpgid cannot.
        if os.name != "nt" and proc.pid:
            pgid = int(proc.pid)
        try:
            if during is not None:
                during()
        finally:
            gate["release"].set()
        message = _take_result(
            proc,
            result_queue,
            timeout,
            memory_bytes=memory_bytes,
            stop=stop,
            job=guard,
            pgid=pgid,
        )
        proc.join(timeout=60)
        if message[0] != "ok":
            failure = _failure_from_message(message)
            if proc.exitcode not in (0, None):
                raise WorkerFailure(
                    f"walk-forward worker exited {proc.exitcode}\n{failure}",
                    tail=failure.tail,
                )
            raise failure
        if proc.exitcode not in (0, None):
            raise RuntimeError(f"walk-forward worker exited {proc.exitcode}\n")
        return _accept_child_books(profile, _standalone_frame(frame), message[1], kwargs)
    finally:
        # Success, a strategy exception, and a parent-side failure all come
        # through here. The group is signaled even when the worker has exited.
        _stop_process(proc, guard, pgid)
        if guard is not None:
            guard.close()
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
    billing = [share_factor]
    del share_factor
    common = dict(
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
    result, metrics = execute(strategy, frame, profile, share_factor=_release(billing), **common)
    billing.append(metrics["share_factor"])
    books = {"strategy": (_tag_observer(result, strategy), metrics)}
    if with_benchmarks:
        for label, cls in _benchmarks(profile):
            bench = cls()
            bench_result, bench_metrics = execute(
                bench, frame, profile, share_factor=_release(billing), **common
            )
            billing.append(bench_metrics["share_factor"])
            books[label] = (_tag_observer(bench_result, bench), bench_metrics)
    return books


def write_books(
    out_dir,
    profile: str,
    symbol: str,
    bar_minutes: int,
    frame,
    books,
    *,
    extra: dict | None = None,
    anonymized: bool = False,
) -> dict:
    """Write the strategy curves at the top of ``out_dir`` and benchmarks underneath.

    An anonymized run also writes ``designer/``. That tree is the copy a
    strategy designer may see. It has no ticker, no calendar date, and no
    scale factor. The files next to it are the operator copy and may have those.
    """
    if anonymized:
        from .report import require_anonymizable_symbol

        require_anonymizable_symbol(symbol)
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
    if anonymized:
        publish_designer(out, profile, frame, books, payload, symbol=symbol)
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
    if share_factor is not None and float(share_factor) != 1.0:
        raise ValueError(
            f"share_factor {share_factor} is not 1.0. Raw runs use 1.0. "
            "0.9 is not a kit constant."
        )
    factor = loaded.inspection.share_factor if share_factor is None else float(share_factor)
    if factor != 1.0:
        raise ValueError(
            f"share_factor {factor} is not 1.0. Raw runs use 1.0. "
            "0.9 is not a kit constant."
        )
    warnings = []
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

    The books written here are operator books. They keep the original ticker.
    There is no designer tree, so a symbol that is reserved for anonymized
    reports is allowed.
    """
    if share_factor != 1.0:
        raise ValueError(
            f"share_factor {share_factor} is not 1.0. Raw runs use 1.0. "
            "0.9 is not a kit constant."
        )
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

    An anonymized pack's share_scale is the price factor k. The child engine
    receives that number so per-share fees use real share counts. It does not
    receive the sidecar document, the date map, the ticker, the volume factor,
    or the operator note. Dividend amounts are scaled and re-keyed here first.
    """
    from .pack import load_pack, load_sidecar

    if _report_inside_pack(pack_dir, out_dir):
        raise ValueError("the report must not live inside the pack directory")
    loaded = load_pack(pack_dir)
    secret = load_sidecar(sidecar_path)
    if secret["pack_id"] != loaded.pack_id:
        raise ValueError("sidecar does not match this pack")
    if bool(secret["anonymized"]) != loaded.anonymized:
        raise ValueError("sidecar anonymized flag does not match the pack")
    anonymized = bool(secret["anonymized"])
    share_scale = float(secret["share_scale"])
    symbol = str(secret["symbol"])
    if anonymized:
        from .report import require_anonymizable_symbol

        require_anonymizable_symbol(symbol)
    child_dividends = _pack_dividends(
        dividends,
        anonymized=anonymized,
        share_scale=share_scale,
        date_map=secret["date_map"],
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
        dividends=child_dividends,
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
        anonymized=anonymized,
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


def run_parallel(*args, **kwargs):
    """Score many strategies. See ``backtester.parallel.run_parallel``."""
    from .parallel import run_parallel as _run

    return _run(*args, **kwargs)
