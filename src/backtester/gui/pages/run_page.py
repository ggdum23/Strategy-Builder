"""Launch a single run, a walk-forward, or a batch. The transcript is the contract."""

from __future__ import annotations

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
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ..disclosures import profile_rows
from ..validate import transcript, validate_config
from ..widgets import Banner, DictTable, PageHeader, PathField, Section, clear_layout


class RunPage(QWidget):
    def __init__(self, host):
        super().__init__()
        self.host = host
        self._profile = ""
        self._strategies: list[str] = []
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 16)
        root.setSpacing(12)
        root.addWidget(
            PageHeader(
                "04  Run",
                "Single, walk-forward, or batch",
                "Every argument below is passed through. A blank rate is null, which means the engine default, "
                "not a number this window filled in. Walk-forward has no default cut.",
            )
        )
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
        self.single = QRadioButton("Single")
        self.walk = QRadioButton("Walk-forward")
        self.batch = QRadioButton("Batch")
        self.single.setChecked(True)
        self.mode_group = QButtonGroup(self)
        for button in (self.single, self.walk, self.batch):
            self.mode_group.addButton(button)
            modes.addWidget(button)
            button.toggled.connect(self.refresh)
        sources = QHBoxLayout()
        self.named = QRadioButton("Named files")
        self.pack = QRadioButton("Pack + sidecar")
        self.named.setChecked(True)
        self.source_group = QButtonGroup(self)
        for button in (self.named, self.pack):
            self.source_group.addButton(button)
            sources.addWidget(button)
            button.toggled.connect(self.refresh)
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
        self.borrow.setPlaceholderText("Blank = null = profile default")
        self.financing = QLineEdit()
        self.financing.setPlaceholderText("Blank = null. Arena must stay blank.")
        self.fee = QLineEdit("1")
        self.fee.setToolTip("Desk stress multiple. Arena accepts only 1.")
        self.dividends = PathField("Dividends CSV", "session,amount. Desk only. Blank sends nothing.", kind="file")
        self.warmup_sessions = QSpinBox()
        self.warmup_sessions.setRange(0, 100000)
        self.warmup_days = QSpinBox()
        self.warmup_days.setRange(0, 100000)
        self.design_day = QLineEdit()
        self.design_day.setPlaceholderText("design end day, integer session index")
        self.design_date = QLineEdit()
        self.design_date.setPlaceholderText("design end date YYYY-MM-DD")
        self.design_fraction = QLineEdit()
        self.design_fraction.setPlaceholderText("design fraction strictly between 0 and 1")
        fields = QFormLayout()
        fields.addRow("Ticker", self.ticker)
        fields.addRow("Bar minutes", self.minutes)
        fields.addRow("Initial equity", self.equity)
        fields.addRow("Borrow override", self.borrow)
        fields.addRow("Financing override", self.financing)
        fields.addRow("Fee multiple", self.fee)
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
        layout.addStretch(1)
        form_scroll = QScrollArea()
        form_scroll.setWidgetResizable(True)
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
        side_layout.addWidget(Section("What will be passed"))
        self.preview = QPlainTextEdit()
        self.preview.setObjectName("transcript")
        self.preview.setReadOnly(True)
        side_layout.addWidget(self.preview, 1)
        side_layout.addWidget(Section("Worker"))
        self.log = QPlainTextEdit()
        self.log.setObjectName("runLog")
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(140)
        side_layout.addWidget(self.log)
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
        }

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
        arena = name == "arena"
        self.financing.setEnabled(not arena)
        self.dividends.setEnabled(not arena)
        if arena:
            self.fee.setText("1") if self.fee.text().strip() not in {"", "1", "1.0"} else None
            self.fee.setEnabled(False)
        else:
            self.fee.setEnabled(True)
        walking = self.walk.isChecked()
        self.warmup_sessions.setEnabled(not walking)
        self.warmup_days.setEnabled(walking)
        self.design_day.setEnabled(walking)
        self.design_date.setEnabled(walking)
        self.design_fraction.setEnabled(walking)
        packed = self.pack.isChecked()
        self.minutes.setEnabled(not packed)
        self.src.setEnabled(not packed)
        self.ticker.setEnabled(not packed)
        self.pack_dir.setEnabled(packed)
        self.sidecar.setEnabled(packed)
        text = transcript(self.draft())
        self.preview.setPlainText(text)
        issues = validate_config(self.draft())
        self.run_button.setEnabled(not issues and not self.host.run_in_progress())
        clear_layout(self.banners)
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

    def on_failed(self, text: str) -> None:
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.setFormat("Failed")
        self.log.setPlainText(text)
        clear_layout(self.banners)
        last = text.strip().splitlines()[-1] if text.strip() else "run failed"
        self.banners.addWidget(Banner("error", last))

    def on_cancelled(self) -> None:
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.setFormat("Cancelled")
        self.log.appendPlainText("Cancelled. A partial report directory is not a finished run.")
        clear_layout(self.banners)
        self.banners.addWidget(
            Banner("warn", "Cancelled. If the folder contains a partial file, it is not a finished report.")
        )
