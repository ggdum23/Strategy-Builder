"""One-bar referee.

The official engine stays in this process. The design strategy runs in a child
process that is handed one bar, then the decision for that bar, and only then
the next bar. A read that gets ahead of the parent blocks until the per-call
timeout and the run is disqualified.

A named sample uses the official fee schedule. Arena rescales to the starting
equity and keeps exposure. Desk stays flat until the account is live.
Calendar months are applied here.

A pack replay is a designer surface. Fills go through ``DesignerCostModel``
(0.8 bp flat, no per-share fee) and the canonical designer equity. The parent
checks the sidecar MAC before it reads the price factor or ``share_scale``,
then drops the sidecar. The jailed strategy is not given the sidecar,
``factor_key``, ``factor_mac``, ``share_scale``, or ``share_factor``. The
operator invoice stays on ``run_pack``, which keeps the real schedule.
"""

from __future__ import annotations

import gc
import json
import math
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from . import engine_arena, engine_desk
from .data import frame_to_desk_bars, load_dividends, load_named
from .engine_desk import DeskConfig
from .profiles import get_profile
from .report import enrich_arena_metrics, require_anonymizable_symbol, write_json
from .sandbox import SandboxError, describe_isolation, launch

DEFAULT_CALL_TIMEOUT_S = 10.0
DEFAULT_STARTUP_TIMEOUT_S = 60.0
DEFAULT_MEMORY_BYTES = 2 * 1024 * 1024 * 1024
DEFAULT_CPU_SECONDS = 3600
ARENA_WARMUP_DAYS = engine_arena.VALIDATION_WARMUP_DAYS

_HIDDEN_KEYS = frozenset(
    {
        "share_scale",
        "share_factor",
        "symbol",
        "date_map",
        "sidecar",
        "sidecar_path",
        "price_factor",
        "volume_factor",
        "factor_key",
        "factor_mac",
        "operator_note",
    }
)
_JAIL_MARKERS = ("bwrap:", "landlock", "seccomp", "no-new-privs", "parent-death", "jail metadata")


class Disqualified(Exception):
    """The strategy timed out, crashed, or broke the one-bar protocol."""

    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        self.detail = detail
        text = reason if not detail else f"{reason}: {detail}"
        super().__init__(text)


class Prepared:
    """The book the engine will see, plus the parent-only month maps."""

    def __init__(
        self,
        frame,
        profile: str,
        *,
        reset_day,
        live_start: int,
        month_of_day,
        month_of_session,
        warmup_sessions: int,
        warmup_rule: str,
    ):
        self.frame = frame
        self.profile = profile
        self.reset_day = reset_day
        self.live_start = live_start
        self.month_of_day = month_of_day
        self.month_of_session = month_of_session
        self.warmup_sessions = warmup_sessions
        self.warmup_rule = warmup_rule


class RefereeOutcome:
    def __init__(
        self,
        exit_code: int,
        message: str = "",
        *,
        symbol: str | None = None,
        anonymized: bool = False,
        profile: str = "",
        metrics: dict | None = None,
    ):
        self.exit_code = exit_code
        self.message = message
        self.symbol = symbol
        self.anonymized = anonymized
        self.profile = profile
        self.metrics = metrics


def prepare_book(hidden, *, profile: str, train=None, warmup_days=None) -> Prepared:
    """Concatenate warm-up and the hidden sample without mixing the two rules."""
    name = get_profile(profile).name
    if name == "arena":
        if warmup_days is None:
            warmup_days = ARENA_WARMUP_DAYS
        if isinstance(warmup_days, bool) or not isinstance(warmup_days, int) or warmup_days < 0:
            raise ValueError("warmup_days must be a non-negative integer")
        train_part = _tail_sessions(_stamp(train, live=False), warmup_days) if train is not None else None
    elif name == "desk":
        if warmup_days is not None:
            raise ValueError(
                "Desk warm-up is the whole train history with live false. "
                "A session-count warm-up is the Arena rule."
            )
        train_part = _stamp(train, live=False) if train is not None and len(train) else None
    else:
        raise ValueError(f"unknown profile {profile}")
    hidden_part = _stamp(hidden, live=True)
    if train_part is not None and len(train_part) == 0:
        train_part = None
    _reject_overlap(train_part, hidden_part)
    frame = _concat(train_part, hidden_part)
    used_warmup = train_part is not None
    reset_day = None
    if used_warmup and name == "arena":
        for day, live in zip(frame["day"].tolist(), frame["_live"].tolist()):
            if live:
                reset_day = int(day)
                break
        if reset_day is None:
            raise ValueError("arena warm-up left no hidden session")
    live_start = 0
    if name == "desk":
        seen = set()
        for date, live in zip(frame["date"].tolist(), frame["_live"].tolist()):
            if live:
                continue
            seen.add(str(date))
        live_start = len(seen)
    warmup_sessions = live_start if name == "desk" else (0 if reset_day is None else int(reset_day))
    if not used_warmup:
        rule = "none"
    elif name == "arena":
        rule = "rescale_keep_exposure"
    else:
        rule = "flat_until_live"
    day_month, session_month = _month_maps(frame)
    report = _public_frame(frame)
    return Prepared(
        report,
        name,
        reset_day=reset_day,
        live_start=live_start,
        month_of_day=_lookup(day_month, "day"),
        month_of_session=_lookup(session_month, "session"),
        warmup_sessions=warmup_sessions,
        warmup_rule=rule,
    )


def run_in_process(strategy, prepared: Prepared, **kwargs):
    """The same engine call the referee makes, with the strategy in this process."""
    return _score(strategy, prepared, **kwargs)


def replay(
    strategy_path,
    prepared: Prepared,
    *,
    deny_paths=(),
    memory_bytes: int = DEFAULT_MEMORY_BYTES,
    cpu_seconds: int = DEFAULT_CPU_SECONDS,
    call_timeout: float = DEFAULT_CALL_TIMEOUT_S,
    startup_timeout: float = DEFAULT_STARTUP_TIMEOUT_S,
    **kwargs,
):
    """Run ``prepared`` through the jail. Returns result, metrics, backend, guarantees."""
    _check_timeouts(call_timeout, startup_timeout)
    box = launch(
        strategy_path,
        memory_bytes=int(memory_bytes),
        cpu_seconds=int(cpu_seconds),
        deny_paths=deny_paths,
    )
    pipe = None
    try:
        pipe = _Pipe(box)
        remote = _Remote(pipe, prepared.profile, box, call_timeout=call_timeout)
        ready = pipe.hello(startup_timeout)
        remote.arm(ready)
        try:
            result, metrics = _score(remote, prepared, **kwargs)
        except ValueError as exc:
            if "target" in str(exc):
                raise Disqualified("strategy", str(exc)) from exc
            raise
        return result, metrics, box.backend, box.guarantees
    finally:
        if pipe is not None:
            pipe.close()
        box.close()


def probe(
    strategy_path,
    *,
    deny_paths=(),
    memory_bytes: int = 512 * 1024 * 1024,
    cpu_seconds: int = 30,
    call_timeout: float = DEFAULT_CALL_TIMEOUT_S,
    startup_timeout: float = DEFAULT_STARTUP_TIMEOUT_S,
    bar=None,
    state=None,
):
    """One bar in the jail. Returns the target the strategy produced."""
    _check_timeouts(call_timeout, startup_timeout)
    box = launch(
        strategy_path,
        memory_bytes=int(memory_bytes),
        cpu_seconds=int(cpu_seconds),
        deny_paths=deny_paths,
    )
    pipe = None
    try:
        pipe = _Pipe(box)
        pipe.hello(startup_timeout)
        payload = {
            "op": "bar",
            "bar": bar or {"minute": 0, "close": 1.0},
            "state": state or {"equity": 1.0, "shares": 0, "live": True},
        }
        return pipe.decision(payload, call_timeout)
    finally:
        if pipe is not None:
            pipe.close()
        box.close()


def run_referee(
    *,
    strategy,
    hidden,
    profile,
    out,
    train=None,
    ticker=None,
    sidecar=None,
    train_sidecar=None,
    bar_minutes: int = 1,
    warmup_days=None,
    share_factor=None,
    borrow_rate=None,
    financing_rate=None,
    fee_mult: float = 1.0,
    dividends=None,
    initial_equity: float = 1_000_000.0,
    memory_mb: int = 2048,
    cpu_seconds: int = DEFAULT_CPU_SECONDS,
    call_timeout: float = DEFAULT_CALL_TIMEOUT_S,
    startup_timeout: float = DEFAULT_STARTUP_TIMEOUT_S,
    with_benchmarks: bool = True,
) -> RefereeOutcome:
    """Load the operator files, replay, and write the report. The child is already gone."""
    if not strategy or not hidden or not profile or not out:
        raise ValueError("referee needs --strategy, --hidden, --profile, and --out")
    _check_timeouts(call_timeout, startup_timeout)
    if get_profile(profile).name == "desk" and warmup_days is not None:
        raise ValueError(
            "Desk warm-up is the whole train history with live false. "
            "A session-count warm-up is the Arena rule."
        )
    if not isinstance(memory_mb, int) or isinstance(memory_mb, bool) or memory_mb <= 0:
        raise ValueError("memory cap must be a positive number of megabytes")
    if not isinstance(cpu_seconds, int) or isinstance(cpu_seconds, bool) or cpu_seconds <= 0:
        raise ValueError("cpu cap must be a positive number of seconds")
    name = get_profile(profile).name
    hidden_frame, scale, symbol, anonymized, hidden_minutes, hidden_dividends, deny, explicit_borrow, designer = _load_side(
        hidden,
        ticker=ticker,
        sidecar=sidecar,
        share_factor=share_factor,
        bar_minutes=bar_minutes,
        dividends_path=dividends,
        profile=name,
    )
    train_frame = None
    if train:
        train_frame, _train_scale, _train_symbol, _train_anon, _train_minutes, _ignored, train_deny, _train_borrow, _train_designer = _load_side(
            train,
            ticker=ticker,
            sidecar=train_sidecar,
            share_factor=share_factor,
            bar_minutes=bar_minutes,
            dividends_path=None,
            profile=name,
            require_same_scale=scale,
        )
        deny.extend(train_deny)
    if borrow_rate is None and explicit_borrow is not None:
        borrow_rate = explicit_borrow
    if anonymized:
        require_anonymizable_symbol(symbol)
    prepared = prepare_book(hidden_frame, profile=name, train=train_frame, warmup_days=warmup_days)
    out_dir = Path(out)
    if _inside(hidden, out_dir):
        raise ValueError("the report must not live inside the hidden sample")
    deny.append(out_dir)
    common = dict(
        borrow_rate=borrow_rate,
        financing_rate=financing_rate,
        fee_mult=fee_mult,
        dividends=hidden_dividends,
        initial_equity=initial_equity,
        anonymized=anonymized,
        designer=designer,
    )
    if not designer:
        common["share_scale"] = scale
    guarantees = describe_isolation()
    backend = guarantees.get("backend")
    try:
        result, metrics, backend, guarantees = replay(
            strategy,
            prepared,
            deny_paths=deny,
            memory_bytes=int(memory_mb) * 1024 * 1024,
            cpu_seconds=int(cpu_seconds),
            call_timeout=call_timeout,
            startup_timeout=startup_timeout,
            **common,
        )
    except Disqualified as exc:
        _write_disqualified(
            out_dir,
            profile=name,
            reason=exc.reason,
            detail=exc.detail,
            backend=backend,
            guarantees=guarantees,
            anonymized=anonymized,
            call_timeout=call_timeout,
            warmup=prepared.warmup_rule,
        )
        detail = exc.detail.strip()
        if len(detail) > 500:
            detail = detail[:500]
        message = f"disqualified: {exc.reason}" if not detail else f"disqualified: {exc.reason}: {detail}"
        return RefereeOutcome(1, message, symbol=symbol, anonymized=anonymized, profile=name)
    except SandboxError as exc:
        _write_status(
            out_dir,
            profile=name,
            status="sandbox",
            reason="sandbox",
            detail=str(exc),
            backend=backend,
            guarantees=guarantees,
            anonymized=anonymized,
            call_timeout=call_timeout,
        )
        raise
    books = {"strategy": (result, metrics)}
    if with_benchmarks:
        from .runner import _benchmarks

        for label, cls in _benchmarks(name):
            books[label] = _score(cls(), prepared, **common)
    from .runner import write_books

    payload = write_books(
        out_dir,
        name,
        symbol,
        hidden_minutes,
        prepared.frame,
        books,
        anonymized=anonymized,
    )
    _write_referee_json(
        out_dir,
        profile=name,
        backend=backend,
        guarantees=guarantees,
        call_timeout=call_timeout,
        startup_timeout=startup_timeout,
        warmup=prepared.warmup_rule,
        warmup_sessions=prepared.warmup_sessions,
        anonymized=anonymized,
        disqualified=False,
    )
    return RefereeOutcome(0, "", symbol=symbol, anonymized=anonymized, profile=name, metrics=payload)


def _check_timeouts(call_timeout, startup_timeout) -> None:
    for label, value in (("call timeout", call_timeout), ("startup timeout", startup_timeout)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise ValueError(f"{label} must be a positive number of seconds")


def _stamp(frame, *, live: bool):
    if frame is None:
        return None
    out = frame.copy()
    if "_calendar" not in out.columns:
        if "date" not in out.columns:
            raise ValueError("bars need a date column")
        out["_calendar"] = out["date"].map(lambda value: str(value))
    else:
        out["_calendar"] = out["_calendar"].map(lambda value: str(value))
    if "date" not in out.columns:
        out["date"] = out["_calendar"]
    out["_live"] = bool(live)
    return out


def _tail_sessions(frame, count: int):
    if frame is None or count <= 0 or len(frame) == 0:
        return None
    order = []
    seen = set()
    pairs = sorted(
        ((str(cal), int(day)) for day, cal in zip(frame["day"].tolist(), frame["_calendar"].tolist())),
        key=lambda item: (item[0], item[1]),
    )
    for _cal, day in pairs:
        if day not in seen:
            seen.add(day)
            order.append(day)
    keep = set(order[-count:])
    kept = frame[frame["day"].isin(keep)].copy()
    return kept if len(kept) else None


def _reject_overlap(train, hidden) -> None:
    """Overlap is a real calendar collision.

    Anonymized packs both label their first session ``S00001``. That public id
    is not the calendar, so it is not the overlap check.
    """
    if train is None:
        return
    train_cal = [str(value) for value in train["_calendar"].tolist()]
    hidden_cal = [str(value) for value in hidden["_calendar"].tolist()]
    if set(train_cal) & set(hidden_cal):
        raise ValueError("train and hidden sessions overlap")
    if max(train_cal) >= min(hidden_cal):
        raise ValueError("train history is not strictly before the hidden sample")


def _concat(train, hidden):
    import pandas as pd

    parts = [part for part in (train, hidden) if part is not None and len(part)]
    if not parts:
        raise ValueError("the referee received no bars")
    frame = pd.concat(parts, ignore_index=True)
    phase_rank = {"pre": 0, "rth": 1, "post": 2}
    frame["_phase_rank"] = frame["session"].map(lambda value: phase_rank.get(str(value), 9))
    frame = frame.sort_values(["_calendar", "_phase_rank", "minute"], kind="mergesort").reset_index(drop=True)
    seen = {}
    days = []
    for calendar in frame["_calendar"].tolist():
        key = str(calendar)
        if key not in seen:
            seen[key] = len(seen)
        days.append(seen[key])
    frame["day"] = days
    return _separate_public_sessions(frame)


def _separate_public_sessions(frame):
    """Give a second pack new ``S#####`` ids when the public labels collide.

    Named dates are already the calendar label and are left alone. The
    calendar used for fee months stays in ``_calendar`` and is not a column
    the child is sent.
    """
    groups: dict[str, set[str]] = {}
    for date, calendar in zip(frame["date"].tolist(), frame["_calendar"].tolist()):
        groups.setdefault(str(date), set()).add(str(calendar))
    calendars = []
    seen = set()
    for calendar in frame["_calendar"].tolist():
        key = str(calendar)
        if key not in seen:
            seen.add(key)
            calendars.append(key)
    collided = any(len(values) > 1 for values in groups.values()) or len(groups) != len(calendars)
    if not collided:
        return frame
    mapping = {calendar: f"S{index:05d}" for index, calendar in enumerate(calendars, start=1)}
    frame = frame.copy()
    frame["date"] = [mapping[str(calendar)] for calendar in frame["_calendar"].tolist()]
    return frame


def _month_id(text: str) -> int:
    if len(text) < 7 or text[4] != "-" or not text[:4].isdigit() or not text[5:7].isdigit():
        raise ValueError(f"calendar value {text!r} is not a YYYY-MM-DD date")
    month = int(text[5:7])
    if not 1 <= month <= 12:
        raise ValueError(f"calendar value {text!r} is not a YYYY-MM-DD date")
    return int(text[:4]) * 100 + month


def _month_maps(frame):
    day_month = {}
    session_month = {}
    for day, date, calendar in zip(frame["day"].tolist(), frame["date"].tolist(), frame["_calendar"].tolist()):
        month = _month_id(str(calendar))
        day = int(day)
        session = str(date)
        if day in day_month and day_month[day] != month:
            raise ValueError("one session spans two calendar months")
        if session in session_month and session_month[session] != month:
            raise ValueError("one session spans two calendar months")
        day_month[day] = month
        session_month[session] = month
    return day_month, session_month


def _lookup(mapping, label):
    def month(key):
        try:
            if label == "day":
                return mapping[int(key)]
            return mapping[str(key)]
        except KeyError as exc:
            raise ValueError(f"no calendar month for {label} {key}") from exc

    return month


def _public_frame(frame):
    drop = [
        column
        for column in frame.columns
        if str(column).startswith("_") or column in {"symbol", "timestamp_et", "epoch_utc"}
    ]
    return frame.drop(columns=drop)


def _designer_score(strategy, prepared: Prepared, *, borrow_rate, financing_rate, fee_mult, dividends):
    """Flat fee. This function does not read ``share_scale`` or ``share_factor``."""
    from .designer_run import execute as designer_execute

    name = prepared.profile
    kwargs = {
        "borrow_rate_annual": borrow_rate,
        "financing_rate_annual": financing_rate,
        "fee_mult": fee_mult,
        "dividends": dividends,
    }
    if name == "arena":
        kwargs["reset_day"] = prepared.reset_day
    elif name == "desk":
        kwargs["warmup_sessions"] = prepared.live_start
    return designer_execute(strategy, prepared.frame, name, **kwargs)


def _score(
    strategy,
    prepared: Prepared,
    *,
    share_scale: float = 1.0,
    borrow_rate=None,
    financing_rate=None,
    fee_mult: float = 1.0,
    dividends=None,
    initial_equity: float = 1_000_000.0,
    anonymized: bool = False,
    designer: bool = False,
):
    from .runner import _cost_basis

    if designer:
        return _designer_score(
            strategy,
            prepared,
            borrow_rate=borrow_rate,
            financing_rate=financing_rate,
            fee_mult=fee_mult,
            dividends=dividends,
        )
    if not math.isfinite(float(share_scale)) or float(share_scale) <= 0.0:
        raise ValueError("share scale must be a positive finite number")
    if not math.isfinite(float(initial_equity)) or float(initial_equity) <= 0.0:
        raise ValueError("initial equity must be positive")
    name = prepared.profile
    scale = float(share_scale)
    if name == "arena":
        if fee_mult != 1.0:
            raise ValueError("fee-mult is a desk cost-stress switch. An arena run uses the unmodified Arena schedule.")
        if dividends:
            raise ValueError("The arena profile does not credit dividends.")
        if financing_rate not in (None, 0.0):
            raise ValueError("Arena financing is 0. A financing switch is not part of this referee.")
        borrow = get_profile(name).borrow_rate_annual if borrow_rate is None else borrow_rate
        result = engine_arena.run(
            strategy,
            prepared.frame,
            initial_equity=initial_equity,
            share_scale=scale,
            month_of_day=prepared.month_of_day,
            reset_day=prepared.reset_day,
            borrow_rate_annual=borrow,
        )
        metrics = enrich_arena_metrics(result, engine_arena.metrics(result))
        metrics["share_factor"] = float(result["share_scale"])
        metrics["borrow_rate_annual"] = borrow
        metrics["financing_rate_annual"] = 0.0
        metrics["cost_basis"] = _cost_basis(name, float(result["share_scale"]), anonymized=anonymized)
        return result, metrics
    if name == "desk":
        rate = engine_desk.D.FINANCING_RATE_ANNUAL if financing_rate is None else financing_rate
        borrow = get_profile(name).borrow_rate_annual if borrow_rate is None else borrow_rate
        rows = frame_to_desk_bars(prepared.frame)
        result = engine_desk.run(
            strategy,
            rows,
            DeskConfig(
                initial_equity=initial_equity,
                share_factor=scale,
                borrow_rate_annual=borrow,
                financing_rate_annual=rate,
                fee_mult=fee_mult,
                live_start_ordinal=prepared.live_start,
                dividends=dict(dividends or {}),
                month_of_session=prepared.month_of_session,
            ),
        )
        metrics = engine_desk.metrics(result)
        metrics["cost_basis"] = _cost_basis(name, float(metrics["share_factor"]), anonymized=anonymized)
        return result, metrics
    raise ValueError(f"unknown profile {name}")


def _jsonable(value):
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, bool):
        return bool(value)
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if value == math.inf:
            return "Infinity"
        if value == -math.inf:
            return "-Infinity"
        return float(value)
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            text = str(key)
            if text.startswith("_") or text in _HIDDEN_KEYS:
                continue
            out[text] = _jsonable(item)
        return out
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if type(value).__name__ in {"bool_", "bool8"}:
        return bool(value)
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return _jsonable(item())
        except Exception:
            pass
    raise Disqualified("protocol", f"cannot encode {type(value).__name__}")


def _decode_target(value):
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value)
    if value == "NaN":
        return float("nan")
    if value == "Infinity":
        return float("inf")
    if value == "-Infinity":
        return float("-inf")
    raise Disqualified("protocol", "decision is not a target")


class _Pipe:
    def __init__(self, box):
        self.box = box
        self._buf = bytearray()
        self._err = bytearray()
        self._cv = threading.Condition()
        self._stdout_eof = False
        self._protocol = ""
        self._awaiting = True
        self._closed = False
        # The parent's resident-set cap, when this worker was given one.
        # A decision that is still allocating near that cap is not a timeout:
        # the parent poll is what classifies it as memory.
        self._memory_cap = None
        self._rss_mark = None
        self._stdout_thread = threading.Thread(target=self._read_fd, args=(box.stdout_fd, False), daemon=True)
        self._stderr_thread = threading.Thread(target=self._read_fd, args=(box.stderr_fd, True), daemon=True)
        self._stdout_thread.start()
        self._stderr_thread.start()

    def _read_fd(self, fd: int, stderr: bool) -> None:
        while True:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                chunk = b""
            with self._cv:
                if self._closed:
                    return
                if stderr:
                    self._err.extend(chunk)
                    if len(self._err) > 8000:
                        del self._err[:-8000]
                else:
                    if not chunk:
                        self._stdout_eof = True
                    else:
                        if not self._awaiting:
                            self._protocol = "child wrote when no request was outstanding"
                        self._buf.extend(chunk)
                        if len(self._buf) > 1_000_000:
                            self._protocol = "child wrote more than 1 MB"
                self._cv.notify_all()
                if not chunk:
                    return

    def _write(self, payload: dict) -> None:
        line = json.dumps(_jsonable(payload), separators=(",", ":"), allow_nan=False).encode("utf-8") + b"\n"
        view = memoryview(line)
        try:
            while view:
                count = os.write(self.box.stdin_fd, view)
                view = view[count:]
        except OSError as exc:
            detail = self.stderr_text() or str(exc)
            self._raise_if_jail(detail)
            raise Disqualified("crash", detail) from exc

    def set_memory_cap(self, memory_bytes: int | None) -> None:
        """Remember the parent resident-set cap. It is not sent to the jail."""
        if memory_bytes is None:
            self._memory_cap = None
            return
        self._memory_cap = int(memory_bytes)

    def _read_line(self, timeout: float) -> bytes:
        deadline = time.monotonic() + timeout
        while True:
            with self._cv:
                if self._protocol:
                    raise Disqualified("protocol", self._protocol)
                newline = self._buf.find(b"\n")
                if newline >= 0:
                    line = bytes(self._buf[:newline])
                    del self._buf[: newline + 1]
                    if self._buf:
                        self._protocol = "child wrote past the one-line decision"
                        raise Disqualified("protocol", self._protocol)
                    self._awaiting = False
                    return line
                if self._stdout_eof:
                    detail = self._err.decode("utf-8", "replace").strip()
                    self._raise_if_jail(detail)
                    raise Disqualified("crash", detail or "strategy process closed the pipe")
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    self._cv.wait(remaining)
                    continue
            if self._allocation_still_under_cap():
                deadline = time.monotonic() + timeout
                continue
            break
        self.box.terminate()
        detail = self.stderr_text()
        raise Disqualified("timeout", detail)

    def _allocation_still_under_cap(self) -> bool:
        """True when this call is still growing and has not crossed the cap.

        A sleep holds a flat resident set and times out on the original
        budget. A bomb that is already near the cap, but not over it, gets
        another budget so the parent poll can report the memory stop. Crossing
        the cap here raises that stop directly.
        """
        cap = self._memory_cap
        if cap is None:
            return False
        from .limits import process_tree_rss_bytes

        rss = process_tree_rss_bytes(os.getpid())
        if rss is None:
            return False
        if rss > cap:
            self.box.terminate()
            raise RuntimeError(f"walk-forward worker exceeded memory limit ({rss} > {cap})")
        previous = self._rss_mark
        self._rss_mark = rss
        grown = previous is None or rss >= previous + 32 * 1024 * 1024
        return bool(grown and rss * 2 >= cap)

    def _raise_if_jail(self, detail: str) -> None:
        lowered = detail.lower()
        if any(marker in lowered for marker in _JAIL_MARKERS):
            raise SandboxError(detail or "the sandbox exited before the strategy started")

    def stderr_text(self) -> str:
        with self._cv:
            return self._err.decode("utf-8", "replace").strip()

    def hello(self, timeout: float) -> dict:
        self._write({"op": "hello"})
        payload = self._message(self._read_line(timeout))
        if payload.get("op") == "error":
            raise Disqualified("strategy", str(payload.get("error") or "strategy failed to load"))
        if payload.get("op") != "ready":
            raise Disqualified("protocol", "expected ready")
        return payload

    def decision(self, payload: dict, timeout: float):
        with self._cv:
            if self._protocol:
                raise Disqualified("protocol", self._protocol)
            if self._buf:
                raise Disqualified("protocol", "unread output before the next bar")
            self._awaiting = True
        self._write(payload)
        message = self._message(self._read_line(timeout))
        if message.get("op") == "error":
            raise Disqualified("strategy", str(message.get("error") or "strategy error"))
        if message.get("op") != "decision" or "target" not in message:
            raise Disqualified("protocol", "expected a decision")
        return _decode_target(message.get("target"))

    def state(self, timeout: float) -> dict:
        """JSON-safe attributes the strategy stored on itself. Not a bar."""
        with self._cv:
            if self._protocol:
                raise Disqualified("protocol", self._protocol)
            if self._buf:
                raise Disqualified("protocol", "unread output before the state request")
            self._awaiting = True
        self._write({"op": "state"})
        message = self._message(self._read_line(timeout))
        if message.get("op") == "error":
            raise Disqualified("strategy", str(message.get("error") or "strategy error"))
        values = message.get("values")
        if message.get("op") != "state" or not isinstance(values, dict):
            raise Disqualified("protocol", "expected state")
        return values

    def _message(self, line: bytes) -> dict:
        try:
            payload = json.loads(line.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise Disqualified("protocol", "decision was not JSON") from exc
        if not isinstance(payload, dict):
            raise Disqualified("protocol", "decision was not an object")
        return payload

    def close(self) -> None:
        with self._cv:
            self._closed = True
            self._cv.notify_all()


class _Remote:
    def __init__(self, pipe: _Pipe, profile: str, box, *, call_timeout: float):
        self._pipe = pipe
        self._profile = profile
        self._box = box
        self._timeout = call_timeout
        self.WANTS_EXTENDED_HOURS = False

    def arm(self, ready: dict) -> None:
        self.WANTS_EXTENDED_HOURS = bool(ready.get("wants_extended_hours"))
        if ready.get("has_day_end"):
            self.on_day_end = self._day_end

    def on_bar(self, bar, state):
        key = "account" if self._profile == "desk" else "state"
        return self._pipe.decision({"op": "bar", "bar": bar, key: state}, self._timeout)

    def _day_end(self, summary) -> None:
        self._pipe.decision({"op": "day_end", "summary": summary}, self._timeout)

    def snapshot(self) -> dict:
        return self._pipe.state(self._timeout)

    def exit_code(self) -> int | None:
        """The jail process status. Waits briefly when it is already exiting."""
        proc = self._box.process
        code = proc.poll()
        if code is not None:
            return code
        try:
            return proc.wait(timeout=2)
        except Exception:
            return proc.poll()

    def spill_stdio(self, *, wait: bool) -> None:
        """Copy the jail's print log onto this process's stdout.

        Designer runs capture prints in the jail so they are not protocol
        lines. The worker's own stdio file is what an operator error attaches.
        Waiting is for a child that is already leaving: a live child is left
        alone so a parent-side failure does not sit on the timeout.
        """
        if wait:
            self.exit_code()
        path = Path(self._box.workdir) / "stdio.log"
        try:
            data = path.read_bytes()
        except OSError:
            return
        if not data:
            return
        try:
            os.write(1, data[:8000])
        except OSError:
            pass


@contextmanager
def jailed_strategy(strategy_path, profile, *, deny_paths, memory_bytes, cpu_seconds, call_timeout, memory_cap=None):
    """Run a strategy file in the referee jail. The caller keeps the engine.

    ``on_bar`` sends one bar and reads one decision. The dividend map, the
    accounting record, and the share scale are not written on the pipe.
    ``snapshot`` reads the attributes the strategy stored on itself.
    """
    box = launch(
        strategy_path,
        memory_bytes=int(memory_bytes),
        cpu_seconds=int(cpu_seconds),
        deny_paths=deny_paths,
        capture_stdio=True,
    )
    pipe = _Pipe(box)
    pipe.set_memory_cap(memory_cap)
    try:
        ready = pipe.hello(min(60.0, float(call_timeout)))
        remote = _Remote(pipe, profile, box, call_timeout=float(call_timeout))
        remote.arm(ready)
        yield remote
    finally:
        try:
            pipe.close()
        finally:
            box.terminate()
            box.close()


def _load_side(
    path,
    *,
    ticker,
    sidecar,
    share_factor,
    bar_minutes,
    dividends_path,
    profile,
    require_same_scale=None,
):
    source = Path(path)
    pack = source.is_dir() and (source / "manifest.json").is_file()
    deny = [source if source.is_dir() else source.parent]
    if pack:
        if ticker:
            raise ValueError("a pack run does not take --ticker")
        if not sidecar:
            raise ValueError("a pack run needs --sidecar")
        if share_factor is not None:
            raise ValueError(
                "--share-factor is refused on a pack run. The sidecar holds the scale."
            )
        from .pack import load_pack, load_sidecar, require_build_price_factor

        loaded = load_pack(source)
        secret = load_sidecar(sidecar)
        try:
            if secret["pack_id"] != loaded.pack_id:
                raise ValueError("sidecar does not match this pack")
            if bool(secret["anonymized"]) != loaded.anonymized:
                raise ValueError("sidecar anonymized flag does not match the pack")
            price_factor = require_build_price_factor(secret)
            scale = float(secret["share_scale"])
            symbol = str(secret["symbol"])
            anonymized = bool(secret["anonymized"])
            date_map = {str(key): str(value) for key, value in secret["date_map"].items()}
            if secret.get("borrow_rate_source") == "explicit":
                sidecar_borrow = float(secret["borrow_rate_annual"])
            else:
                sidecar_borrow = None
        finally:
            secret.clear()
            del secret
            gc.collect()
        if require_same_scale is not None and scale != require_same_scale:
            raise ValueError("train and hidden packs do not use the same share scale")
        frame = loaded.frame.copy()
        calendar = []
        for day in frame["day"].tolist():
            key = str(int(day))
            if key not in date_map:
                raise ValueError(f"sidecar date map has no entry for session {key}")
            calendar.append(date_map[key])
        frame["_calendar"] = calendar
        if dividends_path is not None:
            from .runner import _designer_dividends, _pack_dividends

            loaded_dividends = load_dividends(dividends_path)
            if anonymized:
                dividends = _designer_dividends(loaded_dividends, date_map, price_factor)
            else:
                dividends = _pack_dividends(
                    loaded_dividends,
                    anonymized=False,
                    share_scale=1.0,
                    date_map=date_map,
                )
        else:
            dividends = None
        deny.append(Path(sidecar).resolve().parent)
        return frame, scale, symbol, anonymized, int(loaded.bar_minutes), dividends, deny, sidecar_borrow, True
    if sidecar:
        raise ValueError("named data does not take --sidecar")
    if share_factor not in (None, 1.0):
        raise ValueError(
            f"share_factor {share_factor} is not 1.0. Raw runs use 1.0. 0.9 is not a kit constant."
        )
    if not ticker:
        raise ValueError("named data needs --ticker")
    loaded = load_named(source, ticker, bar_minutes=bar_minutes)
    if not loaded.inspection.runnable:
        raise ValueError(f"{ticker} is not runnable. This phase executes raw prices only.")
    if loaded.inspection.share_factor != 1.0:
        raise ValueError("share_factor is not 1.0. Raw runs use 1.0. 0.9 is not a kit constant.")
    if require_same_scale not in (None, 1.0):
        raise ValueError("train and hidden packs do not use the same share scale")
    frame = loaded.frame.copy()
    frame["_calendar"] = frame["date"].map(lambda value: str(value))
    dividends = load_dividends(dividends_path) if dividends_path else None
    return frame, 1.0, str(ticker), False, int(bar_minutes), dividends, deny, None, False


def _inside(sample, out_dir: Path) -> bool:
    sample_path = Path(sample).resolve()
    root = sample_path if sample_path.is_dir() else sample_path.parent
    out = Path(out_dir).resolve()
    try:
        out.relative_to(root)
    except ValueError:
        return False
    return True


def _referee_document(**fields) -> dict:
    document = {
        "profile": fields["profile"],
        "backend": fields.get("backend"),
        "guarantees": fields.get("guarantees") or {},
        "call_timeout_s": fields["call_timeout"],
        "startup_timeout_s": fields.get("startup_timeout", DEFAULT_STARTUP_TIMEOUT_S),
        "warmup": fields.get("warmup"),
        "warmup_sessions": fields.get("warmup_sessions"),
        "disqualified": bool(fields.get("disqualified")),
        "anonymized": bool(fields.get("anonymized")),
        "fail_closed": True,
    }
    if fields.get("reason"):
        document["reason"] = fields["reason"]
    return document


def _write_referee_json(out_dir: Path, **fields) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    write_json(out_dir / "referee.json", _referee_document(**fields))


def _write_status(out_dir: Path, *, profile, status, reason, detail, backend, guarantees, anonymized, call_timeout) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    detail = str(detail or "")
    if len(detail) > 2000:
        detail = detail[:2000]
    write_json(
        out_dir / "error.json",
        {
            "profile": profile,
            "status": status,
            "reason": reason,
            "error": detail,
            "anonymized": bool(anonymized),
            "timeout_s": call_timeout,
        },
    )
    _write_referee_json(
        out_dir,
        profile=profile,
        backend=backend,
        guarantees=guarantees,
        call_timeout=call_timeout,
        anonymized=anonymized,
        disqualified=True,
        reason=reason,
    )


def _write_disqualified(out_dir, *, profile, reason, detail, backend, guarantees, anonymized, call_timeout, warmup) -> None:
    _write_status(
        out_dir,
        profile=profile,
        status="disqualified",
        reason=reason,
        detail=detail,
        backend=backend,
        guarantees=guarantees,
        anonymized=anonymized,
        call_timeout=call_timeout,
    )
    document = json.loads((out_dir / "referee.json").read_text(encoding="utf-8"))
    document["warmup"] = warmup
    write_json(out_dir / "referee.json", document)
