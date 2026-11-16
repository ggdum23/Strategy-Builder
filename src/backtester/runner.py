
from __future__ import annotations

import errno
import gc
import hashlib
import inspect
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
from .accounting import normalize_price_basis, price_basis_refusal, resolve_accounting, stamp_accounting
from .data import WalkSlices, load_named, prepare_desk_bars, slice_walkforward
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
    "cash_interest",
    "financing_cost",
    "dividends_net",
    "percent_up_days",
    "cost_basis",
    "price_basis",
    "total_return_method",
    "series_kind",
    "cash_rate_annual",
    "financing_rate_annual",
    "withholding_rate",
    "accrual",
    "sessions_per_year",
    "dividends_applied",
)

_DESK_KEYS = _ARENA_KEYS + (
    "margin_calls_queued",
    "margin_calls_filled",
    "max_gross_leverage",
    "avg_gross_leverage",
    "worst_equity_over_hwm",
    "disqualified",
    "borrow_rate_annual",
    "share_factor",
    "fee_mult",
)


def _cost_basis(profile: str, share_factor: float, *, anonymized: bool) -> dict:
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
    if len(holder) != 1:
        raise RuntimeError("billing handoff is empty")
    return holder.pop()


def _report_inside_pack(pack_dir, out_dir) -> bool:
    pack = Path(pack_dir).resolve()
    out = Path(out_dir).resolve()
    try:
        out.relative_to(pack)
    except ValueError:
        return False
    return True


def _pack_dividends(dividends, *, anonymized: bool, share_scale: float, date_map: dict):
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


def _designer_dividends(dividends, date_map, price_factor: float):
    from .designer_run import session_dividends

    if not dividends:
        return session_dividends(dividends, date_map)
    restated = {str(key): float(amount) * float(price_factor) for key, amount in dividends.items()}
    return session_dividends(restated, date_map)


def _record_scale_override(books, *, price_factor: float, share_scale: float) -> None:
    if float(price_factor) == float(share_scale):
        return
    note = (
        f"share_scale={share_scale} differs from the build-time price factor {price_factor}. "
        "Per-share fees are not real share counts."
    )
    for _result, metrics in books.values():
        basis = metrics.get("cost_basis")
        if not isinstance(basis, dict):
            continue
        basis["price_factor"] = float(price_factor)
        basis["per_share_fees"] = "billing_override"
        basis["note"] = note


def _summary(profile: str, metrics: dict) -> dict:
    keys = _DESK_KEYS if profile == "desk" else _ARENA_KEYS
    return {key: metrics.get(key) for key in keys}


def _dividends_for_frame(frame, dividends):
    if not dividends:
        return dividends
    sessions = set()
    if "date" in frame.columns:
        sessions.update(str(value) for value in frame["date"].tolist())
    if "day" in frame.columns:
        sessions.update(str(int(value)) for value in frame["day"].tolist())
    kept = {}
    for key, amount in dividends.items():
        text = str(key)
        if text in sessions:
            kept[key] = amount
            continue
        try:
            day = int(text)
        except (TypeError, ValueError):
            continue
        if str(day) in sessions:
            kept[key] = amount
    return kept


def _arena_dividend_days(frame, dividends) -> dict:
    if not dividends:
        return {}
    if "date" not in frame.columns:
        raise ValueError("dividend mapping needs a date column")
    by_date = {}
    present = set()
    for date, day in zip(frame["date"].tolist(), frame["day"].tolist()):
        day_i = int(day)
        present.add(day_i)
        key = str(date)
        previous = by_date.get(key)
        if previous is not None and previous != day_i:
            raise ValueError(f"dividend date {key} maps to two sessions")
        by_date[key] = day_i
    out = {}
    for key, amount in dividends.items():
        text = str(key)
        value = float(amount)
        if not math.isfinite(value):
            raise ValueError(f"dividend for {text} is not finite")
        if text in by_date:
            day = by_date[text]
        else:
            try:
                day = int(text)
            except ValueError as exc:
                raise ValueError(f"dividend date {text} is not in this sample") from exc
            if day not in present:
                raise ValueError(f"dividend date {text} is not in this sample")
        if day in out:
            raise ValueError(f"dividend date {text} maps to session {day} twice")
        out[day] = value
    return out


def _bound_accounting(
    profile: str,
    *,
    price_basis,
    series_kind,
    cash_rate_annual,
    financing_rate_annual,
    withholding_rate,
    dividends,
):
    name = get_profile(profile).name
    if name == "desk" and financing_rate_annual is None:
        financing_rate_annual = engine_desk.D.FINANCING_RATE_ANNUAL
    return resolve_accounting(
        name,
        price_basis=price_basis,
        series_kind=series_kind,
        cash_rate_annual=cash_rate_annual,
        financing_rate_annual=financing_rate_annual,
        withholding_rate=withholding_rate,
        dividends=dividends,
    )


def _accounting_for(profile: str, kwargs: dict):
    provided = kwargs.get("accounting")
    if provided is not None:
        return provided
    return resolve_accounting(
        profile,
        price_basis=kwargs.get("price_basis", "raw"),
        series_kind=kwargs.get("series_kind", "raw"),
        cash_rate_annual=kwargs.get("cash_rate_annual"),
        financing_rate_annual=kwargs.get("financing_rate_annual"),
        withholding_rate=kwargs.get("withholding_rate"),
        dividends=kwargs.get("dividends"),
    )


def load_run_series(src, symbol: str, *, bar_minutes: int = 1, price_basis: str = "raw", registry_path=None, splits=()):
    from .accounting import normalize_price_basis
    from .registry import get_series, load_registry

    basis = normalize_price_basis(price_basis)
    declared = None
    if registry_path is not None:
        entry = get_series(load_registry(registry_path), symbol)
        declared = str(entry["price_adjustment"])
        if entry.get("splits"):
            raise ValueError(price_basis_refusal(symbol, splits=True, declared=declared))
        if declared == "total_return" and basis != "total_return":
            raise ValueError(price_basis_refusal(symbol, splits=False, declared="total_return"))
    kind = "total_return" if declared == "total_return" and basis == "total_return" else "raw"
    loaded = load_named(src, symbol, bar_minutes=bar_minutes, splits=splits, price_kind=kind)
    if kind == "total_return":
        if loaded.inspection.splits:
            raise ValueError(price_basis_refusal(symbol, splits=True, declared="total_return"))
        return loaded, "total_return"
    if not loaded.inspection.runnable:
        raise ValueError(
            price_basis_refusal(
                symbol,
                splits=bool(loaded.inspection.splits),
                declared=str(loaded.inspection.price_adjustment),
            )
        )
    return loaded, "raw"


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
    price_basis: str = "raw",
    series_kind: str = "raw",
    cash_rate_annual: float | None = None,
    withholding_rate: float | None = None,
    accounting=None,
    desk_bars=None,
):
    billing = [share_factor]
    del share_factor
    name = get_profile(profile).name
    if max_day is not None and len(frame) and int(frame["day"].max()) > int(max_day):
        raise RuntimeError(
            f"refusing to run: frame contains day {int(frame['day'].max())} after declared end {int(max_day)}"
        )
    if accounting is None:
        accounting = _bound_accounting(
            name,
            price_basis=price_basis,
            series_kind=series_kind,
            cash_rate_annual=cash_rate_annual,
            financing_rate_annual=financing_rate_annual,
            withholding_rate=withholding_rate,
            dividends=dividends,
        )
    if name == "arena":
        if fee_mult != 1.0:
            raise ValueError(
                "fee-mult is a desk cost-stress switch. An arena run uses the unmodified Arena schedule."
            )
        effective_reset = reset_day
        if warmup_sessions and reset_day is None:
            days = list(dict.fromkeys(int(day) for day in frame["day"].tolist()))
            if warmup_sessions >= len(days):
                raise ValueError("arena warmup covers the whole sample")
            effective_reset = days[warmup_sessions]
        borrow = get_profile(name).borrow_rate_annual if borrow_rate_annual is None else borrow_rate_annual
        arena_dividends = _arena_dividend_days(frame, dividends) if accounting.dividends_applied else None
        result = engine_arena.run(
            strategy,
            frame,
            initial_equity=initial_equity,
            share_scale=_release(billing),
            reset_day=effective_reset,
            borrow_rate_annual=borrow,
            cash_rate_annual=accounting.cash_rate_annual,
            financing_rate_annual=accounting.financing_rate_annual,
            withholding_rate=accounting.withholding_rate,
            dividends=arena_dividends,
        )
        metrics = enrich_arena_metrics(result, engine_arena.metrics(result))
        scale = float(result["share_scale"])
        metrics["share_factor"] = scale
        metrics["borrow_rate_annual"] = borrow
        metrics["cost_basis"] = _cost_basis(name, scale, anonymized=anonymized)
        stamp_accounting(metrics, accounting)
        return result, metrics
    if name == "desk":
        borrow = get_profile(name).borrow_rate_annual if borrow_rate_annual is None else borrow_rate_annual
        rows = desk_bars if desk_bars is not None else prepare_desk_bars(frame)
        result = engine_desk.run(
            strategy,
            rows,
            DeskConfig(
                initial_equity=initial_equity,
                share_factor=_release(billing),
                borrow_rate_annual=borrow,
                financing_rate_annual=accounting.financing_rate_annual,
                cash_rate_annual=accounting.cash_rate_annual,
                withholding_rate=accounting.withholding_rate,
                fee_mult=fee_mult,
                live_start_ordinal=warmup_sessions,
                dividends=dict(dividends or {}),
            ),
        )
        metrics = engine_desk.metrics(result)
        metrics["cost_basis"] = _cost_basis(name, metrics["share_factor"], anonymized=anonymized)
        stamp_accounting(metrics, accounting)
        return result, metrics
    raise ValueError(f"unknown profile {profile}")


def _benchmarks(profile: str):
    if profile == "arena":
        return (("buy_and_hold_1x", engine_arena.BuyAndHold),)
    if profile == "desk":
        return (("buy_and_hold_1x", DeskBuyAndHold), ("long_2x", TwoXLong))
    raise ValueError(profile)


def _strategy_spec(strategy_cls):
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
    out = frame.copy(deep=True).reset_index(drop=True)
    out.attrs.clear()
    return out


_JOB_BANNED = (
    "sidecar",
    "sidecar_path",
    "factor_key",
    "factor_mac",
    "price_factor",
    "date_map",
    "volume_factor",
    "operator_note",
)
_DESIGNER_BANNED = _JOB_BANNED + ("share_factor", "share_scale")


def _refuse_keys(kwargs, banned, label: str) -> None:
    found = [key for key in banned if key in kwargs]
    if found:
        raise RuntimeError(f"{label} was given {', '.join(found)}")


def _plain_kwargs(kwargs):
    _refuse_keys(kwargs, _JOB_BANNED, "worker job")
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

    def __init__(self, values: dict):
        self._fields = dict(values)

    def __getattr__(self, name: str):
        fields = object.__getattribute__(self, "_fields")
        try:
            return fields[name]
        except KeyError:
            raise AttributeError(name) from None


def _instance_fields(obj) -> list[tuple[str, object]]:
    missing = object()
    kind = type(obj)
    found = []
    for key in dir(obj):
        if not isinstance(key, str) or key == "" or key in {"__dict__", "__weakref__"}:
            continue
        try:
            owned = inspect.getattr_static(obj, key)
        except AttributeError:
            continue
        try:
            declared = inspect.getattr_static(kind, key)
        except AttributeError:
            declared = missing
        if declared is not missing and owned is declared:
            continue
        try:
            value = getattr(obj, key)
        except Exception:
            continue
        found.append((key, value))
    return found


def _observer_view(observer):
    module = getattr(type(observer), "__module__", "")
    if not str(module).startswith("backtester_user_strategy_"):
        return observer
    values = {}
    for key, value in _instance_fields(observer):
        try:
            pickle.dumps(value)
        except Exception:
            continue
        values[key] = value
    return _ObserverView(values)


def _picklable_books(books):
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
        "LOCALAPPDATA",
        "LC_CTYPE",
        "TZ",
    }
)
_CHILD_ARGV = ["backtester-design"]
_LAUNCH_LOCK = threading.Lock()
_STDIO_CAP_BYTES = 256 * 1024
_STDIO_NAME = "stdio.log"


_BANNER_LIMIT = 240


def failure_line(type_name: str, message: str) -> str:
    name = str(type_name or "Exception").split()
    label = name[0] if name else "Exception"
    text = " ".join(str(message).split())
    line = f"{label}: {text}" if text else f"strategy raised {label}"
    if len(line) <= _BANNER_LIMIT:
        return line
    return line[: _BANNER_LIMIT - 3].rstrip() + "..."


class WorkerFailure(RuntimeError):

    def __init__(
        self,
        message: str,
        *,
        tail: str = "",
        type_name: str = "",
        detail: str = "",
        trace: str = "",
    ):
        super().__init__(message)
        self.tail = tail or ""
        self.type_name = type_name or ""
        self.detail = detail if isinstance(detail, str) else ""
        self.trace = trace if isinstance(trace, str) else ""


class _StdioCapture:

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
    if os.name == "nt":
        return
    os.setsid()


def _drop_directory(entries, banned: str) -> list:
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
                _forget_phase_log(cwd)
    return proc, cwd


def _set_dumpable(enabled: bool) -> int | None:
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


def _reported_failure(exc: BaseException) -> tuple[str, str, str]:
    reason = getattr(exc, "reason", None)
    detail = getattr(exc, "detail", None)
    child_trace = getattr(exc, "trace", None)
    if reason == "strategy" and isinstance(detail, str):
        head, sep, tail = detail.partition(": ")
        if sep and head.isidentifier():
            trace = child_trace if isinstance(child_trace, str) and child_trace.strip() else traceback.format_exc()
            return head, tail, trace
    return type(exc).__name__, str(exc), traceback.format_exc()


def _forget_phase_log(cwd) -> None:
    if os.name != "nt" or not cwd:
        return
    try:
        phase_log_path(cwd).unlink(missing_ok=True)
    except OSError:
        pass


def phase_log_path(cwd) -> Path:
    directory = Path(cwd)
    return directory.with_name(directory.name + ".phase.log")


def _books_worker(queue, job):
    _new_session()
    spool = None
    try:
        inherited_argv, inherited_cwd = _enter_child(job)
        spool = _StdioCapture(job["cwd"])
        if os.name == "nt":
            from .sandbox import note_phase, watch_phase

            watch_phase(str(phase_log_path(Path.cwd())))
            note_phase("worker-entered")
        gate = job.get("gate")
        if gate is not None:
            gate["started"].set()
            if not gate["release"].wait(timeout=180):
                raise RuntimeError("design parent did not release the walk-forward child")
            if os.name == "nt":
                from .sandbox import note_phase

                note_phase("worker-released")
        spec = job["strategy"]
        series = job.get("series")
        frame = job.get("frame")
        profile = job["profile"]
        audience = job.get("audience") or "operator"
        kwargs = job["kwargs"]
        call_timeout = float(job.get("call_timeout", _JAIL_CALL_TIMEOUT_S))
        memory_cap = job.get("memory_bytes")
        job.clear()
        if series:
            from .prepared import load_frame

            frame = load_frame(series)
            del series
        if audience == "designer":
            _refuse_keys(kwargs, _DESIGNER_BANNED, "designer child")
        if spec.get("kind") == "path":
            books = _run_jailed_books(
                spec["path"],
                frame,
                profile,
                kwargs,
                inherited_argv,
                inherited_cwd,
                call_timeout,
                audience=audience,
                memory_cap=memory_cap,
            )
        else:
            from .sandbox import lock_worker_session

            lock_worker_session()
            strategy_cls = _load_strategy(spec)
            if audience == "designer":
                from .designer_run import run_books as designer_books

                books = designer_books(strategy_cls, frame, profile, **kwargs)
            else:
                books = run_books(
                    strategy_cls,
                    frame,
                    profile,
                    share_factor=kwargs.pop("share_factor"),
                    **kwargs,
                )
            _note_child(books, inherited_argv, inherited_cwd)
        queue.put(("ok", _picklable_books(books)))
    except BaseException as exc:
        tail = ""
        if spool is not None:
            try:
                tail = spool.tail()
            except Exception:
                tail = ""
        type_name, text, trace = _reported_failure(exc)
        queue.put(
            (
                "err",
                {
                    "type": type_name,
                    "message": text,
                    "traceback": trace,
                },
                tail,
            )
        )
    finally:
        queue.close()
        queue.join_thread()


_JAIL_MEMORY_BYTES = 2 * 1024 * 1024 * 1024
_JAIL_CAP_MARGIN = 256 * 1024 * 1024
_JAIL_CPU_SECONDS = 3600
_JAIL_CALL_TIMEOUT_S = 10.0


def jail_memory_bytes(memory_cap, *, ceiling: int = _JAIL_MEMORY_BYTES, margin: int = _JAIL_CAP_MARGIN) -> int:
    if memory_cap is None or isinstance(memory_cap, bool) or not isinstance(memory_cap, int):
        return int(ceiling)
    if memory_cap <= 0:
        return int(ceiling)
    limited = int(memory_cap) + int(margin)
    if limited > int(ceiling):
        return int(ceiling)
    return limited


def _bind_strategy(strategy, *, in_process: bool):
    if in_process:
        if isinstance(strategy, (str, os.PathLike)):
            from .strategy_loader import load_strategy_class

            strategy = load_strategy_class(strategy)
        if getattr(strategy, "__strategy_path__", None):
            try:
                delattr(strategy, "__strategy_path__")
            except AttributeError:
                strategy.__strategy_path__ = None
        return strategy, None
    if isinstance(strategy, (str, os.PathLike)):
        return None, os.path.abspath(strategy)
    path = getattr(strategy, "__strategy_path__", None)
    if path:
        return None, os.path.abspath(path)
    return strategy, None


def _run_jailed_books(
    strategy_path,
    frame,
    profile,
    kwargs,
    inherited_argv,
    inherited_cwd,
    call_timeout,
    audience="operator",
    memory_cap=None,
):
    from .referee import Disqualified, jailed_strategy
    from .sandbox import lock_worker_session

    deny = [Path(strategy_path).resolve().parent]
    cwd = Path.cwd()
    if cwd not in deny:
        deny.append(cwd)
    share_factor = kwargs.pop("share_factor", None)
    if audience == "designer":
        _refuse_keys(kwargs, _DESIGNER_BANNED, "designer child")
        if share_factor is not None:
            raise RuntimeError("designer child was given share_factor")
    elif share_factor is None:
        share_factor = 1.0
    crash_code = None
    with jailed_strategy(
        strategy_path,
        profile,
        deny_paths=deny,
        memory_bytes=jail_memory_bytes(memory_cap),
        cpu_seconds=_JAIL_CPU_SECONDS,
        call_timeout=float(call_timeout),
        memory_cap=memory_cap,
    ) as remote:
        lock_worker_session()
        try:
            if audience == "designer":
                from .designer_run import run_books as designer_books

                books = designer_books(lambda: remote, frame, profile, **kwargs)
            else:
                books = run_books(lambda: remote, frame, profile, share_factor=share_factor, **kwargs)
            surface = remote.snapshot()
        except Disqualified as exc:
            remote.spill_stdio(wait=exc.reason in {"strategy", "crash", "protocol"})
            if exc.reason == "crash":
                code = remote.exit_code()
                if isinstance(code, int) and 1 < code < 128:
                    crash_code = code
                else:
                    raise
            else:
                raise
    if crash_code is not None:
        os._exit(crash_code)
    books["strategy"][0]["observer"] = _ObserverView(surface)
    _note_child(books, inherited_argv, inherited_cwd)
    return books


def _note_child(books, inherited_argv, inherited_cwd) -> None:
    observer = books["strategy"][0].get("observer")
    if observer is None:
        return
    observer.inherited_argv = list(inherited_argv)
    observer.inherited_cwd = inherited_cwd


def _kill_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(int(pgid), sig)
    except ProcessLookupError:
        return
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return
        raise


def _stop_process(proc, job=None, pgid=None) -> None:
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
                raise _failure_from_message(message)
            raise RuntimeError(f"walk-forward worker exited {proc.exitcode} before it was ready")
    raise RuntimeError("walk-forward worker did not start")


def _pipe_bytes(reader) -> int | None:
    if os.name != "nt":
        return None
    import _winapi

    try:
        return int(_winapi.PeekNamedPipe(reader.fileno())[0])
    except OSError:
        return -1


def _phase_child_pids(phase_dir) -> list[int]:
    if not phase_dir:
        return []
    try:
        text = phase_log_path(phase_dir).read_text(encoding="utf-8")
    except OSError:
        return []
    found = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[1] == "child-pid":
            try:
                found.append(int(parts[2]))
            except ValueError:
                continue
    return found


def _watched_rss(pid: int, job, phase_dir=None):
    from .limits import member_list_overflow, process_tree_watch_bytes

    if job is not None and hasattr(job, "assigned_process_count"):
        try:
            assigned = job.assigned_process_count()
        except Exception:
            assigned = None
        problem = member_list_overflow(assigned) if assigned is not None else None
        if problem:
            raise RuntimeError(problem)
    extra = _phase_child_pids(phase_dir)
    if job is not None and hasattr(job, "member_pids"):
        try:
            extra = list(extra) + [int(item) for item in job.member_pids() if int(item) != int(pid)]
        except Exception:
            pass
    return process_tree_watch_bytes(pid, extra)


def _windows_hang_detail(proc, job, phase_dir) -> str:
    parts = []
    if phase_dir:
        path = phase_log_path(phase_dir)
        try:
            lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        except OSError:
            lines = []
        parts.append("phase=" + (" | ".join(lines[-6:]) if lines else "missing"))
    if job is not None and hasattr(job, "describe"):
        try:
            parts.append(job.describe())
        except Exception as exc:
            parts.append(f"job_describe_failed={exc}")
    try:
        from .limits import process_tree_detail

        parts.append(process_tree_detail(int(proc.pid)))
    except Exception as exc:
        parts.append(f"proc_detail_failed={exc}")
    try:
        from .sandbox import profile_lock_state

        parts.append(f"lock={profile_lock_state()}")
    except Exception as exc:
        parts.append(f"lock_failed={exc}")
    text = " ".join(parts)
    return text[:1500]


def _note_windows_wait(proc, pending: int | None, started: float, job=None, phase_dir=None) -> None:
    waited = time.monotonic() - started
    slot = int(waited // 2)
    if slot < 1 or getattr(proc, "_sb_wait_note", -1) == slot:
        return
    proc._sb_wait_note = slot
    try:
        alive = bool(proc.is_alive())
    except OSError:
        alive = False
    sys.__stderr__.write(
        "walk-forward wait waited=%.1fs alive=%s exit=%s pipe_bytes=%s\n"
        % (waited, int(alive), proc.exitcode, pending)
    )
    if slot % 5 == 0:
        try:
            detail = _windows_hang_detail(proc, job, phase_dir)
        except Exception as exc:
            detail = f"diag_failed={exc}"
        sys.__stderr__.write(f"walk-forward diag waited={waited:.1f}s {detail}\n")
    sys.__stderr__.flush()


def _take_result(
    proc, queue, timeout: float, *, memory_bytes: int | None = None, stop=None, job=None, pgid=None, phase_dir=None
):
    import queue as queue_mod
    from multiprocessing.connection import wait

    deadline = time.monotonic() + timeout
    started = time.monotonic()
    reader = queue._reader
    while True:
        pending = _pipe_bytes(reader)
        queued = False
        if os.name == "nt":
            queued = pending is not None and pending > 0
            if not queued:
                _note_windows_wait(proc, pending, started, job, phase_dir)
        elif wait([reader], timeout=0):
            queued = True
        if queued:
            if memory_bytes is not None:
                rss = _watched_rss(proc.pid, job, phase_dir)
                if rss is not None and rss > int(memory_bytes):
                    _stop_process(proc, job, pgid)
                    raise RuntimeError(
                        f"walk-forward worker exceeded memory limit ({rss} > {int(memory_bytes)})"
                    )
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
            rss = _watched_rss(proc.pid, job, phase_dir)
            if rss is not None and rss > int(memory_bytes):
                _stop_process(proc, job, pgid)
                raise RuntimeError(
                    f"walk-forward worker exceeded memory limit ({rss} > {int(memory_bytes)})"
                )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            alive = False
            try:
                alive = bool(proc.is_alive())
            except OSError:
                alive = False
            detail = f"exit {proc.exitcode}"
            if os.name == "nt":
                detail += f", alive={int(alive)}, pipe_bytes={_pipe_bytes(reader)} {_windows_hang_detail(proc, job, phase_dir)}"
            _stop_process(proc, job, pgid)
            raise RuntimeError(f"walk-forward worker timed out ({detail})")
        if os.name == "nt":
            wait([proc.sentinel], timeout=min(0.05, remaining))
        else:
            wait([reader, proc.sentinel], timeout=min(0.2, remaining))


def _metric_delta(child, parent) -> list[str]:
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


def _metrics_from_result(profile: str, result: dict, *, anonymized: bool, borrow_rate_annual, run_kwargs=None):
    name = get_profile(profile).name
    kwargs = dict(run_kwargs or {})
    if name == "desk" and kwargs.get("financing_rate_annual") is None:
        recorded = (result.get("config") or {}).get("financing_rate_annual")
        if recorded is not None:
            kwargs["financing_rate_annual"] = recorded
    accounting = _accounting_for(name, kwargs)
    if name == "arena":
        metrics = enrich_arena_metrics(result, engine_arena.metrics(result))
        scale = float(result["share_scale"])
        borrow = get_profile(name).borrow_rate_annual if borrow_rate_annual is None else borrow_rate_annual
        metrics["share_factor"] = scale
        metrics["borrow_rate_annual"] = borrow
        metrics["cost_basis"] = _cost_basis(name, scale, anonymized=anonymized)
        stamp_accounting(metrics, accounting)
        return metrics
    if name == "desk":
        metrics = engine_desk.metrics(result)
        metrics["cost_basis"] = _cost_basis(name, float(metrics["share_factor"]), anonymized=anonymized)
        stamp_accounting(metrics, accounting)
        return metrics
    raise ValueError(f"unknown profile {profile}")


def _accept_child_books(profile: str, frame, child_books: dict, kwargs: dict) -> dict:
    if "strategy" not in child_books:
        raise RuntimeError("strategy process returned no strategy book")
    strategy_result, child_metrics = child_books["strategy"]
    anonymized = bool(kwargs.get("anonymized", False))
    parent_metrics = _metrics_from_result(
        profile,
        strategy_result,
        anonymized=anonymized,
        borrow_rate_annual=kwargs.get("borrow_rate_annual"),
        run_kwargs=kwargs,
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
        price_basis=kwargs.get("price_basis", "raw"),
        series_kind=kwargs.get("series_kind", "raw"),
        cash_rate_annual=kwargs.get("cash_rate_annual"),
        withholding_rate=kwargs.get("withholding_rate"),
        accounting=kwargs.get("accounting"),
    )
    billing = [float(kwargs.get("share_factor", 1.0))]
    desk_bars = prepare_desk_bars(frame) if get_profile(profile).name == "desk" else None
    for label, cls in _benchmarks(get_profile(profile).name):
        bench_result, bench_metrics = execute(
            cls(), frame, profile, share_factor=_release(billing), desk_bars=desk_bars, **common
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
    if isinstance(detail, dict):
        type_name = detail.get("type")
        label = type_name if isinstance(type_name, str) and type_name.strip() else "Exception"
        text = detail.get("message")
        text = text if isinstance(text, str) else ""
        trace = detail.get("traceback")
        trace = trace if isinstance(trace, str) else ""
        return WorkerFailure(
            trace or failure_line(label, text),
            tail=tail,
            type_name=label,
            detail=text,
            trace=trace,
        )
    return WorkerFailure(str(detail), tail=tail)


def _series_for_child(frame, audience: str) -> str | None:
    from .prepared import store_frame

    kind = "designer" if audience == "designer" else "window"
    try:
        return str(store_frame(frame, kind=kind))
    except ValueError:
        if audience == "designer":
            raise
        return None
    except Exception:
        return None


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
    series_path=None,
    **kwargs,
):
    import multiprocessing

    from .limits import arm_worker_job

    if os.name == "nt":
        from .sandbox import warm_windows_profile

        warm_windows_profile()
    run_kwargs = dict(kwargs)
    call_timeout = float(run_kwargs.pop("call_timeout", _JAIL_CALL_TIMEOUT_S))
    audience = run_kwargs.pop("audience", "operator")
    if strategy_path is not None:
        spec = {"kind": "path", "path": str(strategy_path)}
    elif strategy_cls is None:
        raise ValueError("strategy is missing")
    else:
        spec = _strategy_spec(strategy_cls)
    if spec.get("kind") == "path":
        from .sandbox import note_launch

        note_launch()
    ctx = multiprocessing.get_context("spawn")
    result_queue = ctx.Queue()
    gate = {"started": ctx.Event(), "release": ctx.Event()}
    if frame is None and series_path is None:
        raise ValueError("a jailed book needs a frame or a prepared series")
    if series_path is not None and audience == "designer":
        from .prepared import read_manifest

        if read_manifest(series_path).get("kind") != "designer":
            raise RuntimeError("designer child was given an operator series")
    published = series_path if series_path is not None else _series_for_child(frame, audience)
    job = {
        "strategy": spec,
        "profile": profile,
        "audience": audience,
        "kwargs": _plain_kwargs(run_kwargs),
        "call_timeout": call_timeout,
        "memory_bytes": None if memory_bytes is None else int(memory_bytes),
        "gate": gate,
    }
    if published is not None:
        job["series"] = str(published)
    else:
        job["frame"] = _standalone_frame(frame)
    proc = None
    cwd = None
    guard = None
    pgid = None
    try:
        proc, cwd = _launch_book(ctx, result_queue, job)
        if os.name == "nt" and cwd:
            try:
                with phase_log_path(cwd).open("a", encoding="utf-8") as handle:
                    handle.write("%.3f parent-spawned\n" % time.monotonic())
            except OSError:
                pass
        guard = arm_worker_job(proc.pid, None if memory_bytes is None else int(memory_bytes))
        if os.name == "nt" and guard is None:
            raise RuntimeError("the worker could not be placed in a job object")
        _wait_until_started(proc, gate["started"], result_queue, 60, stop, guard)
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
            phase_dir=cwd,
        )
        proc.join(timeout=60)
        if message[0] != "ok":
            failure = _failure_from_message(message)
            if proc.exitcode not in (0, None):
                shown = failure_line(failure.type_name, failure.detail) if failure.type_name else str(failure)
                raise WorkerFailure(
                    f"walk-forward worker exited {proc.exitcode}\n{shown}",
                    tail=failure.tail,
                    type_name=failure.type_name,
                    detail=failure.detail,
                    trace=failure.trace,
                )
            raise failure
        if proc.exitcode not in (0, None):
            raise RuntimeError(f"walk-forward worker exited {proc.exitcode}\n")
        wants_frame = audience == "designer" or bool(run_kwargs.get("with_benchmarks", True))
        if frame is None:
            if wants_frame:
                from .prepared import load_frame

                held = load_frame(published)
            else:
                held = None
        elif series_path is not None and audience != "designer":
            held = frame
        else:
            held = _standalone_frame(frame)
        if audience == "designer":
            from .designer_run import accept_books

            return accept_books(profile, held, message[1], run_kwargs)
        return _accept_child_books(profile, held, message[1], run_kwargs)
    finally:
        _stop_process(proc, guard, pgid)
        if guard is not None:
            guard.close()
        result_queue.close()
        result_queue.join_thread()
        if cwd is not None:
            shutil.rmtree(cwd, ignore_errors=True)
            _forget_phase_log(cwd)


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
    price_basis: str = "raw",
    series_kind: str = "raw",
    cash_rate_annual: float | None = None,
    withholding_rate: float | None = None,
    accounting=None,
):
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
        price_basis=price_basis,
        series_kind=series_kind,
        cash_rate_annual=cash_rate_annual,
        withholding_rate=withholding_rate,
        accounting=accounting,
    )
    desk_bars = prepare_desk_bars(frame) if get_profile(profile).name == "desk" else None
    strategy = strategy_cls()
    result, metrics = execute(
        strategy, frame, profile, share_factor=_release(billing), desk_bars=desk_bars, **common
    )
    billing.append(metrics["share_factor"])
    books = {"strategy": (_tag_observer(result, strategy), metrics)}
    if with_benchmarks:
        for label, cls in _benchmarks(profile):
            bench = cls()
            bench_result, bench_metrics = execute(
                bench, frame, profile, share_factor=_release(billing), desk_bars=desk_bars, **common
            )
            billing.append(bench_metrics["share_factor"])
            books[label] = (_tag_observer(bench_result, bench), bench_metrics)
    return books


def _write_child_surface(directory, books) -> None:
    observer = books["strategy"][0].get("observer")
    surface = getattr(observer, "surface", None)
    if not isinstance(surface, dict):
        return
    write_json(Path(directory) / "child_surface.json", surface)


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
    designer_books=None,
    scale: float | None = None,
) -> dict:
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
        published = designer_books if designer_books is not None else books
        designer_payload = payload
        if designer_books is not None:
            designer_metrics = designer_books["strategy"][1]
            designer_payload = dict(designer_metrics)
            designer_payload["profile"] = profile
            designer_payload["symbol"] = symbol
            designer_payload["bar_minutes"] = bar_minutes
            designer_benchmarks = {}
            for label, (_result, metrics) in designer_books.items():
                if label == "strategy":
                    continue
                designer_benchmarks[label] = _summary(profile, metrics)
            designer_payload["benchmarks"] = designer_benchmarks
            if extra:
                designer_payload["run"] = extra
            from .report import write_cost_comparison

            write_cost_comparison(out / "cost_comparison.json", profile, books, designer_books)
        publish_designer(out, profile, frame, published, designer_payload, symbol=symbol, scale=scale)
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
    price_basis: str = "raw",
    series_kind: str | None = None,
    cash_rate_annual: float | None = None,
    withholding_rate: float | None = None,
    registry_path=None,
    in_process: bool = False,
    call_timeout: float | None = None,
):
    loaded, declared_kind = load_run_series(
        src,
        symbol,
        bar_minutes=bar_minutes,
        price_basis=price_basis,
        registry_path=registry_path,
        splits=splits,
    )
    if series_kind is not None and series_kind != declared_kind:
        raise ValueError(f"series kind {series_kind} does not match the loaded series ({declared_kind})")
    series_kind = declared_kind
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
    bound_cls, bound_path = _bind_strategy(strategy_cls, in_process=in_process)
    book_kwargs = dict(
        share_factor=factor,
        borrow_rate_annual=borrow_rate_annual,
        financing_rate_annual=financing_rate_annual,
        fee_mult=fee_mult,
        dividends=dividends,
        warmup_sessions=warmup_sessions,
        initial_equity=initial_equity,
        with_benchmarks=with_benchmarks,
        price_basis=price_basis,
        series_kind=series_kind,
        cash_rate_annual=cash_rate_annual,
        withholding_rate=withholding_rate,
    )
    inspection = loaded.inspection
    series_path = loaded.prepared
    if bound_path is not None:
        if call_timeout is not None:
            book_kwargs["call_timeout"] = float(call_timeout)
        if series_path:
            loaded = None
            gc.collect()
            books = _isolated_books(
                bound_cls,
                None,
                profile,
                strategy_path=bound_path,
                series_path=series_path,
                **book_kwargs,
            )
            from .prepared import load_frame

            report_frame = load_frame(series_path)
        else:
            report_frame = loaded.frame
            books = _isolated_books(bound_cls, report_frame, profile, strategy_path=bound_path, **book_kwargs)
    else:
        report_frame = loaded.frame
        books = run_books(bound_cls, report_frame, profile, **book_kwargs)
    payload = write_books(
        out_dir,
        profile,
        symbol,
        bar_minutes,
        report_frame,
        books,
        extra={"warnings": warnings, "inspection": inspection.as_dict()},
    )
    _write_child_surface(out_dir, books)
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
    allow_scale: bool = False,
    borrow_rate_annual: float | None = None,
    financing_rate_annual: float | None = None,
    fee_mult: float = 1.0,
    dividends: dict | None = None,
    initial_equity: float = 1_000_000.0,
    with_benchmarks: bool = True,
    inspection_notes=(),
    during_design=None,
    price_basis: str = "raw",
    series_kind: str = "raw",
    cash_rate_annual: float | None = None,
    withholding_rate: float | None = None,
    anonymized: bool = False,
    in_process: bool = False,
    call_timeout: float | None = None,
):
    strategy_cls, strategy_path = _bind_strategy(strategy_cls, in_process=in_process)
    if share_factor != 1.0 and not allow_scale:
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
    if dividends:
        _arena_dividend_days(frame, dividends)
    accounting = _bound_accounting(
        profile,
        price_basis=price_basis,
        series_kind=series_kind,
        cash_rate_annual=cash_rate_annual,
        financing_rate_annual=financing_rate_annual,
        withholding_rate=withholding_rate,
        dividends=dividends,
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
        initial_equity=initial_equity,
        with_benchmarks=with_benchmarks,
        price_basis=price_basis,
        series_kind=series_kind,
        cash_rate_annual=cash_rate_annual,
        withholding_rate=withholding_rate,
        anonymized=anonymized,
    )
    if call_timeout is not None:
        common["call_timeout"] = float(call_timeout)
    notes = [str(note) for note in (inspection_notes or ())]
    previous_dumpable = _set_dumpable(False)
    try:
        try:
            design_books = _isolated_books(
                strategy_cls,
                design,
                profile,
                strategy_path=strategy_path,
                max_day=meta.design_end_day,
                warmup_sessions=0,
                during=during_design,
                dividends=_dividends_for_frame(design, dividends),
                accounting=accounting,
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
        locked_dividends = _dividends_for_frame(rebuilt.locked_with_warmup, dividends)
        if profile == "arena":
            locked_books = _isolated_books(
                strategy_cls,
                rebuilt.locked_with_warmup,
                profile,
                strategy_path=strategy_path,
                reset_day=meta.locked_start_day if meta.warmup_sessions else None,
                max_day=meta.locked_day_max,
                warmup_sessions=0,
                dividends=locked_dividends,
                accounting=accounting,
                **common,
            )
        elif profile == "desk":
            locked_books = _isolated_books(
                strategy_cls,
                rebuilt.locked_with_warmup,
                profile,
                strategy_path=strategy_path,
                warmup_sessions=meta.warmup_sessions,
                max_day=meta.locked_day_max,
                dividends=locked_dividends,
                accounting=accounting,
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
    _write_child_surface(out / "design", design_books)
    _write_child_surface(out / "locked", locked_books)
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
    design_record = design_payload.get("accounting")
    locked_record = locked_payload.get("accounting")
    if design_record != locked_record:
        raise ValueError("design and locked books disagree on accounting settings")
    if design_record:
        summary["accounting"] = design_record
        for key, value in design_record.items():
            summary.setdefault(key, value)
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
    price_basis: str = "raw",
    cash_rate_annual: float | None = None,
    withholding_rate: float | None = None,
    allow_scale_mismatch: bool = False,
    in_process: bool = False,
    call_timeout: float | None = None,
):
    if in_process:
        raise ValueError("--unsafe-in-process cannot run a pack or an anonymized book")
    strategy_cls, strategy_path = _bind_strategy(strategy_cls, in_process=False)
    from .pack import load_pack, load_sidecar, require_build_price_factor

    if _report_inside_pack(pack_dir, out_dir):
        raise ValueError("the report must not live inside the pack directory")
    secret = load_sidecar(sidecar_path, allow_scale_mismatch=allow_scale_mismatch)
    if bool(secret["anonymized"]):
        from .report import require_anonymizable_symbol

        require_anonymizable_symbol(str(secret["symbol"]))
    loaded = load_pack(pack_dir, symbol=str(secret["symbol"]))
    if secret["pack_id"] != loaded.pack_id:
        raise ValueError("sidecar does not match this pack")
    if bool(secret["anonymized"]) != loaded.anonymized:
        raise ValueError("sidecar anonymized flag does not match the pack")
    anonymized = bool(secret["anonymized"])
    price_factor = require_build_price_factor(secret)
    share_scale = float(secret["share_scale"])
    symbol = str(secret["symbol"])
    series_kind = str(loaded.manifest["price_basis"])
    basis = normalize_price_basis(price_basis)
    if series_kind == "total_return" and basis != "total_return":
        raise ValueError(
            price_basis_refusal(symbol, splits=False, declared="total_return")
        )
    if anonymized:
        from .pack import assert_anonymized_pack_clean
        from .report import require_anonymizable_symbol

        require_anonymizable_symbol(symbol)
        assert_anonymized_pack_clean(pack_dir, symbol=symbol)
    date_map = secret["date_map"]
    designer_dividends = _designer_dividends(dividends, date_map, price_factor) if anonymized else None
    child_dividends = _pack_dividends(
        dividends,
        anonymized=anonymized,
        share_scale=share_scale,
        date_map=date_map,
    )
    if borrow_rate_annual is None and secret["borrow_rate_source"] == "explicit":
        borrow_rate_annual = float(secret["borrow_rate_annual"])
    del secret
    gc.collect()
    designer_books = None
    if anonymized:
        from .designer_run import spawn_kwargs

        designer_books = _isolated_books(
            strategy_cls,
            loaded.frame,
            profile,
            strategy_path=strategy_path,
            call_timeout=_JAIL_CALL_TIMEOUT_S if call_timeout is None else float(call_timeout),
            audience="designer",
            **spawn_kwargs(
                borrow_rate_annual=borrow_rate_annual,
                financing_rate_annual=financing_rate_annual,
                fee_mult=fee_mult,
                dividends=designer_dividends,
                warmup_sessions=warmup_sessions,
                with_benchmarks=with_benchmarks,
                price_basis=basis,
                series_kind=series_kind,
                cash_rate_annual=cash_rate_annual,
                withholding_rate=withholding_rate,
            ),
        )
    books = _isolated_books(
        strategy_cls,
        loaded.frame,
        profile,
        strategy_path=strategy_path,
        call_timeout=_JAIL_CALL_TIMEOUT_S if call_timeout is None else float(call_timeout),
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
        price_basis=basis,
        series_kind=series_kind,
        cash_rate_annual=cash_rate_annual,
        withholding_rate=withholding_rate,
    )
    _record_scale_override(books, price_factor=price_factor, share_scale=share_scale)
    payload = write_books(
        out_dir,
        get_profile(profile).name,
        symbol,
        loaded.bar_minutes,
        loaded.frame,
        books,
        anonymized=anonymized,
        designer_books=designer_books,
        scale=share_scale,
        extra={
            "warnings": [],
            "pack_id": loaded.pack_id,
            "anonymized": anonymized,
        },
    )
    _write_child_surface(out_dir, books)
    observer = books["strategy"][0].get("observer")
    if observer is not None:
        payload["strategy_observer"] = observer
    return payload


def walk_pack(
    strategy_cls,
    pack_dir,
    sidecar_path,
    profile: str,
    out_dir,
    *,
    design_end_day: int | None = None,
    design_end_date: str | None = None,
    design_fraction: float | None = None,
    warmup_days: int = 0,
    borrow_rate_annual: float | None = None,
    financing_rate_annual: float | None = None,
    fee_mult: float = 1.0,
    dividends: dict | None = None,
    initial_equity: float = 1_000_000.0,
    with_benchmarks: bool = True,
    price_basis: str = "raw",
    cash_rate_annual: float | None = None,
    withholding_rate: float | None = None,
):
    from .designer_run import spawn_kwargs
    from .pack import load_pack, load_sidecar, require_build_price_factor
    from .report import publish_designer, write_cost_comparison

    if _report_inside_pack(pack_dir, out_dir):
        raise ValueError("the report must not live inside the pack directory")
    strategy, strategy_path = _bind_strategy(strategy_cls, in_process=False)
    strategy_cls = strategy
    secret = load_sidecar(sidecar_path)
    if bool(secret["anonymized"]):
        from .report import require_anonymizable_symbol

        require_anonymizable_symbol(str(secret["symbol"]))
    loaded = load_pack(pack_dir, symbol=str(secret["symbol"]))
    if secret["pack_id"] != loaded.pack_id:
        raise ValueError("sidecar does not match this pack")
    if bool(secret["anonymized"]) != loaded.anonymized:
        raise ValueError("sidecar anonymized flag does not match the pack")
    anonymized = bool(secret["anonymized"])
    price_factor = require_build_price_factor(secret)
    share_scale = float(secret["share_scale"])
    symbol = str(secret["symbol"])
    series_kind = str(loaded.manifest["price_basis"])
    basis = normalize_price_basis(price_basis)
    if series_kind == "total_return" and basis != "total_return":
        raise ValueError(
            price_basis_refusal(symbol, splits=False, declared="total_return")
        )
    if anonymized:
        from .pack import assert_anonymized_pack_clean
        from .report import require_anonymizable_symbol

        require_anonymizable_symbol(symbol)
        assert_anonymized_pack_clean(pack_dir, symbol=symbol)
    date_map = secret["date_map"]
    designer_dividends = _designer_dividends(dividends, date_map, price_factor) if anonymized else None
    child_dividends = _pack_dividends(
        dividends,
        anonymized=anonymized,
        share_scale=share_scale,
        date_map=date_map,
    )
    if borrow_rate_annual is None and secret["borrow_rate_source"] == "explicit":
        borrow_rate_annual = float(secret["borrow_rate_annual"])
    del secret
    gc.collect()
    summary, rebuilt, design_books, locked_books = walk_forward(
        strategy_path or strategy_cls,
        loaded.frame,
        profile,
        out_dir,
        symbol=symbol,
        bar_minutes=loaded.bar_minutes,
        design_end_day=design_end_day,
        design_end_date=design_end_date,
        design_fraction=design_fraction,
        warmup_days=warmup_days,
        share_factor=share_scale,
        allow_scale=True,
        borrow_rate_annual=borrow_rate_annual,
        financing_rate_annual=financing_rate_annual,
        fee_mult=fee_mult,
        dividends=child_dividends,
        initial_equity=initial_equity,
        with_benchmarks=with_benchmarks,
        price_basis=basis,
        series_kind=series_kind,
        cash_rate_annual=cash_rate_annual,
        withholding_rate=withholding_rate,
        anonymized=anonymized,
    )
    if not anonymized:
        return summary
    name = get_profile(profile).name
    accounting = _bound_accounting(
        name,
        price_basis=basis,
        series_kind=series_kind,
        cash_rate_annual=cash_rate_annual,
        financing_rate_annual=financing_rate_annual,
        withholding_rate=withholding_rate,
        dividends=designer_dividends,
    )
    design_kwargs = spawn_kwargs(
        borrow_rate_annual=borrow_rate_annual,
        financing_rate_annual=financing_rate_annual,
        fee_mult=fee_mult,
        dividends=_dividends_for_frame(rebuilt.design, designer_dividends),
        with_benchmarks=with_benchmarks,
        price_basis=basis,
        series_kind=series_kind,
        cash_rate_annual=cash_rate_annual,
        withholding_rate=withholding_rate,
        accounting=accounting,
        max_day=int(rebuilt.design["day"].max()),
    )
    design_designer = _isolated_books(
        strategy_cls,
        rebuilt.design,
        profile,
        strategy_path=strategy_path,
        call_timeout=_JAIL_CALL_TIMEOUT_S,
        audience="designer",
        **design_kwargs,
    )
    designer_metrics = design_designer["strategy"][1]
    designer_payload = dict(designer_metrics)
    designer_payload["profile"] = name
    designer_payload["symbol"] = symbol
    designer_payload["bar_minutes"] = loaded.bar_minutes
    designer_payload["benchmarks"] = {
        label: _summary(name, metrics)
        for label, (_result, metrics) in design_designer.items()
        if label != "strategy"
    }
    designer_payload["run"] = {
        "warnings": [],
        "pack_id": loaded.pack_id,
        "anonymized": True,
        "window": "design",
    }
    publish_designer(
        out_dir, name, rebuilt.design, design_designer, designer_payload, symbol=symbol, scale=share_scale
    )
    locked_kwargs = spawn_kwargs(
        borrow_rate_annual=borrow_rate_annual,
        financing_rate_annual=financing_rate_annual,
        fee_mult=fee_mult,
        dividends=_dividends_for_frame(rebuilt.locked_with_warmup, designer_dividends),
        with_benchmarks=with_benchmarks,
        price_basis=basis,
        series_kind=series_kind,
        cash_rate_annual=cash_rate_annual,
        withholding_rate=withholding_rate,
        accounting=accounting,
        max_day=int(rebuilt.locked["day"].max()),
    )
    if name == "arena":
        locked_kwargs["reset_day"] = summary["locked_start_day"] if summary["warmup_sessions"] else None
    elif name == "desk":
        locked_kwargs["warmup_sessions"] = summary["warmup_sessions"]
    locked_designer = _isolated_books(
        strategy_cls,
        rebuilt.locked_with_warmup,
        profile,
        strategy_path=strategy_path,
        call_timeout=_JAIL_CALL_TIMEOUT_S,
        audience="designer",
        **locked_kwargs,
    )
    write_cost_comparison(
        Path(out_dir) / "cost_comparison.json",
        name,
        design_books,
        design_designer,
        windows={
            "design": (design_books, design_designer),
            "locked": (locked_books, locked_designer),
        },
    )
    return summary


def run_parallel(*args, **kwargs):
    from .parallel import run_parallel as _run

    return _run(*args, **kwargs)
