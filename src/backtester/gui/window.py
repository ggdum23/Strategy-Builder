"""Research terminal shell. Pages render. Runs go to a worker process."""

from __future__ import annotations

import tempfile
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QFontDatabase, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QStackedWidget,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from .audience import peek_scale, peek_symbols, scrub_text
from .pages.data_page import DataPage
from .pages.history_page import HistoryPage
from .pages.packs_page import PacksPage
from .pages.results_page import ResultsPage
from .pages.run_page import RunPage
from .pages.strategies_page import StrategiesPage
from .preferences import Preferences
from .theme import THEMES, build_palette, build_qss
from .validate import normalize
from .workers import FunctionThread, ProcessSupervisor

_NAV = (
    ("navData", "Data", "Inspect a series"),
    ("navPacks", "Packs", "Named or anonymized"),
    ("navStrategies", "Strategies", "Profile rules"),
    ("navRun", "Run", "Single, walk-forward, batch"),
    ("navResults", "Results", "Reports only"),
    ("navHistory", "History", "Past runs"),
)

_UI_FONTS = ("Segoe UI", "Inter", "Source Sans 3", "IBM Plex Sans", "Noto Sans", "DejaVu Sans")
_MONO_FONTS = ("Cascadia Mono", "Cascadia Code", "JetBrains Mono", "Consolas", "DejaVu Sans Mono")


def choose_fonts() -> tuple[str, str]:
    families = set(QFontDatabase.families())
    ui = next((name for name in _UI_FONTS if name in families), "Sans Serif")
    mono = next((name for name in _MONO_FONTS if name in families), "Monospace")
    return ui, mono


class MainWindow(QMainWindow):
    def __init__(self, preferences_path=None):
        super().__init__()
        self.setWindowTitle("Strategy Builder")
        self.setObjectName("appRoot")
        self.resize(1440, 920)
        self.setMinimumSize(1100, 720)
        self.preferences = Preferences(preferences_path)
        self.theme = THEMES["dark"]
        self._profile = ""
        self._strategies: list[str] = []
        self._thread: FunctionThread | None = None
        self._supervisor = ProcessSupervisor()
        self._supervisor.progress.connect(self._on_progress)
        self._supervisor.finished.connect(self._on_finished)
        self._supervisor.failed.connect(self._on_failed)
        self._supervisor.cancelled.connect(self._on_cancelled)
        self._status_path: Path | None = None
        self._status_text = ""

        shell = QWidget()
        shell.setObjectName("appRoot")
        outer = QVBoxLayout(shell)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._topbar())
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self._rail())
        self.stack = QStackedWidget()
        self.data_page = DataPage(self)
        self.packs_page = PacksPage(self)
        self.run_page = RunPage(self)
        self.results_page = ResultsPage(self)
        self.history_page = HistoryPage(self)
        self.strategies_page = StrategiesPage(self)
        for page in (
            self.data_page,
            self.packs_page,
            self.strategies_page,
            self.run_page,
            self.results_page,
            self.history_page,
        ):
            page.setObjectName("page")
            page.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
            self.stack.addWidget(page)
        body.addWidget(self.stack, 1)
        outer.addLayout(body, 1)
        self.setCentralWidget(shell)
        status = QStatusBar()
        self.setStatusBar(status)
        self._status = status
        self.set_status("Display only. Metrics are read from the run directory. 252 sessions/year, risk-free rate 0.")
        for index in range(6):
            shortcut = QShortcut(QKeySequence(f"Ctrl+{index + 1}"), self)
            shortcut.activated.connect(lambda i=index: self.show_page(i))
        self.apply_theme(self.preferences.data.get("theme") or "dark")
        self._restore()

    def _topbar(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName("topbar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(16, 10, 16, 10)
        mark = QLabel("STRATEGY BUILDER")
        mark.setObjectName("wordmark")
        sub = QLabel("Research terminal")
        sub.setObjectName("muted")
        self.root_chip = QLabel("")
        self.root_chip.setObjectName("muted")
        self.theme_button = QPushButton("Light theme")
        self.theme_button.setObjectName("themeButton")
        self.theme_button.clicked.connect(self.toggle_theme)
        layout.addWidget(mark)
        layout.addWidget(sub)
        layout.addStretch(1)
        layout.addWidget(self.root_chip)
        layout.addWidget(self.theme_button)
        return bar

    def _rail(self) -> QFrame:
        rail = QFrame()
        rail.setObjectName("rail")
        rail.setFixedWidth(196)
        layout = QVBoxLayout(rail)
        layout.setContentsMargins(0, 8, 0, 8)
        layout.setSpacing(2)
        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        self.nav_buttons = []
        for index, (name, title, caption) in enumerate(_NAV):
            button = QPushButton(f"{title}\n{caption}")
            button.setObjectName(name)
            button.setProperty("navRole", "true")
            button.setAccessibleName(name)
            button.setCheckable(True)
            button.clicked.connect(lambda _checked=False, i=index: self.show_page(i))
            self.nav_group.addButton(button)
            self.nav_buttons.append(button)
            layout.addWidget(button)
        self.nav_buttons[0].setChecked(True)
        layout.addStretch(1)
        return rail

    def show_page(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        if 0 <= index < len(self.nav_buttons):
            self.nav_buttons[index].setChecked(True)
        if index == 3:
            self.run_page.refresh()

    def apply_theme(self, name: str) -> None:
        base = THEMES.get(name, THEMES["dark"])
        ui, mono = choose_fonts()
        self.theme = base.with_fonts(ui, mono)
        sheet = build_qss(self.theme)
        self.setStyleSheet(sheet)
        app = QApplication.instance()
        if app is not None:
            app.setStyle("Fusion")
            app.setPalette(build_palette(self.theme))
            app.setStyleSheet(sheet)
        self.theme_button.setText("Light theme" if self.theme.name == "dark" else "Dark theme")
        self.results_page.chart.apply_theme(self.theme)
        self.preferences.set("theme", self.theme.name)

    def toggle_theme(self) -> None:
        self.apply_theme("light" if self.theme.name == "dark" else "dark")
        self.preferences.save()
        self.results_page._redraw()

    def set_status(self, text: str) -> None:
        self._status_text = text
        self._publish_status()

    def note_results_audience(self) -> None:
        """A designer report must not leave a ticker in the status bar or the root chip."""
        self._publish_status()
        self._refresh_chip()

    def _results_are_designer(self) -> bool:
        page = getattr(self, "results_page", None)
        report = getattr(page, "_report", None) if page is not None else None
        return getattr(report, "audience", "") == "designer"

    def _chrome_symbols(self) -> tuple[str, ...]:
        found: list[str] = []
        page = getattr(self, "results_page", None)
        source = getattr(page, "_source_path", "") if page is not None else ""
        if source:
            found.extend(peek_symbols(Path(source)))
        data = getattr(self, "data_page", None)
        if data is not None:
            ticker = data.ticker.text().strip()
            if ticker:
                found.append(ticker)
        last = str(self.preferences.data.get("last_ticker") or "").strip()
        if last:
            found.append(last)
        ordered: list[str] = []
        seen: set[str] = set()
        for symbol in found:
            key = symbol.casefold()
            if not symbol or key in seen:
                continue
            seen.add(key)
            ordered.append(symbol)
        return tuple(ordered)

    def _chrome_scale(self) -> float | None:
        page = getattr(self, "results_page", None)
        source = getattr(page, "_source_path", "") if page is not None else ""
        if not source:
            return None
        return peek_scale(Path(source))

    def _public_text(self, text: str) -> str:
        if not text or not self._results_are_designer():
            return text
        return scrub_text(text, self._chrome_symbols(), scale=self._chrome_scale())

    def _publish_status(self) -> None:
        self._status.showMessage(self._public_text(self._status_text))

    def remember(self, **values: str) -> None:
        for key, value in values.items():
            self.preferences.set(key, value)
        self.preferences.save()
        self._refresh_chip()

    def on_profile_changed(self, name: str) -> None:
        self._profile = name or ""
        if hasattr(self, "run_page"):
            self.run_page.apply_profile(self._profile)
        self.preferences.set("last_profile", self._profile)

    def on_strategies_changed(self, paths: list[str]) -> None:
        self._strategies = list(paths)
        if hasattr(self, "run_page"):
            self.run_page.set_strategies(self._strategies)

    def run_in_progress(self) -> bool:
        return self._supervisor.running()

    def start_task(self, fn, on_success, on_error) -> None:
        if self._thread is not None and self._thread.isRunning():
            on_error("Another task is still running.")
            return
        thread = FunctionThread(fn)
        thread.succeeded.connect(on_success)
        thread.failed.connect(on_error)
        thread.finished.connect(lambda: self.set_status(self._status_text or "Ready"))
        self._thread = thread
        self.set_status("Working…")
        thread.start()

    def start_run(self, config: dict) -> None:
        if self._supervisor.running():
            self.run_page.on_failed("A run is already in progress.")
            return
        folder = Path(tempfile.mkdtemp(prefix="bt-gui-status-"))
        self._status_path = folder / "status.json"
        self.run_page.set_running(True)
        self.set_status("Run started in a worker process. The window stays responsive.")
        self._supervisor.start(config["mode"], normalize(config), self._status_path)

    def cancel_run(self) -> None:
        self._supervisor.cancel()

    def open_results(self, path) -> None:
        if not path:
            return
        self.results_page.load_path(str(path))
        self.preferences.set("last_out", str(path))
        self.preferences.save()
        self.show_page(4)

    def _on_progress(self, payload: dict) -> None:
        self.run_page.on_progress(payload)
        message = payload.get("message")
        if message:
            self.set_status(str(message))

    def _on_finished(self, payload: dict) -> None:
        self.run_page.set_running(False)
        self.run_page.on_finished(payload)
        self.set_status("Run finished. Results are the report files.")

    def _on_failed(self, text: str) -> None:
        self.run_page.set_running(False)
        self.run_page.on_failed(text)
        self.set_status("Run failed. See the worker log. No metric was substituted.")

    def _on_cancelled(self) -> None:
        self.run_page.set_running(False)
        self.run_page.on_cancelled()
        self.set_status("Run cancelled.")

    def _restore(self) -> None:
        data = self.preferences.data
        self.data_page.set_root(data.get("data_root") or "")
        if data.get("last_src"):
            self.data_page.set_src(data["last_src"])
        if data.get("last_ticker"):
            self.data_page.set_ticker(data["last_ticker"])
        if data.get("registry_path"):
            self.data_page.set_registry(data["registry_path"])
            self.packs_page.registry.set_text(data["registry_path"])
        if data.get("last_strategy_dir"):
            self.strategies_page.path.set_text(data["last_strategy_dir"])
        if data.get("runs_root"):
            self.history_page.root_path.set_text(data["runs_root"])
        if data.get("last_out"):
            self.run_page.out.set_text(data["last_out"])
        if data.get("last_profile"):
            self.strategies_page.set_profile(data["last_profile"])
        self._refresh_chip()

    def _refresh_chip(self) -> None:
        root = self._public_text(self.data_page.root_path.text())
        self.root_chip.setText(root)
        self.root_chip.setToolTip(root)

    def _persist(self) -> None:
        self.preferences.set("data_root", self.data_page.root_path.text())
        self.preferences.set("registry_path", self.data_page.registry_path.text())
        self.preferences.set("runs_root", self.history_page.root_path.text())
        self.preferences.set("last_out", self.run_page.out.text())
        self.preferences.set("last_src", self.data_page.src_path.text())
        self.preferences.set("last_ticker", self.data_page.ticker.text().strip())
        self.preferences.set("last_profile", self._profile)
        self.preferences.set("last_strategy_dir", self.strategies_page.path.text())
        self.preferences.save()

    def closeEvent(self, event) -> None:
        if self._supervisor.running():
            self._supervisor.cancel()
        self._persist()
        super().closeEvent(event)
