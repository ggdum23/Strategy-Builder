"""On-disk columns for a series that has already been validated.

A later run maps the columns instead of parsing the monthly files again.
The directory name is a hash. Named series are keyed by the source bytes,
the loader version, and the load parameters. A window or a designer view is
keyed by the column bytes, so a walk-forward slice does not contain the
locked bars and a designer artifact is a different directory from the
operator series.

Designer manifests do not store a source path, a scale, a date map, or the
real ticker. The cache is size-bounded. A write that fails does not fail
the run that already holds the frame.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

SCHEMA = 2
_KEY_RE_TEXT = r"^[0-9a-f]{64}$"
_DEFAULT_MAX_BYTES = 8 * 1024 * 1024 * 1024
_LOCK_STALE_S = 15 * 60
_PARTIAL_STALE_S = 60 * 60
_FLOAT_FIELDS = ("share_factor", "native_tick", "typical_volume_bar", "typical_volume_session")
_BANNED_COLUMNS = frozenset(
    {
        "symbol",
        "timestamp_et",
        "date_map",
        "sidecar",
        "price_factor",
        "volume_factor",
        "share_scale",
        "share_factor",
        "factor_key",
        "factor_mac",
        "operator_note",
        "holdout",
        "source_path",
        "source_root",
        "real_ticker",
        "ticker",
    }
)
_ISO_DATE = __import__("re").compile(r"\d{4}-\d{2}-\d{2}")

# The last cache error. A failed write is not fatal; tests can read this.
LAST_CACHE_ERROR: BaseException | None = None


def _runtime_prefixes() -> list[Path]:
    """Interpreter locations. The default cache is not placed inside them."""
    found: list[Path] = []
    for raw in (sys.prefix, getattr(sys, "base_prefix", ""), os.path.dirname(sys.executable)):
        if not raw:
            continue
        try:
            path = Path(raw).resolve()
        except OSError:
            path = Path(os.path.abspath(raw))
        if path not in found:
            found.append(path)
    return found


def _inside_any(path: Path, roots: list[Path]) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        resolved = Path(os.path.abspath(path))
    return any(resolved == root or root in resolved.parents for root in roots)


def _default_cache_outside_runtime(path: Path) -> Path:
    """Move a computed default that landed inside the interpreter.

    An explicit ``STRATEGY_BUILDER_PREPARED_DIR`` is not passed here. That
    path stays where the operator put it, and the launch refuses it.
    """
    prefixes = _runtime_prefixes()
    if not _inside_any(path, prefixes):
        return path
    try:
        current = path.resolve()
    except OSError:
        current = Path(os.path.abspath(path))
    while _inside_any(current, prefixes):
        parent = current.parent
        if parent == current:
            break
        current = parent
    anchor = Path(current.anchor) if current.anchor else current
    if current == anchor or _inside_any(current, prefixes):
        fallback = Path(os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp")
        if _inside_any(fallback, prefixes):
            fallback = fallback.parent
        return fallback / "strategy-builder" / "prepared"
    return current / "strategy-builder" / "prepared"


def default_cache_dir() -> Path:
    """The prepared-data directory for this machine.

    ``STRATEGY_BUILDER_PREPARED_DIR`` wins, including a path inside the
    interpreter. The computed default never sits inside the interpreter
    prefix. Windows uses LOCALAPPDATA. Everywhere else uses
    ``$XDG_CACHE_HOME`` or ``~/.cache``.
    """
    override = os.environ.get("STRATEGY_BUILDER_PREPARED_DIR")
    if override:
        return Path(override)
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        chosen = Path(base) / "strategy-builder" / "prepared"
    else:
        xdg = os.environ.get("XDG_CACHE_HOME")
        root = Path(xdg) if xdg else Path.home() / ".cache"
        chosen = root / "strategy-builder" / "prepared"
    return _default_cache_outside_runtime(chosen)


def max_bytes() -> int:
    raw = os.environ.get("STRATEGY_BUILDER_PREPARED_MAX_BYTES")
    if raw is None or raw == "":
        return _DEFAULT_MAX_BYTES
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError("STRATEGY_BUILDER_PREPARED_MAX_BYTES is not an integer") from exc
    if value < 1:
        raise ValueError("STRATEGY_BUILDER_PREPARED_MAX_BYTES must be positive")
    return value


def lookup_named(symbol: str, *, bar_minutes: int, price_kind: str, phase_map, paths) -> tuple[str, object | None]:
    """Return ``(key, series)``. ``series`` is None when the cache misses.

    A corrupt entry is removed. The caller then builds the frame and stores
    it under the same key.
    """
    key = named_key(
        symbol=symbol,
        bar_minutes=bar_minutes,
        price_kind=price_kind,
        phase_map=phase_map,
        paths=paths,
    )
    try:
        loaded = _open_named_key(key)
    except Exception as exc:
        _remember(exc)
        return key, None
    return key, loaded


def save_named(
    key: str,
    frame: pd.DataFrame,
    inspection,
    *,
    symbol: str,
    bar_minutes: int,
    price_kind: str,
    phase_map,
    source,
) -> Path | None:
    """Write a named artifact. Returns None when the write cannot be finished."""
    try:
        extra = {
            "symbol": str(symbol),
            "source_root": str(Path(source).resolve()),
            "params": {
                "bar_minutes": int(bar_minutes),
                "price_kind": str(price_kind),
                "phase_map": _phase_pairs(phase_map),
            },
            "inspection": _inspection_payload(inspection),
        }
        return _store_arrays(key, frame, kind="named", extra=extra)
    except Exception as exc:
        _remember(exc)
        return None


def store_frame(frame: pd.DataFrame, *, kind: str, symbol: str | None = None) -> Path:
    """Content-addressed columns. Designer frames are screened first.

    ``symbol``, when given, is used only to refuse a designer view that
    still contains that ticker. It is not written into the manifest.
    """
    if kind not in {"window", "designer", "operator"}:
        raise ValueError("prepared kind is not supported")
    if kind == "designer":
        screen_designer(frame, symbol=symbol)
    key = frame_key(frame, kind)
    extra = {"symbol": str(symbol)} if kind == "operator" and symbol else {}
    if kind == "designer":
        extra = {}
    return _store_arrays(key, frame, kind=kind, extra=extra)


def load_frame(path) -> pd.DataFrame:
    """Map one artifact. A failed integrity check removes it and raises."""
    root = Path(path)
    manifest = _verified_manifest(root)
    if manifest is None:
        raise ValueError("prepared entry failed its integrity check")
    _touch(root)
    return _materialize(root, manifest)


def read_manifest(path) -> dict:
    """The manifest of an artifact that passed the integrity check."""
    manifest = _verified_manifest(Path(path))
    if manifest is None:
        raise ValueError("prepared entry failed its integrity check")
    return manifest


def list_entries(root=None) -> list[dict]:
    """One public record per artifact. Designer rows are labeled RANDOM."""
    base = Path(root) if root is not None else default_cache_dir()
    if not base.is_dir():
        return []
    rows = []
    for child in sorted(base.iterdir(), key=lambda item: item.name):
        if not _is_entry(child):
            continue
        try:
            manifest = json.loads((child / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            rows.append(
                {
                    "key": child.name,
                    "kind": "unreadable",
                    "symbol": "",
                    "rows": 0,
                    "bytes": _dir_size(child),
                }
            )
            continue
        kind = str(manifest.get("kind") or "")
        if kind == "designer":
            symbol = "RANDOM"
        else:
            symbol = str(manifest.get("symbol") or "")
        try:
            count = int(manifest.get("rows") or 0)
        except (TypeError, ValueError):
            count = 0
        rows.append(
            {
                "key": child.name,
                "kind": kind,
                "symbol": symbol,
                "rows": count,
                "bytes": _dir_size(child),
            }
        )
    return rows


def delete_entry(key: str, root=None) -> None:
    """Remove one artifact. The key is a hash, not a path."""
    if not isinstance(key, str) or len(key) != 64 or any(ch not in "0123456789abcdef" for ch in key):
        raise ValueError("prepared key is not a cache id")
    base = Path(root) if root is not None else default_cache_dir()
    dest = base / key
    if not dest.is_dir():
        raise ValueError("prepared entry is missing")
    shutil.rmtree(dest)


def rebuild_entry(key: str, root=None):
    """Delete a named artifact and load its source again.

    Window and designer artifacts have no source path. They are rebuilt when
    the run that produced them stores them again.
    """
    if not isinstance(key, str) or len(key) != 64 or any(ch not in "0123456789abcdef" for ch in key):
        raise ValueError("prepared key is not a cache id")
    base = Path(root) if root is not None else default_cache_dir()
    dest = base / key
    manifest_path = dest / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError("prepared entry is missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("kind") != "named":
        raise ValueError("only a named series can be rebuilt from its source")
    source = manifest.get("source_root")
    symbol = manifest.get("symbol")
    params = manifest.get("params") or {}
    if not source or not symbol:
        raise ValueError("named entry has no source")
    phase_map = params.get("phase_map")
    if phase_map is not None:
        phase_map = tuple((str(item[0]), str(item[1])) for item in phase_map)
    shutil.rmtree(dest)
    from .data import load_named

    return load_named(
        source,
        str(symbol),
        bar_minutes=int(params.get("bar_minutes") or 1),
        price_kind=str(params.get("price_kind") or "raw"),
        phase_map=phase_map,
    )


def screen_designer(frame: pd.DataFrame, *, symbol: str | None = None, manifest: dict | None = None) -> None:
    """Refuse a designer artifact that still carries operator data.

    The error text does not include the ticker, the date, or the cell.
    """
    columns = {str(name) for name in frame.columns}
    if columns & _BANNED_COLUMNS:
        raise ValueError("designer view contains an operator column")
    if manifest:
        if set(map(str, manifest)) & _BANNED_COLUMNS:
            raise ValueError("designer manifest contains an operator field")
    for name in frame.columns:
        values = frame[name]
        if not _is_text(values):
            continue
        if isinstance(values.dtype, pd.CategoricalDtype):
            texts = [str(value) for value in values.cat.categories]
        else:
            texts = [str(value) for value in pd.unique(values)]
        for value in texts:
            text = str(value)
            if _ISO_DATE.search(text):
                raise ValueError("designer view contains a calendar date")
            if symbol and _contains_ticker(text, symbol):
                raise ValueError("designer view contains the ticker")


def named_key(*, symbol: str, bar_minutes: int, price_kind: str, phase_map, paths) -> str:
    from .data import LOADER_VERSION

    digest = hashlib.blake2b(digest_size=32)
    digest.update(b"named\0")
    digest.update(str(SCHEMA).encode("ascii"))
    digest.update(b"\0")
    digest.update(str(LOADER_VERSION).encode("ascii"))
    digest.update(b"\0")
    payload = {
        "bar_minutes": int(bar_minutes),
        "phase_map": _phase_pairs(phase_map),
        "price_kind": str(price_kind),
        "symbol": str(symbol),
    }
    digest.update(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    for path in paths:
        digest.update(b"\0")
        digest.update(Path(path).name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(_hash_file(Path(path)).encode("ascii"))
    return digest.hexdigest()


def frame_key(frame: pd.DataFrame, kind: str) -> str:
    from .data import LOADER_VERSION

    digest = hashlib.blake2b(digest_size=32)
    digest.update(b"frame\0")
    digest.update(kind.encode("ascii"))
    digest.update(b"\0")
    digest.update(str(SCHEMA).encode("ascii"))
    digest.update(b"\0")
    digest.update(str(LOADER_VERSION).encode("ascii"))
    for name in frame.columns:
        digest.update(b"\0")
        digest.update(str(name).encode("utf-8"))
        for blob in _column_hash_bytes(frame[name]):
            digest.update(b"\0")
            digest.update(blob)
    return digest.hexdigest()


def _open_named_key(key: str):
    root = default_cache_dir() / key
    manifest = _verified_manifest(root)
    if manifest is None:
        return None
    if manifest.get("kind") != "named" or "inspection" not in manifest:
        shutil.rmtree(root, ignore_errors=True)
        return None
    _touch(root)
    frame = _materialize(root, manifest)
    inspection = _inspection_from_payload(manifest["inspection"])
    from .data import LoadedSeries

    return LoadedSeries(frame=frame, inspection=inspection, prepared=str(root))


def _store_arrays(key: str, frame: pd.DataFrame, *, kind: str, extra: dict) -> Path:
    if kind == "designer":
        screen_designer(frame, manifest=extra)
    root = default_cache_dir()
    root.mkdir(parents=True, exist_ok=True)
    _sweep_partials(root)
    dest = root / key
    if _verified_manifest(dest) is not None:
        _touch(dest)
        _enforce_budget(root, keep=key)
        return dest
    lock = root / f".lock-{key}"
    _acquire(lock)
    partial = root / f".partial-{key}-{os.getpid()}-{time.time_ns()}"
    try:
        if _verified_manifest(dest) is not None:
            _touch(dest)
            return dest
        partial.mkdir(parents=True)
        columns = []
        for index, name in enumerate(list(frame.columns)):
            columns.append(_write_column(partial, index, str(name), frame[name]))
        manifest = {
            "schema": SCHEMA,
            "kind": kind,
            "rows": int(len(frame)),
            "columns": columns,
        }
        manifest.update(extra)
        if kind == "designer":
            screen_designer(frame, manifest=manifest)
        text = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
        manifest_path = partial / "manifest.json"
        manifest_path.write_text(text, encoding="utf-8")
        _fsync_file(manifest_path)
        _fsync_dir(partial)
        try:
            os.rename(partial, dest)
        except OSError:
            if _verified_manifest(dest) is None:
                raise
        _fsync_dir(root)
        _touch(dest)
        _enforce_budget(root, keep=key)
        return dest
    finally:
        shutil.rmtree(partial, ignore_errors=True)
        _release(lock)


def _save_array(directory: Path, file_name: str, array: np.ndarray) -> dict:
    target = directory / file_name
    stored = np.ascontiguousarray(array)
    with open(target, "wb") as handle:
        np.save(handle, stored)
        handle.flush()
        os.fsync(handle.fileno())
    return {"file": file_name, "dtype": stored.dtype.str, "blake2b": _hash_file(target)}


def _category_digest(categories) -> str:
    blob = json.dumps(list(categories), separators=(",", ":")).encode("utf-8")
    return hashlib.blake2b(blob, digest_size=32).hexdigest()


def _column_hash_bytes(series: pd.Series) -> list[bytes]:
    """Stable bytes for one column. Categorical text is codes plus labels."""
    from .frame_columns import StampArray

    if isinstance(series.dtype, pd.CategoricalDtype):
        codes = np.ascontiguousarray(series.cat.codes.to_numpy())
        labels = json.dumps([str(value) for value in series.cat.categories], separators=(",", ":")).encode("utf-8")
        return [b"category", codes.dtype.str.encode("ascii"), codes.tobytes(), labels]
    array = getattr(series, "array", None)
    if isinstance(array, StampArray):
        parts = (array._year, array._month, array._day, array._minute, array._offset)
        blobs = [b"stamp"]
        for part in parts:
            stored = np.ascontiguousarray(part)
            blobs.append(stored.dtype.str.encode("ascii"))
            blobs.append(stored.tobytes())
        return blobs
    exported = _export_column(series)
    return [b"plain", exported.dtype.str.encode("ascii"), exported.tobytes()]


def _write_column(directory: Path, index: int, name: str, series: pd.Series) -> dict:
    from .frame_columns import StampArray

    stem = f"{index:02d}-{_safe_name(name)}"
    if isinstance(series.dtype, pd.CategoricalDtype):
        codes = np.ascontiguousarray(series.cat.codes.to_numpy())
        categories = [str(value) for value in series.cat.categories]
        saved = _save_array(directory, f"{stem}.npy", codes)
        return {
            "name": name,
            "encoding": "category",
            "categories": categories,
            "categories_blake2b": _category_digest(categories),
            **saved,
        }
    array = getattr(series, "array", None)
    if isinstance(array, StampArray):
        parts = []
        for key, values in (
            ("year", array._year),
            ("month", array._month),
            ("day", array._day),
            ("minute", array._minute),
            ("offset", array._offset),
        ):
            saved = _save_array(directory, f"{stem}-{key}.npy", values)
            saved["key"] = key
            parts.append(saved)
        return {"name": name, "encoding": "stamp", "parts": parts}
    if name == "symbol":
        values = series.tolist()
        if values and all(str(value) == str(values[0]) for value in values):
            return {"name": name, "encoding": "constant", "value": str(values[0])}
    exported = _export_column(series)
    saved = _save_array(directory, f"{stem}.npy", exported)
    return {"name": name, "encoding": "plain", **saved}


def _materialize(root: Path, manifest: dict) -> pd.DataFrame:
    from .frame_columns import StampArray

    columns = {}
    rows = int(manifest["rows"])
    for spec in manifest["columns"]:
        encoding = spec.get("encoding") or "plain"
        if encoding == "category":
            codes = np.load(root / spec["file"], mmap_mode="c")
            columns[spec["name"]] = pd.Categorical.from_codes(np.asarray(codes), spec["categories"])
        elif encoding == "stamp":
            loaded = {part["key"]: np.load(root / part["file"], mmap_mode="c") for part in spec["parts"]}
            columns[spec["name"]] = StampArray(
                loaded["year"], loaded["month"], loaded["day"], loaded["minute"], loaded["offset"]
            )
        elif encoding == "constant":
            columns[spec["name"]] = pd.Categorical.from_codes(
                np.zeros(rows, dtype=np.int8), [str(spec.get("value") or "")]
            )
        else:
            array = np.load(root / spec["file"], mmap_mode="c")
            if array.dtype.kind == "U":
                columns[spec["name"]] = pd.Series(array.tolist(), dtype="str")
            else:
                columns[spec["name"]] = array
    ordered = {spec["name"]: columns[spec["name"]] for spec in manifest["columns"]}
    return pd.DataFrame(ordered, copy=False)


def _verified_manifest(root: Path) -> dict | None:
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        columns = manifest["columns"]
        rows = int(manifest["rows"])
        if int(manifest.get("schema") or 0) != SCHEMA:
            raise ValueError("schema")
        for spec in columns:
            encoding = spec.get("encoding") or "plain"
            if encoding == "constant":
                continue
            if encoding == "category":
                if _category_digest(spec["categories"]) != spec["categories_blake2b"]:
                    raise ValueError("digest")
            files = spec["parts"] if encoding == "stamp" else [spec]
            for part in files:
                target = root / part["file"]
                if _hash_file(target) != part["blake2b"]:
                    raise ValueError("digest")
                array = np.load(target, mmap_mode="r")
                if int(array.shape[0]) != rows:
                    raise ValueError("rows")
                del array
    except Exception:
        shutil.rmtree(root, ignore_errors=True)
        return None
    return manifest


def _export_column(series: pd.Series) -> np.ndarray:
    values = series.to_numpy()
    if getattr(values, "dtype", None) is not None and values.dtype.kind in {"i", "u", "f", "b"}:
        return np.ascontiguousarray(values)
    texts = []
    for value in series.tolist():
        if value is None:
            texts.append("")
        else:
            texts.append(str(value))
    width = 1
    for text in texts:
        if len(text) > width:
            width = len(text)
    return np.array(texts, dtype=f"U{width}")


def _inspection_payload(inspection) -> dict:
    payload = inspection.as_dict()
    for key in _FLOAT_FIELDS:
        payload[key] = float(payload[key]).hex()
    return payload


def _inspection_from_payload(payload: dict):
    from .data import Inspection, SessionSegment, SessionVariant

    data = dict(payload)
    for key in _FLOAT_FIELDS:
        value = data[key]
        data[key] = float.fromhex(value) if isinstance(value, str) else float(value)
    segments = []
    for item in data["session_segments"]:
        variants = tuple(
            SessionVariant(
                start_minute=int(variant["start_minute"]),
                end_minute=int(variant["end_minute"]),
                bars=int(variant["bars"]),
                n_sessions=int(variant["n_sessions"]),
            )
            for variant in item["variants"]
        )
        segments.append(
            SessionSegment(
                label=str(item["label"]),
                engine_phase=str(item["engine_phase"]),
                start_minute=int(item["start_minute"]),
                end_minute=int(item["end_minute"]),
                modal_bars=int(item["modal_bars"]),
                min_bars=int(item["min_bars"]),
                max_bars=int(item["max_bars"]),
                n_sessions=int(item["n_sessions"]),
                variant_sessions=int(item["variant_sessions"]),
                gapped_sessions=int(item["gapped_sessions"]),
                variants=variants,
            )
        )
    phase_map = tuple((str(item["from"]), str(item["to"])) for item in data["phase_map"])
    stale = data["stale_when_filled"]
    if stale is not None:
        stale = bool(stale)
    return Inspection(
        symbol=str(data["symbol"]),
        columns=tuple(data["columns"]),
        session_labels=tuple(data["session_labels"]),
        timezone_note=str(data["timezone_note"]),
        price_adjustment=str(data["price_adjustment"]),
        splits=tuple(data["splits"]),
        share_factor=data["share_factor"],
        native_bar_minutes=int(data["native_bar_minutes"]),
        bar_minutes=int(data["bar_minutes"]),
        n_rows=int(data["n_rows"]),
        n_sessions=int(data["n_sessions"]),
        first_timestamp_et=str(data["first_timestamp_et"]),
        last_timestamp_et=str(data["last_timestamp_et"]),
        source_glob=str(data["source_glob"]),
        notes=tuple(data["notes"]),
        native_rows=int(data["native_rows"]),
        timezone_offsets=tuple(data["timezone_offsets"]),
        timezone_dst_pair=bool(data["timezone_dst_pair"]),
        session_segments=tuple(segments),
        native_tick=data["native_tick"],
        typical_volume_bar=data["typical_volume_bar"],
        typical_volume_session=data["typical_volume_session"],
        fill_policy=str(data["fill_policy"]),
        filled_rows=int(data["filled_rows"]),
        unfilled_rows=int(data["unfilled_rows"]),
        stale_when_filled=stale,
        phase_map=phase_map,
        original_session_labels=tuple(data["original_session_labels"]),
        runnable=bool(data["runnable"]),
    )


def _phase_pairs(phase_map):
    if phase_map is None:
        return None
    if isinstance(phase_map, dict):
        pairs = [(str(key), str(value)) for key, value in phase_map.items()]
    else:
        pairs = [(str(left), str(right)) for left, right in phase_map]
    return sorted(pairs)


def _contains_ticker(text: str, symbol: str) -> bool:
    from .report import ticker_token_spans

    return bool(ticker_token_spans(text, symbol))


def _is_text(series: pd.Series) -> bool:
    dtype = series.dtype
    if isinstance(dtype, pd.CategoricalDtype):
        return True
    if getattr(dtype, "name", "") == "stamp":
        return True
    kind = getattr(dtype, "kind", "")
    if kind in {"i", "u", "f", "b", "c"}:
        return False
    return True


def _safe_name(name: str) -> str:
    cleaned = []
    for char in name:
        if char.isalnum() or char == "_":
            cleaned.append(char)
        else:
            cleaned.append("_")
    token = "".join(cleaned) or "col"
    return token[:40]


def _hash_file(path: Path) -> str:
    digest = hashlib.blake2b(digest_size=32)
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _dir_size(path: Path) -> int:
    total = 0
    for child in path.rglob("*"):
        if child.is_file():
            try:
                total += child.stat().st_size
            except OSError:
                continue
    return total


def _is_entry(path: Path) -> bool:
    if not path.is_dir():
        return False
    name = path.name
    if name.startswith("."):
        return False
    if len(name) != 64:
        return False
    return all(char in "0123456789abcdef" for char in name)


def _touch(path: Path) -> None:
    try:
        os.utime(path, None)
    except OSError:
        pass


def _fsync_file(path: Path) -> None:
    """Flush one file. A read-only handle makes Windows ``fsync`` raise EBADF.

    The bytes are already in the file. A platform that still refuses the
    flush does not fail the cache write; the reader checks the BLAKE2b.
    """
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError:
        return
    try:
        try:
            os.fsync(fd)
        except OSError:
            pass
    finally:
        os.close(fd)


def _fsync_dir(path: Path) -> None:
    """Flush a directory. Windows often cannot open a directory this way."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        try:
            os.fsync(fd)
        except OSError:
            pass
    finally:
        os.close(fd)


def _acquire(lock: Path) -> None:
    deadline = time.monotonic() + 30
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            try:
                age = time.time() - lock.stat().st_mtime
            except OSError:
                age = _LOCK_STALE_S + 1
            if age > _LOCK_STALE_S:
                try:
                    lock.unlink()
                except OSError:
                    pass
                continue
            if time.monotonic() > deadline:
                raise TimeoutError("prepared lock is held")
            time.sleep(0.05)
            continue
        try:
            os.write(fd, f"{os.getpid()} {time.time()}".encode("ascii"))
        finally:
            os.close(fd)
        return


def _release(lock: Path) -> None:
    try:
        lock.unlink()
    except OSError:
        pass


def _sweep_partials(root: Path) -> None:
    now = time.time()
    for child in root.iterdir():
        if not child.name.startswith(".partial-"):
            continue
        try:
            age = now - child.stat().st_mtime
        except OSError:
            continue
        if age > _PARTIAL_STALE_S:
            shutil.rmtree(child, ignore_errors=True)


def _drop_cache_entry(child: Path) -> bool:
    """Remove one cache directory. True only when this delete finished.

    A Windows sharing violation leaves the directory in place. Retry only
    when the delete raises and the directory is still there. A delete that
    returns has finished; a directory published again under the same name
    is a new entry and is not removed by this pass.
    """
    for _attempt in range(2):
        if not child.exists():
            return True
        try:
            shutil.rmtree(child)
        except OSError:
            continue
        return True
    return not child.exists()


def _enforce_budget(root: Path, *, keep: str) -> None:
    """Drop oldest entries until the directory is within ``max_bytes``.

    The size is subtracted only after the directory is confirmed gone. A
    delete that raises is retried once and then skipped, so an open handle
    does not free budget that is still on disk. A delete that returns is
    finished, and a directory published again under that name stays.

    An entry that is already mapped stays readable until that mapping is
    dropped. Unlinking the directory does not unmap it. The published files
    are immutable after the rename. The next open hashes them again and
    deletes the directory when the BLAKE2b or the row count does not match.
    """
    cap = max_bytes()
    entries = []
    total = 0
    for child in root.iterdir():
        if not _is_entry(child):
            continue
        size = _dir_size(child)
        total += size
        try:
            accessed = child.stat().st_atime
        except OSError:
            accessed = 0
        entries.append((accessed, child, size))
    entries.sort()
    for _accessed, child, size in entries:
        if total <= cap:
            break
        if child.name == keep:
            continue
        if not _drop_cache_entry(child):
            continue
        total -= size


def _remember(exc: BaseException) -> None:
    global LAST_CACHE_ERROR
    LAST_CACHE_ERROR = exc
