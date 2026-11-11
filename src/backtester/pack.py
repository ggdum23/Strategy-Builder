"""Chunked bar packs and the operator sidecar.

An anonymized pack rescales prices and volume and replaces calendar dates with
session ordinals. The ticker, the date map, and the scale factors are written
only to the sidecar. Designer-facing file names carry neither a ticker nor a
year. A chunk is about 250 trading sessions and each file stays under 25 MB.

Prices multiplied by k are paired with share_scale = k. The sidecar file is
where that number is stored. The runner reads it and passes it to the child
engine so per-share fees use real share counts. The pack files do not contain it.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import math
import re
import secrets
import shutil
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from .data import load_named
from .registry import get_series, load_registry, same_series

SCHEMA_VERSION = 1
CHUNK_SESSIONS = 250
MAX_FILE_BYTES = 25 * 1024 * 1024
ASSET_NAME = "ASSET"

PACK_COLUMNS = (
    "day",
    "weekday",
    "minute",
    "time",
    "session",
    "session_id",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "wap",
    "count",
    "filled",
    "short_session",
    "bar_minutes",
    "partial",
)

_SIDECAR_KEYS = {
    "schema_version",
    "pack_id",
    "anonymized",
    "symbol",
    "price_factor",
    "volume_factor",
    "share_scale",
    "date_map",
    "borrow_rate_annual",
    "borrow_rate_source",
    "registered_profile",
    "source_root",
    "source_glob",
    "bar_minutes",
    "operator_note",
}
_MANIFEST_KEYS = {
    "schema_version",
    "anonymized",
    "bar_minutes",
    "asset_name",
    "pack_id",
    "columns",
    "chunks",
    "daily_file",
    "n_sessions",
}
_CHUNK_KEYS = {"file", "first_day", "last_day", "sessions"}
_DATE_RE = re.compile(r"\b(?:19|20)\d{2}-\d{2}-\d{2}\b")
_YEAR_RE = re.compile(r"(?:19|20)\d{2}")
_BANNED_TEXT = (
    "price_factor",
    "volume_factor",
    "share_scale",
    "date_map",
    "operator_note",
    "source_root",
    "source_glob",
    "sidecar",
)


@dataclass(frozen=True)
class PackBuild:
    pack_dir: Path
    sidecar_path: Path
    anonymized: bool
    n_chunks: int
    n_sessions: int
    bar_minutes: int
    pack_id: str
    share_scale: float


@dataclass(frozen=True)
class LoadedPack:
    pack_dir: Path
    manifest: dict
    frame: pd.DataFrame
    pack_id: str
    anonymized: bool
    bar_minutes: int


def draw_factor(generator: secrets.SystemRandom | None = None) -> float:
    """A rescale away from 1 and away from the old 0.9 kit constant."""
    rng = generator or secrets.SystemRandom()
    value = math.exp(rng.uniform(math.log(0.15), math.log(0.75)))
    if not math.isfinite(value) or value <= 0.0 or value == 1.0 or abs(value - 0.9) < 1e-9:
        raise RuntimeError("refusing a degenerate scale factor")
    return value


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _fail(path: str, message: str) -> None:
    raise ValueError(f"sidecar {path}: {message}")


def _keys(value, expected: set[str], path: str) -> None:
    if not isinstance(value, dict):
        _fail(path, "expected an object")
    extra = sorted(set(value) - expected)
    missing = sorted(expected - set(value))
    if extra:
        _fail(path, f"unknown field {extra[0]!r}")
    if missing:
        _fail(path, f"missing field {missing[0]!r}")


def validate_sidecar(document: dict) -> None:
    if not isinstance(document, dict):
        _fail("", "the file is not an object")
    _keys(document, _SIDECAR_KEYS, "")
    if document["schema_version"] != SCHEMA_VERSION:
        _fail("schema_version", f"{document['schema_version']!r} is not supported")
    if not isinstance(document["anonymized"], bool):
        _fail("anonymized", "expected true or false")
    for name in ("pack_id", "symbol", "source_root", "source_glob"):
        if not isinstance(document[name], str) or not document[name]:
            _fail(name, "expected a non-empty string")
    if not isinstance(document["operator_note"], str):
        _fail("operator_note", "expected a string")
    for name in ("price_factor", "volume_factor", "share_scale"):
        value = document[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or float(value) <= 0.0:
            _fail(name, "expected a positive finite number")
    if document["anonymized"]:
        if float(document["share_scale"]) != float(document["price_factor"]):
            _fail("share_scale", "must equal price_factor (prices x k means share_scale = k)")
        if float(document["price_factor"]) == 1.0 or float(document["volume_factor"]) == 1.0:
            _fail("price_factor", "an anonymized pack must actually rescale prices and volume")
    else:
        if float(document["share_scale"]) != 1.0 or float(document["price_factor"]) != 1.0 or float(document["volume_factor"]) != 1.0:
            _fail("share_scale", "a named pack is unscaled, so every factor is 1")
    date_map = document["date_map"]
    if not isinstance(date_map, dict) or not date_map:
        _fail("date_map", "expected ordinal to calendar date")
    for key, value in date_map.items():
        if not isinstance(key, str) or not isinstance(value, str):
            _fail("date_map", "keys and values must be strings")
        if _DATE_RE.fullmatch(value) is None:
            _fail("date_map", f"{value!r} is not a calendar date")
    if document["borrow_rate_source"] not in {"profile_default", "explicit", "unregistered"}:
        _fail("borrow_rate_source", "unknown source")
    borrow = document["borrow_rate_annual"]
    if borrow is not None and (isinstance(borrow, bool) or not isinstance(borrow, (int, float)) or float(borrow) < 0.0):
        _fail("borrow_rate_annual", "expected a non-negative number or null")
    profile = document["registered_profile"]
    if profile is not None and profile not in {"arena", "desk"}:
        _fail("registered_profile", "expected arena, desk, or null")
    if isinstance(document["bar_minutes"], bool) or not isinstance(document["bar_minutes"], int) or document["bar_minutes"] < 1:
        _fail("bar_minutes", "expected a positive integer")


def _pack_id(files: list[tuple[str, bytes]]) -> str:
    digest = hashlib.sha256()
    for name, blob in sorted(files):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(len(blob).to_bytes(8, "big"))
        digest.update(blob)
    return digest.hexdigest()


def _gzip_csv(frame: pd.DataFrame) -> bytes:
    ordered = frame.loc[:, list(PACK_COLUMNS)].copy()
    for name in ("day", "weekday", "minute", "count", "filled", "short_session", "bar_minutes", "partial"):
        ordered[name] = ordered[name].astype(int)
    text = ordered.to_csv(index=False, float_format="%.17g")
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0, filename="") as handle:
        handle.write(text.encode("utf-8"))
    return buffer.getvalue()


def _read_csv_gz(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path, dtype={"time": str, "session": str, "session_id": str})
    if tuple(frame.columns) != PACK_COLUMNS:
        raise ValueError(f"{path.name} columns are {list(frame.columns)}; expected the pack schema")
    return frame


def _token_present(symbol: str, text: str) -> bool:
    if len(symbol) >= 2:
        return symbol in text
    return re.search(rf"(?<![A-Za-z0-9]){re.escape(symbol)}(?![A-Za-z0-9])", text) is not None


def _gzip_member(blob: bytes) -> tuple[int, bytes, bool]:
    """mtime, file name, and whether an FCOMMENT is present.

    RFC 1952 order after the 10-byte header is FEXTRA, FNAME, FCOMMENT, FHCRC.
    """
    if len(blob) < 10 or blob[:2] != b"\x1f\x8b":
        raise ValueError("gzip member is not gzip")
    flags = blob[3]
    mtime = int.from_bytes(blob[4:8], "little")
    pos = 10
    if flags & 0x04:
        if pos + 2 > len(blob):
            raise ValueError("gzip extra field is truncated")
        extra = int.from_bytes(blob[pos : pos + 2], "little")
        pos += 2 + extra
    fname = b""
    if flags & 0x08:
        end = blob.find(b"\x00", pos)
        if end < 0:
            raise ValueError("gzip file name is truncated")
        fname = blob[pos:end]
    has_comment = bool(flags & 0x10)
    return mtime, fname, has_comment


def assert_anonymized_pack_clean(pack_dir, *, symbol: str) -> None:
    """Refuse a designer tree that still carries identity or a scale factor."""
    root = Path(pack_dir)
    if not symbol:
        raise ValueError("symbol is empty")
    files = [path for path in root.rglob("*") if path.is_file()]
    if not files:
        raise ValueError("pack directory is empty")
    for path in files:
        name = path.name
        if _YEAR_RE.search(name) or _token_present(symbol, name):
            raise ValueError(f"{name} leaks identity in the file name")
        raw = path.read_bytes()
        if path.suffix == ".gz":
            mtime, fname, has_comment = _gzip_member(raw)
            if mtime != 0:
                raise ValueError(f"{name} gzip mtime is {mtime}, not 0")
            if fname:
                shown = fname.decode("utf-8", "replace")
                raise ValueError(f"{name} gzip file name is {shown!r}")
            if has_comment:
                raise ValueError(f"{name} gzip member has an FCOMMENT")
            text = gzip.decompress(raw).decode("utf-8")
        else:
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError(f"{name} is not text; refusing to ship it") from exc
        if _token_present(symbol, text):
            raise ValueError(f"{name} contains the ticker")
        for key in _BANNED_TEXT:
            if key in text:
                raise ValueError(f"{name} contains {key}")
        if _DATE_RE.search(text):
            raise ValueError(f"{name} contains a calendar date")
        if path.stat().st_size >= MAX_FILE_BYTES:
            raise ValueError(f"{name} is {path.stat().st_size} bytes, which is not under {MAX_FILE_BYTES}")


def _designer_frame(frame: pd.DataFrame, *, anonymize: bool, price_factor: float, volume_factor: float):
    dates = list(dict.fromkeys(str(value) for value in frame["date"].tolist()))
    if anonymize:
        ordinal = {date: index + 1 for index, date in enumerate(dates)}
        session_ids = {date: f"S{ordinal[date]:05d}" for date in dates}
        date_map = {str(ordinal[date]): date for date in dates}
    else:
        # Named packs keep the loader's day index and the real session date.
        ordinal = {date: int(day) for date, day in zip(frame["date"].tolist(), frame["day"].tolist())}
        session_ids = {date: date for date in dates}
        date_map = {str(ordinal[date]): date for date in dates}
    out = pd.DataFrame(
        {
            "day": [ordinal[str(value)] for value in frame["date"].tolist()],
            "weekday": frame["weekday"].astype(int).to_numpy(),
            "minute": frame["minute"].astype(int).to_numpy(),
            "time": frame["time"].astype(str).to_numpy(),
            "session": frame["session"].astype(str).to_numpy(),
            "session_id": [session_ids[str(value)] for value in frame["date"].tolist()],
            "open": frame["open"].astype(float).to_numpy() * price_factor,
            "high": frame["high"].astype(float).to_numpy() * price_factor,
            "low": frame["low"].astype(float).to_numpy() * price_factor,
            "close": frame["close"].astype(float).to_numpy() * price_factor,
            "volume": frame["volume"].astype(float).to_numpy() * volume_factor,
            "wap": frame["wap"].astype(float).to_numpy() * price_factor,
            "count": frame["count"].astype(int).to_numpy(),
            "filled": frame["filled"].astype(int).to_numpy(),
            "short_session": frame["short_session"].astype(int).to_numpy(),
            "bar_minutes": frame["bar_minutes"].astype(int).to_numpy(),
            "partial": frame["partial"].astype(int).to_numpy() if "partial" in frame.columns else 0,
        }
    )
    return out.loc[:, list(PACK_COLUMNS)].reset_index(drop=True), date_map


def _daily_frame(frame: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for day, group in frame.groupby("day", sort=True):
        regular = group.loc[group["session"] == "rth"]
        source = regular if len(regular) else group
        volume = float(source["volume"].sum())
        if volume > 0.0:
            wap = float((source["wap"] * source["volume"]).sum()) / volume
        else:
            wap = float(source["close"].iloc[-1])
        rows.append(
            {
                "day": int(day),
                "weekday": int(source["weekday"].iloc[0]),
                "minute": int(source["minute"].iloc[0]),
                "time": str(source["time"].iloc[0]),
                "session": "rth" if len(regular) else str(source["session"].iloc[0]),
                "session_id": str(source["session_id"].iloc[0]),
                "open": float(source["open"].iloc[0]),
                "high": float(source["high"].max()),
                "low": float(source["low"].min()),
                "close": float(source["close"].iloc[-1]),
                "volume": volume,
                "wap": wap,
                "count": int(source["count"].sum()),
                "filled": int(source["filled"].max()),
                "short_session": int(source["short_session"].iloc[0]),
                "bar_minutes": int(source["bar_minutes"].iloc[0]),
                "partial": int(source["partial"].max()),
            }
        )
    return pd.DataFrame(rows, columns=list(PACK_COLUMNS))


def _write_chunks(frame: pd.DataFrame, directory: Path, chunk_sessions: int, max_bytes: int):
    days = list(dict.fromkeys(int(day) for day in frame["day"].tolist()))
    written = []
    start = 0
    part = 1
    while start < len(days):
        width = min(chunk_sessions, len(days) - start)
        while True:
            chosen = days[start : start + width]
            chosen_set = set(chosen)
            subset = frame.loc[frame["day"].isin(chosen_set)]
            blob = _gzip_csv(subset)
            if len(blob) < max_bytes:
                name = f"bars_part_{part:03d}.csv.gz"
                (directory / name).write_bytes(blob)
                written.append(
                    {
                        "file": name,
                        "first_day": int(chosen[0]),
                        "last_day": int(chosen[-1]),
                        "sessions": len(chosen),
                        "bytes": blob,
                    }
                )
                start += width
                part += 1
                break
            if width == 1:
                raise ValueError(
                    f"session {chosen[0]} is {len(blob)} bytes, which is not under {max_bytes}. "
                    "Refusing to split one session across files."
                )
            width //= 2
    return written


def build_pack(
    src,
    symbol: str,
    out_dir,
    sidecar_path,
    *,
    anonymize: bool,
    profile: str | None = None,
    registry_path=None,
    bar_minutes: int = 1,
    price_factor: float | None = None,
    volume_factor: float | None = None,
    chunk_sessions: int = CHUNK_SESSIONS,
    max_bytes: int = MAX_FILE_BYTES,
    phase_map=None,
    price_kind: str = "raw",
    operator_note: str = "",
) -> PackBuild:
    """Inspect ``src``, optionally anonymize, and write a pack plus a sidecar.

    The sidecar is never placed inside the pack directory. An existing pack
    directory must be empty.
    """
    if isinstance(chunk_sessions, bool) or not isinstance(chunk_sessions, int) or chunk_sessions < 1:
        raise ValueError("chunk_sessions must be a positive integer")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise ValueError("max_bytes must be a positive integer")
    if not isinstance(operator_note, str):
        raise ValueError("operator_note must be a string")
    destination = Path(out_dir)
    sidecar = Path(sidecar_path)
    if destination == sidecar or _inside(sidecar, destination):
        raise ValueError("the sidecar must not live inside the pack directory")
    if destination.exists() and any(destination.iterdir()):
        raise ValueError(f"{destination} is not empty. Refusing to mix a pack into it.")
    loaded = load_named(src, symbol, bar_minutes=bar_minutes, phase_map=phase_map, price_kind=price_kind)
    if not loaded.inspection.runnable:
        raise ValueError(
            f"{symbol} is not runnable ({loaded.inspection.price_adjustment}). "
            "Refusing to pack a series this phase cannot execute."
        )
    borrow_rate = None
    borrow_source = "unregistered"
    registered_profile = None
    source_root = str(src)
    if registry_path is not None:
        entry = get_series(load_registry(registry_path), symbol)
        same_series(entry, loaded.inspection)
        if not entry["runnable"]:
            raise ValueError(f"{symbol} is registered as not runnable")
        source_root = entry["source_root"]
        borrow_source = entry["borrow_rate_source"]
        registered_profile = entry["profile"]
        if profile is not None and entry["profile"] != profile:
            raise ValueError(
                f"{symbol} is registered as {entry['profile']}, not {profile}. "
                "Re-ingest to change the registered profile. The run command still chooses the engine."
            )
        if borrow_source == "explicit":
            borrow_rate = entry["borrow_rate_annual"]
    if anonymize:
        price = draw_factor() if price_factor is None else float(price_factor)
        volume = draw_factor() if volume_factor is None else float(volume_factor)
        if price == 1.0 or volume == 1.0 or price <= 0.0 or volume <= 0.0:
            raise ValueError("anonymized price_factor and volume_factor must be positive and not 1")
        if not math.isfinite(price) or not math.isfinite(volume):
            raise ValueError("scale factors must be finite")
    else:
        if price_factor not in (None, 1.0) or volume_factor not in (None, 1.0):
            raise ValueError("a named pack is not rescaled. Omit the scale factors.")
        price = 1.0
        volume = 1.0
    share_scale = price
    frame, date_map = _designer_frame(
        loaded.frame, anonymize=anonymize, price_factor=price, volume_factor=volume
    )
    destination.mkdir(parents=True, exist_ok=True)
    sidecar_existed = sidecar.exists()
    try:
        chunks = _write_chunks(frame, destination, chunk_sessions, max_bytes)
        daily_blob = _gzip_csv(_daily_frame(frame))
        if len(daily_blob) >= max_bytes:
            raise ValueError(f"daily file is {len(daily_blob)} bytes, which is not under {max_bytes}")
        daily_name = "daily_rth.csv.gz"
        (destination / daily_name).write_bytes(daily_blob)
        hashed = [(item["file"], item["bytes"]) for item in chunks]
        hashed.append((daily_name, daily_blob))
        pack_id = _pack_id(hashed)
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "anonymized": bool(anonymize),
            "bar_minutes": int(bar_minutes),
            "asset_name": ASSET_NAME if anonymize else symbol,
            "pack_id": pack_id,
            "columns": list(PACK_COLUMNS),
            "chunks": [
                {
                    "file": item["file"],
                    "first_day": item["first_day"],
                    "last_day": item["last_day"],
                    "sessions": item["sessions"],
                }
                for item in chunks
            ],
            "daily_file": daily_name,
            "n_sessions": int(frame["day"].nunique()),
        }
        (destination / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        if anonymize:
            assert_anonymized_pack_clean(destination, symbol=symbol)
        sidecar_document = {
            "schema_version": SCHEMA_VERSION,
            "pack_id": pack_id,
            "anonymized": bool(anonymize),
            "symbol": symbol,
            "price_factor": price,
            "volume_factor": volume,
            "share_scale": share_scale,
            "date_map": date_map,
            "borrow_rate_annual": borrow_rate,
            "borrow_rate_source": borrow_source,
            "registered_profile": registered_profile,
            "source_root": source_root,
            "source_glob": loaded.inspection.source_glob,
            "bar_minutes": int(bar_minutes),
            "operator_note": operator_note,
        }
        validate_sidecar(sidecar_document)
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text(json.dumps(sidecar_document, indent=2) + "\n", encoding="utf-8")
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        if not sidecar_existed and sidecar.is_file():
            sidecar.unlink()
        raise
    return PackBuild(
        pack_dir=destination,
        sidecar_path=sidecar,
        anonymized=bool(anonymize),
        n_chunks=len(chunks),
        n_sessions=int(frame["day"].nunique()),
        bar_minutes=int(bar_minutes),
        pack_id=pack_id,
        share_scale=share_scale,
    )


def _json_pairs(pairs):
    """Object hook that refuses a repeated key. ``json.loads`` would keep the last one."""
    document = {}
    for key, value in pairs:
        if key in document:
            raise ValueError(f"duplicate JSON key {key!r}")
        document[key] = value
    return document


def load_sidecar(path) -> dict:
    file = Path(path)
    try:
        document = json.loads(file.read_text(encoding="utf-8"), object_pairs_hook=_json_pairs)
    except json.JSONDecodeError as exc:
        raise ValueError(f"sidecar {file} is not JSON: {exc}") from exc
    validate_sidecar(document)
    return document


def _validate_manifest(document: dict) -> None:
    if not isinstance(document, dict):
        raise ValueError("pack manifest is not an object")
    extra = sorted(set(document) - _MANIFEST_KEYS)
    missing = sorted(_MANIFEST_KEYS - set(document))
    if extra or missing:
        raise ValueError(f"pack manifest fields are wrong (missing {missing}, unknown {extra})")
    if document["schema_version"] != SCHEMA_VERSION:
        raise ValueError(f"pack schema_version {document['schema_version']!r} is not supported")
    if not isinstance(document["anonymized"], bool):
        raise ValueError("pack anonymized flag is not true or false")
    if document["anonymized"] and document["asset_name"] != ASSET_NAME:
        raise ValueError("an anonymized pack must call the asset ASSET")
    if list(document["columns"]) != list(PACK_COLUMNS):
        raise ValueError("pack columns do not match the schema")
    if not isinstance(document["chunks"], list) or not document["chunks"]:
        raise ValueError("pack has no chunks")
    previous = None
    for chunk in document["chunks"]:
        if set(chunk) != _CHUNK_KEYS:
            raise ValueError(f"chunk record {chunk!r} does not match the schema")
        if _YEAR_RE.search(chunk["file"]) or chunk["file"] != Path(chunk["file"]).name:
            raise ValueError(f"chunk file name {chunk['file']!r} is not allowed")
        if chunk["sessions"] < 1 or chunk["last_day"] < chunk["first_day"]:
            raise ValueError(f"chunk {chunk['file']} has an empty day range")
        if previous is not None and chunk["first_day"] <= previous:
            raise ValueError("pack chunks overlap or are out of order")
        previous = chunk["last_day"]


def load_pack(pack_dir) -> LoadedPack:
    """Read a pack. This does not open a sidecar."""
    root = Path(pack_dir)
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"pack manifest is missing: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    _validate_manifest(manifest)
    frames = []
    hashed = []
    for chunk in manifest["chunks"]:
        path = root / chunk["file"]
        blob = path.read_bytes()
        if len(blob) >= MAX_FILE_BYTES:
            raise ValueError(f"{chunk['file']} is not under {MAX_FILE_BYTES} bytes")
        hashed.append((chunk["file"], blob))
        frame = _read_csv_gz(path)
        days = sorted(int(day) for day in frame["day"].unique())
        if not days or days[0] != chunk["first_day"] or days[-1] != chunk["last_day"] or len(days) != chunk["sessions"]:
            raise ValueError(f"{chunk['file']} does not match its manifest day range")
        frames.append(frame)
    daily_path = root / manifest["daily_file"]
    daily_blob = daily_path.read_bytes()
    hashed.append((manifest["daily_file"], daily_blob))
    pack_id = _pack_id(hashed)
    if pack_id != manifest["pack_id"]:
        raise ValueError("pack files do not match the manifest pack_id")
    frame = pd.concat(frames, ignore_index=True)
    frame["date"] = frame["session_id"].astype(str)
    return LoadedPack(
        pack_dir=root,
        manifest=manifest,
        frame=frame,
        pack_id=pack_id,
        anonymized=bool(manifest["anonymized"]),
        bar_minutes=int(manifest["bar_minutes"]),
    )
