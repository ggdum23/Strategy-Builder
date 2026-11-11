"""Ninth review. Clock tokens, PNG text, and typed designer publish."""

from __future__ import annotations

import json
import os
import struct
import zlib
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYTEST_QT_API", "pyside6")
os.environ.setdefault("MPLBACKEND", "Agg")

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")
pytest.importorskip("pytestqt")

from PySide6.QtWidgets import QApplication, QLabel

from backtester.gui.audience import export_allowed, export_text
from backtester.runner import run_books, write_books

from test_window import _window

_PLANT = "fill 450.12 scale 7.777 and \u22127.777 and \uff17\uff0e\uff17\uff17\uff17"
_NEEDLES = ("450.12", "7.777", "\u22127.777", "\uff17\uff0e\uff17\uff17\uff17")
_TIMES = ("09:20", "09:25", "09:50", "07:30")


def _frame():
    import pandas as pd

    rows = []
    for day in (0, 1):
        for minute, phase in ((450, "pre"), (560, "pre"), (565, "pre"), (570, "rth"), (590, "rth")):
            rows.append(
                {
                    "date": f"2026-03-0{2 + day}",
                    "day": day,
                    "weekday": 0,
                    "minute": minute,
                    "time": f"{minute // 60:02d}:{minute % 60:02d}",
                    "session": "rth" if phase == "rth" else "pre",
                    "open": 100.0,
                    "high": 101.0,
                    "low": 99.0,
                    "close": 100.0,
                    "volume": 1000.0,
                    "wap": 100.0,
                    "count": 1,
                    "filled": 0,
                    "short_session": 0,
                }
            )
    return pd.DataFrame(rows)


class _Hold:
    def on_bar(self, _row, _account):
        return 1.0


def _shown(page) -> str:
    parts = [page.figure_note.text(), page.fills_note.text()]
    for label in page.findChildren(QLabel):
        parts.append(label.text())
    model = page.fills.model()
    if model is not None:
        for row in range(model.rowCount()):
            for column in range(model.columnCount()):
                parts.append(str(model.data(model.index(row, column)) or ""))
    return "\n".join(parts)


def _plant_text(png: bytes, caption: str) -> bytes:
    data = b"Comment\x00" + caption.encode("latin-1")
    crc = zlib.crc32(b"tEXt" + data) & 0xFFFFFFFF
    chunk = struct.pack(">I", len(data)) + b"tEXt" + data + struct.pack(">I", crc)
    start = png.rfind(b"IEND") - 4
    return png[:start] + chunk + png[start:]


def test_desk_clocks_export_and_a_designer_folder_shows_no_plant(qtbot, tmp_path):
    """Desk fill times survive the scales that share their minutes, and the plant is not in designer/."""
    frame = _frame()
    books = run_books(_Hold, frame, "desk", share_factor=7.777, with_benchmarks=False)
    result, metrics = books["strategy"]
    result["fills"].extend(
        {
            "session": "" if stamp == "09:25" else "S00001",
            "time": stamp,
            "phase": "rth" if stamp in {"09:25", "09:50"} else "pre",
            "minute": int(stamp[:2]) * 60 + int(stamp[3:]),
            "kind": "strategy",
        }
        for stamp in _TIMES
    )
    metrics["message"] = _PLANT
    metrics["M1"] = "close 450.12"
    metrics["name"] = "build_7.777"
    out = tmp_path / "desk"
    write_books(out, "desk", "QQQ", 1, frame, books, anonymized=True)
    designer = out / "designer"
    visible_files = "\n".join(
        path.read_text(encoding="utf-8")
        for path in designer.rglob("*")
        if path.is_file() and path.suffix != ".png"
    )
    for needle in (*_NEEDLES, "build_7.777"):
        assert needle not in visible_files
    assert "IDAT" not in export_text(designer / "equity.png")

    window = _window(qtbot, tmp_path)
    page = window.results_page
    page.load_path(str(designer), audience="designer")
    QApplication.processEvents()
    shown = _shown(page)
    for needle in _NEEDLES:
        assert needle not in shown
    for stamp in _TIMES:
        assert stamp in shown
    assert "withheld" not in page.figure_note.text().casefold()
    assert page.figure_note.text().startswith("Saved figure")

    operator = json.loads((out / "metrics.json").read_text(encoding="utf-8"))
    for scale in (0.25, 0.5, 0.7, 2):
        operator["cost_basis"]["share_scale"] = scale
        operator["cost_basis"]["share_factor"] = scale
        (out / "metrics.json").write_text(json.dumps(operator) + "\n", encoding="utf-8")
        page.load_path(str(out), audience="designer")
        QApplication.processEvents()
        shown = _shown(page)
        for stamp in _TIMES:
            assert stamp in shown, (scale, stamp, shown)
        exported = tmp_path / f"export-{scale}"
        written = page.export_to(exported)
        assert "fills.csv" in written
        fills = (exported / "fills.csv").read_text(encoding="utf-8")
        for stamp in _TIMES:
            assert stamp in fills
        assert "equity.png" in written
        assert (exported / "equity.png").is_file()

    png = designer / "equity.png"
    original = png.read_bytes()
    png.write_bytes(_plant_text(original, "scale 7.777 held QQQ"))
    operator["cost_basis"]["share_scale"] = 7.777
    operator["cost_basis"]["share_factor"] = 7.777
    (out / "metrics.json").write_text(json.dumps(operator) + "\n", encoding="utf-8")
    assert not export_allowed(export_text(png), symbols=("QQQ",), scale=7.777)
    page.load_path(str(out), audience="designer")
    QApplication.processEvents()
    assert "withheld" not in page.figure_note.text().casefold()
    assert page.figure_note.text().startswith("Saved figure")
    pixmap = page.figure_label.pixmap()
    assert pixmap is not None and not pixmap.isNull()
    exported = tmp_path / "poisoned"
    written = page.export_to(exported)
    assert "equity.png" in written
    cleaned = (exported / "equity.png").read_bytes()
    assert b"7.777" not in cleaned
    assert b"QQQ" not in cleaned
