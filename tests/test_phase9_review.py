"""The five Lows from the full-range review of main 2b73448.

Refusal text does not echo a planted gzip header or schema version.
The known symbol is passed into ``load_pack``, and the referee audits the
pack before it returns the frame. The anonymized pack directory is scanned
for an NTFS alternate stream. The pack doc names the kept PNG chunks. A raw
run of a total-return series names ``--price-basis total_return``.
"""

from __future__ import annotations

import gzip
import json
import os
import struct
from pathlib import Path

import pytest

from backtester.accounting import price_basis_refusal, resolve_accounting
from backtester.pack import (
    _stream_names,
    _stream_names_from_information,
    assert_anonymized_pack_clean,
    build_pack,
    copy_anonymized_pack,
    load_pack,
    load_sidecar,
)
from backtester.parallel import PackMarket, _prepare_pack
from backtester.referee import _load_side
from backtester.registry import entry_from_inspection, save_registry, upsert_series
from backtester.runner import load_run_series, run_pack, walk_pack
from test_pack import _Quiet, _recompress, _series, _with_comment

ROOT = Path(__file__).resolve().parents[1]
_PLANTED_MTIME = 1700000000
_PLANTED_NAME = "LEAK-QQQ-2024.csv"
_PLANTED_SCHEMA = "LEAK-SCHEMA-9f3a"
_PLANTED_STREAM = "LEAK-STREAM-NAME"


def _pack(tmp_path, *, anonymize=True, sessions=1):
    src, registry = _series(tmp_path, sessions)
    pack = tmp_path / "pack"
    sidecar = tmp_path / "side.json"
    factors = {"price_factor": 0.5, "volume_factor": 0.4} if anonymize else {}
    build_pack(
        src,
        "NIK",
        pack,
        sidecar,
        anonymize=anonymize,
        registry_path=registry,
        **factors,
    )
    return pack, sidecar


def _assert_absent(text: str, *planted: str) -> None:
    for token in planted:
        assert token not in text


def test_gzip_refusal_does_not_echo_mtime_filename_or_comment(tmp_path):
    pack, _sidecar = _pack(tmp_path)
    target = pack / "bars_part_001.csv.gz"
    original = target.read_bytes()
    payload = gzip.decompress(original)
    cases = [
        (_recompress(payload, mtime=_PLANTED_MTIME), "gzip mtime is not 0", str(_PLANTED_MTIME)),
        (_recompress(payload, filename=_PLANTED_NAME), "gzip member has a file name", _PLANTED_NAME),
        (_with_comment(original, b"LEAK-COMMENT-QQQ"), "FCOMMENT", "LEAK-COMMENT-QQQ"),
    ]
    for blob, sentence, planted in cases:
        target.write_bytes(blob)
        with pytest.raises(ValueError) as caught:
            assert_anonymized_pack_clean(pack, symbol="NIK")
        message = str(caught.value)
        assert sentence in message
        _assert_absent(message, planted, "QQQ")
        target.write_bytes(original)
    assert_anonymized_pack_clean(pack, symbol="NIK")


def _plant_schema(path: Path, token: str) -> None:
    document = json.loads(path.read_text(encoding="utf-8"))
    document["schema_version"] = token
    path.write_text(json.dumps(document), encoding="utf-8")


def test_schema_refusal_does_not_echo_the_planted_version(tmp_path):
    pack, sidecar = _pack(tmp_path)
    _plant_schema(pack / "manifest.json", _PLANTED_SCHEMA)
    with pytest.raises(ValueError, match="pack schema_version is not supported") as anonymized:
        load_pack(pack, symbol="NIK")
    _assert_absent(str(anonymized.value), _PLANTED_SCHEMA)

    named, named_side = _pack(tmp_path / "named", anonymize=False)
    _plant_schema(named / "manifest.json", _PLANTED_SCHEMA)
    with pytest.raises(ValueError, match="pack schema_version is not supported") as plain:
        load_pack(named)
    _assert_absent(str(plain.value), _PLANTED_SCHEMA)

    _plant_schema(sidecar, _PLANTED_SCHEMA)
    with pytest.raises(ValueError, match="is not supported") as side:
        load_sidecar(sidecar)
    _assert_absent(str(side.value), _PLANTED_SCHEMA)


def test_load_sites_pass_the_sidecar_symbol(tmp_path, monkeypatch):
    pack, sidecar = _pack(tmp_path)
    seen = []
    real = load_pack

    def wrapped(pack_dir, *, symbol=None):
        seen.append(symbol)
        return real(pack_dir, symbol=symbol)

    monkeypatch.setattr("backtester.pack.load_pack", wrapped)
    from backtester import runner as runner_mod

    _prepare_pack(PackMarket(str(pack), str(sidecar)), None, None, runner_mod, price_basis="raw")
    _load_side(
        pack,
        ticker=None,
        sidecar=sidecar,
        share_factor=None,
        bar_minutes=1,
        dividends_path=None,
        profile="arena",
    )
    run_pack(_Quiet, pack, sidecar, "arena", tmp_path / "out", with_benchmarks=False)

    def stop(*_args, **_kwargs):
        raise RuntimeError("stop-after-load")

    monkeypatch.setattr(runner_mod, "walk_forward", stop)
    with pytest.raises(RuntimeError, match="stop-after-load"):
        walk_pack(_Quiet, pack, sidecar, "arena", tmp_path / "walk", with_benchmarks=False)
    assert seen == ["NIK", "NIK", "NIK", "NIK"]


def test_referee_audits_the_pack_before_returning_the_frame(tmp_path, monkeypatch):
    pack, sidecar = _pack(tmp_path)
    order = []
    real_load = load_pack
    real_assert = assert_anonymized_pack_clean

    def wrapped_load(pack_dir, *, symbol=None):
        order.append(("load", symbol))
        return real_load(pack_dir, symbol=symbol)

    def wrapped_assert(pack_dir, *, symbol):
        order.append(("assert", symbol))
        return real_assert(pack_dir, symbol=symbol)

    monkeypatch.setattr("backtester.pack.load_pack", wrapped_load)
    monkeypatch.setattr("backtester.pack.assert_anonymized_pack_clean", wrapped_assert)
    frame, _scale, symbol, anonymized, *_rest = _load_side(
        pack,
        ticker=None,
        sidecar=sidecar,
        share_factor=None,
        bar_minutes=1,
        dividends_path=None,
        profile="arena",
    )
    assert symbol == "NIK"
    assert anonymized is True
    assert len(frame) > 0
    assert order == [("load", "NIK"), ("assert", "NIK")]


def _names_for(directory: Path, names: list[str]):
    target = directory.resolve()

    def fake(path):
        if Path(path).resolve() == target:
            return list(names)
        return []

    return fake


def _stream_info(*names: str) -> bytes:
    """A ``FileStreamInformation`` buffer. Offsets are 8-byte aligned."""
    parts = []
    for name in names:
        encoded = name.encode("utf-16-le")
        raw = 24 + len(encoded)
        pad = (8 - raw % 8) % 8
        parts.append((encoded, raw + pad))
    blob = b""
    for index, (encoded, size) in enumerate(parts):
        step = 0 if index == len(parts) - 1 else size
        blob += struct.pack("<IIQQ", step, len(encoded), 0, 0) + encoded
        blob += b"\x00" * (size - 24 - len(encoded))
    return blob


def test_stream_information_names_match_find_first_stream():
    blob = _stream_info("::$DATA", ":$I30:$INDEX_ALLOCATION", ":dir_date_map:$DATA")
    assert _stream_names_from_information(blob + b"\x00" * 64) == [
        "::$DATA",
        ":$I30:$INDEX_ALLOCATION",
        ":dir_date_map:$DATA",
    ]
    assert _stream_names_from_information(b"\x00" * 24) == []


def test_directory_stream_is_refused_without_its_name(tmp_path, monkeypatch):
    pack, _sidecar = _pack(tmp_path)
    monkeypatch.setattr(
        "backtester.pack._stream_names",
        _names_for(pack, [":$I30:$INDEX_ALLOCATION", f":{_PLANTED_STREAM}:$DATA"]),
    )
    with pytest.raises(ValueError, match="pack directory has an alternate data stream") as caught:
        assert_anonymized_pack_clean(pack, symbol="NIK")
    message = str(caught.value)
    assert message == "pack directory has an alternate data stream"
    _assert_absent(message, _PLANTED_STREAM, "2024")
    with pytest.raises(ValueError, match="pack directory has an alternate data stream"):
        load_pack(pack, symbol="NIK")
    with pytest.raises(ValueError, match="pack directory has an alternate data stream"):
        copy_anonymized_pack(pack, tmp_path / "copy")

    monkeypatch.setattr(
        "backtester.pack._stream_names",
        _names_for(pack, [":$I30:$INDEX_ALLOCATION", "::$DATA"]),
    )
    assert_anonymized_pack_clean(pack, symbol="NIK")
    load_pack(pack, symbol="NIK")


@pytest.mark.skipif(os.name != "nt", reason="NTFS alternate data streams")
def test_ntfs_directory_alternate_stream_is_refused(tmp_path):
    pack, _sidecar = _pack(tmp_path)
    stream = "dir_date_map"
    payload = b"2024-01-02\n"
    with open(f"{os.fspath(pack)}:{stream}", "wb") as handle:
        handle.write(payload)
    names = _stream_names(pack)
    assert any(stream in name for name in names)
    with pytest.raises(ValueError, match="pack directory has an alternate data stream") as caught:
        assert_anonymized_pack_clean(pack, symbol="NIK")
    message = str(caught.value)
    _assert_absent(message, stream, "2024", payload.decode("ascii").strip())
    with pytest.raises(ValueError, match="pack directory has an alternate data stream"):
        load_pack(pack, symbol="NIK")


def test_docs_name_png_chunks_and_the_total_return_opt_in():
    pack = (ROOT / "docs" / "PACK.md").read_text(encoding="utf-8")
    for chunk in ("IHDR", "PLTE", "IDAT", "IEND", "tRNS", "gAMA", "sRGB", "pHYs"):
        assert chunk in pack
    assert "Text chunks are dropped." in pack
    registry = (ROOT / "docs" / "REGISTRY.md").read_text(encoding="utf-8")
    table = registry.split("## Commands", 1)[0]
    assert "runnable: false" in table
    assert "run --price-basis total_return" in table
    assert "when the split list is empty" in table
    assert "not runnable in this phase" not in table
    banner = (ROOT / "src" / "backtester" / "gui" / "pages" / "data_page.py").read_text(encoding="utf-8")
    assert "which this phase does not execute" not in banner
    assert "--price-basis total_return" in banner


def test_price_basis_sentences():
    empty = price_basis_refusal("NIK", splits=False, declared="total_return")
    assert "The raw price basis was refused." in empty
    assert "Pass --price-basis total_return to run this series." in empty
    assert "This phase executes raw prices only" not in empty
    splits = price_basis_refusal("NIK", splits=True, declared="raw")
    assert "A declared split list is not applied." in splits
    assert "--price-basis total_return does not run a series with splits." in splits
    assert "The raw price basis was refused." not in splits
    accounting = price_basis_refusal(None, splits=False)
    assert accounting.startswith("series is total_return and is not runnable.")
    assert "--price-basis total_return" in accounting
    with pytest.raises(ValueError, match="raw price basis was refused") as caught:
        resolve_accounting("desk", series_kind="total_return")
    assert "not runnable" in str(caught.value)
    assert "--price-basis total_return" in str(caught.value)


def test_load_run_series_names_the_opt_in_and_refuses_splits(tmp_path):
    src, registry = _series(tmp_path, 1)
    from backtester.data import load_named

    loaded = load_named(src, "NIK", price_kind="total_return")
    entry = entry_from_inspection(loaded.inspection, source_root=str(src), profile="arena")
    save_registry(registry, upsert_series({"schema_version": 1, "series": []}, entry))
    with pytest.raises(ValueError, match="raw price basis was refused") as raw:
        load_run_series(src, "NIK", registry_path=registry)
    text = str(raw.value)
    assert "--price-basis total_return" in text
    assert "not runnable" in text
    assert "This phase executes raw prices only" not in text

    raw_loaded = load_named(src, "NIK")
    split_entry = entry_from_inspection(
        raw_loaded.inspection,
        source_root=str(src),
        profile="arena",
        splits=({"date": "2020-08-31", "ratio": 4.0},),
    )
    save_registry(registry, upsert_series({"schema_version": 1, "series": []}, split_entry))
    with pytest.raises(ValueError, match="does not run a series with splits") as split:
        load_run_series(src, "NIK", registry_path=registry)
    assert "raw price basis was refused" not in str(split.value)
    assert "--price-basis total_return" in str(split.value)


def test_referee_named_refusal_names_the_opt_in(tmp_path, monkeypatch):
    src, _registry = _series(tmp_path, 1)
    from backtester.data import load_named as real_load

    def wrapped(*args, **kwargs):
        kwargs["price_kind"] = "total_return"
        return real_load(*args, **kwargs)

    monkeypatch.setattr("backtester.referee.load_named", wrapped)
    with pytest.raises(ValueError, match="raw price basis was refused") as caught:
        _load_side(
            src,
            ticker="NIK",
            sidecar=None,
            share_factor=None,
            bar_minutes=1,
            dividends_path=None,
            profile="arena",
        )
    assert "--price-basis total_return" in str(caught.value)
    assert "This phase executes raw prices only" not in str(caught.value)


def test_pack_total_return_refusal_names_the_opt_in(tmp_path):
    pack, sidecar = _pack(tmp_path)
    manifest_path = pack / "manifest.json"
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    document["price_basis"] = "total_return"
    manifest_path.write_text(json.dumps(document), encoding="utf-8")
    from backtester import runner as runner_mod

    with pytest.raises(ValueError, match="raw price basis was refused") as caught:
        _prepare_pack(PackMarket(str(pack), str(sidecar)), None, None, runner_mod, price_basis="raw")
    assert "--price-basis total_return" in str(caught.value)
    assert "This phase executes raw prices only" not in str(caught.value)
