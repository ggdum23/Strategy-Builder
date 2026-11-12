"""Browse past run directories. Opening one loads its report files."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from ..backend import scan_history
from ..jobs import failure_message
from ..widgets import DictTable, PageHeader, PathField, Section


class HistoryPage(QWidget):
    def __init__(self, host):
        super().__init__()
        self.host = host
        self._rows = []
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 16)
        root.setSpacing(12)
        root.addWidget(
            PageHeader(
                "06  History",
                "Past runs",
                "Folders that contain metrics.json or walkforward.json. "
                "Headline numbers are the ones those files stored. Design and locked legs of a walk-forward stay under the parent.",
            )
        )
        row = QHBoxLayout()
        self.root_path = PathField("Runs folder", "Scanned a few levels down. Market-data files are not opened.")
        scan = QPushButton("Scan")
        scan.setObjectName("primary")
        scan.clicked.connect(self.scan)
        row.addWidget(self.root_path, 1)
        row.addWidget(scan)
        root.addLayout(row)
        self.note = QLabel("")
        self.note.setObjectName("muted")
        root.addWidget(self.note)
        root.addWidget(Section("Runs"))
        self.table = DictTable()
        self.table.setObjectName("historyTable")
        self.table.doubleClicked.connect(self._open)
        root.addWidget(self.table, 1)

    def scan(self) -> None:
        path = self.root_path.text()

        def work():
            return scan_history(path)

        self.host.start_task(work, self.show_rows, self._failed)

    def show_rows(self, rows) -> None:
        self._rows = list(rows)
        records = []
        for item in self._rows:
            records.append(
                {
                    "modified": item.modified,
                    "kind": item.kind,
                    "profile": item.profile,
                    "symbol": item.symbol,
                    "total_return": item.total_return,
                    "sharpe": item.sharpe,
                    "max_drawdown": item.max_drawdown,
                    "final_equity": item.final_equity,
                    "disqualified": item.disqualified,
                    "strategy": item.strategy,
                    "path": item.path,
                }
            )
        columns = (
            "modified",
            "kind",
            "profile",
            "symbol",
            "total_return",
            "sharpe",
            "max_drawdown",
            "final_equity",
            "disqualified",
            "strategy",
            "path",
        )
        self.table.set_records(
            columns,
            records,
            {
                "modified": "Modified",
                "kind": "Kind",
                "profile": "Profile",
                "symbol": "Symbol",
                "total_return": "Total return",
                "sharpe": "Sharpe",
                "max_drawdown": "Max drawdown",
                "final_equity": "Final equity",
                "disqualified": "Disqualified",
                "strategy": "Strategy",
                "path": "Path",
            },
            {
                "total_return": "fraction",
                "sharpe": "ratio",
                "max_drawdown": "fraction",
                "final_equity": "money",
                "disqualified": "flag",
            },
        )
        self.note.setText(f"{len(records)} run folder(s). Double-click a row to open the report.")
        self.host.remember(runs_root=self.root_path.text())
        self.host.set_status(f"History: {len(records)} run(s)")

    def _open(self, index) -> None:
        row = self.table.source_row(index)
        if not 0 <= row < len(self.table.raw_rows()):
            return
        path = self.table.raw_rows()[row].get("path")
        if path:
            self.host.open_results(str(path))

    def _failed(self, failure) -> None:
        sentence = failure_message(failure)
        self.note.setText("scan failed" if sentence == "run failed" else sentence)
