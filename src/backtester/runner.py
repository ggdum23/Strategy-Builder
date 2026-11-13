"""Run one strategy, or a design window and a locked tail.

Benchmarks go through the same engine and the same cost configuration as the
strategy. Omitted switches are the profile defaults: Arena raw prices, cash
0, financing 0; Desk raw prices, cash 0, financing 0.07. Opting into
total-return, a cash rate, or a financing rate is recorded on the metrics.

Phase 1 walk-forward isolation is the heap, class attributes, module globals,
the environment, argv, and cwd. A strategy file does not run in that worker.
It runs in the referee jail, one bar at a time, while this process keeps the
engine, the accounting record, and the window's dividends. See docs/ISOLATION.md.
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
from .accounting import normalize_price_basis, resolve_accounting, stamp_accounting
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


def _designer_dividends(dividends, date_map, price_factor: float):
    """Cash per pack share is the file amount times the build-time price factor.

    The child receives the restated map. This is not ``share_scale``.
    """
    from .designer_run import session_dividends

    if not dividends:
        return session_dividends(dividends, date_map)
    restated = {str(key): float(amount) * float(price_factor) for key, amount in dividends.items()}
    return session_dividends(restated, date_map)


def _record_scale_override(books, *, price_factor: float, share_scale: float) -> None:
    """Name both factors when the billing scale is not the build-time price factor.

    Equal factors keep the invoice label the operator files already use.
    """
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
    """Dividends whose session is in ``frame``.

    Walk-forward calls this in the parent, once per child, before that child
    is spawned. A key that names a session outside the frame is left out.
    ``None`` stays ``None`` (no file). When the file had entries and none of
    them fall in this frame, the result is an empty dict: the file was
    present and this window has no ex-date. ``run`` does not use this.
    """
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
    """Map a dividend file onto Arena day indexes.

    Keys may already be day indexes, or calendar dates / pack session ids
    that appear in ``frame["date"]``.
    """
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
    """Name the method from the dividend file this process was given.

    Desk's omitted financing rate is ``costs_desk.FINANCING_RATE_ANNUAL``,
    read here. Walk-forward calls this once in the parent, on the full file,
    and hands that record to each child. A child does not call it on a slice.
    """
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
    """The record already resolved for this book, or a fresh one.

    Walk-forward puts the full-file record on ``kwargs``. That record wins.
    Resolving again from a window's dividend map would treat an empty slice
    as "no file".
    """
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
    """Load one named series for a run.

    A registry entry declared ``total_return`` stays ``runnable: false``.
    This function still returns it when ``price_basis`` is ``total_return``
    and the entry has no declared splits. A raw request keeps the refusal.
    """
    from .accounting import normalize_price_basis
    from .registry import get_series, load_registry

    basis = normalize_price_basis(price_basis)
    declared = None
    if registry_path is not None:
        entry = get_series(load_registry(registry_path), symbol)
        declared = str(entry["price_adjustment"])
        if entry.get("splits"):
            raise ValueError(
                f"{symbol} is {declared} and is not runnable. This phase executes raw prices only."
            )
        if declared == "total_return" and basis != "total_return":
            raise ValueError(
                f"{symbol} is total_return and is not runnable. This phase executes raw prices only."
            )
    kind = "total_return" if declared == "total_return" and basis == "total_return" else "raw"
    loaded = load_named(src, symbol, bar_minutes=bar_minutes, splits=splits, price_kind=kind)
    if kind == "total_return":
        if loaded.inspection.splits:
            raise ValueError(
                f"{symbol} is total_return and is not runnable. This phase executes raw prices only."
            )
        return loaded, "total_return"
    if not loaded.inspection.runnable:
        raise ValueError(
            f"{symbol} is {loaded.inspection.price_adjustment} and is not runnable. "
            "This phase executes raw prices only."
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
):
    """Run one strategy instance on ``frame``.

    ``max_day``, when set, refuses a frame whose last session is later than
    that day. Walk-forward uses it so a design run cannot be handed the tail.

    ``accounting``, when given, is the record already resolved from the full
    dividend file. This function does not look at whether ``dividends`` is
    empty. An empty map credits no cash and still keeps that record.
    """
    # Drop the parameter name before on_bar. The engine call receives the value.
    billing = [share_factor]
    del share_factor
    name = get_profile(profile).name
    if max_day is not None and len(frame) and int(frame["day"].max()) > int(max_day):
        raise RuntimeError(
            f"refusing to run: frame contains day {int(frame['day'].max())} after declared end {int(max_day)}"
        )
    # No record yet: name the method here. Desk's omitted rate is the live
    # constant, read in this process. A strategy that patches it changes the
    # child book, and the parent benchmark (unpatched) rejects the row.
    # Walk-forward passes the record it already resolved from the full file,
    # so an empty slice is not read as "no file".
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
        rows = frame_to_desk_bars(frame)
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
        # The Windows AppContainer profile is stored under this directory.
        # The worker keeps the value so the jail can be created. The jail
        # denies the directory. It is not a data root the strategy can read.
        "LOCALAPPDATA",
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


# One line on a banner. A strategy traceback stays in the log and in the
# operator error file, not in this sentence.
_BANNER_LIMIT = 240


def failure_line(type_name: str, message: str) -> str:
    """``Type: message``, or ``strategy raised Type`` when the message is empty.

    Newlines collapse. The outermost exception is the caller's job: pass its
    type and ``str(exc)``, not ``__cause__``.
    """
    name = str(type_name or "Exception").split()
    label = name[0] if name else "Exception"
    text = " ".join(str(message).split())
    line = f"{label}: {text}" if text else f"strategy raised {label}"
    if len(line) <= _BANNER_LIMIT:
        return line
    return line[: _BANNER_LIMIT - 3].rstrip() + "..."


class WorkerFailure(RuntimeError):
    """A book process failed. ``tail`` is its own stdout/stderr, not a path.

    ``type_name`` and ``detail`` are the strategy exception. ``trace`` is the
    traceback. The RuntimeError message stays the traceback so an operator
    error file still has it. A banner uses :func:`failure_line` instead.
    """

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
                _forget_phase_log(cwd)
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


def _reported_failure(exc: BaseException) -> tuple[str, str, str]:
    """Type, message, and traceback stored on the worker's error.

    The jail reports a strategy exception as ``Disqualified`` reason
    ``strategy``. The detail is ``Type: message`` and ``trace`` is the
    strategy process traceback. The operator banner uses the type and the
    message. The log uses the traceback. A timeout, a protocol break, and a
    worker failure stay the exception this process caught.
    """
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
    """Remove the sibling phase log after the jail directory is gone."""
    if os.name != "nt" or not cwd:
        return
    try:
        phase_log_path(cwd).unlink(missing_ok=True)
    except OSError:
        pass


def phase_log_path(cwd) -> Path:
    """Phase log beside the jail directory, not inside it.

    The strategy lists its cwd. A log written there is a file the child
    can read. The parent and the worker both use this sibling path.
    """
    directory = Path(cwd)
    return directory.with_name(directory.name + ".phase.log")


def _books_worker(queue, job):
    _new_session()
    # The session is created before the jail. The seccomp filter that denies
    # setsid is installed after a file strategy's jail is up: bubblewrap needs
    # setsid for its own new session, and the filter would be inherited.
    # Class strategies have no jail process, so the filter is installed before
    # they run. See docs/ISOLATION.md.
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
        frame = job["frame"]
        profile = job["profile"]
        audience = job.get("audience") or "operator"
        kwargs = job["kwargs"]
        call_timeout = float(job.get("call_timeout", _JAIL_CALL_TIMEOUT_S))
        memory_cap = job.get("memory_bytes")
        job.clear()
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
# Headroom above the caller's cap so the jail can cross it by one small
# allocation and the poll can still report the number. It is not a second
# ceiling: the kernel limit stays this close to the caller's cap.
# One poll interval of 8 MiB chunks, plus the headroom the memory tests
# allocate past the cap before they sleep. Still far under the 2 GiB ceiling,
# so one mapping cannot reach that ceiling.
_JAIL_CAP_MARGIN = 256 * 1024 * 1024
_JAIL_CPU_SECONDS = 3600
# Same default as the referee. A longer bar has to opt in.
_JAIL_CALL_TIMEOUT_S = 10.0


def jail_memory_bytes(memory_cap, *, ceiling: int = _JAIL_MEMORY_BYTES, margin: int = _JAIL_CAP_MARGIN) -> int:
    """Kernel ceiling for the jailed strategy.

    With no caller cap the ceiling stays at ``ceiling`` (2 GiB). A caller
    cap is passed through with ``margin`` added, and it is never raised
    above ``ceiling``. The worker job does not receive this number.
    """
    if memory_cap is None or isinstance(memory_cap, bool) or not isinstance(memory_cap, int):
        return int(ceiling)
    if memory_cap <= 0:
        return int(ceiling)
    limited = int(memory_cap) + int(margin)
    if limited > int(ceiling):
        return int(ceiling)
    return limited


def _bind_strategy(strategy, *, in_process: bool):
    """Return ``(class_or_none, path_or_none)``.

    A path is the jail. A class stamped with ``__strategy_path__`` is a file
    the caller already loaded, and it takes the same jail unless
    ``in_process`` is set. ``in_process`` loads a path in this process and
    strips the file marker so the worker does not jail it. A class with no
    file marker stays in this process.
    """
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
        return None, str(strategy)
    path = getattr(strategy, "__strategy_path__", None)
    if path:
        return None, str(path)
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
    """Engine in this process. The strategy file runs in the referee jail.

    ``kwargs`` already holds the accounting record and this window's dividend
    slice. Those stay here. The pipe carries bars and decisions only, so a
    locked ex-date is not in the design strategy and the scale is not in
    either strategy process. The jail's kernel ceiling is the caller's cap
    plus a small margin, and never the 2 GiB default when a cap was set.
    The parent's poll still reports that cap. The worker job has no commit
    limit.
    """
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
        # The jail is already a new session. Later forks of this worker cannot
        # leave the group.
        lock_worker_session()
        try:
            if audience == "designer":
                from .designer_run import run_books as designer_books

                books = designer_books(lambda: remote, frame, profile, **kwargs)
            else:
                books = run_books(lambda: remote, frame, profile, share_factor=share_factor, **kwargs)
            surface = remote.snapshot()
        except Disqualified as exc:
            # Prints live in the jail log. Attach them to this worker's stdio
            # before the operator error is built. A hard exit (os._exit) has
            # no decision line; leave with that status so the row is a crash.
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
                raise _failure_from_message(message)
            raise RuntimeError(f"walk-forward worker exited {proc.exitcode} before it was ready")
    raise RuntimeError("walk-forward worker did not start")


def _pipe_bytes(reader) -> int | None:
    """Bytes already in a Windows result pipe. ``None`` on other systems.

    ``PeekNamedPipe`` does not consume the message. ``connection.wait`` on
    this pipe posts a zero-byte overlapped read and then cancels it, which
    leaves a ``WriteFile`` of a result larger than the 8 KiB pipe buffer
    blocked in the child.
    """
    if os.name != "nt":
        return None
    import _winapi

    try:
        return int(_winapi.PeekNamedPipe(reader.fileno())[0])
    except OSError:
        return -1


def _phase_child_pids(phase_dir) -> list[int]:
    """Jailed pids the worker recorded. Toolhelp can miss an AppContainer."""
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
    """Resident set of the worker, its children, and any jailed pid it named.

    On Windows the same walk also reads commit, and the larger total is
    what the cap compares. Job members and phase-log pids that toolhelp
    already listed are not counted twice. Children of those extra pids
    are included. A job that reports more than 32 members fails the book
    instead of summing the 32-pid prefix.
    """
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
    """Phase, job accounting, commit, and who holds the profile mutex."""
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
    """One line on the real stderr every two seconds, for the Windows CI log.

    Every ten seconds the line includes the phase file, the job limits, the
    worker commit, and whether the profile mutex is held. A pytest timeout
    kills the test before the worker-timeout exception is raised, so the
    diagnosis has to be on this path.
    """
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
    """Read the result, or raise as soon as the child exits without one.

    A dead child used to block on ``queue.get`` until the 600s timeout.
    On Linux the process sentinel and the queue are waited together. On
    Windows only the sentinel is waited: the pipe is drained with
    ``PeekNamedPipe`` and a real read. ``memory_bytes`` and ``stop`` are
    watched in this process. They are not sent to the child.
    """
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
            # A timeout posted as the resident set crosses the cap is the
            # memory stop. The cap is what the caller asked to enforce.
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


def _metrics_from_result(profile: str, result: dict, *, anonymized: bool, borrow_rate_annual, run_kwargs=None):
    """The statistics ``execute`` would attach, computed in this process."""
    name = get_profile(profile).name
    kwargs = dict(run_kwargs or {})
    if name == "desk" and kwargs.get("financing_rate_annual") is None:
        # The strategy book was already run. Stamp the rate stored on that
        # result so the recomputation matches it. Benchmarks are re-run here
        # and still read the live constant.
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

    if os.name == "nt":
        # The runtime ACL is once per machine. Pay for it here, before the
        # worker's timeout clock starts, so a book does not spend its budget
        # walking the Python tree.
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
        # launch() runs in the worker, where the pytest observer is not set.
        from .sandbox import note_launch

        note_launch()
    ctx = multiprocessing.get_context("spawn")
    result_queue = ctx.Queue()
    gate = {"started": ctx.Event(), "release": ctx.Event()}
    job = {
        "strategy": spec,
        "frame": _standalone_frame(frame),
        "profile": profile,
        "audience": audience,
        "kwargs": _plain_kwargs(run_kwargs),
        "call_timeout": call_timeout,
        "memory_bytes": None if memory_bytes is None else int(memory_bytes),
        "gate": gate,
    }
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
        if audience == "designer":
            from .designer_run import accept_books

            return accept_books(profile, _standalone_frame(frame), message[1], run_kwargs)
        return _accept_child_books(profile, _standalone_frame(frame), message[1], run_kwargs)
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
        price_basis=price_basis,
        series_kind=series_kind,
        cash_rate_annual=cash_rate_annual,
        withholding_rate=withholding_rate,
        accounting=accounting,
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


def _write_child_surface(directory, books) -> None:
    """Write a strategy's own report when it stored one.

    The jail cannot create that file beside the strategy. The worker writes
    it into the operator directory after the engine has finished. It is not
    copied into ``designer/``.
    """
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
    """Write the strategy curves at the top of ``out_dir`` and benchmarks underneath.

    An anonymized run also writes ``designer/``. That tree is the copy a
    strategy designer may see. It has no ticker, no calendar date, and no
    scale factor. When ``designer_books`` is given, that tree is those books
    (flat fee, canonical equity). The files next to it are the operator copy
    and may have the real ticker and the real fees. Those operator files are
    written from ``books`` either way.
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
    if bound_path is not None:
        if call_timeout is not None:
            book_kwargs["call_timeout"] = float(call_timeout)
        books = _isolated_books(bound_cls, loaded.frame, profile, strategy_path=bound_path, **book_kwargs)
    else:
        books = run_books(bound_cls, loaded.frame, profile, **book_kwargs)
    payload = write_books(
        out_dir,
        profile,
        symbol,
        bar_minutes,
        loaded.frame,
        books,
        extra={"warnings": warnings, "inspection": loaded.inspection.as_dict()},
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
    """Design run sees only design bars. The locked run may replay earlier bars as warm-up.

    The parent keeps the design slice and writes the locked window as a sealed
    blob. The key stays in this function: it is not put in the child job, the
    environment, argv, or any file. The caller's frame is dropped before the
    design child starts, and the locked window is opened only after that child
    has exited. Each book is a new spawned interpreter. See docs/ISOLATION.md.

    Arena warm-up trades, then rescales to the starting equity and keeps exposure.
    Desk warm-up is flat (``live`` false) and goes live flat at the starting equity.
    Those are different engines. This function does not merge them.

    The parent resolves one accounting record from the full dividend file,
    then gives each child that record and only the ex-dates inside that
    child's frame. An empty map means the file was present and this window
    has no ex-date. The child does not decide the method from the map.
    A dividend date that is not in the full sample fails here, for Arena and
    for Desk, before either child starts. An Arena ``run`` still rejects a
    date that is not in its sample. A Desk ``run`` still looks a session up
    and credits nothing when the key is absent.

    The books written here are operator books. They keep the original ticker.
    There is no designer tree, so a symbol that is reserved for anonymized
    reports is allowed. ``anonymized`` stamps those operator books. Named
    walks leave it false.
    """
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
        # Both profiles. A locked ex-date is in this frame, so it passes here
        # and is left out of the design child below. A date that is in neither
        # window is not in the sample.
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
    """Run a pack. The sidecar stays in this process.

    The operator child is billed on real share counts (``share_scale``).
    An anonymized pack also runs a designer child: flat fee, canonical
    equity, no scale factor. Dividend cash per pack share for that child is
    the file amount times the price factor authenticated by the operator
    MAC, never ``share_scale``. The operator child still receives amounts
    times ``share_scale``. Neither child receives the sidecar document, the
    date map, the ticker, the volume factor, the factor key, or the note.

    ``allow_scale_mismatch`` is the test override that bills a scale other
    than that price factor. The invoice then records both factors. The MAC
    is still checked.
    """
    if in_process:
        raise ValueError("--unsafe-in-process cannot run a pack or an anonymized book")
    strategy_cls, strategy_path = _bind_strategy(strategy_cls, in_process=False)
    from .pack import load_pack, load_sidecar, require_build_price_factor

    if _report_inside_pack(pack_dir, out_dir):
        raise ValueError("the report must not live inside the pack directory")
    loaded = load_pack(pack_dir)
    secret = load_sidecar(sidecar_path, allow_scale_mismatch=allow_scale_mismatch)
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
            f"{symbol} is total_return and is not runnable. This phase executes raw prices only."
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
    # The rest of the sidecar (date map, ticker, volume factor, note) is not
    # copied into the child job. Drop the document before the spawn.
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
    # Not written to metrics.json. A test reads what the child strategy observed.
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
    """Walk-forward a pack.

    Operator design and locked books use the real fee schedule and
    ``share_scale``. An anonymized pack publishes the design window under
    ``designer/`` on the flat fee and the canonical equity. The locked
    window stays an operator book. ``cost_comparison.json`` shows both
    windows on both cost models.
    """
    from .designer_run import spawn_kwargs
    from .pack import load_pack, load_sidecar, require_build_price_factor
    from .report import publish_designer, write_cost_comparison

    if _report_inside_pack(pack_dir, out_dir):
        raise ValueError("the report must not live inside the pack directory")
    strategy, strategy_path = _bind_strategy(strategy_cls, in_process=False)
    strategy_cls = strategy
    loaded = load_pack(pack_dir)
    secret = load_sidecar(sidecar_path)
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
            f"{symbol} is total_return and is not runnable. This phase executes raw prices only."
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
    """Score many strategies. See ``backtester.parallel.run_parallel``."""
    from .parallel import run_parallel as _run

    return _run(*args, **kwargs)
