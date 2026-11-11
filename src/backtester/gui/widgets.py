"""Shared controls. Layout and labels only."""

from __future__ import annotations

from PySide6.QtCore import QAbstractTableModel, Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QFrame,
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
        heading = QLabel(title)
        heading.setObjectName("pageTitle")
        body = QLabel(lede)
        body.setObjectName("muted")
        body.setWordWrap(True)
        layout.addWidget(kick)
        layout.addWidget(heading)
        layout.addWidget(body)


class Banner(QFrame):
    def __init__(self, severity: str, text: str):
        super().__init__()
        self.setObjectName(f"banner-{severity}")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        label = QLabel(text)
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(label)


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
    ) -> None:
        titles = titles or {}
        model = DictModel(list(columns), list(rows), titles, kinds or {})
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

    def __init__(self, columns, rows, titles, kinds=None):
        super().__init__()
        self.columns = columns
        self.rows = rows
        self.titles = titles
        self.kinds = kinds or {}
        self._filter = ""
        self._sort_column = 0
        self._sort_order = Qt.SortOrder.AscendingOrder
        self._order: list[int] = []
        self._recompute()

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
        record = self.rows[self._order[index.row()]]
        key = self.columns[index.column()]
        value = record.get(key)
        if role == Qt.ItemDataRole.DisplayRole:
            return self._display(key, value)
        if role == Qt.ItemDataRole.TextAlignmentRole:
            token = sort_token(value)
            if token[0] == 1:
                return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.ToolTipRole:
            return "" if value is None else str(value)
        return None

    def flags(self, index):
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    def _display(self, key: str, value) -> str:
        kind = self.kinds.get(key)
        if kind:
            return format_number(kind, value)
        return format_cell(value)

    def _matches(self, record: dict) -> bool:
        if not self._filter:
            return True
        for key in self.columns:
            if self._filter in self._display(key, record.get(key)).casefold():
                return True
        return False

    def _recompute(self) -> None:
        indices = [index for index, record in enumerate(self.rows) if self._matches(record)]
        if 0 <= self._sort_column < len(self.columns):
            key = self.columns[self._sort_column]
            indices.sort(
                key=lambda index: sort_token(self.rows[index].get(key)),
                reverse=self._sort_order == Qt.SortOrder.DescendingOrder,
            )
        self._order = indices

    def _reset(self) -> None:
        self.beginResetModel()
        self._recompute()
        self.endResetModel()
