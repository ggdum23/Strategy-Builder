"""Tenth review. A planted designer chart displays and exports without the caption."""

from __future__ import annotations

import io
import os
import struct
import zlib

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYTEST_QT_API", "pyside6")
os.environ.setdefault("MPLBACKEND", "Agg")

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")
pytest.importorskip("pytestqt")

from PySide6.QtWidgets import QApplication

from backtester.gui.audience import export_allowed, export_text
from backtester.runner import write_books

from test_window import _window

_FULLWIDTH_SCALE = "\uff17\uff0e\uff17\uff17\uff17"
_FULLWIDTH_TICKER = "\uff31\uff31\uff31"
_PRICE = "close 450.12"


def _chunk(kind: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(kind + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)


def _insert_before_iend(png: bytes, chunk: bytes) -> bytes:
    start = png.rfind(b"IEND") - 4
    return png[:start] + chunk + png[start:]


def _plant(png: bytes) -> bytes:
    caption = f"scale {_FULLWIDTH_SCALE} held {_FULLWIDTH_TICKER}"
    text = caption.encode("utf-8")
    itxt = b"Comment\x00" + bytes((0, 0)) + b"en\x00" + "note".encode("utf-8") + b"\x00" + text
    planted = _insert_before_iend(png, _chunk(b"iTXt", itxt))
    planted = _insert_before_iend(planted, _chunk(b"tEXt", b"Comment\x00" + _PRICE.encode("latin-1")))
    return planted + b"\n" + _PRICE.encode("ascii") + _FULLWIDTH_SCALE.encode("utf-8") + _FULLWIDTH_TICKER.encode("utf-8")


def _pixels(data: bytes) -> tuple[tuple[int, int], bytes]:
    from PIL import Image

    with Image.open(io.BytesIO(data)) as image:
        image.load()
        return image.size, image.convert("RGBA").tobytes()


def test_designer_view_and_export_drop_png_plants(qtbot, tmp_path):
    result = {
        "daily": [{"session": "S00001", "ordinal": 0, "equity": 1_000_000.0, "exposure": 0.0}],
        "fills": [],
        "episodes": [],
    }
    metrics = {"total_return": 0.0, "profile": "desk", "share_factor": 2}
    out = tmp_path / "run"
    write_books(
        out,
        "desk",
        "QQQ",
        1,
        None,
        {"strategy": (result, metrics)},
        anonymized=True,
    )
    figure = out / "designer" / "equity.png"
    original = figure.read_bytes()
    assert _pixels(original) == _pixels((out / "equity.png").read_bytes())
    planted = _plant(original)
    figure.write_bytes(planted)
    assert not export_allowed(export_text(figure), symbols=("QQQ",), scale=7.777)

    window = _window(qtbot, tmp_path)
    page = window.results_page
    page.load_path(str(out / "designer"), audience="designer")
    QApplication.processEvents()
    assert page.figure_note.text().startswith("Saved figure")
    assert "withheld" not in page.figure_note.text().casefold()
    pixmap = page.figure_label.pixmap()
    assert pixmap is not None and not pixmap.isNull()

    exported = tmp_path / "export"
    written = page.export_to(exported)
    assert "equity.png" in written
    cleaned = (exported / "equity.png").read_bytes()
    assert _FULLWIDTH_SCALE.encode("utf-8") not in cleaned
    assert _FULLWIDTH_TICKER.encode("utf-8") not in cleaned
    assert _PRICE.encode("ascii") not in cleaned
    assert _pixels(cleaned) == _pixels(original)
