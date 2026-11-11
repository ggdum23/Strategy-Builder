"""Dashboard over report files. Numbers are read, not recomputed."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..backend import RunReport, chart_overlay, drawdown_line, exposure_line, load_run
from ..charts import ChartPanel
from ..format import MISSING, format_number, is_missing
from ..schema import ACTIVITY_KEYS, DESK_KEYS, HEADLINE_KEYS, SPECS
from ..widgets import Banner, DictTable, MetricCard, PageHeader, PathField, Section, clear_layout


class ResultsPage(QWidget):
    def __init__(self, host):
        super().__init__()
        self.host = host
        self._report: RunReport | None = None
        self._cards: dict[str, MetricCard] = {}
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 16)
        root.setSpacing(10)
        root.addWidget(
            PageHeader(
                "05  Results",
                "Operator report",
                "Figures come from the run directory. Tooltips name the unit and the annualization. "
                "A dash means the file did not contain that field.",
            )
        )
        bar = QHBoxLayout()
        self.path = PathField("Run directory", "Folder that contains metrics.json, or a walk-forward, or a batch")
        load = QPushButton("Load")
        load.setObjectName("primary")
        load.clicked.connect(self.load_current)
        export = QPushButton("Export copy")
        export.clicked.connect(self.export_current)
        bar.addWidget(self.path, 1)
        bar.addWidget(load)
        bar.addWidget(export)
        root.addLayout(bar)
        self.banners = QVBoxLayout()
        root.addLayout(self.banners)
        self.leg = QComboBox()
        self.leg.setObjectName("legCombo")
        self.leg.currentIndexChanged.connect(self._leg_changed)
        self.leg.setVisible(False)
        root.addWidget(self.leg)
        self.tabs = QTabWidget()
        self.overview = QWidget()
        self.fills_tab = QWidget()
        self.desk_tab = QWidget()
        self.trades_tab = QWidget()
        self.figure_tab = QWidget()
        self.tabs.addTab(self.overview, "Overview")
        self.tabs.addTab(self.fills_tab, "Fills")
        self.tabs.addTab(self.desk_tab, "Desk risk")
        self.tabs.addTab(self.trades_tab, "Round trips")
        self.tabs.addTab(self.figure_tab, "Saved figure")
        root.addWidget(self.tabs, 1)
        for tab in (self.overview, self.fills_tab, self.desk_tab, self.trades_tab, self.figure_tab):
            tab.setObjectName("page")
            tab.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._build_overview()
        self._build_fills()
        self._build_desk()
        self._build_trades()
        self._build_figure()

    def _build_overview(self) -> None:
        body = QWidget()
        body.setObjectName("page")
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 8, 8, 8)
        self.identity = QLabel("")
        self.identity.setWordWrap(True)
        self.cost_line = QLabel("")
        self.cost_line.setWordWrap(True)
        self.cost_line.setObjectName("muted")
        self.cards_grid = QGridLayout()
        self.cards_grid.setSpacing(8)
        self.chart = ChartPanel()
        self.chart_mode = QComboBox()
        self.chart_mode.addItem("Equity", "equity")
        self.chart_mode.addItem("Drawdown (picture of the equity column)", "drawdown")
        self.chart_mode.addItem("Exposure", "exposure")
        self.chart_mode.currentIndexChanged.connect(self._redraw)
        self.benchmarks = DictTable()
        self.benchmarks.setMinimumHeight(140)
        self.other = DictTable()
        self.other.setMinimumHeight(100)
        self.leaderboard = DictTable()
        self.leaderboard.setObjectName("leaderboard")
        self.leaderboard.setMinimumHeight(120)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter the leaderboard")
        self.filter.textChanged.connect(self.leaderboard.set_filter)
        layout.addWidget(self.identity)
        layout.addWidget(self.cost_line)
        layout.addLayout(self.cards_grid)
        layout.addWidget(self.chart_mode)
        layout.addWidget(self.chart, 1)
        layout.addWidget(Section("Benchmarks"))
        layout.addWidget(self.benchmarks)
        layout.addWidget(Section("Leaderboard"))
        layout.addWidget(self.filter)
        layout.addWidget(self.leaderboard)
        layout.addWidget(Section("Other reported fields"))
        layout.addWidget(self.other)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(body)
        outer = QVBoxLayout(self.overview)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)

    def _build_fills(self) -> None:
        layout = QVBoxLayout(self.fills_tab)
        self.fills_note = QLabel("")
        self.fills_note.setObjectName("muted")
        self.fills_filter = QLineEdit()
        self.fills_filter.setObjectName("fillsFilter")
        self.fills_filter.setPlaceholderText("Filter fills")
        self.fills = DictTable()
        self.fills.setObjectName("fillsTable")
        self.fills_filter.textChanged.connect(self.fills.set_filter)
        layout.addWidget(self.fills_note)
        layout.addWidget(self.fills_filter)
        layout.addWidget(self.fills, 1)

    def _build_desk(self) -> None:
        layout = QVBoxLayout(self.desk_tab)
        self.desk_banners = QVBoxLayout()
        self.desk_grid = QGridLayout()
        self.desk_cards: dict[str, MetricCard] = {}
        self.postmortem = QPlainTextEdit()
        self.postmortem.setObjectName("mono")
        self.postmortem.setReadOnly(True)
        self.postmortem.setMinimumHeight(220)
        layout.addLayout(self.desk_banners)
        layout.addLayout(self.desk_grid)
        layout.addWidget(Section("Liquidation post-mortem"))
        layout.addWidget(self.postmortem, 1)

    def _build_trades(self) -> None:
        layout = QVBoxLayout(self.trades_tab)
        self.trades_note = QLabel("")
        self.trades_note.setObjectName("muted")
        self.trades = DictTable()
        layout.addWidget(self.trades_note)
        layout.addWidget(self.trades, 1)

    def _build_figure(self) -> None:
        layout = QVBoxLayout(self.figure_tab)
        self.figure_note = QLabel("")
        self.figure_note.setWordWrap(True)
        self.figure_label = QLabel("")
        self.figure_label.setAlignment(Qt_alignment())
        layout.addWidget(self.figure_note)
        layout.addWidget(self.figure_label, 1)

    def load_path(self, path: str) -> None:
        self.path.set_text(path or "")
        self.show_report(load_run(path))

    def load_current(self) -> None:
        self.load_path(self.path.text())

    def show_report(self, report: RunReport) -> None:
        self._report = report
        clear_layout(self.banners)
        for warning in report.warnings:
            severity = "error" if "disagree" in warning or "could not" in warning else "warn"
            self.banners.addWidget(Banner(severity, warning))
        if report.missing:
            self.banners.addWidget(
                Banner("info", "Not in this report, so left blank: " + ", ".join(report.missing) + ".")
            )
        if report.kind == "walkforward":
            self.leg.blockSignals(True)
            self.leg.clear()
            for child in report.children:
                self.leg.addItem(child["leg"], child["leg"])
            self.leg.setVisible(self.leg.count() > 0)
            self.leg.blockSignals(False)
        else:
            self.leg.setVisible(False)
            self.leg.clear()
        self._render_active()

    def metric_text(self, key: str) -> str:
        card = self._cards.get(key)
        if card is None:
            return ""
        return card.value.text()

    def _active_book(self) -> RunReport | None:
        report = self._report
        if report is None:
            return None
        if report.kind == "walkforward":
            leg = self.leg.currentData()
            for child in report.children:
                if child.get("leg") == leg:
                    return child.get("report")
            return None
        if report.kind == "book":
            return report
        return None

    def _leg_changed(self) -> None:
        self._render_active()

    def _render_active(self) -> None:
        report = self._report
        book = self._active_book()
        if report is None:
            return
        if report.kind == "batch":
            self.identity.setText(f"Batch  {report.directory}")
            self._set_cards({})
            self.chart.set_lines([], "equity", [], "A batch has one report per strategy. Open a row from History, or a child folder.")
            source = report.leaderboard.source if report.leaderboard else ""
            self.cost_line.setText(source)
            if report.leaderboard:
                self.leaderboard.set_records(report.leaderboard.columns, list(report.leaderboard.rows))
            else:
                self.leaderboard.set_records(("strategy",), [])
            self._show_failures(report)
            self._clear_book_tables()
            return
        if book is None:
            self.identity.setText(report.kind)
            self._set_cards({})
            return
        metrics = book.metrics
        symbol = metrics.get("symbol")
        profile = metrics.get("profile")
        parts = [str(part) for part in (profile, symbol, f"{metrics.get('bar_minutes')} min" if "bar_minutes" in metrics else None) if part not in (None, "")]
        if report.kind == "walkforward" and report.walkforward:
            summary = report.walkforward
            parts.append(
                f"design through {summary.get('design_end_date')} ({summary.get('design_sessions')} sessions), "
                f"locked from {summary.get('locked_start_date')} ({summary.get('locked_sessions')} sessions), "
                f"warm-up {summary.get('warmup_rule')}"
            )
        self.identity.setText("   ".join(parts) if parts else "Report has no profile or symbol field.")
        basis = book.cost_basis or {}
        if basis:
            self.cost_line.setText(
                "Cost basis from the report: "
                f"share_scale={basis.get('share_scale', MISSING)}  "
                f"schedule={basis.get('cost_schedule', MISSING)}  "
                f"per-share={basis.get('per_share_fees', MISSING)}.  "
                f"{basis.get('note') or ''}"
            )
            self.cost_line.setToolTip("This is the invoice record the runner wrote. The window did not reprice the book.")
        else:
            self.cost_line.setText("No cost_basis object in this report.")
        shown = {}
        for key in HEADLINE_KEYS + tuple(key for key in ACTIVITY_KEYS if key in metrics):
            shown[key] = metrics.get(key) if key in metrics else None
        self._set_cards(shown)
        self._fill_benchmarks(book)
        self._fill_other(book)
        self._fill_fills(book)
        self._fill_trades(book)
        self._fill_desk(book)
        self._fill_figure(book)
        if report.leaderboard and report.kind != "book":
            self.leaderboard.set_records(report.leaderboard.columns, list(report.leaderboard.rows))
        else:
            self.leaderboard.set_records(("note",), [{"note": "Leaderboard is a batch or a leaderboard.csv. This view is one book."}])
        self._redraw()
        if metrics.get("disqualified") is True:
            self.identity.setText("DISQUALIFIED.  " + self.identity.text())

    def _show_failures(self, report: RunReport) -> None:
        for child in report.children:
            if child.get("state") != "ok":
                self.banners.addWidget(
                    Banner("error", f"{child.get('name')}: {child.get('state')}  {child.get('error') or ''}".strip())
                )

    def _set_cards(self, values: dict) -> None:
        clear_layout(self.cards_grid)
        self._cards = {}
        theme = self.host.theme
        columns = 5
        for index, key in enumerate(values):
            spec = SPECS.get(key)
            title = spec.label if spec else key
            tooltip = spec.tooltip if spec else "Reported field. This window does not define a second formula for it."
            kind = spec.kind if spec else "text"
            card = MetricCard(title, tooltip, theme.mono)
            text = format_number(kind, values[key]) if spec else (MISSING if is_missing(values[key]) else str(values[key]))
            tone = None
            if not is_missing(values[key]) and kind in {"fraction", "money", "ratio"}:
                try:
                    number = float(values[key])
                    if number < 0:
                        tone = theme.negative
                    elif number > 0 and key in {"total_return", "cagr"}:
                        tone = theme.positive
                except (TypeError, ValueError):
                    tone = None
            if key == "disqualified" and values[key] is True:
                tone = theme.negative
            card.set_value(text, tone=tone)
            self._cards[key] = card
            self.cards_grid.addWidget(card, index // columns, index % columns)

    def _fill_benchmarks(self, book: RunReport) -> None:
        columns = ("book", "total_return", "cagr", "sharpe", "max_drawdown", "calmar", "final_equity", "fees_total", "n_fills")
        rows = [_benchmark_row("strategy", book.metrics)]
        for label, metrics in book.benchmarks.items():
            rows.append(_benchmark_row(label, metrics))
        titles = {key: SPECS[key].label for key in columns if key in SPECS}
        titles["book"] = "Book"
        kinds = {key: SPECS[key].kind for key in columns if key in SPECS}
        self.benchmarks.set_records(columns, rows, titles, kinds)

    def _fill_other(self, book: RunReport) -> None:
        rows = [{"field": key, "value": value} for key, value in sorted(book.other_fields.items())]
        self.other.set_records(("field", "value"), rows)

    def _fill_fills(self, book: RunReport) -> None:
        if book.fills is None:
            self.fills.set_records(("fills",), [])
            self.fills_note.setText("No fills file in this run.")
            return
        self.fills.set_records(book.fills.columns, list(book.fills.rows))
        self.fills_note.setText(f"{len(book.fills.rows)} rows from {book.fills.source}. Sort by any column. The filter matches the displayed text.")

    def _fill_trades(self, book: RunReport) -> None:
        if book.episodes is None:
            self.trades.set_records(("round trips",), [])
            self.trades_note.setText("No round-trip file in this run.")
            return
        self.trades.set_records(book.episodes.columns, list(book.episodes.rows))
        self.trades_note.setText(f"{len(book.episodes.rows)} rows from {book.episodes.source}.")

    def _fill_desk(self, book: RunReport) -> None:
        clear_layout(self.desk_banners)
        clear_layout(self.desk_grid)
        self.desk_cards = {}
        metrics = book.metrics
        theme = self.host.theme
        interesting = [key for key in DESK_KEYS if key in metrics or metrics.get("profile") == "desk"]
        if not interesting:
            self.desk_banners.addWidget(Banner("info", "This report has no Desk risk fields."))
            self.postmortem.setPlainText("")
            return
        if metrics.get("disqualified") is True:
            self.desk_banners.addWidget(Banner("error", "DISQUALIFIED"))
        elif "disqualified" not in metrics:
            self.desk_banners.addWidget(Banner("warn", "disqualified is not in this report. It was not assumed to be false."))
        for index, key in enumerate(DESK_KEYS):
            if key not in metrics and metrics.get("profile") != "desk":
                continue
            spec = SPECS[key]
            card = MetricCard(spec.label, spec.tooltip, theme.mono)
            card.set_value(format_number(spec.kind, metrics.get(key) if key in metrics else None))
            self.desk_cards[key] = card
            self.desk_grid.addWidget(card, index // 4, index % 4)
        if book.liquidation:
            body = json.dumps(book.liquidation, indent=2, default=str)
            source = book.liquidation_source or "report"
            self.postmortem.setPlainText(f"source: {source}\n\n{body}")
        elif metrics.get("disqualified") is True:
            self.postmortem.setPlainText("DISQUALIFIED is set and this report has no liquidation object. Nothing was invented.")
        else:
            self.postmortem.setPlainText("No liquidation post-mortem in this report.")

    def _fill_figure(self, book: RunReport) -> None:
        if book.figure is None or not book.figure.is_file():
            self.figure_label.setPixmap(QPixmap())
            self.figure_label.setText("")
            self.figure_note.setText("equity.png is not in this run directory. The interactive chart is drawn from the equity CSV.")
            return
        pixmap = QPixmap(str(book.figure))
        self.figure_note.setText(f"Saved figure: {book.figure.name}. The interactive chart is separate and uses the CSV.")
        if pixmap.isNull():
            self.figure_label.setText(f"Could not read {book.figure.name}")
        else:
            self.figure_label.setPixmap(
                pixmap.scaledToWidth(960, Qt.TransformationMode.SmoothTransformation)
            )

    def _clear_book_tables(self) -> None:
        self.benchmarks.set_records(("book",), [])
        self.other.set_records(("field",), [])
        self.fills.set_records(("fills",), [])
        self.trades.set_records(("round trips",), [])
        self.postmortem.setPlainText("")

    def _redraw(self) -> None:
        book = self._active_book()
        theme = self.host.theme
        self.chart.apply_theme(theme)
        if book is None or not book.curves:
            self.chart.set_lines([], "USD", [], "No equity file in this run.")
            return
        mode = self.chart_mode.currentData()
        sessions = book.curves[0].sessions
        if mode == "drawdown":
            line = drawdown_line(book)
            if line is None:
                self.chart.set_lines([], "drawdown", [], "")
                return
            xs, ys, note = line
            self.chart.set_lines(
                [("drawdown", xs, ys, theme.drawdown)],
                "drawdown %",
                sessions,
                note,
                percent=True,
            )
            return
        if mode == "exposure":
            line = exposure_line(book)
            if line is None:
                self.chart.set_lines([], "exposure", sessions, "This equity file has no exposure or position column.")
                return
            xs, ys = line
            self.chart.set_lines(
                [("exposure", xs, ys, theme.exposure)],
                "exposure",
                sessions,
                "Column copied from the equity file. Not recomputed from shares and prices.",
            )
            return
        lines, note = chart_overlay(book)
        colors = {
            "Strategy": theme.strategy,
            "1× buy and hold": theme.bench,
            "2× long": theme.bench2,
        }
        painted = []
        for name, xs, ys in lines:
            painted.append((name, xs, ys, colors.get(name, theme.strategy)))
        caption = "Equity from the report files. " + note
        self.chart.set_lines(painted, "USD", sessions, caption)

    def export_current(self) -> None:
        from PySide6.QtWidgets import QFileDialog

        chosen = QFileDialog.getExistingDirectory(self, "Export a copy of the report", self.path.text())
        if chosen:
            self.export_to(chosen)

    def export_to(self, dest) -> list[str]:
        """Copy original report files and a display sheet of the values already shown."""
        book = self._active_book()
        target = Path(dest)
        target.mkdir(parents=True, exist_ok=True)
        written = []
        if book is not None:
            folder = book.directory
            for name in ("metrics.json", "equity_curve.csv", "equity.csv", "fills.csv", "trades.csv", "equity.png"):
                source = folder / name
                if source.is_file():
                    shutil.copy2(source, target / name)
                    written.append(name)
            rows = ["key,raw,display"]
            for key, card in self._cards.items():
                raw = book.metrics.get(key) if key in book.metrics else ""
                rows.append(f"{key},{_csv(raw)},{_csv(card.value.text())}")
            sheet = target / "displayed_metrics.csv"
            sheet.write_text("\n".join(rows) + "\n", encoding="utf-8")
            written.append(sheet.name)
        report = self._report
        if report is not None and report.leaderboard is not None:
            board = target / "leaderboard_view.csv"
            columns = list(report.leaderboard.columns)
            lines = [",".join(columns)]
            for row in report.leaderboard.rows:
                lines.append(",".join(_csv(row.get(column)) for column in columns))
            board.write_text("\n".join(lines) + "\n", encoding="utf-8")
            written.append(board.name)
        self.host.set_status("Exported " + ", ".join(written) if written else "Nothing to export")
        return written


def _benchmark_row(label: str, metrics: dict) -> dict:
    row = {"book": label}
    for key in ("total_return", "cagr", "sharpe", "max_drawdown", "calmar", "final_equity", "fees_total", "n_fills"):
        row[key] = metrics.get(key) if isinstance(metrics, dict) and key in metrics else None
    return row


def _csv(value) -> str:
    text = "" if value is None else str(value)
    if any(mark in text for mark in (",", '"', "\n")):
        return '"' + text.replace('"', '""') + '"'
    return text


def Qt_alignment():
    return Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
