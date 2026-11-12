"""Chunked bar packs and the operator sidecar.

An anonymized pack rescales prices and volume and replaces calendar dates with
session ordinals. The ticker, the date map, and the scale factors are written
only to the sidecar. Designer-facing file names carry neither a ticker nor a
year. A chunk is about 250 trading sessions and each file stays under 25 MB.

Prices multiplied by k are paired with share_scale = k. The sidecar file is
where that number is stored. It also holds a random key and an HMAC over a
versioned binding of pack_id, the anonymized flag, the date map, k,
share_scale, and the borrow rate. A null borrow rate stays JSON null. The
pack files do not contain the factor, the key, or any digest of the factor.
The runner checks the MAC before it uses k. A sidecar without that MAC is
refused, named or anonymized. Verification accepts only this version.
Session ids use ASCII digits. An anonymized pack is printable ASCII
under the builder's file names, and that text is scanned for calendar dates.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import hmac
import io
import json
import math
import re
import secrets
import shutil
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
# ASCII digits only. Session ids and date_map values. The leak scan uses
# its own pattern and does not fold Unicode.
_DATE_RE = re.compile(r"\b(?:19|20)[0-9]{2}-[0-9]{2}-[0-9]{2}\b")
# Named packs use day 0. Every later ordinal is digits with no leading zero.
_DATE_MAP_KEY_RE = re.compile(r"^(?:0|[1-9][0-9]*)$")
_ANON_SESSION_RE = re.compile(r"^S[0-9]{5}$")
_BARS_NAME_RE = re.compile(r"^bars_part_[0-9]{3}\.csv\.gz$")
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
# Digit lookarounds, not \\b, so a letter or underscore beside the date does
# not hide it. Numeric price and volume cells are masked before this runs.
# Compact form is exactly YYYYMMDD. The separated forms also accept 2024/1/2.
_LEAK_DATE_RE = re.compile(
    rf"(?<![0-9])(?:{_YEAR}{_SEP}{_MONTH}{_SEP}{_DAY}|{_DAY}{_SEP}{_MONTH}{_SEP}{_YEAR}|{_YEAR}{_MONTH2}{_DAY2})(?![0-9])"
)
_NUMBER_CELL_RE = re.compile(r"^[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$")
_HEX64_RE = re.compile(r"[0-9a-f]{64}")
_NUMERIC_PACK_COLUMNS = frozenset(
    {
        "day",
        "weekday",
        "minute",
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
    }
)
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


def canonical_price_factor(price_factor: float) -> str:
    """Shortest round-trip decimal of a scale factor.

    ``1.7`` stays ``1.7``. The decimal is one field of the HMAC message. It
    is not written into the pack.
    """
    if isinstance(price_factor, bool) or not isinstance(price_factor, (int, float)):
        raise ValueError("price factor must be a positive finite number")
    value = float(price_factor)
    if not math.isfinite(value) or value <= 0.0:
        raise ValueError("price factor must be a positive finite number")
    return json.dumps(value, allow_nan=False)


def _canonical_borrow_rate(value):
    """``None`` stays JSON null. Any other rate is a finite non-negative float."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("borrow rate must be a non-negative number or null")
    number = float(value)
    if not math.isfinite(number) or number < 0.0:
        raise ValueError("borrow rate must be a non-negative number or null")
    return number


def _factor_mac_message(document: dict) -> bytes:
    """Versioned canonical JSON bound to this pack, not only to k.

    Version 2 also binds ``borrow_rate_source`` and ``borrow_rate_annual``.
    A null borrow rate stays JSON null. The dividend file is not a pack or
    sidecar field. It is an operator input supplied when the pack is run, so
    it is not part of this message. ``date_map`` is what places those amounts
    on session ids, and it is bound.
    """
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
    """HMAC-SHA256 of the canonical pack binding. ``key`` is 32 raw bytes."""
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
    """Refuse a sidecar whose pack binding was not authenticated at build.

    Every pack sidecar needs the version-2 MAC. A missing tag fails closed
    for a named pack and for an anonymized pack. A version-1 tag does not
    match this message, so that pack has to be rebuilt. ``load_sidecar`` is
    the gate for ``run_pack``, ``walk_pack``, and ``_prepare_pack``. A raw
    local run never loads a sidecar.
    """
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
    """The price factor whose MAC matches the operator key.

    Call this in the operator process before restating designer dividends
    and before comparing ``share_scale`` to this factor. The MAC binds
    ``pack_id``, the anonymized flag, the date map, this factor,
    ``share_scale``, and the borrow rate (null stays null). Only version 2
    verifies. A missing tag or a version-1 tag is refused.
    """
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
    # The MAC binds pack_id, the flag, the date map, k, share_scale, and the
    # borrow rate. Check it before the share_scale comparison below, and again
    # from require_build_price_factor before dividend restatement.
    require_build_price_factor(document)
    if document["anonymized"]:
        # The builder writes share_scale equal to the price factor authenticated
        # by the operator MAC. A later sidecar may not bill a different scale
        # and still say the invoice used real share counts.
        # allow_scale_mismatch is the test override that keeps one pack's
        # designer tree comparable across billing scales. The caller records
        # both factors in that case. The MAC is still checked.
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
    """The report token rule. A raw substring would treat ``SS`` as present in ``RANDOM``."""
    return bool(ticker_token_spans(text, symbol))


def _json_values_have_ticker(value, symbol: str) -> bool:
    """String values only. Keys and pack column names are schema labels.

    The manifest stores ``session_id`` in ``columns``. That label is not the
    ticker ``ID``, which is the same exemption the report audit gives a CSV
    header. Any other string still uses the token rule.
    """
    if isinstance(value, dict):
        return any(_json_values_have_ticker(item, symbol) for item in value.values())
    if isinstance(value, list):
        return any(_json_values_have_ticker(item, symbol) for item in value)
    if isinstance(value, str):
        # ``raw`` and ``total_return`` are series-kind labels. A symbol that
        # casefolds to either word is rejected before this pack is written.
        if value in PACK_COLUMNS or value in {"raw", "total_return"}:
            return False
        return _token_present(symbol, value)
    return False


def _text_has_ticker(name: str, text: str, symbol: str) -> bool:
    """Apply the token rule the way the report audit does.

    CSV headers and JSON keys are not tickers, so ``session_id`` is not the
    symbol ``ID``. Every other string is.
    """
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
    """mtime, file name, FCOMMENT, FEXTRA, FHCRC, and the deflate offset.

    RFC 1952 order after the 10-byte header is FEXTRA, FNAME, FCOMMENT, FHCRC.
    ``blob`` is one member, or a member followed by further members. Only the
    first header is read here. The offset is where the raw deflate stream
    begins, so a later member is not swallowed with this one.
    """
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
    """Every gzip member in ``blob``, not only the first.

    A concatenated second member can carry a file name or a comment that the
    first header does not. Each header is checked. The payloads are returned
    in order so the caller can scan the text. ``GzipFile.read`` follows every
    member in one call, so each member is inflated on its own with zlib.
    """
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
    """One clean gzip member, inflated.

    A second member is rejected even when its header is clean. ``GzipFile``
    concatenates members, and duplicate rows still pass the unique-day check,
    so the loaded book would be longer than the pack that was built. The
    builder writes a single member.
    """
    payloads = []
    for (mtime, fname, has_comment, has_extra, has_hcrc), payload in _gzip_members(blob):
        if mtime != 0:
            raise ValueError(f"{name} gzip mtime is {mtime}, not 0")
        if fname:
            shown = fname.decode("utf-8", "replace")
            raise ValueError(f"{name} gzip file name is {shown!r}")
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


def _canonical_anonymized_name(name: str) -> bool:
    """Builder file names only. Anything else is refused, not scanned."""
    return name in {_MANIFEST_NAME, _DAILY_NAME} or _BARS_NAME_RE.fullmatch(name) is not None


def _printable_ascii(blob: bytes) -> bool:
    return blob.translate(None, bytes(_PRINTABLE_ASCII)) == b""


def _mask_numeric_csv(text: str) -> str:
    """Replace plain numbers in numeric columns so 2024.50 is not a date."""
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return text
    numeric = {index for index, name in enumerate(rows[0]) if name in _NUMERIC_PACK_COLUMNS}
    masked = [",".join(rows[0])]
    for row in rows[1:]:
        cells = []
        for index, cell in enumerate(row):
            if index in numeric and _NUMBER_CELL_RE.fullmatch(cell.strip()):
                cells.append("0")
            else:
                cells.append(cell)
        masked.append(",".join(cells))
    return "\n".join(masked)


def _json_scan_text(text: str) -> str:
    """Manifest text with a 64-hex pack id removed so the digest is not a date.

    Numeric fields stay in the text. A ``first_day`` of 20240102 is a date.
    """
    return _HEX64_RE.sub("0", text)


def _leaks_calendar(name: str, text: str) -> bool:
    """Calendar dates on ASCII text. Numeric CSV cells are masked first."""
    if name.endswith(".csv.gz") or name.endswith(".csv"):
        scanned = _mask_numeric_csv(text)
    elif name.endswith(".json"):
        scanned = _json_scan_text(text)
    else:
        scanned = text
    return _LEAK_DATE_RE.search(scanned) is not None


def _anonymized_text(name: str, raw: bytes) -> str:
    """Printable ASCII text. Gzip is checked after inflation, not as a container."""
    if name.endswith(".gz"):
        blob = b"".join(_anonymized_gzip_payloads(name, raw))
    else:
        blob = raw
    if not _printable_ascii(blob):
        raise ValueError(f"{name} is not printable ASCII")
    return blob.decode("ascii")


def _reject_anonymized_pack(root: Path) -> list[tuple[str, str]]:
    """Names, ASCII bytes, and calendar dates. Shared by the audit and load_pack."""
    files = [path for path in root.rglob("*") if path.is_file()]
    if not files:
        raise ValueError("pack directory is empty")
    texts = []
    for path in files:
        name = path.name
        if path.parent != root or not _canonical_anonymized_name(name):
            raise ValueError(f"{name} is not a pack file name")
        if path.stat().st_size >= MAX_FILE_BYTES:
            raise ValueError(f"{name} is {path.stat().st_size} bytes, which is not under {MAX_FILE_BYTES}")
        text = _anonymized_text(name, path.read_bytes())
        if _leaks_calendar(name, text):
            raise ValueError(f"{name} contains a calendar date")
        texts.append((name, text))
    return texts


def assert_anonymized_pack_clean(pack_dir, *, symbol: str) -> None:
    """Refuse a designer tree that still carries identity or a scale factor."""
    root = Path(pack_dir)
    if not symbol:
        raise ValueError("symbol is empty")
    for name, text in _reject_anonymized_pack(root):
        if _token_present(symbol, name):
            raise ValueError(f"{name} leaks identity in the file name")
        if _text_has_ticker(name, text, symbol):
            raise ValueError(f"{name} contains the ticker")
        for key in _BANNED_TEXT:
            if key in text:
                raise ValueError(f"{name} contains {key}")


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
    """Object hook that refuses a repeated key. ``json.loads`` would keep the last one."""
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
        raise ValueError(f"pack schema_version {document['schema_version']!r} is not supported")
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
        if document["anonymized"] and _BARS_NAME_RE.fullmatch(chunk_name) is None:
            raise ValueError(f"chunk file name {chunk_name!r} is not allowed")
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
    """The same gzip rules as the designer-pack audit.

    One member, mtime 0, no file name, no comment, no FEXTRA, and no FHCRC.
    A second member is rejected here as well, including one whose header is clean.
    ``load_pack`` repeats the check so a pack that bypassed the builder still
    fails closed.
    """
    _anonymized_gzip_payloads(name, blob)


def _require_anonymized_flag(manifest: dict, session_ids: list[str]) -> None:
    """Tie structural anonymization markers to the manifest flag.

    ``S#####`` session ids and the stand-in asset name require
    ``anonymized`` true. A named pack's session ids are ASCII calendar
    dates. Anything else is refused. The operator MAC is checked on the
    sidecar, not here.
    """
    anonymized = manifest["anonymized"] is True
    if not anonymized and manifest.get("asset_name") == TICKER_STAND_IN:
        raise ValueError(
            f"pack asset name {TICKER_STAND_IN} is an anonymized marker, "
            "so the manifest anonymized flag must be true"
        )
    for value in session_ids:
        if _ANON_SESSION_RE.fullmatch(value):
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


def load_pack(pack_dir) -> LoadedPack:
    """Read a pack. This does not open a sidecar."""
    root = Path(pack_dir)
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"pack manifest is missing: {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"), object_pairs_hook=_json_pairs)
    except json.JSONDecodeError as exc:
        raise ValueError(f"pack manifest is not JSON: {exc}") from exc
    _validate_manifest(manifest)
    if manifest["anonymized"]:
        _reject_anonymized_pack(root)
    frames = []
    hashed = []
    for chunk in manifest["chunks"]:
        path = root / chunk["file"]
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
    # Before any runner reads share_scale. An S##### id cannot be relabeled
    # as a named pack to skip the operator MAC.
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
