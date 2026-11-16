
from __future__ import annotations

import csv
import gzip
import hashlib
import hmac
import io
import json
import math
import os
import re
import secrets
import shutil
import stat
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from .data import load_named
from .registry import get_series, load_registry, same_series
from .report import TICKER_STAND_IN, require_anonymizable_symbol, ticker_token_spans

SCHEMA_VERSION = 1
CHUNK_SESSIONS = 250
MAX_FILE_BYTES = 25 * 1024 * 1024

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
    "price_basis",
}
_FACTOR_MAC_KEYS = {"factor_key", "factor_mac"}
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
    "price_basis",
}
_CHUNK_KEYS = {"file", "first_day", "last_day", "sessions"}
_DATE_RE = re.compile(r"\b(?:19|20)[0-9]{2}-[0-9]{2}-[0-9]{2}\b")
_DATE_MAP_KEY_RE = re.compile(r"^(?:0|[1-9][0-9]*)$")
_BARS_NAME_RE = re.compile(r"^bars_part_([0-9]+)\.csv\.gz$")
_DAILY_NAME = "daily_rth.csv.gz"
_MANIFEST_NAME = "manifest.json"
_FACTOR_MAC_VERSION = 2
_BORROW_SOURCES = {"profile_default", "explicit", "unregistered"}
_PRINTABLE_ASCII = frozenset(b"\t\n\r" + bytes(range(0x20, 0x7F)))
_YEAR = r"(?:19|20)[0-9]{2}"
_MONTH = r"(?:0?[1-9]|1[0-2])"
_DAY = r"(?:0?[1-9]|[12][0-9]|3[01])"
_MONTH2 = r"(?:0[1-9]|1[0-2])"
_DAY2 = r"(?:0[1-9]|[12][0-9]|3[01])"
_SEP = r"[-/._ ]"
_LEAK_DATE_RE = re.compile(
    rf"(?<![0-9])(?:{_YEAR}{_SEP}{_MONTH}{_SEP}{_DAY}|{_DAY}{_SEP}{_MONTH}{_SEP}{_YEAR}|{_YEAR}{_MONTH2}{_DAY2})(?![0-9])"
)
_ALT_STREAM = "pack file has an alternate data stream"
_STREAM_UNREADABLE = "pack file streams could not be read"
_DIR_ALT_STREAM = "pack directory has an alternate data stream"
_DIR_STREAM_UNREADABLE = "pack directory streams could not be read"
_DEFAULT_STREAM = "::$DATA"
_DIRECTORY_INDEX_STREAM = ":$i30:$index_allocation"
_PACK_ID_RE = re.compile(r"^[0-9a-f]{64}$")
_INT_RE = re.compile(r"^(?:0|[1-9][0-9]*)$")
_PHASES = frozenset({"pre", "rth", "post"})
_PRICE_COLUMNS = frozenset({"open", "high", "low", "close", "volume", "wap"})
_FLAG_COLUMNS = frozenset({"filled", "short_session", "partial"})
_MAX_ORDINAL = 1_000_000_000
_MAX_MINUTE = 24 * 60 - 1
_MAX_BAR_MINUTES = 24 * 60
_NOT_REGULAR = "pack directory entry is not a regular pack file"
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


def canonical_price_factor(price_factor: float) -> str:
    if isinstance(price_factor, bool) or not isinstance(price_factor, (int, float)):
        raise ValueError("price factor must be a positive finite number")
    value = float(price_factor)
    if not math.isfinite(value) or value <= 0.0:
        raise ValueError("price factor must be a positive finite number")
    return json.dumps(value, allow_nan=False)


def _canonical_borrow_rate(value):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("borrow rate must be a non-negative number or null")
    number = float(value)
    if not math.isfinite(number) or number < 0.0:
        raise ValueError("borrow rate must be a non-negative number or null")
    return number


def _factor_mac_message(document: dict) -> bytes:
    if not isinstance(document.get("anonymized"), bool):
        raise ValueError("anonymized flag must be true or false")
    pack_id = document.get("pack_id")
    if not isinstance(pack_id, str) or not pack_id:
        raise ValueError("pack_id must be a non-empty string")
    date_map = document.get("date_map")
    if not isinstance(date_map, dict) or not date_map:
        raise ValueError("date_map must map ordinals to calendar dates")
    canonical_map = {}
    for key in sorted(date_map):
        value = date_map[key]
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValueError("date_map keys and values must be strings")
        canonical_map[key] = value
    source = document.get("borrow_rate_source")
    if source not in _BORROW_SOURCES:
        raise ValueError("borrow_rate_source is not a known source")
    payload = {
        "v": _FACTOR_MAC_VERSION,
        "pack_id": pack_id,
        "anonymized": document["anonymized"],
        "date_map": canonical_map,
        "price_factor": canonical_price_factor(document["price_factor"]),
        "share_scale": canonical_price_factor(document["share_scale"]),
        "borrow_rate_source": source,
        "borrow_rate_annual": _canonical_borrow_rate(document.get("borrow_rate_annual")),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def factor_mac(key: bytes, document: dict) -> str:
    if not isinstance(key, (bytes, bytearray)) or len(key) != 32:
        raise ValueError("factor key must be 32 bytes")
    if not isinstance(document, dict):
        raise ValueError("factor MAC document must be an object")
    return hmac.new(bytes(key), _factor_mac_message(document), hashlib.sha256).hexdigest()


def _hex32(text, label: str) -> bytes:
    if not isinstance(text, str) or len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        _fail(label, "expected 32 bytes of lowercase hex")
    return bytes.fromhex(text)


def _verify_factor_mac(document: dict) -> None:
    present = _FACTOR_MAC_KEYS & set(document)
    if not present:
        _fail("factor_mac", "sidecar has no operator factor MAC; rebuild the pack")
    if present != _FACTOR_MAC_KEYS:
        _fail("factor_mac", "the operator factor MAC is incomplete")
    key = _hex32(document.get("factor_key"), "factor_key")
    _hex32(document.get("factor_mac"), "factor_mac")
    try:
        expected = factor_mac(key, document)
    except ValueError:
        _fail("factor_mac", "does not match the build-time price factor")
    if not hmac.compare_digest(document["factor_mac"], expected):
        _fail("factor_mac", "does not match the build-time price factor")


def require_build_price_factor(sidecar: dict) -> float:
    if not isinstance(sidecar, dict):
        raise ValueError("sidecar is not an object")
    _verify_factor_mac(sidecar)
    return float(sidecar["price_factor"])


def validate_sidecar(document: dict, *, allow_scale_mismatch: bool = False) -> None:
    if not isinstance(document, dict):
        _fail("", "the file is not an object")
    extra = sorted(set(document) - _SIDECAR_KEYS - _FACTOR_MAC_KEYS)
    missing = sorted(_SIDECAR_KEYS - set(document))
    if extra:
        _fail("", f"unknown field {extra[0]!r}")
    if missing:
        _fail("", f"missing field {missing[0]!r}")
    if document["schema_version"] != SCHEMA_VERSION:
        _fail("schema_version", "is not supported")
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
    date_map = document["date_map"]
    if not isinstance(date_map, dict) or not date_map:
        _fail("date_map", "expected ordinal to calendar date")
    for key, value in date_map.items():
        if not isinstance(key, str) or not isinstance(value, str):
            _fail("date_map", "keys and values must be strings")
        if _DATE_MAP_KEY_RE.fullmatch(key) is None:
            _fail("date_map", f"key {key!r} is not a canonical ordinal")
        if _DATE_RE.fullmatch(value) is None:
            _fail("date_map", f"{value!r} is not a calendar date")
    if document["borrow_rate_source"] not in _BORROW_SOURCES:
        _fail("borrow_rate_source", "unknown source")
    borrow = document["borrow_rate_annual"]
    if borrow is not None and (
        isinstance(borrow, bool)
        or not isinstance(borrow, (int, float))
        or not math.isfinite(float(borrow))
        or float(borrow) < 0.0
    ):
        _fail("borrow_rate_annual", "expected a non-negative number or null")
    require_build_price_factor(document)
    if document["anonymized"]:
        if float(document["price_factor"]) == 1.0 or float(document["volume_factor"]) == 1.0:
            _fail("price_factor", "an anonymized pack must actually rescale prices and volume")
        if not allow_scale_mismatch and float(document["share_scale"]) != float(document["price_factor"]):
            _fail("share_scale", "must equal the build-time price factor")
    else:
        if float(document["share_scale"]) != 1.0 or float(document["price_factor"]) != 1.0 or float(document["volume_factor"]) != 1.0:
            _fail("share_scale", "a named pack is unscaled, so every factor is 1")
    if document.get("price_basis") not in {"raw", "total_return"}:
        _fail("price_basis", "expected raw or total_return")
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


def _emitted_count(value) -> float:
    return float(format(float(value), ".17g"))


def _gzip_csv(frame: pd.DataFrame) -> bytes:
    ordered = frame.loc[:, list(PACK_COLUMNS)].copy()
    for name in ("day", "weekday", "minute", "filled", "short_session", "bar_minutes", "partial"):
        ordered[name] = ordered[name].astype(int)
    ordered["count"] = [_emitted_count(value) for value in ordered["count"].tolist()]
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
    return bool(ticker_token_spans(text, symbol))


def _json_values_have_ticker(value, symbol: str) -> bool:
    if isinstance(value, dict):
        return any(_json_values_have_ticker(item, symbol) for item in value.values())
    if isinstance(value, list):
        return any(_json_values_have_ticker(item, symbol) for item in value)
    if isinstance(value, str):
        if value in PACK_COLUMNS or value in {"raw", "total_return"}:
            return False
        return _token_present(symbol, value)
    return False


def _text_has_ticker(name: str, text: str, symbol: str) -> bool:
    lower = name.lower()
    if lower.endswith(".json"):
        try:
            document = json.loads(text)
        except json.JSONDecodeError:
            return _token_present(symbol, text)
        return _json_values_have_ticker(document, symbol)
    if ".csv" in lower:
        rows = list(csv.reader(io.StringIO(text)))
        for row in rows[1:]:
            if any(_token_present(symbol, cell) for cell in row):
                return True
        return False
    return _token_present(symbol, text)


def _gzip_member(blob: bytes) -> tuple[int, bytes, bool, bool, bool, int]:
    if len(blob) < 10 or blob[:2] != b"\x1f\x8b":
        raise ValueError("gzip member is not gzip")
    flags = blob[3]
    mtime = int.from_bytes(blob[4:8], "little")
    has_extra = bool(flags & 0x04)
    has_hcrc = bool(flags & 0x02)
    pos = 10
    if flags & 0x04:
        if pos + 2 > len(blob):
            raise ValueError("gzip extra field is truncated")
        extra = int.from_bytes(blob[pos : pos + 2], "little")
        pos += 2 + extra
        if pos > len(blob):
            raise ValueError("gzip extra field is truncated")
    fname = b""
    if flags & 0x08:
        end = blob.find(b"\x00", pos)
        if end < 0:
            raise ValueError("gzip file name is truncated")
        fname = blob[pos:end]
        pos = end + 1
    has_comment = bool(flags & 0x10)
    if flags & 0x10:
        end = blob.find(b"\x00", pos)
        if end < 0:
            raise ValueError("gzip comment is truncated")
        pos = end + 1
    if flags & 0x02:
        pos += 2
    if pos > len(blob):
        raise ValueError("gzip member is truncated")
    return mtime, fname, has_comment, has_extra, has_hcrc, pos


def _gzip_members(blob: bytes):
    rest = blob
    while True:
        rest = rest.lstrip(b"\x00")
        if not rest:
            return
        mtime, fname, has_comment, has_extra, has_hcrc, start = _gzip_member(rest)
        inflator = zlib.decompressobj(wbits=-zlib.MAX_WBITS)
        payload = inflator.decompress(rest[start:])
        unused = inflator.unused_data
        if not inflator.eof or len(unused) < 8:
            raise ValueError("gzip member is truncated")
        crc, length = struct.unpack("<II", unused[:8])
        if crc != zlib.crc32(payload) or length != (len(payload) & 0xFFFFFFFF):
            raise ValueError("gzip member failed its checksum")
        yield (mtime, fname, has_comment, has_extra, has_hcrc), payload
        rest = unused[8:]


def _anonymized_gzip_payloads(name: str, blob: bytes) -> list[bytes]:
    payloads = []
    for (mtime, fname, has_comment, has_extra, has_hcrc), payload in _gzip_members(blob):
        if mtime != 0:
            raise ValueError("gzip mtime is not 0")
        if fname:
            raise ValueError("gzip member has a file name")
        if has_comment:
            raise ValueError(f"{name} gzip member has an FCOMMENT")
        if has_extra:
            raise ValueError(f"{name} gzip member has an FEXTRA")
        if has_hcrc:
            raise ValueError(f"{name} gzip member has an FHCRC")
        payloads.append(payload)
    if not payloads:
        raise ValueError(f"{name} gzip member is not gzip")
    if len(payloads) != 1:
        raise ValueError(f"{name} gzip has {len(payloads)} members; expected 1")
    return payloads


def _canonical_bars_name(name: str) -> bool:
    match = _BARS_NAME_RE.fullmatch(name)
    if match is None:
        return False
    digits = match.group(1)
    number = int(digits)
    return 1 <= number <= _MAX_ORDINAL and digits == f"{number:03d}"


def _canonical_anonymized_name(name: str) -> bool:
    return name in {_MANIFEST_NAME, _DAILY_NAME} or _canonical_bars_name(name)


def _entry_is_regular_pack_file(mode: int, name: str) -> bool:
    return stat.S_ISREG(mode) and _canonical_anonymized_name(name)


def _printable_ascii(blob: bytes) -> bool:
    return blob.translate(None, bytes(_PRINTABLE_ASCII)) == b""


def _session_ordinal(cell: str) -> int | None:
    if len(cell) < 6 or len(cell) > 16 or cell[0] != "S":
        return None
    digits = cell[1:]
    if not digits.isascii() or not digits.isdigit():
        return None
    number = int(digits)
    if number < 1 or number > _MAX_ORDINAL or digits != f"{number:05d}":
        return None
    return number


def _canonical_int(cell: str, low: int, high: int) -> int | None:
    if len(cell) > 18 or _INT_RE.fullmatch(cell) is None:
        return None
    value = int(cell)
    if value < low or value > high:
        return None
    return value


def _canonical_decimal(cell: str) -> bool:
    if not cell or len(cell) > 64 or cell[0] == "+":
        return False
    try:
        value = float(cell)
    except ValueError:
        return False
    return math.isfinite(value) and cell == format(value, ".17g")


def _canonical_count(cell: str) -> bool:
    if not _canonical_decimal(cell):
        return False
    return float(cell) >= 0.0


def _fail_cell(name: str, column: str, cell: str) -> None:
    if _LEAK_DATE_RE.search(cell):
        raise ValueError(f"{name} contains a calendar date")
    raise ValueError(f"{column} cell is not the builder form")


def _manifest_int(value, low: int, high: int, label: str) -> int:
    if isinstance(value, bool) or type(value) is not int or not low <= value <= high:
        raise ValueError(f"{label} is not an ordinal")
    return value


def _manifest_bar_minutes(value) -> int:
    if isinstance(value, bool) or type(value) is not int or not 1 <= value <= _MAX_BAR_MINUTES:
        raise ValueError("bar_minutes is not an allowed bar size")
    return value


def _validate_anonymized_manifest(document: dict) -> None:
    if not isinstance(document, dict):
        raise ValueError("pack manifest is not an object")
    extra = sorted(set(document) - _MANIFEST_KEYS)
    missing = sorted(_MANIFEST_KEYS - set(document))
    if extra or missing:
        raise ValueError(f"pack manifest fields are wrong (missing {missing}, unknown {extra})")
    if isinstance(document["schema_version"], bool) or document["schema_version"] != SCHEMA_VERSION:
        raise ValueError("pack schema_version is not supported")
    if document.get("price_basis") not in {"raw", "total_return"}:
        raise ValueError("pack price_basis must be raw or total_return")
    if document["anonymized"] is not True:
        raise ValueError("pack anonymized flag is not true or false")
    if document["asset_name"] != TICKER_STAND_IN:
        raise ValueError(f"an anonymized pack must call the asset {TICKER_STAND_IN}")
    pack_id = document["pack_id"]
    if not isinstance(pack_id, str) or _PACK_ID_RE.fullmatch(pack_id) is None:
        raise ValueError("pack_id is not 64 lowercase hex digits")
    _manifest_bar_minutes(document["bar_minutes"])
    if document["columns"] != list(PACK_COLUMNS):
        raise ValueError("pack columns do not match the schema")
    chunks = document["chunks"]
    if not isinstance(chunks, list) or not chunks:
        raise ValueError("pack has no chunks")
    names = []
    previous = None
    for chunk in chunks:
        if not isinstance(chunk, dict) or set(chunk) != _CHUNK_KEYS:
            raise ValueError("chunk record does not match the schema")
        chunk_name = chunk["file"]
        if (
            not isinstance(chunk_name, str)
            or chunk_name != Path(chunk_name).name
            or not _canonical_bars_name(chunk_name)
        ):
            raise ValueError("chunk file name is not allowed")
        first = _manifest_int(chunk["first_day"], 1, _MAX_ORDINAL, "first_day")
        last = _manifest_int(chunk["last_day"], 1, _MAX_ORDINAL, "last_day")
        sessions = _manifest_int(chunk["sessions"], 1, _MAX_ORDINAL, "sessions")
        if last < first or sessions > (last - first + 1):
            raise ValueError(f"chunk {chunk_name} has an empty day range")
        if previous is not None and first <= previous:
            raise ValueError("pack chunks overlap or are out of order")
        previous = last
        names.append(chunk_name)
    if len(names) != len(set(names)):
        raise ValueError("chunk file names repeat")
    if document["daily_file"] != _DAILY_NAME:
        raise ValueError("daily file name is not allowed")
    total = sum(chunk["sessions"] for chunk in chunks)
    counted = _manifest_int(document["n_sessions"], 1, _MAX_ORDINAL, "n_sessions")
    if counted != total:
        raise ValueError("n_sessions is not the chunk session count")


def _clock_text(minute: int) -> str:
    return f"{minute // 60:02d}:{minute % 60:02d}"


def _csv_days(
    name: str,
    text: str,
    *,
    symbol: str | None,
    bar_minutes: int,
    single_row: bool = False,
) -> tuple[list[int], dict[int, float], dict[int, float]]:
    try:
        rows = list(csv.reader(io.StringIO(text)))
    except csv.Error as exc:
        raise ValueError(f"{name} CSV header is not the pack schema") from exc
    if not rows or rows[0] != list(PACK_COLUMNS):
        raise ValueError(f"{name} CSV header is not the pack schema")
    index = {column: position for position, column in enumerate(PACK_COLUMNS)}
    days: list[int] = []
    counts: dict[int, float] = {}
    session_counts: dict[int, float] = {}
    saw_rth: set[int] = set()
    previous: int | None = None
    for row in rows[1:]:
        if symbol and any(_token_present(symbol, cell) for cell in row):
            raise ValueError(f"{name} contains the ticker")
        if len(row) != len(PACK_COLUMNS):
            raise ValueError(f"{name} CSV row does not match the pack header")
        day = _canonical_int(row[index["day"]], 1, _MAX_ORDINAL)
        if day is None:
            _fail_cell(name, "day", row[index["day"]])
        if _canonical_int(row[index["weekday"]], 0, 6) is None:
            _fail_cell(name, "weekday", row[index["weekday"]])
        minute = _canonical_int(row[index["minute"]], 0, _MAX_MINUTE)
        if minute is None:
            _fail_cell(name, "minute", row[index["minute"]])
        if row[index["time"]] != _clock_text(minute):
            _fail_cell(name, "time", row[index["time"]])
        if row[index["session"]] not in _PHASES:
            _fail_cell(name, "session", row[index["session"]])
        if row[index["session_id"]] != f"S{day:05d}":
            _fail_cell(name, "session_id", row[index["session_id"]])
        for column in _PRICE_COLUMNS:
            if not _canonical_decimal(row[index[column]]):
                _fail_cell(name, column, row[index[column]])
        count_cell = row[index["count"]]
        if not _canonical_count(count_cell):
            _fail_cell(name, "count", count_cell)
        for column in _FLAG_COLUMNS:
            if _canonical_int(row[index[column]], 0, 1) is None:
                _fail_cell(name, column, row[index[column]])
        if _canonical_int(row[index["bar_minutes"]], bar_minutes, bar_minutes) is None:
            _fail_cell(name, "bar_minutes", row[index["bar_minutes"]])
        if previous is None or day == previous + 1:
            days.append(day)
        elif day == previous:
            if single_row:
                raise ValueError(f"{name} repeats a session")
        else:
            raise ValueError(f"{name} sessions are not sequential")
        previous = day
        value = float(count_cell)
        counts[day] = counts.get(day, 0.0) + value
        if row[index["session"]] == "rth":
            saw_rth.add(day)
            session_counts[day] = session_counts.get(day, 0.0) + value
    if not days:
        raise ValueError(f"{name} CSV row does not match the pack header")
    for day in days:
        if day not in saw_rth:
            session_counts[day] = counts[day]
    return days, counts, session_counts


def _reject_banned(name: str, text: str) -> None:
    for key in _BANNED_TEXT:
        if key in text:
            raise ValueError(f"{name} contains {key}")


def _stream_names(path: Path) -> list[str]:
    if os.name != "nt":
        return []
    try:
        directory = stat.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        raise OSError(_STREAM_UNREADABLE) from None
    if directory:
        return _directory_stream_names(path)
    return _file_stream_names(path)


def _file_stream_names(path: Path) -> list[str]:
    import ctypes
    from ctypes import wintypes

    class _StreamData(ctypes.Structure):
        _fields_ = (
            ("StreamSize", ctypes.c_longlong),
            ("cStreamName", ctypes.c_wchar * 296),
        )

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    find_first = kernel32.FindFirstStreamW
    find_first.argtypes = [wintypes.LPCWSTR, ctypes.c_int, ctypes.POINTER(_StreamData), wintypes.DWORD]
    find_first.restype = wintypes.HANDLE
    find_next = kernel32.FindNextStreamW
    find_next.argtypes = [wintypes.HANDLE, ctypes.POINTER(_StreamData)]
    find_next.restype = wintypes.BOOL
    find_close = kernel32.FindClose
    find_close.argtypes = [wintypes.HANDLE]
    find_close.restype = wintypes.BOOL
    data = _StreamData()
    handle = find_first(os.fspath(path), 0, ctypes.byref(data), 0)
    if handle is None or int(handle) in {0, -1, 2**32 - 1, 2**64 - 1}:
        raise OSError(_STREAM_UNREADABLE)
    names = []
    try:
        while True:
            names.append(data.cStreamName.replace("\x00", ""))
            data = _StreamData()
            if not find_next(handle, ctypes.byref(data)):
                error = ctypes.get_last_error()
                if error in {18, 38}:
                    return names
                raise OSError(_STREAM_UNREADABLE)
    finally:
        find_close(handle)


def _stream_names_from_information(blob: bytes) -> list[str]:
    names = []
    offset = 0
    while offset + 24 <= len(blob):
        next_off = int.from_bytes(blob[offset : offset + 4], "little")
        name_len = int.from_bytes(blob[offset + 4 : offset + 8], "little")
        end = offset + 24 + name_len
        if end > len(blob):
            raise OSError(_STREAM_UNREADABLE)
        if name_len:
            try:
                text = blob[offset + 24 : end].decode("utf-16-le").replace("\x00", "")
            except UnicodeError:
                raise OSError(_STREAM_UNREADABLE) from None
            if text:
                names.append(text)
        if next_off == 0:
            return names
        if next_off < 24:
            raise OSError(_STREAM_UNREADABLE)
        offset += next_off
    return names


def _directory_stream_names(path: Path) -> list[str]:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll")
    create = kernel32.CreateFileW
    create.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create.restype = wintypes.HANDLE
    close = kernel32.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL

    class _IoStatus(ctypes.Structure):
        _fields_ = (("Pointer", ctypes.c_void_p), ("Information", ctypes.c_size_t))

    query = ntdll.NtQueryInformationFile
    query.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_IoStatus),
        ctypes.c_void_p,
        wintypes.ULONG,
        ctypes.c_int,
    ]
    query.restype = ctypes.c_long

    handle = create(os.fspath(path), 0x80000000, 0x7, None, 3, 0x02200000, None)
    if handle is None or int(handle) in {0, -1, 2**32 - 1, 2**64 - 1}:
        raise OSError(_STREAM_UNREADABLE)
    try:
        size = 4096
        status = 1
        raw = b""
        while size <= 1_048_576:
            buf = ctypes.create_string_buffer(size)
            status = int(query(handle, ctypes.byref(_IoStatus()), ctypes.cast(buf, ctypes.c_void_p), size, 22))
            status &= 0xFFFFFFFF
            if status == 0:
                raw = buf.raw
                break
            if status not in {0x80000005, 0xC0000023, 0xC0000004}:
                raise OSError(_STREAM_UNREADABLE)
            size *= 2
        if status != 0:
            raise OSError(_STREAM_UNREADABLE)
        return _stream_names_from_information(raw)
    finally:
        close(handle)


def _has_nondefault_stream(path: Path, *, directory: bool = False) -> bool:
    allowed = {_DEFAULT_STREAM.casefold()}
    if directory:
        allowed.add(_DIRECTORY_INDEX_STREAM)
    return any(name.casefold() not in allowed for name in _stream_names(path))


def _reject_alternate_streams(path: Path) -> None:
    try:
        extra = _has_nondefault_stream(path)
    except OSError:
        raise ValueError(_STREAM_UNREADABLE) from None
    if extra:
        raise ValueError(_ALT_STREAM)


def _reject_directory_streams(path: Path) -> None:
    try:
        extra = _has_nondefault_stream(path, directory=True)
    except OSError:
        raise ValueError(_DIR_STREAM_UNREADABLE) from None
    if extra:
        raise ValueError(_DIR_ALT_STREAM)


def _anonymized_directory(root: Path) -> list[str]:
    try:
        info = os.lstat(root)
    except OSError:
        raise ValueError("pack directory is missing") from None
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError(_NOT_REGULAR)
    _reject_directory_streams(root)
    names = []
    try:
        scan = os.scandir(root)
    except OSError:
        raise ValueError("pack directory is missing") from None
    with scan:
        for entry in scan:
            try:
                mode = entry.stat(follow_symlinks=False).st_mode
            except OSError:
                raise ValueError(_NOT_REGULAR) from None
            if not _entry_is_regular_pack_file(mode, entry.name):
                raise ValueError(_NOT_REGULAR)
            names.append(entry.name)
    if not names:
        raise ValueError("pack directory is empty")
    return names


def _read_regular(root: Path, name: str) -> bytes:
    path = root / name
    try:
        info = os.lstat(path)
    except OSError:
        raise ValueError(_NOT_REGULAR) from None
    if not _entry_is_regular_pack_file(info.st_mode, name):
        raise ValueError(_NOT_REGULAR)
    _reject_alternate_streams(path)
    if info.st_size >= MAX_FILE_BYTES:
        raise ValueError(f"{name} is not under {MAX_FILE_BYTES} bytes")
    flags = os.O_RDONLY
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags | nofollow)
    except OSError:
        raise ValueError(_NOT_REGULAR) from None
    try:
        with os.fdopen(fd, "rb") as handle:
            blob = handle.read()
    except OSError:
        raise ValueError(_NOT_REGULAR) from None
    if len(blob) >= MAX_FILE_BYTES:
        raise ValueError(f"{name} is not under {MAX_FILE_BYTES} bytes")
    return blob


def _anonymized_text(name: str, raw: bytes) -> str:
    if name.endswith(".gz"):
        blob = b"".join(_anonymized_gzip_payloads(name, raw))
    else:
        blob = raw
    if not _printable_ascii(blob):
        raise ValueError(f"{name} is not printable ASCII")
    return blob.decode("ascii")


def _anonymized_pack_gate(root: Path, *, symbol: str | None = None) -> dict:
    names = _anonymized_directory(root)
    if _MANIFEST_NAME not in names:
        raise ValueError("pack manifest is missing")
    raw_manifest = _read_regular(root, _MANIFEST_NAME)
    if not _printable_ascii(raw_manifest):
        raise ValueError(f"{_MANIFEST_NAME} is not printable ASCII")
    text = raw_manifest.decode("ascii")
    if "\\u" in text or "\\U" in text:
        raise ValueError("pack manifest contains a unicode escape")
    if "\\" in text:
        raise ValueError("pack manifest contains an escape")
    try:
        document = json.loads(text, object_pairs_hook=_json_pairs)
    except json.JSONDecodeError as exc:
        raise ValueError(f"pack manifest is not JSON: {exc}") from exc
    _validate_anonymized_manifest(document)
    expected = {_MANIFEST_NAME, _DAILY_NAME, *(chunk["file"] for chunk in document["chunks"])}
    if set(names) != expected:
        raise ValueError("pack files are not the manifest set")
    bar_minutes = document["bar_minutes"]
    seen: list[int] = []
    bar_counts: dict[int, float] = {}
    hashed: list[tuple[str, bytes]] = []
    next_day = 1
    for chunk in document["chunks"]:
        name = chunk["file"]
        blob = _read_regular(root, name)
        hashed.append((name, blob))
        payload = _anonymized_text(name, blob)
        days, _totals, sums = _csv_days(name, payload, symbol=symbol, bar_minutes=bar_minutes)
        if days[0] != chunk["first_day"] or days[-1] != chunk["last_day"] or len(days) != chunk["sessions"]:
            raise ValueError(f"{name} does not match its manifest day range")
        if days[0] != next_day:
            raise ValueError(f"{name} sessions are not sequential")
        next_day = days[-1] + 1
        seen.extend(days)
        bar_counts.update(sums)
        _reject_banned(name, payload)
        if symbol and _text_has_ticker(name, payload, symbol):
            raise ValueError(f"{name} contains the ticker")
    if seen != list(range(1, len(seen) + 1)) or document["n_sessions"] != len(seen):
        raise ValueError("pack sessions are not sequential")
    daily_blob = _read_regular(root, _DAILY_NAME)
    hashed.append((_DAILY_NAME, daily_blob))
    daily = _anonymized_text(_DAILY_NAME, daily_blob)
    daily_days, daily_counts, _session_counts = _csv_days(
        _DAILY_NAME, daily, symbol=symbol, bar_minutes=bar_minutes, single_row=True
    )
    if daily_days != seen:
        raise ValueError(f"{_DAILY_NAME} does not match its manifest day range")
    for day, total in daily_counts.items():
        if format(total, ".17g") != format(bar_counts[day], ".17g"):
            raise ValueError("count is not the session trade count")
    _reject_banned(_DAILY_NAME, daily)
    if symbol and _text_has_ticker(_DAILY_NAME, daily, symbol):
        raise ValueError(f"{_DAILY_NAME} contains the ticker")
    _reject_banned(_MANIFEST_NAME, text)
    if symbol and _text_has_ticker(_MANIFEST_NAME, text, symbol):
        raise ValueError(f"{_MANIFEST_NAME} contains the ticker")
    if symbol:
        for name in names:
            if _token_present(symbol, name):
                raise ValueError(f"{name} leaks identity in the file name")
    if _pack_id(hashed) != document["pack_id"]:
        raise ValueError("pack files do not match the manifest pack_id")
    return document


def assert_anonymized_pack_clean(pack_dir, *, symbol: str) -> None:
    if not symbol:
        raise ValueError("symbol is empty")
    _anonymized_pack_gate(Path(pack_dir), symbol=symbol)


def copy_anonymized_pack(source, destination) -> None:
    src = Path(source)
    dest = Path(destination)
    if dest.exists() and any(dest.iterdir()):
        raise ValueError("pack copy destination is not empty")
    names = _anonymized_directory(src)
    dest.mkdir(parents=True, exist_ok=True)
    try:
        for name in sorted(names):
            blob = _read_regular(src, name)
            target = dest / name
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            nofollow = getattr(os, "O_NOFOLLOW", 0)
            try:
                fd = os.open(target, flags | nofollow, 0o644)
            except OSError:
                raise ValueError(_NOT_REGULAR) from None
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(blob)
            except OSError:
                raise ValueError(_NOT_REGULAR) from None
    except Exception:
        shutil.rmtree(dest, ignore_errors=True)
        raise


def _designer_frame(frame: pd.DataFrame, *, anonymize: bool, price_factor: float, volume_factor: float):
    dates = list(dict.fromkeys(str(value) for value in frame["date"].tolist()))
    if anonymize:
        ordinal = {date: index + 1 for index, date in enumerate(dates)}
        session_ids = {date: f"S{ordinal[date]:05d}" for date in dates}
        date_map = {str(ordinal[date]): date for date in dates}
    else:
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
            "count": [
                _emitted_count((float(value) * volume_factor) if anonymize else value)
                for value in frame["count"].tolist()
            ],
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
        count = 0.0
        for value in source["count"].tolist():
            count += float(value)
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
                "count": count,
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
    if isinstance(chunk_sessions, bool) or not isinstance(chunk_sessions, int) or chunk_sessions < 1:
        raise ValueError("chunk_sessions must be a positive integer")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise ValueError("max_bytes must be a positive integer")
    if not isinstance(operator_note, str):
        raise ValueError("operator_note must be a string")
    if anonymize:
        require_anonymizable_symbol(symbol)
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
        daily_name = _DAILY_NAME
        (destination / daily_name).write_bytes(daily_blob)
        hashed = [(item["file"], item["bytes"]) for item in chunks]
        hashed.append((daily_name, daily_blob))
        pack_id = _pack_id(hashed)
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "anonymized": bool(anonymize),
            "bar_minutes": int(bar_minutes),
            "asset_name": TICKER_STAND_IN if anonymize else symbol,
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
            "price_basis": loaded.inspection.price_adjustment,
        }
        (destination / _MANIFEST_NAME).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        if anonymize:
            assert_anonymized_pack_clean(destination, symbol=symbol)
        factor_key = secrets.token_bytes(32)
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
            "price_basis": loaded.inspection.price_adjustment,
            "factor_key": factor_key.hex(),
        }
        sidecar_document["factor_mac"] = factor_mac(factor_key, sidecar_document)
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
    document = {}
    for key, value in pairs:
        if key in document:
            raise ValueError(f"duplicate JSON key {key!r}")
        document[key] = value
    return document


def load_sidecar(path, *, allow_scale_mismatch: bool = False) -> dict:
    file = Path(path)
    try:
        document = json.loads(file.read_text(encoding="utf-8"), object_pairs_hook=_json_pairs)
    except json.JSONDecodeError as exc:
        raise ValueError(f"sidecar {file} is not JSON: {exc}") from exc
    validate_sidecar(document, allow_scale_mismatch=allow_scale_mismatch)
    return document


def _validate_manifest(document: dict) -> None:
    if not isinstance(document, dict):
        raise ValueError("pack manifest is not an object")
    extra = sorted(set(document) - _MANIFEST_KEYS)
    missing = sorted(_MANIFEST_KEYS - set(document))
    if extra or missing:
        raise ValueError(f"pack manifest fields are wrong (missing {missing}, unknown {extra})")
    if document["schema_version"] != SCHEMA_VERSION:
        raise ValueError("pack schema_version is not supported")
    if document.get("price_basis") not in {"raw", "total_return"}:
        raise ValueError("pack price_basis must be raw or total_return")
    if not isinstance(document["anonymized"], bool):
        raise ValueError("pack anonymized flag is not true or false")
    if document["anonymized"] and document["asset_name"] != TICKER_STAND_IN:
        raise ValueError(f"an anonymized pack must call the asset {TICKER_STAND_IN}")
    if list(document["columns"]) != list(PACK_COLUMNS):
        raise ValueError("pack columns do not match the schema")
    if not isinstance(document["chunks"], list) or not document["chunks"]:
        raise ValueError("pack has no chunks")
    previous = None
    for chunk in document["chunks"]:
        if set(chunk) != _CHUNK_KEYS:
            raise ValueError(f"chunk record {chunk!r} does not match the schema")
        chunk_name = chunk["file"]
        if not isinstance(chunk_name, str) or chunk_name != Path(chunk_name).name:
            raise ValueError(f"chunk file name {chunk_name!r} is not allowed")
        if document["anonymized"] and not _canonical_bars_name(chunk_name):
            raise ValueError("chunk file name is not allowed")
        if chunk["sessions"] < 1 or chunk["last_day"] < chunk["first_day"]:
            raise ValueError(f"chunk {chunk['file']} has an empty day range")
        if previous is not None and chunk["first_day"] <= previous:
            raise ValueError("pack chunks overlap or are out of order")
        previous = chunk["last_day"]
    daily_name = document.get("daily_file")
    if not isinstance(daily_name, str) or daily_name != Path(daily_name).name:
        raise ValueError(f"daily file name {daily_name!r} is not allowed")
    if document["anonymized"] and daily_name != _DAILY_NAME:
        raise ValueError(f"daily file name {daily_name!r} is not allowed")


def _reject_anonymized_gzip(name: str, blob: bytes) -> None:
    _anonymized_gzip_payloads(name, blob)


def _require_anonymized_flag(manifest: dict, session_ids: list[str]) -> None:
    anonymized = manifest["anonymized"] is True
    if not anonymized and manifest.get("asset_name") == TICKER_STAND_IN:
        raise ValueError(
            f"pack asset name {TICKER_STAND_IN} is an anonymized marker, "
            "so the manifest anonymized flag must be true"
        )
    for value in session_ids:
        if _session_ordinal(value) is not None:
            if not anonymized:
                raise ValueError(
                    f"pack session id {value} is an anonymized marker, "
                    "so the manifest anonymized flag must be true"
                )
            continue
        if _DATE_RE.fullmatch(value):
            if anonymized:
                raise ValueError(
                    f"pack session id {value} is a calendar date, "
                    "so the manifest anonymized flag must be false"
                )
            continue
        if anonymized:
            raise ValueError(f"pack session id {value} is not an anonymized session id")
        raise ValueError(
            f"pack session id {value} is not a calendar date, so a named pack cannot use it"
        )


def load_pack(pack_dir, *, symbol: str | None = None) -> LoadedPack:
    root = Path(pack_dir)
    manifest_path = root / _MANIFEST_NAME
    if manifest_path.is_symlink():
        raise ValueError(_NOT_REGULAR)
    try:
        info = os.lstat(manifest_path)
    except OSError:
        raise FileNotFoundError(f"pack manifest is missing: {manifest_path}") from None
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(_NOT_REGULAR)
    try:
        manifest_text = manifest_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise ValueError(f"{_MANIFEST_NAME} is not printable ASCII") from None
    try:
        manifest = json.loads(manifest_text, object_pairs_hook=_json_pairs)
    except json.JSONDecodeError as exc:
        raise ValueError(f"pack manifest is not JSON: {exc}") from exc
    if isinstance(manifest, dict) and manifest.get("anonymized") is True:
        manifest = _anonymized_pack_gate(root, symbol=symbol)
    else:
        _validate_manifest(manifest)
    frames = []
    hashed = []
    for chunk in manifest["chunks"]:
        path = root / chunk["file"]
        if manifest["anonymized"]:
            blob = _read_regular(root, chunk["file"])
        else:
            blob = path.read_bytes()
        if len(blob) >= MAX_FILE_BYTES:
            raise ValueError(f"{chunk['file']} is not under {MAX_FILE_BYTES} bytes")
        if manifest["anonymized"]:
            _reject_anonymized_gzip(chunk["file"], blob)
        hashed.append((chunk["file"], blob))
        frame = _read_csv_gz(path)
        days = sorted(int(day) for day in frame["day"].unique())
        if not days or days[0] != chunk["first_day"] or days[-1] != chunk["last_day"] or len(days) != chunk["sessions"]:
            raise ValueError(f"{chunk['file']} does not match its manifest day range")
        frames.append(frame)
    daily_path = root / manifest["daily_file"]
    if manifest["anonymized"]:
        daily_blob = _read_regular(root, manifest["daily_file"])
    else:
        daily_blob = daily_path.read_bytes()
    if manifest["anonymized"]:
        _reject_anonymized_gzip(manifest["daily_file"], daily_blob)
    hashed.append((manifest["daily_file"], daily_blob))
    pack_id = _pack_id(hashed)
    if pack_id != manifest["pack_id"]:
        raise ValueError("pack files do not match the manifest pack_id")
    daily = _read_csv_gz(daily_path)
    session_ids = [str(value) for frame in frames for value in frame["session_id"].tolist()]
    session_ids.extend(str(value) for value in daily["session_id"].tolist())
    _require_anonymized_flag(manifest, session_ids)
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
