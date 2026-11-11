"""Versioned series registry.

A ticker is a registry entry plus an ingest. The file is JSON, schema_version 1,
and a reader rejects a version, a key, or a type it does not know. Session hours
are whatever the inspection inferred. This module does not print.
"""

from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path

from .profiles import get_profile

SCHEMA_VERSION = 1

_OFFSET_RE = re.compile(r"^[+-]\d{2}:\d{2}$")
_CLOCK_RE = re.compile(r"^\d{2}:\d{2}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PHASES = {"pre", "rth", "post"}
_PROFILES = {"arena", "desk"}
_PRICE_KINDS = {"raw", "total_return"}
_BORROW_SOURCES = {"profile_default", "explicit"}
_FILL_POLICIES = {"flag_present_none_set", "stale_forward_fill"}

_DOCUMENT_KEYS = {"schema_version", "series"}
_SERIES_KEYS = {
    "symbol",
    "source_root",
    "source_glob",
    "columns",
    "timezone",
    "session_segments",
    "hours_assumed",
    "price_adjustment",
    "splits",
    "fill_policy",
    "native_tick",
    "native_bar_minutes",
    "typical_volume",
    "share_factor",
    "borrow_rate_annual",
    "borrow_rate_source",
    "cost_schedule",
    "profile",
    "n_rows",
    "n_sessions",
    "first_timestamp",
    "last_timestamp",
    "phase_map",
    "original_session_labels",
    "runnable",
    "notes",
}
_TIMEZONE_KEYS = {"column", "offsets", "dst_pair"}
_SEGMENT_KEYS = {
    "label",
    "engine_phase",
    "start_minute",
    "end_minute",
    "start_clock",
    "end_clock",
    "modal_bars",
    "min_bars",
    "max_bars",
    "n_sessions",
    "variant_sessions",
    "gapped_sessions",
    "variants",
}
_VARIANT_KEYS = {"start_minute", "end_minute", "start_clock", "end_clock", "bars", "n_sessions"}
_FILL_KEYS = {"policy", "filled_rows", "unfilled_rows", "stale_when_filled"}
_VOLUME_KEYS = {"median_bar", "median_session"}
_SPLIT_KEYS = {"date", "ratio"}
_PHASE_MAP_KEYS = {"from", "to"}


def empty_registry() -> dict:
    return {"schema_version": SCHEMA_VERSION, "series": []}


def parse_phase_map(text: str | None) -> dict[str, str] | None:
    """``label=pre,label=rth``. Empty means the file already uses engine labels."""
    if text is None or str(text).strip() == "":
        return None
    mapping: dict[str, str] = {}
    for part in str(text).split(","):
        piece = part.strip()
        if not piece:
            continue
        if "=" not in piece:
            raise ValueError(f"phase map entry {piece!r} must look like label=rth")
        src, dst = piece.split("=", 1)
        src, dst = src.strip(), dst.strip()
        if not src or not dst:
            raise ValueError(f"phase map entry {piece!r} is incomplete")
        if src in mapping:
            raise ValueError(f"phase map repeats {src!r}")
        mapping[src] = dst
    if not mapping:
        raise ValueError("phase map is empty")
    return mapping


def parse_splits(text: str | None) -> tuple[dict, ...]:
    """``YYYY-MM-DD:ratio`` pairs, or ``none``.

    A declared split is recorded and not applied. ``none`` records an empty list.
    """
    if text is None or str(text).strip() == "" or str(text).strip().lower() == "none":
        return ()
    splits = []
    seen = set()
    for part in str(text).split(","):
        piece = part.strip()
        if ":" not in piece:
            raise ValueError(f"split {piece!r} must look like YYYY-MM-DD:ratio")
        date, ratio_text = piece.split(":", 1)
        date = date.strip()
        if _DATE_RE.match(date) is None:
            raise ValueError(f"split date {date!r} is not YYYY-MM-DD")
        if date in seen:
            raise ValueError(f"split date {date} is repeated")
        seen.add(date)
        try:
            ratio = float(ratio_text)
        except ValueError as exc:
            raise ValueError(f"split ratio {ratio_text!r} is not a number") from exc
        if isinstance(ratio, bool) or not math.isfinite(ratio) or ratio <= 0.0 or ratio == 1.0:
            raise ValueError(f"split ratio {ratio_text!r} is ambiguous")
        splits.append({"date": date, "ratio": ratio})
    return tuple(splits)


def _fail(path: str, message: str) -> None:
    raise ValueError(f"registry {path}: {message}")


def _keys(value, expected: set[str], path: str) -> None:
    if not isinstance(value, dict):
        _fail(path, "expected an object")
    extra = sorted(set(value) - expected)
    missing = sorted(expected - set(value))
    if extra:
        _fail(path, f"unknown field {extra[0]!r}")
    if missing:
        _fail(path, f"missing field {missing[0]!r}")


def _bool(value, path: str) -> bool:
    if not isinstance(value, bool):
        _fail(path, "expected true or false")
    return value


def _string(value, path: str) -> str:
    if not isinstance(value, str) or value == "":
        _fail(path, "expected a non-empty string")
    return value


def _number(value, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(path, "expected a number")
    number = float(value)
    if not math.isfinite(number):
        _fail(path, "expected a finite number")
    return number


def _int(value, path: str) -> int:
    if isinstance(value, bool) or isinstance(value, float) or not isinstance(value, int):
        _fail(path, "expected an integer")
    return value


def _nonneg_int(value, path: str) -> int:
    number = _int(value, path)
    if number < 0:
        _fail(path, "expected a non-negative integer")
    return number


def validate_entry(entry: dict) -> None:
    """Raise if ``entry`` is not one schema-1 series."""
    _keys(entry, _SERIES_KEYS, "series")
    symbol = _string(entry["symbol"], "series.symbol")
    if "/" in symbol or "\\" in symbol:
        _fail("series.symbol", "must not contain a path separator")
    _string(entry["source_root"], "series.source_root")
    glob = _string(entry["source_glob"], "series.source_glob")
    if symbol not in glob:
        _fail("series.source_glob", "does not name the symbol")
    columns = entry["columns"]
    if not isinstance(columns, list) or not columns or not all(isinstance(item, str) and item for item in columns):
        _fail("series.columns", "expected a list of column names")
    if len(columns) != len(set(columns)):
        _fail("series.columns", "repeats a column")
    _keys(entry["timezone"], _TIMEZONE_KEYS, "series.timezone")
    if entry["timezone"]["column"] != "timestamp_et":
        _fail("series.timezone.column", "this schema reads timestamp_et")
    offsets = entry["timezone"]["offsets"]
    if not isinstance(offsets, list) or not offsets:
        _fail("series.timezone.offsets", "expected the offsets that were observed")
    for offset in offsets:
        if not isinstance(offset, str) or _OFFSET_RE.match(offset) is None:
            _fail("series.timezone.offsets", f"bad offset {offset!r}")
    if len(set(offsets)) != len(offsets):
        _fail("series.timezone.offsets", "repeats an offset")
    dst = _bool(entry["timezone"]["dst_pair"], "series.timezone.dst_pair")
    if dst != (len(offsets) == 2):
        _fail("series.timezone.dst_pair", "does not match the number of offsets")
    segments = entry["session_segments"]
    if not isinstance(segments, list) or not segments:
        _fail("series.session_segments", "expected at least one inferred segment")
    seen_labels = []
    for index, segment in enumerate(segments):
        path = f"series.session_segments[{index}]"
        _keys(segment, _SEGMENT_KEYS, path)
        label = _string(segment["label"], f"{path}.label")
        if label in seen_labels:
            _fail(path, "repeats a label")
        seen_labels.append(label)
        phase = _string(segment["engine_phase"], f"{path}.engine_phase")
        if phase not in _PHASES:
            _fail(f"{path}.engine_phase", "is not an engine phase")
        start = _nonneg_int(segment["start_minute"], f"{path}.start_minute")
        end = _nonneg_int(segment["end_minute"], f"{path}.end_minute")
        if end < start or start > 24 * 60 - 1 or end > 24 * 60 - 1:
            _fail(path, "clock window is impossible")
        if segment["start_clock"] != _clock(start) or segment["end_clock"] != _clock(end):
            _fail(path, "clock text does not match the minutes")
        for name in ("modal_bars", "min_bars", "max_bars", "n_sessions"):
            if _int(segment[name], f"{path}.{name}") < 1:
                _fail(f"{path}.{name}", "expected a positive integer")
        _nonneg_int(segment["variant_sessions"], f"{path}.variant_sessions")
        _nonneg_int(segment["gapped_sessions"], f"{path}.gapped_sessions")
        if segment["min_bars"] > segment["max_bars"]:
            _fail(path, "min_bars exceeds max_bars")
        variants = segment["variants"]
        if not isinstance(variants, list):
            _fail(f"{path}.variants", "expected a list")
        for vindex, variant in enumerate(variants):
            vpath = f"{path}.variants[{vindex}]"
            _keys(variant, _VARIANT_KEYS, vpath)
            vstart = _nonneg_int(variant["start_minute"], f"{vpath}.start_minute")
            vend = _nonneg_int(variant["end_minute"], f"{vpath}.end_minute")
            if variant["start_clock"] != _clock(vstart) or variant["end_clock"] != _clock(vend):
                _fail(vpath, "clock text does not match the minutes")
            if _int(variant["bars"], f"{vpath}.bars") < 1 or _int(variant["n_sessions"], f"{vpath}.n_sessions") < 1:
                _fail(vpath, "expected positive counts")
        accounted = sum(item["n_sessions"] for item in variants)
        if accounted != segment["variant_sessions"]:
            _fail(f"{path}.variants", "session counts do not add up")
    if _bool(entry["hours_assumed"], "series.hours_assumed"):
        _fail("series.hours_assumed", "must be false; hours are inferred")
    price = entry["price_adjustment"]
    if price not in _PRICE_KINDS:
        _fail("series.price_adjustment", "expected raw or total_return")
    splits = entry["splits"]
    if not isinstance(splits, list):
        _fail("series.splits", "expected a list")
    seen_dates = set()
    for index, split in enumerate(splits):
        path = f"series.splits[{index}]"
        _keys(split, _SPLIT_KEYS, path)
        if _DATE_RE.match(_string(split["date"], f"{path}.date")) is None:
            _fail(f"{path}.date", "expected YYYY-MM-DD")
        if split["date"] in seen_dates:
            _fail(path, "repeats a date")
        seen_dates.add(split["date"])
        ratio = _number(split["ratio"], f"{path}.ratio")
        if ratio <= 0.0 or ratio == 1.0:
            _fail(f"{path}.ratio", "is ambiguous")
    _keys(entry["fill_policy"], _FILL_KEYS, "series.fill_policy")
    policy = entry["fill_policy"]["policy"]
    if policy not in _FILL_POLICIES:
        _fail("series.fill_policy.policy", "unknown fill policy")
    filled_rows = _nonneg_int(entry["fill_policy"]["filled_rows"], "series.fill_policy.filled_rows")
    unfilled_rows = _nonneg_int(entry["fill_policy"]["unfilled_rows"], "series.fill_policy.unfilled_rows")
    stale = entry["fill_policy"]["stale_when_filled"]
    if policy == "flag_present_none_set":
        if filled_rows != 0 or stale is not None:
            _fail("series.fill_policy", "none-set policy disagrees with the counts")
    else:
        if filled_rows < 1 or stale is not True:
            _fail("series.fill_policy", "stale policy disagrees with the counts")
    if filled_rows + unfilled_rows != _nonneg_int(entry["n_rows"], "series.n_rows"):
        _fail("series.fill_policy", "filled plus unfilled is not n_rows")
    tick = _number(entry["native_tick"], "series.native_tick")
    if tick <= 0.0:
        _fail("series.native_tick", "must be positive")
    if _int(entry["native_bar_minutes"], "series.native_bar_minutes") != 1:
        _fail("series.native_bar_minutes", "this loader's files are 1-minute")
    _keys(entry["typical_volume"], _VOLUME_KEYS, "series.typical_volume")
    for name in ("median_bar", "median_session"):
        volume = _number(entry["typical_volume"][name], f"series.typical_volume.{name}")
        if volume < 0.0:
            _fail(f"series.typical_volume.{name}", "cannot be negative")
    share = _number(entry["share_factor"], "series.share_factor")
    if share != 1.0:
        _fail("series.share_factor", "raw files are 1.0; the anonymized factor belongs in the sidecar")
    borrow = _number(entry["borrow_rate_annual"], "series.borrow_rate_annual")
    if borrow < 0.0:
        _fail("series.borrow_rate_annual", "cannot be negative")
    source = entry["borrow_rate_source"]
    if source not in _BORROW_SOURCES:
        _fail("series.borrow_rate_source", "expected profile_default or explicit")
    profile = entry["profile"]
    if profile not in _PROFILES:
        _fail("series.profile", "expected arena or desk")
    if entry["cost_schedule"] != profile:
        _fail("series.cost_schedule", "must name the same profile; the two schedules are not merged")
    if source == "profile_default" and borrow != get_profile(profile).borrow_rate_annual:
        _fail(
            "series.borrow_rate_annual",
            "is marked profile_default but is not that profile's borrow rate",
        )
    _nonneg_int(entry["n_sessions"], "series.n_sessions")
    if entry["n_sessions"] < 1 or entry["n_rows"] < 1:
        _fail("series", "row and session counts must be positive")
    _string(entry["first_timestamp"], "series.first_timestamp")
    _string(entry["last_timestamp"], "series.last_timestamp")
    phase_map = entry["phase_map"]
    if not isinstance(phase_map, list):
        _fail("series.phase_map", "expected a list")
    mapped_from = []
    for index, item in enumerate(phase_map):
        path = f"series.phase_map[{index}]"
        _keys(item, _PHASE_MAP_KEYS, path)
        src = _string(item["from"], f"{path}.from")
        dst = _string(item["to"], f"{path}.to")
        if dst not in _PHASES:
            _fail(f"{path}.to", "is not an engine phase")
        if src in mapped_from:
            _fail(path, "repeats a label")
        mapped_from.append(src)
    originals = entry["original_session_labels"]
    if not isinstance(originals, list) or not originals or not all(isinstance(item, str) and item for item in originals):
        _fail("series.original_session_labels", "expected the labels from the file")
    if sorted(originals) != originals:
        _fail("series.original_session_labels", "must be sorted")
    if sorted(seen_labels) != originals and sorted(seen_labels) != sorted(originals):
        _fail("series.session_segments", "labels do not match original_session_labels")
    runnable = _bool(entry["runnable"], "series.runnable")
    if price != "raw" or splits:
        if runnable:
            _fail("series.runnable", "a total-return series or a declared split list cannot be executed in this phase")
    elif not runnable:
        _fail("series.runnable", "a raw series with no declared splits is executable")
    notes = entry["notes"]
    if not isinstance(notes, list) or not all(isinstance(item, str) for item in notes):
        _fail("series.notes", "expected a list of strings")


def _clock(minute: int) -> str:
    return f"{minute // 60:02d}:{minute % 60:02d}"


def validate_registry(document: dict) -> None:
    """Raise unless ``document`` is a schema-1 registry."""
    if not isinstance(document, dict):
        _fail("", "the file is not an object")
    _keys(document, _DOCUMENT_KEYS, "")
    version = document["schema_version"]
    if version != SCHEMA_VERSION:
        _fail(
            "schema_version",
            f"{version!r} is not supported. This build reads schema {SCHEMA_VERSION} and will not guess.",
        )
    series = document["series"]
    if not isinstance(series, list):
        _fail("series", "expected a list")
    seen = set()
    for index, entry in enumerate(series):
        try:
            validate_entry(entry)
        except ValueError as exc:
            raise ValueError(f"registry series[{index}] ({exc})") from exc
        if entry["symbol"] in seen:
            _fail("series", f"symbol {entry['symbol']} is listed twice")
        seen.add(entry["symbol"])


def entry_from_inspection(
    inspection,
    *,
    source_root: str,
    profile: str,
    borrow_rate_annual: float | None = None,
    borrow_rate_source: str | None = None,
    splits: tuple | list | None = None,
) -> dict:
    """Build one series entry. Hours and tick come from ``inspection`` only."""
    profile_name = get_profile(profile).name
    if borrow_rate_annual is None:
        rate = get_profile(profile_name).borrow_rate_annual
        source = "profile_default"
    else:
        rate = float(borrow_rate_annual)
        source = "explicit" if borrow_rate_source is None else borrow_rate_source
    # load_named rejects a non-empty split list, so a declaration arrives here
    # from the operator and is stored without being applied to prices.
    declared = [] if splits is None else [dict(item) for item in splits]
    runnable = inspection.runnable and not declared and inspection.price_adjustment == "raw"
    entry = {
        "symbol": inspection.symbol,
        "source_root": str(source_root),
        "source_glob": inspection.source_glob,
        "columns": list(inspection.columns),
        "timezone": {
            "column": "timestamp_et",
            "offsets": list(inspection.timezone_offsets),
            "dst_pair": bool(inspection.timezone_dst_pair),
        },
        "session_segments": [segment.as_dict() for segment in inspection.session_segments],
        "hours_assumed": False,
        "price_adjustment": inspection.price_adjustment,
        "splits": declared,
        "fill_policy": {
            "policy": inspection.fill_policy,
            "filled_rows": inspection.filled_rows,
            "unfilled_rows": inspection.unfilled_rows,
            "stale_when_filled": inspection.stale_when_filled,
        },
        "native_tick": inspection.native_tick,
        "native_bar_minutes": inspection.native_bar_minutes,
        "typical_volume": {
            "median_bar": inspection.typical_volume_bar,
            "median_session": inspection.typical_volume_session,
        },
        "share_factor": 1.0,
        "borrow_rate_annual": rate,
        "borrow_rate_source": source,
        "cost_schedule": profile_name,
        "profile": profile_name,
        "n_rows": inspection.native_rows,
        "n_sessions": inspection.n_sessions,
        "first_timestamp": inspection.first_timestamp_et,
        "last_timestamp": inspection.last_timestamp_et,
        "phase_map": [{"from": src, "to": dst} for src, dst in inspection.phase_map],
        "original_session_labels": list(inspection.original_session_labels),
        "runnable": runnable,
        "notes": list(inspection.notes),
    }
    if declared and runnable:
        entry["runnable"] = False
    if declared:
        entry["notes"] = list(entry["notes"]) + [
            "Splits are recorded and are not applied. This phase does not adjust prices, so the series is not runnable."
        ]
        entry["runnable"] = False
    validate_entry(entry)
    return entry


def _json_pairs(pairs):
    """Object hook that refuses a repeated key. ``json.loads`` would keep the last one."""
    document = {}
    for key, value in pairs:
        if key in document:
            raise ValueError(f"duplicate JSON key {key!r}")
        document[key] = value
    return document


def load_registry(path) -> dict:
    file = Path(path)
    if not file.is_file():
        raise FileNotFoundError(f"registry does not exist: {file}")
    try:
        document = json.loads(file.read_text(encoding="utf-8"), object_pairs_hook=_json_pairs)
    except json.JSONDecodeError as exc:
        raise ValueError(f"registry {file} is not JSON: {exc}") from exc
    validate_registry(document)
    return document


def load_or_new(path) -> dict:
    file = Path(path)
    if not file.exists():
        return empty_registry()
    return load_registry(file)


def save_registry(path, document: dict) -> None:
    validate_registry(document)
    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(document, indent=2) + "\n"
    temporary = file.with_name(file.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, file)


def upsert_series(document: dict, entry: dict) -> dict:
    validate_registry(document)
    validate_entry(entry)
    series = [item for item in document["series"] if item["symbol"] != entry["symbol"]]
    series.append(entry)
    series.sort(key=lambda item: item["symbol"])
    updated = {"schema_version": SCHEMA_VERSION, "series": series}
    validate_registry(updated)
    return updated


def get_series(document: dict, symbol: str) -> dict:
    validate_registry(document)
    matches = [item for item in document["series"] if item["symbol"] == symbol]
    if not matches:
        known = ", ".join(item["symbol"] for item in document["series"]) or "(none)"
        raise KeyError(f"{symbol} is not in the registry. Known symbols: {known}")
    return matches[0]


def hours_text(entry: dict) -> str:
    parts = []
    for segment in entry["session_segments"]:
        parts.append(f"{segment['label']} {segment['start_clock']}-{segment['end_clock']}")
    return "; ".join(parts)


def format_list(document: dict) -> str:
    """Plain-text table for the operator. No market data, no secrets."""
    validate_registry(document)
    if not document["series"]:
        return "Registry is empty. Ingest a ticker to add one."
    header = f"{'symbol':<12} {'profile':<8} {'sessions':>8} {'tick':>10} {'prices':<14} hours"
    lines = [header, "-" * len(header)]
    for entry in document["series"]:
        runnable = "" if entry["runnable"] else " (not runnable)"
        lines.append(
            f"{entry['symbol']:<12} {entry['profile']:<8} {entry['n_sessions']:>8} "
            f"{entry['native_tick']:>10} {entry['price_adjustment']:<14} {hours_text(entry)}{runnable}"
        )
    return "\n".join(lines)


def format_show(entry: dict) -> str:
    """Plain-text description of one series. Hours are the inferred ones."""
    validate_entry(entry)
    borrow_note = "profile default" if entry["borrow_rate_source"] == "profile_default" else "set by the operator"
    lines = [
        entry["symbol"],
        f"  profile: {entry['profile']}",
        f"  cost schedule: {entry['cost_schedule']} (that profile's own table)",
        f"  prices: {entry['price_adjustment']}",
        f"  share_factor: {entry['share_factor']} (raw files; anonymized runs use the sidecar)",
        f"  borrow: {entry['borrow_rate_annual']} per year ({borrow_note})",
        f"  native tick: {entry['native_tick']}",
        f"  native bar: {entry['native_bar_minutes']} minute",
        (
            "  typical volume: "
            f"{entry['typical_volume']['median_bar']} shares/bar, "
            f"{entry['typical_volume']['median_session']} shares/session (median)"
        ),
        (
            "  timezone: offsets "
            + ", ".join(entry["timezone"]["offsets"])
            + (" (daylight-time pair)" if entry["timezone"]["dst_pair"] else " (one offset)")
        ),
        f"  sessions: {entry['n_sessions']}",
        f"  rows: {entry['n_rows']}",
        f"  from: {entry['first_timestamp']}",
        f"  to:   {entry['last_timestamp']}",
        "  segments (inferred from the file):",
    ]
    for segment in entry["session_segments"]:
        extra = ""
        if segment["variant_sessions"]:
            extra += f", {segment['variant_sessions']} variant session(s)"
        if segment["gapped_sessions"]:
            extra += f", {segment['gapped_sessions']} gapped"
        phase = ""
        if segment["engine_phase"] != segment["label"]:
            phase = f" -> {segment['engine_phase']}"
        lines.append(
            f"    {segment['label']}{phase}  {segment['start_clock']}-{segment['end_clock']}  "
            f"{segment['modal_bars']} bars  ({segment['n_sessions']} sessions{extra})"
        )
        for variant in segment["variants"]:
            lines.append(
                f"      variant {variant['start_clock']}-{variant['end_clock']}  "
                f"{variant['bars']} bars  x{variant['n_sessions']}"
            )
    lines.append(f"  fill policy: {entry['fill_policy']['policy']}")
    if entry["splits"]:
        rendered = ", ".join(f"{item['date']} x{item['ratio']}" for item in entry["splits"])
        lines.append(f"  splits: recorded, not applied ({rendered})")
    else:
        lines.append("  splits: none declared; prices are not adjusted")
    if entry["phase_map"]:
        rendered = ", ".join(f"{item['from']}->{item['to']}" for item in entry["phase_map"])
        lines.append(f"  phase map: {rendered}")
    lines.append(f"  runnable: {'yes' if entry['runnable'] else 'no'}")
    lines.append(f"  source: {entry['source_glob']}")
    lines.append(f"  root: {entry['source_root']}")
    return "\n".join(lines)


def same_series(entry: dict, inspection) -> None:
    """Fail when the files no longer match the registry entry.

    Compared facts are the native series, not a resampled delivery.
    """
    validate_entry(entry)
    problems = []
    if entry["symbol"] != inspection.symbol:
        problems.append("symbol")
    if entry["n_rows"] != inspection.native_rows:
        problems.append("n_rows")
    if entry["n_sessions"] != inspection.n_sessions:
        problems.append("n_sessions")
    if entry["native_tick"] != inspection.native_tick:
        problems.append("native_tick")
    if entry["first_timestamp"] != inspection.first_timestamp_et or entry["last_timestamp"] != inspection.last_timestamp_et:
        problems.append("timestamp span")
    if entry["price_adjustment"] != inspection.price_adjustment:
        problems.append("price_adjustment")
    recorded = [
        (segment["label"], segment["start_minute"], segment["end_minute"], segment["n_sessions"])
        for segment in entry["session_segments"]
    ]
    fresh = [
        (segment.label, segment.start_minute, segment.end_minute, segment.n_sessions)
        for segment in inspection.session_segments
    ]
    if recorded != fresh:
        problems.append("session hours")
    if problems:
        raise ValueError(
            f"{inspection.symbol} no longer matches the registry ({', '.join(problems)}). "
            "Re-ingest before using it."
        )
