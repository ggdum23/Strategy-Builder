"""Launch a single run, a walk-forward, or a batch.

The page shows a readable summary of the draft. The stored contract is still
``run_config.json``, written by the worker before the engine starts.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ..disclosures import profile_rows
from ..jobs import failure_detail, failure_message
from ..validate import validate_config
from ..widgets import Banner, DictTable, PageHeader, PathField, Section, clear_layout


class RunPage(QWidget):
    def __init__(self, host):
        super().__init__()
        self.host = host
        self._profile = ""
        self._strategies: list[str] = []
        self._failure_text: str | None = None
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 16)
        root.setSpacing(12)
        header = PageHeader(
            "04  Run",
            "Single, walk-forward, or batch",
            "Set the mode and the paths, then read the summary of what will be sent.",
        )
        header.setToolTip(
            "A blank rate is passed as null, and the runner uses the profile default. "
            "Walk-forward has no default cut. Workers are a process count and do not change a metric."
        )
        root.addWidget(header)
        self.banners = QVBoxLayout()
        root.addLayout(self.banners)
        controls = QHBoxLayout()
        self.run_button = QPushButton("Run")
        self.run_button.setObjectName("runButton")
        self.run_button.clicked.connect(self.launch)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setObjectName("cancelButton")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.host.cancel_run)
        controls.addWidget(self.run_button)
        controls.addWidget(self.cancel_button)
        controls.addStretch(1)
        root.addLayout(controls)
        self.progress = QProgressBar()
        self.progress.setObjectName("runProgress")
        self.progress.setTextVisible(True)
        self.progress.setValue(0)
        self.progress.setFormat("Idle")
        root.addWidget(self.progress)
        splitter = QSplitter()
        form = QWidget()
        layout = QVBoxLayout(form)
        layout.setContentsMargins(0, 0, 8, 0)
        self.profile_label = QLabel("Profile: not selected")
        self.profile_label.setObjectName("section")
        self.strategy_label = QLabel("Strategies: none checked")
        self.strategy_label.setWordWrap(True)
        modes = QHBoxLayout()
        modes.setSpacing(18)
        modes.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.single = QRadioButton("Single")
        self.walk = QRadioButton("Walk-forward")
        self.batch = QRadioButton("Batch")
        self.single.setChecked(True)
        self.mode_group = QButtonGroup(self)
        for button in (self.single, self.walk, self.batch):
            button.setMinimumHeight(24)
            self.mode_group.addButton(button)
            modes.addWidget(button)
            button.toggled.connect(self.refresh)
        modes.addStretch(1)
        sources = QHBoxLayout()
        sources.setSpacing(18)
        sources.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.named = QRadioButton("Named files")
        self.pack = QRadioButton("Pack + sidecar")
        self.named.setChecked(True)
        self.source_group = QButtonGroup(self)
        for button in (self.named, self.pack):
            button.setMinimumHeight(24)
            self.source_group.addButton(button)
            sources.addWidget(button)
            button.toggled.connect(self.refresh)
        sources.addStretch(1)
        self.src = PathField("Filled-file folder", "Named runs only")
        self.ticker = QLineEdit()
        self.ticker.setPlaceholderText("Ticker as in the file name")
        self.pack_dir = PathField("Pack directory", "Designer-facing pack")
        self.sidecar = PathField("Sidecar path", "Contents are not read by this page", kind="file")
        self.out = PathField("Report directory", "Created for this run. Not inside the pack or the data folder.")
        self.minutes = QSpinBox()
        self.minutes.setRange(1, 1440)
        self.minutes.setValue(1)
        self.equity = QLineEdit("1000000")
        self.equity.setToolTip("Starting capital passed to the engine. The figure in the box is the figure that is sent.")
        self.borrow = QLineEdit()
        self.borrow.setPlaceholderText("Blank uses the profile rate.")
        self.borrow.setToolTip("A blank borrow rate is passed as null. The runner then uses the profile default.")
        self.financing = QLineEdit()
        self.financing.setPlaceholderText("Blank uses the profile rate.")
        self.financing.setToolTip("A blank financing rate is passed as null. Arena rejects any other rate.")
        self.fee = QLineEdit("1")
        self.fee.setToolTip("Desk stress multiple. Arena accepts only 1, so this field is hidden on Arena.")
        self.workers = QSpinBox()
        self.workers.setObjectName("workers")
        self.workers.setRange(1, 64)
        self.workers.setValue(1)
        self.workers.setToolTip("Process count passed to a batch. It does not change a metric.")
        self.dividends = PathField("Dividends CSV", "session,amount. Desk only. Blank sends nothing.", kind="file")
        self.warmup_sessions = QSpinBox()
        self.warmup_sessions.setRange(0, 100000)
        self.warmup_days = QSpinBox()
        self.warmup_days.setRange(0, 100000)
        self.design_day = QLineEdit()
        self.design_day.setPlaceholderText("One cut, as a session index.")
        self.design_day.setToolTip("Walk-forward needs exactly one cut. There is no default.")
        self.design_date = QLineEdit()
        self.design_date.setPlaceholderText("One cut, as a date.")
        self.design_date.setToolTip("Walk-forward needs exactly one of day, date, or fraction.")
        self.design_fraction = QLineEdit()
        self.design_fraction.setPlaceholderText("One cut, as a fraction between 0 and 1.")
        self.design_fraction.setToolTip("Walk-forward needs exactly one cut. There is no default.")
        fields = QFormLayout()
        self._form = fields
        fields.addRow("Ticker", self.ticker)
        fields.addRow("Bar minutes", self.minutes)
        fields.addRow("Initial equity", self.equity)
        fields.addRow("Borrow override", self.borrow)
        fields.addRow("Financing override", self.financing)
        fields.addRow("Fee multiple", self.fee)
        fields.addRow("Batch workers", self.workers)
        fields.addRow("Warm-up sessions", self.warmup_sessions)
        fields.addRow("Warm-up days", self.warmup_days)
        fields.addRow("Design end day", self.design_day)
        fields.addRow("Design end date", self.design_date)
        fields.addRow("Design fraction", self.design_fraction)
        use_data = QPushButton("Copy inspected folder")
        use_data.clicked.connect(self.copy_inspected)
        use_pack = QPushButton("Copy last pack paths")
        use_pack.clicked.connect(self.copy_pack)
        layout.addWidget(self.profile_label)
        layout.addWidget(self.strategy_label)
        layout.addLayout(modes)
        layout.addLayout(sources)
        layout.addWidget(self.src)
        layout.addLayout(fields)
        layout.addWidget(self.pack_dir)
        layout.addWidget(self.sidecar)
        layout.addWidget(self.out)
        layout.addWidget(self.dividends)
        layout.addWidget(use_data)
        layout.addWidget(use_pack)
        form.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        form_scroll = QScrollArea()
        form_scroll.setWidgetResizable(True)
        form_scroll.setAlignment(Qt.AlignmentFlag.AlignTop)
        form_scroll.setFrameShape(QFrame.Shape.NoFrame)
        form_scroll.setWidget(form)
        side = QWidget()
        side_layout = QVBoxLayout(side)
        side_layout.setContentsMargins(8, 0, 0, 0)
        side_layout.addWidget(Section("Rules for the selected profile"))
        self.rules = QLabel("Choose a profile on Strategies.")
        self.rules.setWordWrap(True)
        rules_scroll = QScrollArea()
        rules_scroll.setWidgetResizable(True)
        rules_scroll.setFrameShape(QFrame.Shape.NoFrame)
        rules_scroll.setWidget(self.rules)
        rules_scroll.setMaximumHeight(168)
        side_layout.addWidget(rules_scroll)
        side_layout.addWidget(Section("Run summary"))
        self.summary = QWidget()
        self.summary.setObjectName("runSummary")
        self._summary_layout = QFormLayout(self.summary)
        self._summary_layout.setContentsMargins(0, 0, 0, 0)
        self._summary_layout.setSpacing(4)
        self._summary_labels: list[tuple[str, QLabel]] = []
        self.summary.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        self.summary_scroll = QScrollArea()
        self.summary_scroll.setWidgetResizable(True)
        self.summary_scroll.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.summary_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.summary_scroll.setWidget(self.summary)
        self.summary_scroll.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        side_layout.addWidget(self.summary_scroll, 0)
        side_layout.addWidget(Section("Worker"))
        self.log = QPlainTextEdit()
        self.log.setObjectName("runLog")
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(120)
        side_layout.addWidget(self.log, 1)
        self.batch_table = DictTable()
        self.batch_table.setMinimumHeight(120)
        self.batch_table.setVisible(False)
        side_layout.addWidget(self.batch_table)
        splitter.addWidget(form_scroll)
        splitter.addWidget(side)
        splitter.setSizes([460, 860])
        root.addWidget(splitter, 1)
        for widget in (
            self.ticker,
            self.borrow,
            self.financing,
            self.fee,
            self.equity,
            self.design_day,
            self.design_date,
            self.design_fraction,
        ):
            widget.textChanged.connect(self.refresh)
        self.minutes.valueChanged.connect(self.refresh)
        self.workers.valueChanged.connect(self.refresh)
        self.warmup_sessions.valueChanged.connect(self.refresh)
        self.warmup_days.valueChanged.connect(self.refresh)
        for field in (self.src, self.pack_dir, self.sidecar, self.out, self.dividends):
            field.edit.textChanged.connect(self.refresh)
        self.refresh()

    def apply_profile(self, name: str) -> None:
        self._profile = name or ""
        self.refresh()

    def set_strategies(self, paths: list[str]) -> None:
        self._strategies = list(paths)
        self.refresh()

    def copy_inspected(self) -> None:
        data = self.host.data_page
        if data.src_path.text():
            self.src.set_text(data.src_path.text())
        if data.ticker.text().strip():
            self.ticker.setText(data.ticker.text().strip())

    def copy_pack(self) -> None:
        pack = self.host.preferences.data.get("last_pack") or ""
        sidecar = self.host.preferences.data.get("last_sidecar") or ""
        if pack:
            self.pack_dir.set_text(pack)
        if sidecar:
            self.sidecar.set_text(sidecar)
        self.pack.setChecked(True)

    def draft(self) -> dict:
        def blank_number(text, *, integer=False):
            raw = text.strip()
            if raw == "":
                return None
            try:
                return int(raw) if integer else float(raw)
            except ValueError:
                return raw

        if self.walk.isChecked():
            mode = "walkforward"
        elif self.batch.isChecked():
            mode = "batch"
        else:
            mode = "single"
        return {
            "mode": mode,
            "profile": self._profile,
            "source": "pack" if self.pack.isChecked() else "named",
            "strategies": list(self._strategies),
            "src": self.src.text(),
            "ticker": self.ticker.text().strip(),
            "pack": self.pack_dir.text(),
            "sidecar": self.sidecar.text(),
            "out": self.out.text(),
            "bar_minutes": int(self.minutes.value()),
            "initial_equity": blank_number(self.equity.text()),
            "borrow_rate_annual": blank_number(self.borrow.text()),
            "financing_rate_annual": blank_number(self.financing.text()),
            "fee_mult": blank_number(self.fee.text()),
            "dividends": self.dividends.text(),
            "warmup_sessions": int(self.warmup_sessions.value()),
            "warmup_days": int(self.warmup_days.value()),
            "design_end_day": blank_number(self.design_day.text(), integer=True),
            "design_end_date": self.design_date.text().strip(),
            "design_fraction": blank_number(self.design_fraction.text()),
            "workers": int(self.workers.value()),
        }

    def _rate_line(self, text: str) -> str:
        if text.strip() == "":
            return "profile default"
        return text.strip()

    def _show(self, widget, visible: bool, *, form: bool = False) -> None:
        widget.setVisible(visible)
        if form:
            self._form.setRowVisible(widget, visible)

    def _fill_summary(self, rows: list[tuple[str, str]]) -> None:
        clear_layout(self._summary_layout)
        self._summary_labels = []
        for key, value in rows:
            label = QLabel(value)
            label.setWordWrap(True)
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            label.setObjectName("summaryValue")
            self._summary_layout.addRow(key, label)
            self._summary_labels.append((key, label))

    def summary_text(self) -> str:
        return "\n".join(f"{key}: {label.text()}" for key, label in self._summary_labels)

    def replace_summary(self, old: str, new: str) -> None:
        """Swap displayed paths after validation, for the screenshot fixtures."""
        if not old:
            return
        for _key, label in self._summary_labels:
            label.setText(label.text().replace(old, new))

    def refresh(self) -> None:
        name = self._profile
        if name:
            self.profile_label.setText(f"Profile: {name}")
            bits = [f"{label}: {body}" for label, body in profile_rows(name)[:4]]
            self.rules.setText("\n\n".join(bits))
        else:
            self.profile_label.setText("Profile: not selected")
            self.rules.setText("Choose Arena or Desk on the Strategies page. This page will not assume one.")
        count = len(self._strategies)
        if count == 0:
            self.strategy_label.setText("Strategies: none checked")
        else:
            shown = "\n".join(self._strategies[:6])
            extra = "" if count <= 6 else f"\n… {count - 6} more"
            self.strategy_label.setText(f"Strategies ({count}):\n{shown}{extra}")
        desk = name == "desk"
        if name == "arena" and self.fee.text().strip() not in {"", "1", "1.0"}:
            self.fee.setText("1")
        walking = self.walk.isChecked()
        if walking and self.pack.isChecked():
            self.named.blockSignals(True)
            self.pack.blockSignals(True)
            self.named.setChecked(True)
            self.named.blockSignals(False)
            self.pack.blockSignals(False)
        batch = self.batch.isChecked()
        packed = self.pack.isChecked() and not walking
        self._show(self.src, not packed)
        self._show(self.ticker, not packed, form=True)
        self._show(self.minutes, not packed, form=True)
        self._show(self.equity, True, form=True)
        self._show(self.borrow, True, form=True)
        self._show(self.financing, desk, form=True)
        self._show(self.fee, desk, form=True)
        self._show(self.workers, batch, form=True)
        self._show(self.warmup_sessions, batch, form=True)
        self._show(self.warmup_days, walking, form=True)
        self._show(self.design_day, walking, form=True)
        self._show(self.design_date, walking, form=True)
        self._show(self.design_fraction, walking, form=True)
        self._show(self.pack_dir, packed)
        self._show(self.sidecar, packed)
        self._show(self.dividends, desk)
        self._show(self.out, True)
        draft = self.draft()
        mode = {"single": "Single", "walkforward": "Walk-forward", "batch": "Batch"}.get(draft["mode"], draft["mode"])
        source = "Pack + sidecar" if packed else "Named files"
        strategies = draft["strategies"] or ["none checked"]
        rows = [
            ("Profile", self._profile or "not selected"),
            ("Mode", mode),
            ("Source", source),
            ("Strategies", "\n".join(strategies)),
        ]
        if not packed:
            rows.append(("Data folder", draft["src"] or "—"))
            rows.append(("Ticker", draft["ticker"] or "—"))
            rows.append(("Bar minutes", str(draft["bar_minutes"])))
        if packed:
            rows.append(("Pack", draft["pack"] or "—"))
            rows.append(("Sidecar", draft["sidecar"] or "—"))
        rows.append(("Report", draft["out"] or "—"))
        rows.append(("Initial equity", "—" if draft["initial_equity"] is None else str(draft["initial_equity"])))
        rows.append(("Borrow", self._rate_line(self.borrow.text())))
        if desk:
            rows.append(("Financing", self._rate_line(self.financing.text())))
            rows.append(("Fee multiple", "—" if draft["fee_mult"] is None else str(draft["fee_mult"])))
            rows.append(("Dividends", draft["dividends"] or "none"))
        if batch:
            rows.append(("Warm-up sessions", str(draft["warmup_sessions"])))
            rows.append(("Batch workers", str(draft["workers"])))
        if walking:
            rows.append(("Warm-up days", str(draft["warmup_days"])))
            cut = draft["design_end_date"] or draft["design_end_day"] or draft["design_fraction"]
            rows.append(("Walk-forward cut", "—" if cut in (None, "") else str(cut)))
        self._fill_summary(rows)
        margins = self._summary_layout.contentsMargins()
        height = self._summary_layout.sizeHint().height() + margins.top() + margins.bottom() + 4
        height = max(height, 26 * len(self._summary_labels) + 8)
        self.summary_scroll.setMinimumHeight(height)
        self.summary_scroll.setMaximumHeight(height)
        issues = validate_config(draft)
        self.run_button.setEnabled(not issues and not self.host.run_in_progress())
        clear_layout(self.banners)
        if self._failure_text:
            self.banners.addWidget(Banner("error", self._failure_text))
        if issues:
            self.banners.addWidget(Banner("warn", issues[0] if len(issues) == 1 else f"{len(issues)} items to fix. The first: {issues[0]}"))

    def launch(self) -> None:
        config = self.draft()
        issues = validate_config(config)
        if issues:
            self.refresh()
            return
        self.log.setPlainText("")
        self.batch_table.setVisible(False)
        self.batch_table.set_records(("strategy", "state", "error"), [])
        self.progress.setRange(0, 0)
        self.progress.setFormat("Running")
        self.host.start_run(config)

    def set_running(self, running: bool) -> None:
        self.cancel_button.setEnabled(running)
        if running:
            self._failure_text = None
            clear_layout(self.banners)
            self.run_button.setEnabled(False)
        else:
            self.refresh()

    def on_progress(self, payload: dict) -> None:
        message = str(payload.get("message") or payload.get("state") or "")
        self.progress.setFormat(message or "Running")
        count = payload.get("count")
        index = payload.get("index")
        if isinstance(count, int) and count > 0 and isinstance(index, int):
            self.progress.setRange(0, count)
            self.progress.setValue(min(index, count))
        self.log.appendPlainText(message)
        rows = payload.get("strategies")
        if isinstance(rows, list):
            kept = [row for row in rows if isinstance(row, dict)]
            self.batch_table.setVisible(bool(kept))
            self.batch_table.set_records(("strategy", "state", "error"), kept)

    def on_finished(self, payload: dict) -> None:
        self._failure_text = None
        clear_layout(self.banners)
        result = payload.get("result") or {}
        self.progress.setRange(0, 1)
        self.progress.setValue(1)
        failed = result.get("failed")
        if failed:
            self.progress.setFormat(f"Finished with {failed} failure(s)")
        else:
            self.progress.setFormat("Finished")
        rows = result.get("strategies")
        if isinstance(rows, list):
            kept = [row for row in rows if isinstance(row, dict)]
            self.batch_table.setVisible(bool(kept))
            self.batch_table.set_records(("strategy", "state", "error"), kept)
        self.log.appendPlainText(f"wrote {result.get('out')}")
        self.host.open_results(result.get("out"))

    def on_failed(self, failure) -> None:
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.setFormat("Failed")
        self.log.setPlainText(failure_detail(failure))
        self._failure_text = failure_message(failure)
        clear_layout(self.banners)
        self.banners.addWidget(Banner("error", self._failure_text))

    def on_cancelled(self) -> None:
        self._failure_text = None
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.setFormat("Cancelled")
        self.log.appendPlainText("Cancelled. A partial report directory is not a finished run.")
        clear_layout(self.banners)
        self.banners.addWidget(
            Banner("warn", "Cancelled. If the folder contains a partial file, it is not a finished report.")
        )

