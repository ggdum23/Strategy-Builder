"""Operator-side work. No Qt, and no sidecar fields in the status payload.

The worker process is the parent the runner already expects: it may read the
sidecar, then ``run_pack`` drops that document before the strategy process
starts. Status files receive paths and headlines only.
"""

from __future__ import annotations

import json
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path

from ..data import load_dividends, load_named
from ..pack import build_pack
from ..registry import (
    entry_from_inspection,
    load_or_new,
    load_registry,
    parse_phase_map,
    parse_splits,
    save_registry,
    upsert_series,
)
from ..report import json_ready
from ..runner import run_named, run_pack, walk_forward
from ..strategy_loader import load_strategy_class
from .backend import read_designer_manifest
from .preferences import as_path
from .validate import config_record, normalize, validate_config

_PUBLIC_PACK_KEYS = (
    "pack_dir",
    "sidecar_path",
    "anonymized",
    "n_chunks",
    "n_sessions",
    "bar_minutes",
    "pack_id",
    "manifest",
    "sidecar_displayed",
)


def _status(path, payload: dict) -> None:
    if not path:
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(json_ready(payload), indent=2, allow_nan=False) + "\n"
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(target)


def _cancelled(path) -> bool:
    if not path:
        return False
    return Path(str(path) + ".cancel").is_file()


def _write_record(out: Path, config: dict) -> None:
    out.mkdir(parents=True, exist_ok=True)
    record = config_record(config)
    text = json.dumps(json_ready(record), indent=2, allow_nan=False) + "\n"
    (out / "run_config.json").write_text(text, encoding="utf-8")


def _dividends(config: dict):
    if not config.get("dividends"):
        return None
    return load_dividends(as_path(config["dividends"]))


def _optional_float(value):
    if value is None or value == "":
        return None
    return float(value)


def _optional_int(value):
    if value is None or value == "":
        return None
    return int(value)


def inspect_series(src, symbol: str, *, bar_minutes: int = 1, price_kind: str, phase_map_text: str = "", splits_text: str = "") -> dict:
    """Inspect filled files. Declared splits are not passed into the loader."""
    if not str(symbol).strip():
        raise ValueError("ticker is required")
    if price_kind not in ("raw", "total_return"):
        raise ValueError("price kind must be raw or total_return. Refusing to guess.")
    phase_map = parse_phase_map(phase_map_text)
    splits = parse_splits(splits_text)
    loaded = load_named(
        src,
        str(symbol).strip(),
        bar_minutes=int(bar_minutes),
        phase_map=phase_map,
        price_kind=price_kind,
    )
    info = loaded.inspection.as_dict()
    gaps = []
    for segment in info["session_segments"]:
        if segment.get("gapped_sessions"):
            gaps.append(
                f"{segment['label']} has {segment['gapped_sessions']} session(s) with a hole in the minute grid"
            )
        if segment.get("variant_sessions"):
            gaps.append(
                f"{segment['label']} has {segment['variant_sessions']} session(s) whose hours are not the modal window"
            )
    runnable = bool(info["runnable"]) and not splits and info["price_adjustment"] == "raw"
    return {
        "inspection": info,
        "declared_splits": [dict(item) for item in splits],
        "gaps": gaps,
        "runnable_if_recorded": runnable,
    }


def record_series(
    src,
    symbol: str,
    registry_path,
    *,
    profile: str,
    price_kind: str,
    bar_minutes: int = 1,
    phase_map_text: str = "",
    splits_text: str = "",
    borrow_rate: float | None = None,
) -> dict:
    """Registry write. Profile and price kind are required, matching the CLI."""
    if not profile:
        raise ValueError("recording a series needs a profile. Refusing to guess from the ticker.")
    if price_kind not in ("raw", "total_return"):
        raise ValueError("recording a series needs price kind raw or total_return. Refusing to guess.")
    if not str(registry_path).strip():
        raise ValueError("recording a series needs a registry path")
    phase_map = parse_phase_map(phase_map_text)
    splits = parse_splits(splits_text)
    loaded = load_named(
        src,
        str(symbol).strip(),
        bar_minutes=int(bar_minutes),
        phase_map=phase_map,
        price_kind=price_kind,
    )
    entry = entry_from_inspection(
        loaded.inspection,
        source_root=str(src),
        profile=profile,
        borrow_rate_annual=None if borrow_rate is None else float(borrow_rate),
        splits=splits,
    )
    document = upsert_series(load_or_new(registry_path), entry)
    save_registry(registry_path, document)
    return {"registry": str(registry_path), "entry": entry, "series_count": len(document["series"])}


def list_registry(registry_path) -> dict:
    document = load_registry(registry_path)
    return document


def build_pack_job(
    src,
    symbol: str,
    out_dir,
    sidecar_path,
    registry_path,
    *,
    anonymize: bool | None,
    profile: str | None = None,
    bar_minutes: int = 1,
    price_kind: str = "raw",
    phase_map_text: str = "",
) -> dict:
    """Build a pack. The returned dict has no scale factor and no date map."""
    if anonymize is None:
        raise ValueError("choose named or anonymized. Refusing to guess.")
    if not str(registry_path).strip():
        raise ValueError("pack build needs the registry this series was recorded in")
    result = build_pack(
        src,
        str(symbol).strip(),
        out_dir,
        sidecar_path,
        anonymize=bool(anonymize),
        profile=profile or None,
        registry_path=registry_path,
        bar_minutes=int(bar_minutes),
        phase_map=parse_phase_map(phase_map_text),
        price_kind=price_kind,
    )
    manifest = read_designer_manifest(result.pack_dir)
    payload = {
        "pack_dir": str(result.pack_dir),
        "sidecar_path": str(result.sidecar_path),
        "anonymized": bool(result.anonymized),
        "n_chunks": int(result.n_chunks),
        "n_sessions": int(result.n_sessions),
        "bar_minutes": int(result.bar_minutes),
        "pack_id": result.pack_id,
        "manifest": manifest,
        "sidecar_displayed": False,
    }
    extra = set(payload) - set(_PUBLIC_PACK_KEYS)
    if extra:
        raise RuntimeError(f"pack result leaked {sorted(extra)}")
    return payload


def _named_kwargs(config: dict) -> dict:
    return {
        "bar_minutes": int(config["bar_minutes"]),
        "borrow_rate_annual": _optional_float(config["borrow_rate_annual"]),
        "financing_rate_annual": _optional_float(config["financing_rate_annual"]),
        "fee_mult": float(config["fee_mult"]),
        "dividends": _dividends(config),
        "warmup_sessions": int(config["warmup_sessions"] or 0),
        "initial_equity": float(config["initial_equity"]),
        "with_benchmarks": True,
    }


def _pack_kwargs(config: dict) -> dict:
    return {
        "borrow_rate_annual": _optional_float(config["borrow_rate_annual"]),
        "financing_rate_annual": _optional_float(config["financing_rate_annual"]),
        "fee_mult": float(config["fee_mult"]),
        "dividends": _dividends(config),
        "warmup_sessions": int(config["warmup_sessions"] or 0),
        "initial_equity": float(config["initial_equity"]),
        "with_benchmarks": True,
    }


def _headline(payload: dict) -> dict:
    """A few fields the status line may show. The dashboard rereads the files."""
    keys = ("final_equity", "total_return", "profile", "symbol", "n_fills", "disqualified")
    return {key: payload.get(key) for key in keys if key in payload}


def _run_single(config: dict, status_path) -> dict:
    issues = validate_config(config)
    if issues:
        raise ValueError("; ".join(issues))
    config = normalize(config)
    out = as_path(config["out"])
    _write_record(out, config)
    _status(status_path, {"state": "running", "message": "Running the engine and the profile benchmarks"})
    strategy = load_strategy_class(config["strategies"][0])
    if config["source"] == "pack":
        payload = run_pack(
            strategy,
            config["pack"],
            config["sidecar"],
            config["profile"],
            out,
            **_pack_kwargs(config),
        )
    else:
        payload = run_named(
            strategy,
            config["src"],
            config["ticker"],
            config["profile"],
            out,
            **_named_kwargs(config),
        )
    payload.pop("strategy_observer", None)
    return {"out": str(out), "headline": _headline(payload)}


def _run_walk(config: dict, status_path) -> dict:
    issues = validate_config(config)
    if issues:
        raise ValueError("; ".join(issues))
    config = normalize(config)
    out = as_path(config["out"])
    _write_record(out, config)
    _status(status_path, {"state": "running", "message": "Loading the named series in the operator process"})
    loaded = load_named(config["src"], config["ticker"], bar_minutes=int(config["bar_minutes"]))
    holder = {
        "frame": loaded.frame,
        "notes": tuple(loaded.inspection.notes),
        "factor": loaded.inspection.share_factor,
    }
    del loaded
    strategy = load_strategy_class(config["strategies"][0])
    _status(status_path, {"state": "running", "message": "Design window, then the locked tail"})
    summary, _slices, _design, _locked = walk_forward(
        strategy,
        holder.pop("frame"),
        config["profile"],
        out,
        symbol=config["ticker"],
        bar_minutes=int(config["bar_minutes"]),
        design_end_day=_optional_int(config["design_end_day"]),
        design_end_date=config["design_end_date"] or None,
        design_fraction=_optional_float(config["design_fraction"]),
        warmup_days=int(config["warmup_days"] or 0),
        share_factor=holder["factor"],
        borrow_rate_annual=_optional_float(config["borrow_rate_annual"]),
        financing_rate_annual=_optional_float(config["financing_rate_annual"]),
        fee_mult=float(config["fee_mult"]),
        dividends=_dividends(config),
        initial_equity=float(config["initial_equity"]),
        with_benchmarks=True,
        inspection_notes=holder["notes"],
    )
    return {
        "out": str(out),
        "design_end_date": summary.get("design_end_date"),
        "locked_start_date": summary.get("locked_start_date"),
        "warmup_rule": summary.get("warmup_rule"),
        "design_sessions": summary.get("design_sessions"),
        "locked_sessions": summary.get("locked_sessions"),
    }


def _slug(path: str, used: set[str]) -> str:
    file = as_path(path)
    base = file.parent.name if file.name == "strategy.py" else file.stem
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in base).strip("._") or "strategy"
    candidate = cleaned
    index = 2
    while candidate in used:
        candidate = f"{cleaned}-{index}"
        index += 1
    used.add(candidate)
    return candidate


@dataclass(frozen=True)
class _StrategyRef:
    strategy_id: str
    path: str


@dataclass(frozen=True)
class _NamedMarket:
    src: str
    symbol: str
    bar_minutes: int = 1
    source_id: str | None = None


@dataclass(frozen=True)
class _PackMarket:
    pack_dir: str
    sidecar_path: str
    source_id: str | None = None


class _CancelToken:
    """Same two methods as ``backtester.parallel.CancelToken`` when Phase 3 is absent."""

    def __init__(self) -> None:
        self._flag = threading.Event()

    def cancel(self) -> None:
        self._flag.set()

    def cancelled(self) -> bool:
        return self._flag.is_set()


def _parallel_types():
    """Prefer the Phase 3 types. Fall back to local stand-ins so the call still happens."""
    try:
        from backtester.parallel import CancelToken, NamedMarket, PackMarket, StrategyRef
    except ImportError:
        return _StrategyRef, _NamedMarket, _PackMarket, _CancelToken
    return StrategyRef, NamedMarket, PackMarket, CancelToken


def _watch_cancel(status_path, token) -> None:
    while not token.cancelled():
        if _cancelled(status_path):
            token.cancel()
            return
        time.sleep(0.05)


def _progress_fields(event) -> dict:
    if isinstance(event, dict):
        return event
    return {
        "completed": getattr(event, "completed", None),
        "total": getattr(event, "total", None),
        "strategy_id": getattr(event, "strategy_id", ""),
        "source_id": getattr(event, "source_id", ""),
        "status": getattr(event, "status", ""),
        "message": getattr(event, "message", ""),
    }


def _run_batch(config: dict, status_path) -> dict:
    """One call to ``runner.run_parallel``, which runs ``backtester.parallel``.

    This file does not score the books. It reads the report files the runner wrote.
    A test can replace ``backtester.runner.run_parallel``.
    """
    from ..runner import run_parallel

    issues = validate_config(config)
    if issues:
        raise ValueError("; ".join(issues))
    config = normalize(config)
    parent = as_path(config["out"])
    parent.mkdir(parents=True, exist_ok=True)
    _write_record(parent, config)
    strategy_type, named_type, pack_type, cancel_type = _parallel_types()
    used: set[str] = set()
    strategies = []
    for path in config["strategies"]:
        strategies.append(strategy_type(strategy_id=_slug(path, used), path=str(as_path(path))))
    if config["source"] == "pack":
        markets = [pack_type(pack_dir=str(as_path(config["pack"])), sidecar_path=str(as_path(config["sidecar"])))]
    else:
        markets = [
            named_type(
                src=str(as_path(config["src"])),
                symbol=str(config["ticker"]),
                bar_minutes=int(config["bar_minutes"]),
                source_id=str(config["ticker"] or "named"),
            )
        ]
    workers = int(config.get("workers") or 1)
    token = cancel_type()
    watcher = None
    if status_path:
        watcher = threading.Thread(
            target=_watch_cancel,
            args=(status_path, token),
            name="backtester-gui-cancel",
            daemon=True,
        )
        watcher.start()

    def on_progress(event) -> None:
        fields = _progress_fields(event)
        completed = fields.get("completed")
        total = fields.get("total")
        message = str(fields.get("message") or "").strip()
        if not message:
            message = f"{fields.get('strategy_id') or ''} {fields.get('status') or ''}".strip()
        message = f"{message}  workers {workers}".strip()
        _status(
            status_path,
            {
                "state": "running",
                "message": message or "running",
                "index": completed if isinstance(completed, int) else 0,
                "count": total if isinstance(total, int) else 0,
            },
        )

    result = run_parallel(
        strategies,
        markets,
        str(parent),
        profile=config["profile"],
        workers=workers,
        on_progress=on_progress,
        cancel=token,
        borrow_rate_annual=_optional_float(config["borrow_rate_annual"]),
        financing_rate_annual=_optional_float(config["financing_rate_annual"]),
        fee_mult=float(config["fee_mult"]),
        dividends=_dividends(config),
        warmup_sessions=int(config.get("warmup_sessions") or 0),
        initial_equity=float(config["initial_equity"]),
        with_benchmarks=True,
    )
    rows = []
    for item in getattr(result, "results", ()) or ():
        status = getattr(item, "status", None) or "ok"
        rows.append(
            {
                "strategy": getattr(item, "strategy_id", ""),
                "state": "ok" if status == "ok" else status,
                "out": getattr(item, "out_dir", None),
                "error": getattr(item, "error", None),
            }
        )
    failed = getattr(result, "failures", None)
    if failed is None:
        failed = sum(1 for row in rows if row["state"] not in {"ok", "success"})
    return {
        "out": str(getattr(result, "out_dir", parent)),
        "strategies": rows,
        "failed": failed,
        "count": len(rows) or len(strategies),
        "workers": workers,
    }


def execute_run(config: dict, status_path=None) -> dict:
    mode = str(config.get("mode") or "")
    if mode == "batch":
        return _run_batch(config, status_path)
    if mode == "walkforward":
        return _run_walk(config, status_path)
    if mode == "single":
        return _run_single(config, status_path)
    raise ValueError(f"unknown mode {mode}")


def process_entry(mode: str, config: dict, status_path: str) -> None:
    """Spawn target. ``mode`` is repeated so the child does not trust a partial dict silently."""
    import os
    import sys

    if sys.platform != "win32":
        try:
            os.setsid()
            Path(str(status_path) + ".session").write_text("1\n", encoding="utf-8")
        except OSError:
            pass
    try:
        if config.get("mode") != mode:
            raise ValueError(f"worker mode {mode!r} does not match the config")
        _status(status_path, {"state": "running", "message": "Starting"})
        result = execute_run(config, status_path)
        _status(status_path, {"state": "done", "result": result})
    except Exception:
        _status(status_path, {"state": "error", "error": traceback.format_exc()})
