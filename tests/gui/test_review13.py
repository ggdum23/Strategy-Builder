"""Thirteenth review. A rejected date id opens its opaque designer directory."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_parallel import FLAT, _anon_pack, _strategy
from test_window import _window
from windows_open import windows_jails

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

DATES = ("04-Jan-2024", "today 2024")


def _column(model, column: int) -> list[str]:
    return [str(model.data(model.index(row, column)) or "") for row in range(model.rowCount())]


def _publish(tmp_path: Path) -> Path:
    from backtester.parallel import PackMarket, run_parallel

    root = tmp_path / "batch"
    names = DATES + ("opening_drive",)
    strategies = [_strategy(root / "strategies", name, FLAT) for name in names]
    pack, sidecar, _built = _anon_pack(root, "QQQ", name="pack", price_factor=1.1)
    out = root / "out"
    run_parallel(
        strategies,
        [PackMarket(str(pack), str(sidecar))],
        out,
        profile="arena",
        workers=1,
        timeout_s=60,
        with_benchmarks=False,
    )
    return out


@windows_jails(6)
def test_double_click_opens_the_opaque_directory(qtbot, tmp_path):
    out = _publish(tmp_path)
    window = _window(qtbot, tmp_path)
    window.show_page(4)
    page = window.results_page
    page.load_path(str(out), audience="designer")
    QApplication.processEvents()
    assert page._report is not None
    assert page._report.audience == "designer"
    for child in page._report.children:
        assert "path" not in child
    jobs = page.jobs.model()
    assert jobs is not None
    shown = _column(jobs, 0)
    assert shown
    assert all(text != "—" for text in shown)
    assert any("opening_drive" in text for text in shown)
    for text in shown:
        for date in DATES:
            assert date not in text
        assert "2024" not in text
        assert "today" not in text.casefold()
    records = page.jobs.raw_rows()
    for record in records:
        target = record.get("designer")
        assert target
        assert Path(target).is_dir()
        assert "2024" not in str(target)
        for date in DATES:
            assert date not in str(target)
    for record in records:
        label = str(record.get("job") or "")
        if "opening_drive" in label:
            continue
        page.load_path(str(out), audience="designer")
        QApplication.processEvents()
        jobs = page.jobs.model()
        target = None
        for row in range(jobs.rowCount()):
            if str(jobs.data(jobs.index(row, 0)) or "") == label:
                target = jobs.index(row, 0)
                break
        assert target is not None
        page.jobs.doubleClicked.emit(target)
        QApplication.processEvents()
        report = page._report
        assert report is not None
        assert report.kind != "batch"
        assert report.directory is not None
        assert report.directory.name != "04-Jan-2024"
        assert "2024" not in str(report.directory)
        assert "today 2024" not in str(report.directory)
        assert "designer" in report.directory.parts
        assert (report.directory / "error.json").is_file()
        document = json.loads((report.directory / "error.json").read_text(encoding="utf-8"))
        assert document["error"] == "the strategy failed"
        assert document["strategy_id"] == report.directory.parent.name
        assert label.startswith(document["strategy_id"])
    page.load_path(str(out), audience="designer")
    QApplication.processEvents()
    jobs = page.jobs.model()
    opened = None
    for row in range(jobs.rowCount()):
        if "opening_drive" in str(jobs.data(jobs.index(row, 0)) or ""):
            opened = jobs.index(row, 0)
            break
    assert opened is not None
    page.jobs.doubleClicked.emit(opened)
    QApplication.processEvents()
    report = page._report
    assert report is not None
    assert report.kind == "book"
    assert report.directory is not None
    assert report.directory.parent.name == "opening_drive"
    assert "designer" in report.directory.parts
