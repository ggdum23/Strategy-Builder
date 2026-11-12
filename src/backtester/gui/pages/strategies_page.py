"""Pick strategy files and read the profile rules before anything runs."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..discover import discover_strategies, summarize_strategy
from ..jobs import failure_message
from ..disclosures import profile_rows
from ..widgets import Banner, PageHeader, PathField, Section, clear_layout


class StrategiesPage(QWidget):
    def __init__(self, host):
        super().__init__()
        self.host = host
        self._rows: list[dict] = []
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 16)
        root.setSpacing(12)
        root.addWidget(
            PageHeader(
                "03  Strategies",
                "Files and profile rules",
                "A file or a folder of strategy.py modules. The list is an AST check, not an import. "
                "Profile rules below are the engine's rules. Changing them here does not change the engine.",
            )
        )
        splitter = QSplitter()
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 8, 0)
        self.path = PathField("Strategy file or folder", "strategy.py, or a folder that contains them")
        buttons = QHBoxLayout()
        scan = QPushButton("Scan")
        scan.setObjectName("primary")
        scan.clicked.connect(self.scan)
        check_all = QPushButton("Check all")
        check_all.clicked.connect(lambda: self._set_all(Qt.CheckState.Checked))
        clear = QPushButton("Clear checks")
        clear.clicked.connect(lambda: self._set_all(Qt.CheckState.Unchecked))
        buttons.addWidget(scan)
        buttons.addWidget(check_all)
        buttons.addWidget(clear)
        self.files = QListWidget()
        self.files.setObjectName("strategyList")
        self.files.itemChanged.connect(lambda _item: self._emit())
        self.files.currentItemChanged.connect(self._show_current)
        left_layout.addWidget(self.path)
        left_layout.addLayout(buttons)
        left_layout.addWidget(self.files, 1)
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(8, 0, 0, 0)
        self.profile = QComboBox()
        self.profile.setObjectName("profileCombo")
        self.profile.addItem("Select a profile", "")
        self.profile.addItem("Arena  ·  clamp ±2, next delivered bar", "arena")
        self.profile.addItem("Desk  ·  clamp ±3, next regular open", "desk")
        self.profile.currentIndexChanged.connect(self._profile_changed)
        self.banners = QVBoxLayout()
        self.disclosure = QWidget()
        self.disclosure_layout = QVBoxLayout(self.disclosure)
        self.disclosure_layout.setContentsMargins(0, 0, 0, 0)
        self.excerpt = QTextEdit()
        self.excerpt.setReadOnly(True)
        self.excerpt.setObjectName("mono")
        self.excerpt.setPlaceholderText("Designer PROFILE.md, when one sits next to strategy.py. Not an official report.")
        right_layout.addWidget(QLabel("Profile"))
        right_layout.addWidget(self.profile)
        right_layout.addLayout(self.banners)
        right_layout.addWidget(Section("What this profile will do"))
        right_layout.addWidget(self.disclosure, 1)
        right_layout.addWidget(Section("Designer note"))
        right_layout.addWidget(self.excerpt, 1)
        right.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        right_scroll = QScrollArea()
        right_scroll.setWidgetResizable(True)
        right_scroll.setFrameShape(QFrame.Shape.NoFrame)
        right_scroll.setWidget(right)
        splitter.addWidget(left)
        splitter.addWidget(right_scroll)
        splitter.setSizes([420, 900])
        root.addWidget(splitter, 1)
        self._profile_changed()

    def scan(self) -> None:
        path = self.path.text()

        def work():
            return [summarize_strategy(item) for item in discover_strategies(path)]

        self.host.start_task(work, self.set_rows, self._failed)

    def set_rows(self, rows: list[dict]) -> None:
        self._rows = list(rows)
        self.files.blockSignals(True)
        self.files.clear()
        for row in self._rows:
            label = row.get("name") or row.get("path")
            if not row.get("has_strategy"):
                label = f"{label}  — no Strategy class"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, row.get("path"))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            tip = row.get("path") or ""
            if row.get("error"):
                tip = f"{tip}\n{row['error']}"
            extended = row.get("wants_extended_hours")
            if extended is True:
                tip += "\nWANTS_EXTENDED_HOURS = True"
            elif extended is False:
                tip += "\nWANTS_EXTENDED_HOURS = False"
            item.setToolTip(tip)
            self.files.addItem(item)
        self.files.blockSignals(False)
        if self.files.count():
            self.files.setCurrentRow(0)
        self._emit()
        self.host.remember(last_strategy_dir=self.path.text())
        self.host.set_status(f"{len(self._rows)} strategy file(s) scanned, none imported")

    def set_profile(self, name: str) -> None:
        index = self.profile.findData(name)
        if index >= 0 and index != self.profile.currentIndex():
            self.profile.setCurrentIndex(index)
        else:
            self._render_disclosure(name)

    def selected_paths(self) -> list[str]:
        paths = []
        for index in range(self.files.count()):
            item = self.files.item(index)
            if item.checkState() == Qt.CheckState.Checked:
                paths.append(str(item.data(Qt.ItemDataRole.UserRole)))
        return paths

    def check_paths(self, paths: list[str]) -> None:
        wanted = set(paths)
        self.files.blockSignals(True)
        for index in range(self.files.count()):
            item = self.files.item(index)
            item.setCheckState(
                Qt.CheckState.Checked if str(item.data(Qt.ItemDataRole.UserRole)) in wanted else Qt.CheckState.Unchecked
            )
        self.files.blockSignals(False)
        self._emit()

    def disclosure_text(self) -> str:
        chunks = []
        for index in range(self.disclosure_layout.count()):
            widget = self.disclosure_layout.itemAt(index).widget()
            if isinstance(widget, QLabel):
                chunks.append(widget.text())
        return "\n".join(chunks)

    def _set_all(self, state) -> None:
        self.files.blockSignals(True)
        for index in range(self.files.count()):
            self.files.item(index).setCheckState(state)
        self.files.blockSignals(False)
        self._emit()

    def _emit(self) -> None:
        self.host.on_strategies_changed(self.selected_paths())

    def _show_current(self, current, _previous) -> None:
        if current is None:
            self.excerpt.clear()
            return
        path = current.data(Qt.ItemDataRole.UserRole)
        row = next((item for item in self._rows if item.get("path") == path), None)
        if row is None:
            self.excerpt.clear()
            return
        parts = [row.get("path") or ""]
        if row.get("error"):
            parts.append(row["error"])
        if row.get("wants_extended_hours") is not None:
            parts.append(f"WANTS_EXTENDED_HOURS = {row['wants_extended_hours']}")
        excerpt = row.get("profile_excerpt")
        if excerpt:
            parts.append("")
            parts.append("PROFILE.md (designer note, not this run's report)")
            parts.append(excerpt)
        self.excerpt.setPlainText("\n".join(parts))

    def _profile_changed(self) -> None:
        name = self.profile.currentData()
        self._render_disclosure(name)
        self.host.on_profile_changed(name or "")

    def _render_disclosure(self, name: str) -> None:
        clear_layout(self.disclosure_layout)
        clear_layout(self.banners)
        if not name:
            self.banners.addWidget(Banner("warn", "No profile selected. A run stays blocked until you choose Arena or Desk."))
            return
        for label, body in profile_rows(name):
            title = QLabel(label)
            title.setObjectName("section")
            text = QLabel(body)
            text.setWordWrap(True)
            text.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
            self.disclosure_layout.addWidget(title)
            self.disclosure_layout.addWidget(text)
        self.disclosure_layout.addStretch(1)

    def _failed(self, failure) -> None:
        clear_layout(self.banners)
        sentence = failure_message(failure)
        if sentence == "run failed":
            sentence = "scan failed"
        self.banners.addWidget(Banner("error", sentence))
        self.host.set_status("Strategy scan failed")
