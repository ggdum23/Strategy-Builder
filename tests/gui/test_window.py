"""Offscreen window tests. Skipped unless the gui extra is installed."""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYTEST_QT_API", "pyside6")

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")
pytest.importorskip("pytestqt")

from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import QApplication, QPushButton

from backtester.gui.backend import HistoryRow, load_run, scan_history
from backtester.gui.discover import summarize_strategy
from backtester.gui.jobs import build_pack_job, inspect_series, record_series
from backtester.gui.window import MainWindow

from synthetic import ARENA_METRICS, DESK_METRICS, write_book, write_filled, write_strategy

ROOT = Path(__file__).resolve().parents[2]
SHOTS = ROOT / "docs" / "screenshots"
WINDOWS_ROOT = r"C:\Users\Jeu\Desktop\Backtester"
WINDOWS_SRC = r"C:\Users\Jeu\Desktop\Strategy Builder\QQQ\filled"


def _window(qtbot, tmp_path) -> MainWindow:
    window = MainWindow(preferences_path=tmp_path / "preferences.json")
    qtbot.addWidget(window)
    window.resize(1440, 920)
    window.show()
    qtbot.waitExposed(window)
    for _ in range(3):
        QApplication.processEvents()
    return window


def _click(qtbot, widget) -> None:
    qtbot.mouseClick(widget, Qt.MouseButton.LeftButton)


def _button(window, text: str) -> QPushButton:
    found = [button for button in window.findChildren(QPushButton) if button.text() == text]
    assert found, text
    return found[0]


def _grab(window, name: str, tmp_path: Path) -> None:
    window.resize(1680, 1180)
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    for _ in range(4):
        QApplication.processEvents()
    pixmap = window.grab()
    assert not pixmap.isNull()
    assert pixmap.width() >= 1600
    target = tmp_path / name
    assert pixmap.save(str(target), "PNG")
    blob = target.read_bytes()
    assert blob.startswith(b"\x89PNG")
    assert len(blob) > 20_000
    if os.environ.get("STRATEGY_BUILDER_UPDATE_SCREENSHOTS") == "1":
        SHOTS.mkdir(parents=True, exist_ok=True)
        assert pixmap.save(str(SHOTS / name), "PNG")


def test_pages_theme_profile_and_path_with_spaces(qtbot, tmp_path):
    window = _window(qtbot, tmp_path)
    assert window.data_page.root_path.text() == WINDOWS_ROOT
    for index, name in enumerate(("navData", "navPacks", "navStrategies", "navRun", "navResults", "navHistory")):
        _click(qtbot, window.findChild(QPushButton, name))
        assert window.stack.currentIndex() == index
    _click(qtbot, window.theme_button)
    assert window.theme.name == "light"
    _click(qtbot, window.theme_button)
    assert window.theme.name == "dark"
    window.show_page(2)
    combo = window.strategies_page.profile
    combo.setCurrentIndex(combo.findData("arena"))
    text = window.strategies_page.disclosure_text()
    assert "±2" in text
    assert "next delivered bar" in text.lower() or "next bar" in text.lower()
    combo.setCurrentIndex(combo.findData("desk"))
    desk = window.strategies_page.disclosure_text()
    assert "±3" in desk
    assert "7.00%" in desk
    assert "DISQUALIFIED" in desk
    assert "price_factor" not in desk
    spaced = str(tmp_path / "Strategy Builder" / "strategies")
    window.strategies_page.path.set_text(spaced)
    assert window.strategies_page.path.text() == spaced
    window.show_page(3)
    window.run_page.src.set_text(str(tmp_path / "Strategy Builder" / "QQQ" / "filled"))
    window.run_page.out.set_text(str(tmp_path / "Strategy Builder" / "out"))
    rendered = window.run_page.preview.toPlainText()
    assert "Strategy Builder" in rendered
    assert "null" in rendered
    assert not window.run_page.run_button.isEnabled()


def test_inspect_click_shows_the_gap_banner(qtbot, tmp_path):
    src = write_filled(tmp_path / "Strategy Builder" / "QQQ" / "filled", gap=True)
    window = _window(qtbot, tmp_path)
    window.show_page(0)
    window.data_page.set_src(str(src))
    window.data_page.set_ticker("QQQ")
    window.data_page.set_price_kind("raw")
    _click(qtbot, _button(window.data_page, "Inspect"))
    qtbot.waitUntil(lambda: any("GAP" in text for text in window.data_page.banner_texts()), timeout=20000)
    summary = window.data_page.summary.text()
    assert "0.01" in summary
    assert "-05:00" in summary
    assert "share factor 1.0" in summary


def test_results_filter_sort_and_export_use_the_file(qtbot, tmp_path):
    folder = write_book(tmp_path / "arena", ARENA_METRICS)
    window = _window(qtbot, tmp_path)
    window.open_results(str(folder))
    assert window.stack.currentIndex() == 4
    assert window.results_page.metric_text("total_return") == "4.00%"
    assert window.results_page.metric_text("sharpe") == "0.800"
    assert window.results_page.metric_text("max_drawdown") == "-8.00%"
    assert "phase3" not in window.results_page.metric_text("total_return")
    stored = load_run(folder).metrics
    assert stored["total_return"] == 0.04
    window.results_page.tabs.setCurrentIndex(1)
    assert window.results_page.fills.model().rowCount() == 2
    window.results_page.fills_filter.setText("2024-01-03")
    QApplication.processEvents()
    assert window.results_page.fills.model().rowCount() == 1
    window.results_page.fills_filter.clear()
    window.results_page.chart_mode.setCurrentIndex(1)
    QApplication.processEvents()
    assert "running peak" in window.results_page.chart.caption.text()
    exported = tmp_path / "export"
    written = window.results_page.export_to(exported)
    assert "metrics.json" in written
    assert (exported / "metrics.json").read_text(encoding="utf-8") == (folder / "metrics.json").read_text(encoding="utf-8")
    desk = write_book(tmp_path / "desk", DESK_METRICS)
    window.results_page.load_path(str(desk))
    window.results_page.tabs.setCurrentIndex(2)
    assert "DISQUALIFIED" in window.results_page.identity.text()
    assert "2024-03-14" in window.results_page.postmortem.toPlainText()
    assert window.results_page.desk_cards["financing_cost"].value.text() == "$2,100.00"
    assert "volume_factor" not in window.results_page.postmortem.toPlainText()


def test_run_click_matches_metrics_file(qtbot, tmp_path):
    src = write_filled(tmp_path / "Strategy Builder" / "QQQ" / "filled")
    strategy = write_strategy(tmp_path / "Strategy Builder" / "book" / "strategy.py")
    out = tmp_path / "Strategy Builder" / "out" / "click"
    window = _window(qtbot, tmp_path)
    rows = [summarize_strategy(strategy)]
    window.show_page(2)
    window.strategies_page.set_rows(rows)
    window.strategies_page.check_paths([str(strategy)])
    window.strategies_page.set_profile("arena")
    window.show_page(3)
    window.run_page.src.set_text(str(src))
    window.run_page.ticker.setText("QQQ")
    window.run_page.out.set_text(str(out))
    window.run_page.single.setChecked(True)
    window.run_page.named.setChecked(True)
    window.run_page.refresh()
    assert window.run_page.run_button.isEnabled()
    assert "Strategy Builder" in window.run_page.preview.toPlainText()
    _click(qtbot, window.run_page.run_button)
    qtbot.waitUntil(lambda: (out / "metrics.json").is_file() and not window.run_in_progress(), timeout=120000)
    qtbot.waitUntil(lambda: window.results_page._report is not None and window.results_page._report.kind == "book", timeout=10000)
    stored = load_run(out).metrics
    assert window.results_page.metric_text("total_return") == _percent(stored["total_return"])
    assert window.results_page.metric_text("final_equity").replace(",", "").replace("$", "") == f"{stored['final_equity']:.2f}"


def test_history_opens_a_run(qtbot, tmp_path):
    write_book(tmp_path / "runs" / "one", ARENA_METRICS)
    window = _window(qtbot, tmp_path)
    window.show_page(5)
    window.history_page.root_path.set_text(str(tmp_path / "runs"))
    _click(qtbot, _button(window.history_page, "Scan"))
    qtbot.waitUntil(lambda: window.history_page.table.model() is not None and window.history_page.table.model().rowCount() == 1, timeout=20000)
    index = window.history_page.table.model().index(0, 0)
    window.history_page.table.doubleClicked.emit(index)
    assert window.stack.currentIndex() == 4
    assert window.results_page.metric_text("total_return") == "4.00%"


def test_screenshots(qtbot, tmp_path):
    src = write_filled(tmp_path / "fixture" / "filled", gap=True)
    payload = inspect_series(src, "QQQ", bar_minutes=1, price_kind="raw")
    registry = tmp_path / "registry.json"
    record_series(src, "QQQ", registry, profile="arena", price_kind="raw")
    pack = tmp_path / "pack"
    sidecar = tmp_path / "side" / "sidecar.json"
    built = build_pack_job(
        src,
        "QQQ",
        pack,
        sidecar,
        registry,
        anonymize=True,
        profile="arena",
        price_kind="raw",
    )
    arena = write_book(tmp_path / "shot-arena", ARENA_METRICS, equity=_shot_equity())
    desk = write_book(tmp_path / "shot-desk", DESK_METRICS, equity=[1_000_000 - i * 9000 for i in range(40)])
    window = _window(qtbot, tmp_path)
    window.data_page.set_root(WINDOWS_ROOT)
    window.data_page.set_src(WINDOWS_SRC)
    window.data_page.set_ticker("QQQ")
    window.data_page.set_price_kind("raw")
    window.data_page.set_profile("arena")
    window.data_page.show_inspection(payload)
    window.data_page.show_registry(
        {
            "series": [
                {
                    "symbol": "QQQ",
                    "profile": "arena",
                    "n_sessions": payload["inspection"]["n_sessions"],
                    "native_tick": payload["inspection"]["native_tick"],
                    "price_adjustment": "raw",
                    "runnable": True,
                    "session_segments": payload["inspection"]["session_segments"],
                }
            ]
        }
    )
    window.show_page(0)
    _grab(window, "01-data-dark.png", tmp_path)

    window.packs_page.registry.set_text(r"C:\Users\Jeu\Desktop\Strategy Builder\registry.json")
    window.packs_page.src.set_text(WINDOWS_SRC)
    window.packs_page.ticker.setText("QQQ")
    window.packs_page.out.set_text(r"C:\Users\Jeu\Desktop\Strategy Builder\packs\qqq")
    window.packs_page.sidecar.set_text(r"C:\Users\Jeu\Desktop\Strategy Builder\operator\qqq.sidecar.json")
    window.packs_page.anon.setChecked(True)
    shown = dict(built)
    shown["pack_dir"] = r"C:\Users\Jeu\Desktop\Strategy Builder\packs\qqq"
    shown["sidecar_path"] = r"C:\Users\Jeu\Desktop\Strategy Builder\operator\qqq.sidecar.json"
    window.packs_page.show_build(shown)
    assert "price_factor" not in window.packs_page.summary.text()
    assert "ASSET" in window.packs_page.summary.text()
    window.show_page(1)
    _grab(window, "02-packs-dark.png", tmp_path)

    folder = ROOT / "examples" / "strategies"
    rows = [summarize_strategy(path) for path in sorted(folder.glob("D*/strategy.py"))]
    window.strategies_page.set_rows(rows)
    window.strategies_page.check_paths([str(folder / "D02" / "strategy.py")])
    window.strategies_page.files.setCurrentRow(1)
    window.strategies_page.set_profile("arena")
    window.strategies_page.path.set_text(r"C:\Users\Jeu\Desktop\Strategy Builder\examples\strategies")
    window.show_page(2)
    _grab(window, "03-strategies-dark.png", tmp_path)

    live_src = write_filled(tmp_path / "Strategy Builder" / "QQQ" / "filled")
    live_out = tmp_path / "Strategy Builder" / "out" / "D02"
    window.run_page.src.set_text(str(live_src))
    window.run_page.ticker.setText("QQQ")
    window.run_page.out.set_text(str(live_out))
    window.run_page.equity.setText("1000000")
    window.run_page.fee.setText("1")
    window.run_page.single.setChecked(True)
    window.run_page.named.setChecked(True)
    window.run_page.refresh()
    assert window.run_page.run_button.isEnabled()
    window.show_page(3)
    windows_out = r"C:\Users\Jeu\Desktop\Strategy Builder\out\D02"
    for field, shown in ((window.run_page.src, WINDOWS_SRC), (window.run_page.out, windows_out)):
        field.edit.blockSignals(True)
        field.set_text(shown)
        field.edit.blockSignals(False)
    strategy_file = str(folder / "D02" / "strategy.py")
    windows_strategy = r"C:\Users\Jeu\Desktop\Strategy Builder\examples\strategies\D02\strategy.py"
    preview = (
        window.run_page.preview.toPlainText()
        .replace(str(live_src), WINDOWS_SRC)
        .replace(str(live_out), windows_out)
        .replace(strategy_file, windows_strategy)
    )
    window.run_page.preview.setPlainText(preview)
    window.run_page.strategy_label.setText(
        r"Strategies: C:\Users\Jeu\Desktop\Strategy Builder\examples\strategies\D02\strategy.py"
    )
    _grab(window, "04-run-dark.png", tmp_path)

    window.results_page.show_report(load_run(arena))
    window.results_page.path.set_text(r"C:\Users\Jeu\Desktop\Strategy Builder\out\D02")
    window.results_page.tabs.setCurrentIndex(0)
    window.results_page.chart_mode.setCurrentIndex(0)
    window.show_page(4)
    _grab(window, "05-results-dark.png", tmp_path)

    window.results_page.show_report(load_run(desk))
    window.results_page.tabs.setCurrentIndex(2)
    QApplication.processEvents()
    _grab(window, "06-desk-risk-dark.png", tmp_path)

    rows = scan_history(tmp_path)
    rewritten = []
    for item in rows:
        rewritten.append(
            HistoryRow(
                path=rf"C:\Users\Jeu\Desktop\Strategy Builder\out\{Path(item.path).name}",
                kind=item.kind,
                profile=item.profile,
                symbol=item.symbol,
                strategy=item.strategy,
                modified=item.modified,
                total_return=item.total_return,
                final_equity=item.final_equity,
                sharpe=item.sharpe,
                max_drawdown=item.max_drawdown,
                disqualified=item.disqualified,
            )
        )
    window.history_page.root_path.set_text(r"C:\Users\Jeu\Desktop\Strategy Builder\out")
    window.history_page.show_rows(rewritten)
    window.show_page(5)
    _grab(window, "07-history-dark.png", tmp_path)

    window.theme_button.click()
    window.results_page.show_report(load_run(arena))
    window.results_page.tabs.setCurrentIndex(0)
    window.show_page(4)
    _grab(window, "08-results-light.png", tmp_path)


def _shot_equity() -> list[float]:
    """A rise, an 8% dip, and a recovery to the stored final equity. Picture only."""
    values = []
    for index in range(40):
        if index < 16:
            values.append(1_000_000 + index * 4_500)
        elif index < 26:
            values.append(1_067_500 - (index - 15) * 9_000)
        else:
            trough = 977_500
            values.append(trough + (index - 25) * ((1_040_000 - trough) / 14))
    return values


def _percent(value: float) -> str:
    from backtester.gui.format import format_number

    return format_number("fraction", value)
