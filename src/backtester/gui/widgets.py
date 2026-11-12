"""Shared controls. Layout and labels only."""

from __future__ import annotations

import math
import re

from PySide6.QtCore import QAbstractTableModel, Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from .format import MISSING, format_cell, format_number, sort_token


class PageHeader(QWidget):
    def __init__(self, kicker: str, title: str, lede: str):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        kick = QLabel(kicker.upper())
        kick.setObjectName("kicker")
        self.heading = QLabel(title)
        self.heading.setObjectName("pageTitle")
        body = QLabel(lede)
        body.setObjectName("muted")
        body.setWordWrap(True)
        layout.addWidget(kick)
        layout.addWidget(self.heading)
        layout.addWidget(body)

    def set_title(self, title: str) -> None:
        self.heading.setText(title)


class Banner(QFrame):
    def __init__(self, severity: str, text: str):
        super().__init__()
        self.setObjectName(f"banner-{severity}")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        self._label = QLabel(text)
        self._label.setWordWrap(True)
        self._label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self._label)

    def text(self) -> str:
        return self._label.text()


class PathField(QWidget):
    """A single path. Browse dialogs return one string; spaces are not split."""

    def __init__(self, label: str, placeholder: str = "", *, kind: str = "dir"):
        super().__init__()
        self.kind = kind
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        caption = QLabel(label)
        caption.setObjectName("muted")
        row = QHBoxLayout()
        row.setSpacing(6)
        self.edit = QLineEdit()
        self.edit.setPlaceholderText(placeholder)
        self.edit.setClearButtonEnabled(True)
        button = QPushButton("Browse")
        button.clicked.connect(self._browse)
        row.addWidget(self.edit, 1)
        row.addWidget(button)
        layout.addWidget(caption)
        layout.addLayout(row)

    def text(self) -> str:
        return self.edit.text().strip()

    def set_text(self, value: str) -> None:
        self.edit.setText(value)

    def _browse(self) -> None:
        current = self.text()
        if self.kind == "file":
            chosen, _ = QFileDialog.getOpenFileName(self, self.edit.placeholderText() or "Choose a file", current)
        elif self.kind == "save":
            chosen, _ = QFileDialog.getSaveFileName(self, "Choose a file", current)
        else:
            chosen = QFileDialog.getExistingDirectory(self, "Choose a folder", current)
        if chosen:
            self.edit.setText(chosen)


class MetricCard(QFrame):
    def __init__(self, title: str, tooltip: str, mono: str):
        super().__init__()
        self.setObjectName("card")
        self.setToolTip(tooltip)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(2)
        caption = QLabel(title)
        caption.setObjectName("muted")
        self.value = QLabel(MISSING)
        self.value.setObjectName("metricValue")
        font = QFont(mono)
        font.setPointSizeF(15)
        font.setWeight(QFont.Weight.DemiBold)
        self.value.setFont(font)
        self.value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(caption)
        layout.addWidget(self.value)
        self.setMinimumHeight(72)

    def set_value(self, text: str, *, tone: str | None = None) -> None:
        self.value.setText(text)
        if tone:
            self.value.setStyleSheet(f"color: {tone};")
        else:
            self.value.setStyleSheet("")


class Section(QLabel):
    def __init__(self, text: str):
        super().__init__(text)
        self.setObjectName("section")


def clear_layout(layout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.hide()
            widget.setParent(None)
            widget.deleteLater()
        child = item.layout()
        if child is not None:
            clear_layout(child)


class DictTable(QTableView):
    """Sortable table. Display text is formatted; sorting uses the raw value."""

    def __init__(self):
        super().__init__()
        self.setAlternatingRowColors(True)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setSortingEnabled(True)
        self.setWordWrap(False)
        self.verticalHeader().setVisible(False)
        header = self.horizontalHeader()
        header.setStretchLastSection(True)
        header.setDefaultSectionSize(108)
        header.setMinimumSectionSize(72)
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.setFont(QFont(self.font().family(), 9))
        self._model: DictModel | None = None

    def set_records(
        self,
        columns: tuple[str, ...] | list[str],
        rows: list[dict],
        titles: dict | None = None,
        kinds: dict | None = None,
        row_kinds: dict | None = None,
    ) -> None:
        titles = titles or {}
        model = DictModel(list(columns), list(rows), titles, kinds or {}, row_kinds or {})
        self._model = model
        sorting = self.isSortingEnabled()
        self.setSortingEnabled(False)
        self.setModel(model)
        self.setSortingEnabled(sorting)
        self.horizontalHeader().setSortIndicator(0, Qt.SortOrder.AscendingOrder)

    def set_filter(self, text: str) -> None:
        if self._model is not None:
            self._model.set_filter(text)

    def raw_rows(self) -> list[dict]:
        if self._model is None:
            return []
        return list(self._model.rows)

    def source_row(self, index) -> int:
        if self._model is None or not index.isValid():
            return -1
        return self._model.source_position(index.row())


class DictModel(QAbstractTableModel):
    """Filter and sort in Python. Qt's proxy compared the sort tuples and crashed on Windows."""

    def __init__(self, columns, rows, titles, kinds=None, row_kinds=None):
        super().__init__()
        self.columns = columns
        self.rows = rows
        self.titles = titles
        self.kinds = kinds or {}
        self.row_kinds = row_kinds or {}
        self._filter = ""
        self._sort_column = 0
        self._sort_order = Qt.SortOrder.AscendingOrder
        self._order: list[int] = []
        self._cache: list[dict] = []
        self._tokens: list[dict] = []
        self._blob: list[str] = []
        self._build_cache()
        self._recompute()

    def _build_cache(self) -> None:
        """Format each cell once. Filtering a large fill list must not format it again."""
        cache = []
        tokens = []
        blobs = []
        for record in self.rows:
            shown = {}
            token = {}
            parts = []
            for column in self.columns:
                value = record.get(column)
                text = self._display(column, value, record)
                shown[column] = text
                token[column] = sort_token(value)
                parts.append(text.casefold())
            cache.append(shown)
            tokens.append(token)
            blobs.append("\n".join(parts))
        self._cache = cache
        self._tokens = tokens
        self._blob = blobs

    def set_filter(self, text: str) -> None:
        self._filter = (text or "").casefold()
        self._reset()

    def sort(self, column: int, order=Qt.SortOrder.AscendingOrder) -> None:
        self._sort_column = column
        self._sort_order = order
        self._reset()

    def source_position(self, view_row: int) -> int:
        if not 0 <= view_row < len(self._order):
            return -1
        return self._order[view_row]

    def rowCount(self, parent=None):
        if parent is not None and parent.isValid():
            return 0
        return len(self._order)

    def columnCount(self, parent=None):
        if parent is not None and parent.isValid():
            return 0
        return len(self.columns)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal and 0 <= section < len(self.columns):
            key = self.columns[section]
            return self.titles.get(key, key)
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self._order):
            return None
        source = self._order[index.row()]
        key = self.columns[index.column()]
        if role == Qt.ItemDataRole.DisplayRole:
            return self._cache[source][key]
        if role == Qt.ItemDataRole.TextAlignmentRole:
            if self._tokens[source][key][0] == 1:
                return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.ToolTipRole:
            record = self.rows[source]
            extra = record.get(f"{key}_detail")
            if isinstance(extra, str) and extra.strip():
                return extra
            return self._cache[source][key]
        return None

    def flags(self, index):
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    def _display(self, key: str, value, record) -> str:
        kind = self.kinds.get(key)
        source = self.row_kinds.get(key)
        if source and record.get(source):
            kind = record.get(source)
        if kind:
            return format_number(kind, value)
        return format_cell(value)

    def _matches(self, index: int) -> bool:
        if not self._filter:
            return True
        return self._filter in self._blob[index]

    def _recompute(self) -> None:
        indices = [index for index in range(len(self.rows)) if self._matches(index)]
        if 0 <= self._sort_column < len(self.columns):
            key = self.columns[self._sort_column]
            indices.sort(
                key=lambda index: self._tokens[index][key],
                reverse=self._sort_order == Qt.SortOrder.DescendingOrder,
            )
        self._order = indices

    def _reset(self) -> None:
        self.beginResetModel()
        self._recompute()
        self.endResetModel()


_MONTH = re.compile(r"\d{4}-\d{2}")
_YEAR = re.compile(r"\d{4}")


def period_sections(payload) -> list[tuple[str, dict]]:
    """Split a stored ``period_returns`` object into blocks, months, and years."""
    monthly: dict = {}
    yearly: dict = {}
    blocks: dict = {}

    def take(key, value, hint: str = "") -> None:
        if isinstance(value, dict):
            child_hint = str(key).lower()
            for child_key, child in value.items():
                take(child_key, child, child_hint)
            return
        label = str(key)
        if hint in {"monthly", "month", "months"} or _MONTH.fullmatch(label):
            monthly[label] = value
        elif hint in {"yearly", "year", "years", "annual"} or _YEAR.fullmatch(label):
            yearly[label] = value
        else:
            blocks[label] = value

    if isinstance(payload, dict):
        for key, value in payload.items():
            take(key, value)
    sections = []
    if blocks:
        sections.append(("Session blocks", blocks))
    if monthly:
        sections.append(("Monthly", monthly))
    if yearly:
        sections.append(("Yearly", yearly))
    return sections


class PeriodHeat(QWidget):
    """Color is the sign of the stored return. It is not a new metric."""

    def __init__(self):
        super().__init__()
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(6)
        self.setObjectName("periodHeat")

    def clear(self) -> None:
        clear_layout(self._layout)

    def set_periods(self, payload, theme) -> None:
        clear_layout(self._layout)
        sections = period_sections(payload)
        if not sections:
            note = QLabel("No period_returns object in this report.")
            note.setObjectName("muted")
            self._layout.addWidget(note)
            return
        for title, mapping in sections:
            self._layout.addWidget(Section(title))
            grid = QGridLayout()
            grid.setSpacing(4)
            columns = 6
            for index, (label, value) in enumerate(mapping.items()):
                cell = QLabel(f"{label}\n{format_number('fraction', value)}")
                cell.setAlignment(Qt.AlignmentFlag.AlignCenter)
                cell.setToolTip("Stored period return. This window did not recompute it.")
                cell.setMinimumWidth(76)
                try:
                    number = float(value)
                except (TypeError, ValueError):
                    number = float("nan")
                if math.isfinite(number) and number > 0:
                    cell.setStyleSheet(f"background: {theme.ok_bg}; color: {theme.positive}; padding: 6px;")
                elif math.isfinite(number) and number < 0:
                    cell.setStyleSheet(f"background: {theme.danger_bg}; color: {theme.negative}; padding: 6px;")
                else:
                    cell.setStyleSheet(f"background: {theme.elevated}; color: {theme.muted}; padding: 6px;")
                grid.addWidget(cell, index // columns, index % columns)
            holder = QWidget()
            holder.setLayout(grid)
            self._layout.addWidget(holder)
