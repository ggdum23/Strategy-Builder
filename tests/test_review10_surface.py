"""Tenth review. Designer PNGs keep rendering chunks only. Hex pack ids are not scales."""

from __future__ import annotations

import io
import json
import random
import struct
import zlib

import pytest

from backtester.designer_allow import canonical_png, designer_source_id, project_designer
from backtester.gui.audience import _literal_is_scale, _scale_in_token, export_allowed, export_text, scan_publication
from backtester.report import assert_designer_clean, write_book
from backtester.runner import write_books

_FULLWIDTH_SCALE = "\uff17\uff0e\uff17\uff17\uff17"
_FULLWIDTH_TICKER = "\uff31\uff31\uff31"
_PRICE = "close 450.12"
_SCALES = (0.25, 0.5, 0.7, 2)


def _chunk(kind: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(kind + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)


def _insert_before_iend(png: bytes, chunk: bytes) -> bytes:
    start = png.rfind(b"IEND") - 4
    return png[:start] + chunk + png[start:]


def _itxt(text: str) -> bytes:
    body = b"Comment\x00" + bytes((0, 0)) + b"en\x00" + "note".encode("utf-8") + b"\x00" + text.encode("utf-8")
    return _chunk(b"iTXt", body)


def _text(caption: str) -> bytes:
    return _chunk(b"tEXt", b"Comment\x00" + caption.encode("latin-1"))


def _pixels(data: bytes) -> tuple[tuple[int, int], bytes]:
    from PIL import Image

    with Image.open(io.BytesIO(data)) as image:
        image.load()
        return image.size, image.convert("RGBA").tobytes()


def _chunk_types(data: bytes) -> list[bytes]:
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    pos = 8
    kinds = []
    while pos + 12 <= len(data):
        length = int.from_bytes(data[pos : pos + 4], "big")
        kind = data[pos + 4 : pos + 8]
        kinds.append(kind)
        pos += 12 + length
        if kind == b"IEND":
            assert data[pos:] == b""
            break
    return kinds


def _book():
    result = {
        "daily": [{"session": "S00001", "ordinal": 0, "equity": 1_000_000.0, "exposure": 0.0}],
        "fills": [],
        "episodes": [],
    }
    metrics = {"total_return": 0.0, "profile": "desk", "share_factor": 2}
    return result, metrics


def _plant(png: bytes) -> bytes:
    caption = f"scale {_FULLWIDTH_SCALE} held {_FULLWIDTH_TICKER}"
    planted = _insert_before_iend(png, _itxt(caption))
    planted = _insert_before_iend(planted, _text(_PRICE))
    return planted + b"\n" + _PRICE.encode("ascii") + _FULLWIDTH_SCALE.encode("utf-8") + _FULLWIDTH_TICKER.encode("utf-8")


def test_canonical_png_drops_the_three_plants_and_keeps_pixels(tmp_path):
    result, metrics = _book()
    operator = tmp_path / "operator"
    write_book(operator, "desk", result, metrics, None)
    original = (operator / "equity.png").read_bytes()
    assert b"tEXt" in original or b"iTXt" in original or b"zTXt" in original
    assert export_text(operator / "equity.png")
    planted = _plant(original)
    assert _FULLWIDTH_SCALE.encode("utf-8") in planted
    assert _FULLWIDTH_TICKER.encode("utf-8") in planted
    assert _PRICE.encode("ascii") in planted
    clean = canonical_png(planted)
    assert _pixels(clean) == _pixels(original)
    assert _FULLWIDTH_SCALE.encode("utf-8") not in clean
    assert _FULLWIDTH_TICKER.encode("utf-8") not in clean
    assert _PRICE.encode("ascii") not in clean
    allowed = {b"IHDR", b"PLTE", b"IDAT", b"IEND", b"tRNS", b"gAMA", b"sRGB", b"pHYs"}
    assert set(_chunk_types(clean)) <= allowed
    from backtester.gui.audience import png_caption

    assert _FULLWIDTH_SCALE not in png_caption(clean)
    assert "450.12" not in png_caption(clean)
    assert _FULLWIDTH_SCALE in png_caption(planted)
    assert "450.12" in png_caption(planted)
    assert scan_publication(png_caption(planted), scale=7.777) is not None
    assert not export_allowed(png_caption(planted), symbols=("QQQ",), scale=7.777)
    with pytest.raises(ValueError):
        canonical_png(b"not a png")
    bare = b"\x89PNG\r\n\x1a\n" + _text("close 450.12") + _chunk(b"IEND", b"")
    with pytest.raises(ValueError):
        canonical_png(bare)


def test_published_png_matches_the_operator_pixels_and_hides_the_hex_id(tmp_path):
    result, metrics = _book()
    raw = "pack_185e2abcdef0"
    out = tmp_path / "run"
    write_books(
        out,
        "desk",
        "QQQ",
        1,
        None,
        {"strategy": (result, metrics)},
        extra={"source_id": raw, "anonymized": True},
        anonymized=True,
    )
    designer = out / "designer"
    assert designer.is_dir()
    operator_png = (out / "equity.png").read_bytes()
    designer_png = (designer / "equity.png").read_bytes()
    assert _pixels(operator_png) == _pixels(designer_png)
    assert b"strategy-builder" in operator_png
    assert b"strategy-builder" not in designer_png
    assert set(_chunk_types(designer_png)) <= {b"IHDR", b"PLTE", b"IDAT", b"IEND", b"tRNS", b"gAMA", b"sRGB", b"pHYs"}
    assert raw in (out / "metrics.json").read_text(encoding="utf-8")
    for path in designer.rglob("*"):
        assert raw not in path.name
        if path.is_file():
            assert raw.encode("ascii") not in path.read_bytes()
    published = json.loads((designer / "metrics.json").read_text(encoding="utf-8"))
    opaque = designer_source_id(raw)
    assert published["run"]["source_id"] == opaque
    assert not any(char.isdigit() for char in opaque)
    assert_designer_clean(designer, symbol="QQQ", scale=2)

    planted = designer / "equity.png"
    original = planted.read_bytes()
    planted.write_bytes(_plant(original))
    with pytest.raises(RuntimeError):
        assert_designer_clean(designer, symbol="QQQ", scale=7.777)


def test_source_id_mapping_is_letters_and_leaves_other_ids_alone():
    raw = "pack_185e2abcdef0"
    opaque = designer_source_id(raw)
    assert opaque.startswith("pack-")
    assert not any(char.isdigit() for char in opaque)
    assert raw not in opaque
    assert designer_source_id("pack_RANDOM") == "pack_RANDOM"
    assert designer_source_id("pack_COST") == "pack_COST"
    projected = project_designer({"source_id": raw, "name": "pack_92057aa05783", "total_return": 0.1})
    assert projected["source_id"] == opaque
    assert projected["name"] == "pack_92057aa05783"


def test_forty_thousand_hex_ids_are_not_false_scales():
    """The stored letter id is not a scale. A raw id whose value is the scale still is."""
    rng = random.Random(20261111)
    pinned = (
        "pack_25e2abcdef",
        "pack_5e1abcdef0",
        "pack_7e1abcdef0",
        "pack_2e2abcdef0",
        "pack_1e2abcdef0",
        "pack_10e2abcdef",
        "pack_185e2abcdef",
        "pack_4738e304ab",
    )
    ids = list(pinned)
    ids.extend("pack_" + "".join(rng.choice("0123456789abcdef") for _ in range(12)) for _ in range(40_000))
    for ident in ids:
        stored = designer_source_id(ident)
        payload = json.dumps({"source_id": stored})
        for scale in _SCALES:
            assert scan_publication(payload, scale=scale) is None, (ident, stored, scale)
            assert scan_publication(stored, scale=scale) is None, (ident, stored, scale)
    for scale in (*_SCALES, 1e-7, 7.777):
        assert _literal_is_scale("4738e304", scale) is False
        assert scan_publication("4738e304", scale=scale) == "a date"
    assert _literal_is_scale("2e2", 2) is True
    assert _literal_is_scale("7.777e3", 7.777) is True
    assert _scale_in_token("build2_7.777e3", 7.777) is True
    assert _scale_in_token("pack_2e2abcdef0", 2) is True
    assert _scale_in_token("pack_185e2abcdef", 2) is False
