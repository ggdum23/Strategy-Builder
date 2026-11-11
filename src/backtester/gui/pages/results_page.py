"""Dashboard over report files. Numbers are read, not recomputed."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
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

from ..audience import export_allowed, peek_scale, peek_symbols
from ..backend import RunReport, chart_overlay, drawdown_line, exposure_line, load_run
from ..charts import ChartPanel
from ..format import MISSING, format_number, is_missing
from ..schema import ACTIVITY_KEYS, DESK_KEYS, KPI_KEYS, SPECS, leaderboard_kind
from ..widgets import Banner, DictTable, MetricCard, PageHeader, PathField, PeriodHeat, Section, clear_layout


class ResultsPage(QWidget):
    def __init__(self, host):
        super().__init__()
        self.host = host
        self._report: RunReport | None = None
        self._cards: dict[str, MetricCard] = {}
        self._loading = False
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 16)
        root.setSpacing(10)
        root.addWidget(
            PageHeader(
                "05  Results",
                "Operator report",
                "Headline figures, then the equity curve and its drawdown picture, then the tables. "
                "An anonymized book opens as a designer view: ticker, calendar, and invoice scale stay off the screen.",
            )
        )
        bar = QHBoxLayout()
        self.path = PathField("Run directory", "Folder that contains metrics.json, or a walk-forward, or a batch")
        self.audience = QComboBox()
        self.audience.setObjectName("audienceCombo")
        self.audience.addItem("Designer", "designer")
        self.audience.addItem("Operator", "operator")
        self.audience.currentIndexChanged.connect(self._audience_changed)
        load = QPushButton("Load")
        load.setObjectName("primary")
        load.clicked.connect(self.load_current)
        export = QPushButton("Export copy")
        export.clicked.connect(self.export_current)
        bar.addWidget(self.path, 1)
        bar.addWidget(self.audience)
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
        layout.setSpacing(8)
        self.identity = QLabel("")
        self.identity.setWordWrap(True)
        self.cost_line = QLabel("")
        self.cost_line.setWordWrap(True)
        self.cost_line.setObjectName("muted")
        self.cards_grid = QGridLayout()
        self.cards_grid.setSpacing(8)
        self.chart = ChartPanel()
        self.activity_title = Section("Activity")
        self.activity = DictTable()
        self.activity.setMinimumHeight(88)
        self.benchmark_title = Section("Benchmarks")
        self.benchmarks = DictTable()
        self.benchmarks.setMinimumHeight(96)
        self.fees_title = Section("Fees")
        self.fees = DictTable()
        self.fees.setMinimumHeight(72)
        self.period_title = Section("Period returns")
        self.periods = PeriodHeat()
        self.jobs_title = Section("Jobs")
        self.jobs = DictTable()
        self.jobs.setObjectName("jobsTable")
        self.jobs.setMinimumHeight(72)
        self.leaderboard_title = Section("Leaderboard")
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter the leaderboard")
        self.leaderboard = DictTable()
        self.leaderboard.setObjectName("leaderboard")
        self.leaderboard.setMinimumHeight(120)
        self.filter.textChanged.connect(self.leaderboard.set_filter)
        self.other_title = Section("Other reported fields")
        self.other = DictTable()
        self.other.setMinimumHeight(72)
        layout.addWidget(self.identity)
        layout.addWidget(self.cost_line)
        layout.addLayout(self.cards_grid)
        layout.addWidget(self.chart)
        for widget in (
            self.activity_title,
            self.activity,
            self.benchmark_title,
            self.benchmarks,
            self.fees_title,
            self.fees,
            self.period_title,
            self.periods,
            self.jobs_title,
            self.jobs,
            self.leaderboard_title,
            self.filter,
            self.leaderboard,
            self.other_title,
            self.other,
        ):
            layout.addWidget(widget)
        layout.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(body)
        self.overview_scroll = scroll
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

    def load_path(self, path: str, audience: str | None = None) -> None:
        self.path.set_text(path or "")
        chosen = audience or "auto"
        report = load_run(path, audience=chosen)
        self._set_audience_combo(report.audience)
        self.show_report(report)

    def load_current(self) -> None:
        self.load_path(self.path.text(), audience=self.audience.currentData())

    def _set_audience_combo(self, audience: str) -> None:
        self._loading = True
        index = self.audience.findData(audience)
        if index >= 0:
            self.audience.setCurrentIndex(index)
        self._loading = False

    def _audience_changed(self) -> None:
        if self._loading or not self.path.text():
            return
        self.load_path(self.path.text(), audience=self.audience.currentData())

    def show_report(self, report: RunReport) -> None:
        self._report = report
        self._set_audience_combo(report.audience)
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
                self.leg.addItem(str(child.get("leg")), child.get("leg"))
            self.leg.setVisible(self.leg.count() > 0)
            self.leg.blockSignals(False)
        else:
            self.leg.setVisible(False)
            self.leg.clear()
        self._render_active()

    def metric_text(self, key: str) -> str:
        card = self._cards.get(key)
        if card is not None:
            return card.value.text()
        card = self.desk_cards.get(key)
        if card is not None:
            return card.value.text()
        spec = SPECS.get(key)
        label = spec.label if spec else key
        model = self.activity.model()
        if model is None:
            return ""
        for row in range(model.rowCount()):
            if model.index(row, 0).data() == label:
                return str(model.index(row, 1).data() or "")
        return ""

    def _active_book(self) -> RunReport | None:
        report = self._report
        if report is None:
            return None
        if report.kind == "walkforward":
            leg = self.leg.currentData()
            for child in report.children:
                if child.get("leg") == leg:
                    nested = child.get("report")
                    return nested if isinstance(nested, RunReport) else None
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
            title = "Batch"
            if report.audience == "designer":
                title = "Batch · designer view"
            self.identity.setText(title)
            self.cost_line.setText(
                "Designer view. Operator identity is not on this screen."
                if report.audience == "designer"
                else "Each row is a stored result. This window did not rank or recompute it."
            )
            self._set_cards({})
            self._show_chart(False)
            self._show_section(self.activity_title, self.activity, False)
            self._show_section(self.benchmark_title, self.benchmarks, False)
            self._show_section(self.fees_title, self.fees, False)
            self.period_title.setVisible(False)
            self.periods.setVisible(False)
            self._fill_jobs(report)
            self._fill_leaderboard(report)
            self._show_failures(report)
            self._clear_book_tables()
            return
        if book is None:
            self.identity.setText(report.kind)
            self._set_cards({})
            return
        metrics = book.metrics
        self.identity.setText(self._identity_text(report, book))
        self._set_cost_line(report, book)
        self._show_chart(True)
        self._set_cards({key: metrics.get(key) if key in metrics else None for key in KPI_KEYS})
        self._fill_activity(book)
        self._fill_benchmarks(book)
        self._fill_fees(book)
        self._fill_periods(book)
        self._show_section(self.jobs_title, self.jobs, False)
        self._fill_other(book)
        self._fill_fills(book)
        self._fill_trades(book)
        self._fill_desk(book)
        self._fill_figure(book)
        self._fill_leaderboard(report)
        self._redraw()
        if metrics.get("disqualified") is True and report.audience != "designer":
            self.identity.setText("DISQUALIFIED.  " + self.identity.text())
        elif metrics.get("disqualified") is True:
            self.identity.setText("DISQUALIFIED.  " + self.identity.text())

    def _identity_text(self, report: RunReport, book: RunReport) -> str:
        metrics = book.metrics
        if report.audience == "designer":
            parts = ["Designer view"]
            profile = metrics.get("profile")
            if profile:
                parts.append(str(profile))
            if "bar_minutes" in metrics:
                parts.append(f"{metrics.get('bar_minutes')} min")
            return "   ".join(parts)
        parts = []
        for key in ("profile", "symbol"):
            value = metrics.get(key)
            if value not in (None, ""):
                parts.append(str(value))
        if "bar_minutes" in metrics:
            parts.append(f"{metrics.get('bar_minutes')} min")
        if report.kind == "walkforward" and report.walkforward:
            summary = report.walkforward
            parts.append(
                f"design through {summary.get('design_end_date')} ({summary.get('design_sessions')} sessions), "
                f"locked from {summary.get('locked_start_date')} ({summary.get('locked_sessions')} sessions), "
                f"warm-up {summary.get('warmup_rule')}"
            )
        return "   ".join(parts) if parts else "Report has no profile or symbol field."

    def _set_cost_line(self, report: RunReport, book: RunReport) -> None:
        if report.audience == "designer":
            self.cost_line.setText("Designer view. Operator identity is not on this screen.")
            self.cost_line.setToolTip("Ticker, calendar dates, invoice scale, and raw prices are withheld.")
            return
        basis = book.cost_basis or {}
        if basis:
            self.cost_line.setText(
                "Invoice from the report: "
                f"scale {basis.get('share_scale', MISSING)}  "
                f"schedule {basis.get('cost_schedule', MISSING)}  "
                f"per-share {basis.get('per_share_fees', MISSING)}."
            )
            self.cost_line.setToolTip("This is the invoice record the runner wrote. The window did not reprice the book.")
        else:
            self.cost_line.setText("")
            self.cost_line.setToolTip("")

    def _show_failures(self, report: RunReport) -> None:
        for child in report.children:
            if child.get("state") != "ok":
                self.banners.addWidget(
                    Banner("error", f"{child.get('name')}: {child.get('state')}  {child.get('error') or ''}".strip())
                )

    def _show_chart(self, visible: bool) -> None:
        self.chart.setVisible(visible)
        if visible:
            self.chart.setMinimumHeight(480)
            self.chart.setMaximumHeight(16777215)
        else:
            self.chart.setMinimumHeight(0)
            self.chart.setMaximumHeight(0)

    def _show_section(self, title: QWidget, body: QWidget, visible: bool) -> None:
        title.setVisible(visible)
        body.setVisible(visible)

    def _fit_table(self, table, rows: int, cap: int = 8) -> None:
        """Keep a detail table at the height of its rows so it does not swallow the page."""
        header = max(table.horizontalHeader().sizeHint().height(), 28)
        row_h = max(table.verticalHeader().defaultSectionSize(), 24)
        count = min(max(int(rows), 1), cap)
        height = header + row_h * count + 8
        height = min(height, 320)
        table.setMinimumHeight(height)
        table.setMaximumHeight(height)

    def _set_cards(self, values: dict) -> None:
        clear_layout(self.cards_grid)
        self._cards = {}
        if not values:
            return
        theme = self.host.theme
        columns = len(KPI_KEYS)
        for index, key in enumerate(KPI_KEYS):
            if key not in values and values:
                continue
            spec = SPECS.get(key)
            title = spec.label if spec else key
            tooltip = spec.tooltip if spec else "Reported field. This window does not define a second formula for it."
            kind = spec.kind if spec else "text"
            card = MetricCard(title, tooltip, theme.mono)
            raw = values.get(key)
            text = format_number(kind, raw) if spec else (MISSING if is_missing(raw) else str(raw))
            tone = None
            if not is_missing(raw) and kind in {"fraction", "money", "ratio"}:
                try:
                    number = float(raw)
                    if number < 0:
                        tone = theme.negative
                    elif number > 0 and key in {"total_return", "cagr", "excess_total_return"}:
                        tone = theme.positive
                except (TypeError, ValueError):
                    tone = None
            card.set_value(text, tone=tone)
            self._cards[key] = card
            self.cards_grid.addWidget(card, index // columns, index % columns)

    def _fill_activity(self, book: RunReport) -> None:
        keys = [key for key in ACTIVITY_KEYS if key in book.metrics]
        for key in ("ann_vol", "max_dd_duration_sessions", "final_equity"):
            if key in book.metrics and key not in keys:
                keys.append(key)
        rows = []
        for key in keys:
            spec = SPECS.get(key)
            rows.append(
                {
                    "field": spec.label if spec else key,
                    "value": format_number(spec.kind, book.metrics.get(key)) if spec else book.metrics.get(key),
                }
            )
        self.activity.set_records(("field", "value"), rows, {"field": "Field", "value": "Stored"})
        self._fit_table(self.activity, len(rows))
        self._show_section(self.activity_title, self.activity, bool(rows))

    def _fill_benchmarks(self, book: RunReport) -> None:
        columns = (
            "book",
            "total_return",
            "cagr",
            "sharpe",
            "max_drawdown",
            "calmar",
            "final_equity",
            "fees_total",
            "financing_cost",
            "n_fills",
        )
        rows = [_benchmark_row("strategy", book.metrics)]
        for label, metrics in book.benchmarks.items():
            rows.append(_benchmark_row(label, metrics if isinstance(metrics, dict) else {}))
        titles = {key: SPECS[key].label for key in columns if key in SPECS}
        titles["book"] = "Book"
        titles["financing_cost"] = "Financing"
        kinds = {key: SPECS[key].kind for key in columns if key in SPECS}
        kinds["financing_cost"] = "money"
        self.benchmarks.set_records(columns, rows, titles, kinds)
        self._fit_table(self.benchmarks, len(rows))
        self._show_section(self.benchmark_title, self.benchmarks, bool(book.benchmarks) or bool(book.metrics))

    def _fill_fees(self, book: RunReport) -> None:
        payload = book.fees_breakdown if isinstance(book.fees_breakdown, dict) else {}
        rows = [{"component": str(key), "dollars": value} for key, value in payload.items() if not isinstance(value, (dict, list))]
        self.fees.set_records(
            ("component", "dollars"),
            rows,
            {"component": "Component", "dollars": "Dollars"},
            {"dollars": "money"},
        )
        self._fit_table(self.fees, len(rows))
        self._show_section(self.fees_title, self.fees, bool(rows))

    def _fill_periods(self, book: RunReport) -> None:
        payload = book.period_returns if isinstance(book.period_returns, dict) else None
        self.periods.set_periods(payload, self.host.theme)
        visible = bool(payload)
        self.period_title.setVisible(visible)
        self.periods.setVisible(visible)

    def _fill_jobs(self, report: RunReport) -> None:
        rows = []
        for child in report.children:
            rows.append(
                {
                    "job": child.get("name"),
                    "state": child.get("state"),
                    "error": child.get("error"),
                }
            )
        self.jobs.set_records(("job", "state", "error"), rows, {"job": "Job", "state": "State", "error": "Error"})
        self._fit_table(self.jobs, len(rows))
        self._show_section(self.jobs_title, self.jobs, bool(rows))

    def _fill_leaderboard(self, report: RunReport) -> None:
        board = report.leaderboard
        if board is None or not board.rows:
            self.leaderboard.set_records(("note",), [])
            self.leaderboard_title.setVisible(False)
            self.filter.setVisible(False)
            self.leaderboard.setVisible(False)
            return
        kinds = {column: kind for column in board.columns if (kind := leaderboard_kind(column))}
        titles = {column: SPECS[column].label for column in board.columns if column in SPECS}
        self.leaderboard.set_records(board.columns, list(board.rows), titles, kinds)
        self._fit_table(self.leaderboard, len(board.rows))
        self.leaderboard_title.setVisible(True)
        self.filter.setVisible(True)
        self.leaderboard.setVisible(True)

    def _fill_other(self, book: RunReport) -> None:
        rows = [{"field": key, "value": value} for key, value in sorted(book.other_fields.items())]
        self.other.set_records(("field", "value"), rows)
        self._fit_table(self.other, len(rows))
        self._show_section(self.other_title, self.other, bool(rows))

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
        bench = book.benchmarks.get("long_2x") if isinstance(book.benchmarks.get("long_2x"), dict) else {}
        if not interesting and "financing_cost" not in bench:
            self.desk_banners.addWidget(Banner("info", "This report has no Desk risk fields."))
            self.postmortem.setPlainText("")
            return
        if metrics.get("disqualified") is True:
            self.desk_banners.addWidget(Banner("error", "DISQUALIFIED"))
        elif "disqualified" not in metrics and metrics.get("profile") == "desk":
            self.desk_banners.addWidget(Banner("warn", "disqualified is not in this report. It was not assumed to be false."))
        index = 0
        for key in DESK_KEYS:
            if key not in metrics and metrics.get("profile") != "desk":
                continue
            if report_is_designer(book) and key == "share_factor":
                continue
            spec = SPECS[key]
            card = MetricCard(spec.label, spec.tooltip, theme.mono)
            card.set_value(format_number(spec.kind, metrics.get(key) if key in metrics else None))
            self.desk_cards[key] = card
            self.desk_grid.addWidget(card, index // 4, index % 4)
            index += 1
        if "financing_cost" in bench:
            card = MetricCard(
                "2× financing",
                "Stored financing_cost on the 2× long benchmark. The window did not compute it.",
                theme.mono,
            )
            card.set_value(format_number("money", bench.get("financing_cost")))
            self.desk_cards["long_2x_financing"] = card
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
            self.figure_label.setPixmap(pixmap.scaledToWidth(960, Qt.TransformationMode.SmoothTransformation))

    def _clear_book_tables(self) -> None:
        self.other.set_records(("field",), [])
        self._show_section(self.other_title, self.other, False)
        self.fills.set_records(("fills",), [])
        self.trades.set_records(("round trips",), [])
        self.postmortem.setPlainText("")

    def _redraw(self) -> None:
        book = self._active_book()
        theme = self.host.theme
        self.chart.apply_theme(theme)
        if book is None or not book.curves:
            self.chart.set_study(
                equity=[],
                drawdown=[],
                sessions=[],
                equity_note="No equity file in this run.",
                drawdown_note="",
            )
            return
        lines, note = chart_overlay(book)
        colors = {
            "Strategy": theme.strategy,
            "1× buy and hold": theme.bench,
            "2× long": theme.bench2,
        }
        painted = [(name, xs, ys, colors.get(name, theme.strategy)) for name, xs, ys in lines]
        drawdown = []
        dd_note = ""
        line = drawdown_line(book)
        if line is not None:
            xs, ys, dd_note = line
            drawdown = [("drawdown", xs, [value * 100.0 for value in ys], theme.drawdown)]
        exposure = None
        raw_exposure = exposure_line(book)
        if raw_exposure is not None:
            xs, ys = raw_exposure
            exposure = ("exposure", xs, ys, theme.exposure)
        sessions = book.curves[0].sessions
        self.chart.set_study(
            equity=painted,
            drawdown=drawdown,
            sessions=sessions,
            equity_note="Equity from the report files. " + note,
            drawdown_note=dd_note,
            exposure=exposure,
        )

    def export_current(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Export a copy of the report", self.path.text())
        if chosen:
            self.export_to(chosen)

    def export_to(self, dest) -> list[str]:
        """Copy the files this audience is allowed to see, plus the values on screen."""
        book = self._active_book()
        report = self._report
        target = Path(dest)
        target.mkdir(parents=True, exist_ok=True)
        written = []
        designer = report is not None and report.audience == "designer"
        opened = (report.opened if report is not None else None) or (book.directory if book is not None else None)
        symbols = peek_symbols(opened) if designer and opened is not None else ()
        scale = peek_scale(opened) if designer and opened is not None else None
        if book is not None:
            folder = book.directory
            for name in ("metrics.json", "equity_curve.csv", "equity.csv", "fills.csv", "trades.csv", "equity.png"):
                source = folder / name
                if not source.is_file():
                    continue
                if designer:
                    try:
                        text = source.read_text(encoding="utf-8")
                    except UnicodeDecodeError:
                        text = ""
                    if text and not export_allowed(text, symbols=symbols, scale=scale):
                        continue
                shutil.copy2(source, target / name)
                written.append(name)
            if designer:
                sheet = target / "metrics.json"
                sheet.write_text(json.dumps(book.metrics, indent=2, default=str) + "\n", encoding="utf-8")
                if sheet.name not in written:
                    written.append(sheet.name)
            rows = ["key,raw,display"]
            for key, card in self._cards.items():
                raw = book.metrics.get(key) if key in book.metrics else ""
                rows.append(f"{key},{_csv(raw)},{_csv(card.value.text())}")
            display = target / "displayed_metrics.csv"
            display.write_text("\n".join(rows) + "\n", encoding="utf-8")
            written.append(display.name)
        if report is not None and report.leaderboard is not None and self.leaderboard.isVisible():
            model = self.leaderboard.model()
            if model is not None and model.columnCount():
                headers = [str(model.headerData(column, Qt.Orientation.Horizontal) or "") for column in range(model.columnCount())]
                lines = [",".join(_csv(header) for header in headers)]
                for row in range(model.rowCount()):
                    cells = []
                    for column in range(model.columnCount()):
                        cells.append(_csv(model.data(model.index(row, column))))
                    lines.append(",".join(cells))
                board = target / "leaderboard_view.csv"
                board.write_text("\n".join(lines) + "\n", encoding="utf-8")
                written.append(board.name)
        self.host.set_status("Exported " + ", ".join(written) if written else "Nothing to export")
        return written


def report_is_designer(report: RunReport) -> bool:
    return getattr(report, "audience", "operator") == "designer"


def _benchmark_row(label: str, metrics: dict) -> dict:
    row = {"book": label}
    for key in (
        "total_return",
        "cagr",
        "sharpe",
        "max_drawdown",
        "calmar",
        "final_equity",
        "fees_total",
        "financing_cost",
        "n_fills",
    ):
        row[key] = metrics.get(key) if isinstance(metrics, dict) and key in metrics else None
    return row


def _csv(value) -> str:
    text = "" if value is None else str(value)
    if any(mark in text for mark in (",", '"', "\n")):
        return '"' + text.replace('"', '""') + '"'
    return text


def Qt_alignment():
    return Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
