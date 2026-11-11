"""Sixth review of 810a430. Each test fails when its fix is reverted."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYTEST_QT_API", "pyside6")

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")
pytest.importorskip("pytestqt")

from PySide6.QtWidgets import QApplication

from backtester.gui.audience import _consume_paths, designer_error, designer_surface_text, export_allowed
from backtester.gui.backend import load_run
from backtester.gui.jobs import build_pack_job, execute_run, record_series
from backtester.report import TICKER_STAND_IN

from synthetic import write_filled, write_strategy
from test_window import _window

ROOT = Path(__file__).resolve().parents[2]
_SCALE_TOKENS = ("build2_7.777", "v2_7.777", "scale__7.777", "build2_1e7.777")


def _number_present(text: str, number: str) -> bool:
    return re.search(rf"(?<![0-9.]){re.escape(number)}(?![0-9.])", text) is not None


def test_device_paths_and_field_tails_use_the_shared_check():
    samples = (
        r"open \\?\UNC\server\share\Jeu\data.csv, still secret",
        r"open \\?\C:\Users\Jeu\file.csv, still secret",
        "open //?/UNC/server/share/Jeu/file.csv, still secret",
        "open //?/C:/Users/Jeu/file.csv, still secret",
        r"open C:\Users\Jeu\Desktop\Strategy Builder, Inc\QQQ\data.csv, still secret",
        "open /home/jeu/Strategy Builder, Inc/QQQ/data.csv, still secret",
    )
    for sample in samples:
        shown = designer_surface_text(sample, ()).casefold()
        for needle in ("jeu", "users", "secret", "qqq", "inc"):
            assert needle not in shown, (sample, needle, shown)
        assert not export_allowed(sample, symbols=(), scale=None), sample
        assert "secret" not in designer_error(sample).casefold()
    quoted = designer_error(
        "FileNotFoundError: [Errno 2] No such file or directory: "
        r"'C:\Users\Jeu\Desktop\Strategy Builder\QQQ\data.csv'"
    )
    assert "FileNotFoundError" in quoted
    assert "Jeu" not in quoted and "data.csv" not in quoted
    for date in ("2024/1/4", "/2024/1/4", "day 2024"):
        assert _consume_paths(date, "") == date


def test_embedded_scale_decimal_and_exponent_share_the_token_check():
    for token in _SCALE_TOKENS:
        shown = designer_surface_text(f"see {token} now", (), scale=7.777)
        assert "7.777" not in shown, token
        assert token not in shown, token
        assert not export_allowed(token, symbols=(), scale=7.777), token
        kept = designer_surface_text(token, (), scale=2)
        assert token in kept, token
        assert export_allowed(token, symbols=(), scale=2), token
    assert designer_surface_text("long_2x and 2.5", (), scale=2) == "long_2x and 2.5"
    assert export_allowed("long_2x and 2.5", symbols=(), scale=2)
    assert not export_allowed("held 2 shares", symbols=(), scale=2)


def test_redacted_export_cannot_rebuild_close(qtbot, tmp_path):
    folder = tmp_path / "desk"
    folder.mkdir()
    metrics = {
        "profile": "desk",
        "symbol": "QQQ",
        "anonymized": True,
        "total_return": 0.0,
        "final_equity": 1_000_000,
        "initial_equity": 1_000_000,
    }
    (folder / "metrics.json").write_text(json.dumps(metrics) + "\n", encoding="utf-8")
    curve = "session,equity,exposure,shares,close\nS00001,1000000,1.0,2000,500\n"
    (folder / "equity_curve.csv").write_text(curve, encoding="utf-8")
    (folder / "equity.csv").write_text(curve, encoding="utf-8")
    (folder / "fills.csv").write_text(
        "session,shares_delta,price,fill_price,fees,shares_after\nS00001,2000,500,500,1.25,2000\n",
        encoding="utf-8",
    )
    (folder / "trades.csv").write_text("note\nplain row\n", encoding="utf-8")
    window = _window(qtbot, tmp_path)
    page = window.results_page
    page.load_path(str(folder))
    QApplication.processEvents()
    assert page._report.audience == "designer"
    assert page._report.redacted is True
    exported = tmp_path / "redacted"
    page.export_to(exported)
    text = "\n".join(
        path.read_text(encoding="utf-8", errors="replace") for path in exported.rglob("*") if path.is_file()
    )
    assert _number_present(text, "1000000") or "1000000.0" in text
    assert not _number_present(text, "500")
    assert not _number_present(text, "2000")
    hidden = {"close", "price", "fill_price", "shares", "shares_delta", "shares_after", "position_shares"}
    for path in exported.rglob("*.csv"):
        header = path.read_text(encoding="utf-8").splitlines()[0]
        assert hidden.isdisjoint({part.strip() for part in header.split(",")}), (path.name, header)
    curve_out = (exported / "equity_curve.csv").read_text(encoding="utf-8")
    assert curve_out != curve
    assert not (exported / "equity.csv").exists()

    published = folder / "designer"
    published.mkdir()
    (published / "metrics.json").write_text(
        json.dumps({"profile": "desk", "audience": "designer", "total_return": 0.0, "final_equity": 1_000_000}) + "\n",
        encoding="utf-8",
    )
    scaled = "session,equity,exposure,shares,close\nS00001,1000000,1.0,80,17.25\n"
    (published / "equity_curve.csv").write_text(scaled, encoding="utf-8")
    (published / "equity.csv").write_bytes((published / "equity_curve.csv").read_bytes())
    page.load_path(str(folder))
    QApplication.processEvents()
    assert page._report.redacted is False
    copied = tmp_path / "published"
    page.export_to(copied)
    published_text = (copied / "equity_curve.csv").read_text(encoding="utf-8")
    assert "17.25" in published_text
    assert not _number_present(published_text, "500")


def test_phase3_pack_loads_in_operator_and_designer_views(qtbot, tmp_path):
    src = write_filled(tmp_path / "Strategy Builder" / "QQQ" / "filled")
    registry = tmp_path / "registry.json"
    record_series(src, "QQQ", registry, profile="arena", price_kind="raw", bar_minutes=1)
    pack = tmp_path / "pack"
    sidecar = tmp_path / "operator" / "sidecar.json"
    built = build_pack_job(
        src,
        "QQQ",
        pack,
        sidecar,
        registry,
        anonymize=True,
        profile="arena",
        bar_minutes=1,
        price_kind="raw",
    )
    assert built["manifest"]["asset_name"] == TICKER_STAND_IN == "RANDOM"
    strategy = write_strategy(tmp_path / "strategy.py", 1.0)
    out = tmp_path / "out"
    execute_run(
        {
            "mode": "single",
            "profile": "arena",
            "source": "pack",
            "strategies": [str(strategy)],
            "pack": str(pack),
            "sidecar": str(sidecar),
            "out": str(out),
            "bar_minutes": 1,
            "initial_equity": 1_000_000,
            "borrow_rate_annual": None,
            "financing_rate_annual": None,
            "fee_mult": 1,
            "dividends": "",
            "warmup_sessions": 0,
            "warmup_days": 0,
            "design_end_day": None,
            "design_end_date": "",
            "design_fraction": None,
        }
    )
    curve = out / "equity_curve.csv"
    mirror = out / "equity.csv"
    assert curve.is_file() and mirror.is_file()
    assert curve.read_bytes() == mirror.read_bytes()
    assert (out / "equity.png").is_file()
    assert (out / "fills.csv").is_file()
    assert (out / "designer" / "equity_curve.csv").is_file()
    assert not (out / "equity_curve.png").exists()
    assert not (out / "liquidation_postmortem.json").exists()

    operator = load_run(out, audience="operator")
    designer = load_run(out, audience="designer")
    assert operator.audience == "operator"
    assert operator.metrics.get("symbol") == "QQQ"
    assert operator.curves and operator.curves[0].source.endswith("equity_curve.csv")
    assert designer.audience == "designer"
    assert designer.redacted is False
    assert "designer" in Path(designer.curves[0].source).parts
    assert designer.curves[0].source.endswith("equity_curve.csv")
    designer_blob = json.dumps(designer.metrics, default=str)
    assert "QQQ" not in designer_blob
    assert TICKER_STAND_IN == "RANDOM"

    window = _window(qtbot, tmp_path)
    page = window.results_page
    page.load_path(str(out), audience="operator")
    QApplication.processEvents()
    assert page._report.audience == "operator"
    assert "QQQ" in _page_text(page)
    page.load_path(str(out), audience="designer")
    QApplication.processEvents()
    assert page._report.audience == "designer"
    shown = _page_text(page)
    assert "QQQ" not in shown
    assert page._report.directory.name == "designer"


def test_designer_screenshots_do_not_ocr_a_path_or_username():
    tesseract = shutil.which("tesseract")
    if tesseract is None:
        pytest.skip("tesseract is not installed")
    shots = ROOT / "docs" / "screenshots"
    for name in ("21-designer-dark.png", "22-designer-light.png"):
        result = subprocess.run(
            [tesseract, str(shots / name), "stdout"],
            check=False,
            capture_output=True,
            text=True,
        )
        text = result.stdout or ""
        assert text.strip(), name
        folded = text.casefold()
        for needle in ("jeu", "users"):
            assert needle not in folded, (name, text)
        assert not re.search(r"[A-Za-z]:[\\/]", text), text
        assert "\\\\?\\" not in text


def _page_text(page) -> str:
    chunks = []
    from PySide6.QtWidgets import QWidget

    for widget in page.findChildren(QWidget):
        for method in ("text", "toolTip", "placeholderText"):
            fn = getattr(widget, method, None)
            if not callable(fn):
                continue
            try:
                value = fn()
            except Exception:
                continue
            if isinstance(value, str) and value:
                chunks.append(value)
    return "\n".join(chunks)
