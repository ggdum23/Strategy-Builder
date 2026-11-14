"""Inspect a filled-file folder and, when asked, record it."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ...registry import hours_text
from ..jobs import failure_detail, failure_message, inspect_series, list_registry, record_series
from ..widgets import Banner, DictTable, PageHeader, Section, clear_layout

_PRICE = ("", "raw", "total_return")
_PROFILE = ("", "arena", "desk")


def _note_severity(text: str) -> str:
    lowered = text.lower()
    if any(token in lowered for token in ("missing monthly", "not runnable", "refusing", "ambiguous", "no unique modal")):
        return "error"
    if any(token in lowered for token in ("variant", "gapped", "before 15:59")):
        return "warn"
    return "info"


class DataPage(QWidget):
    def __init__(self, host):
        super().__init__()
        self.host = host
        self._payload = None
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 16)
        root.setSpacing(12)
        root.addWidget(
            PageHeader(
                "01  Data",
                "Series inspection",
                "Point at a folder of monthly filled files. Hours, tick, timezone, and gaps come from the inspection. "
                "Nothing is copied onto the desktop, and a price kind is never guessed.",
            )
        )
        buttons = QHBoxLayout()
        inspect = QPushButton("Inspect")
        inspect.setObjectName("primary")
        inspect.clicked.connect(self.inspect)
        record = QPushButton("Record in registry")
        record.clicked.connect(self.record)
        reload_registry = QPushButton("Reload registry")
        reload_registry.clicked.connect(self.reload_registry)
        refresh_prepared = QPushButton("Refresh prepared")
        refresh_prepared.clicked.connect(self.refresh_prepared)
        delete_prepared = QPushButton("Delete prepared")
        delete_prepared.clicked.connect(self.delete_prepared)
        rebuild_prepared = QPushButton("Rebuild prepared")
        rebuild_prepared.clicked.connect(self.rebuild_prepared)
        buttons.addWidget(inspect)
        buttons.addWidget(record)
        buttons.addWidget(reload_registry)
        buttons.addWidget(refresh_prepared)
        buttons.addWidget(delete_prepared)
        buttons.addWidget(rebuild_prepared)
        buttons.addStretch(1)
        root.addLayout(buttons)
        splitter = QSplitter()
        form = QWidget()
        form_layout = QVBoxLayout(form)
        form_layout.setContentsMargins(0, 0, 8, 0)
        self.root_path = self._path("Data root", "Folder that contains the symbol trees")
        self.src_path = self._path("Filled-file folder", "The directory of SYM_1min_filled_YYYY-MM.csv.gz files")
        self.registry_path = self._path("Registry file", "Created when you record. Not required just to inspect.", kind="save")
        self.ticker = QLineEdit()
        self.ticker.setPlaceholderText("Ticker as it appears in the file name, for example BRK.B")
        self.profile = QComboBox()
        self.profile.addItem("Select a profile to record", "")
        self.profile.addItem("Arena", "arena")
        self.profile.addItem("Desk", "desk")
        self.price = QComboBox()
        self.price.addItem("Select price kind", "")
        self.price.addItem("Raw prints", "raw")
        self.price.addItem("Total return (recorded, not runnable)", "total_return")
        self.minutes = QSpinBox()
        self.minutes.setRange(1, 1440)
        self.minutes.setValue(1)
        self.minutes.setToolTip("Delivery bar size. 1 reads the native minute grid. The value in this box is what is passed.")
        self.borrow = QLineEdit()
        self.borrow.setPlaceholderText("Blank records the profile default")
        self.splits = QLineEdit()
        self.splits.setPlaceholderText("none, or YYYY-MM-DD:ratio. Recorded, not applied.")
        self.phase_map = QLineEdit()
        self.phase_map.setPlaceholderText("Blank if the file already says pre, rth, post")
        fields = QFormLayout()
        fields.addRow("Ticker", self.ticker)
        fields.addRow("Profile", self.profile)
        fields.addRow("Price kind", self.price)
        fields.addRow("Bar minutes", self.minutes)
        fields.addRow("Borrow override", self.borrow)
        fields.addRow("Splits", self.splits)
        fields.addRow("Phase map", self.phase_map)
        form_layout.addWidget(self.root_path)
        form_layout.addWidget(self.src_path)
        form_layout.addWidget(self.registry_path)
        form_layout.addLayout(fields)
        form_layout.addStretch(1)
        self.results = QWidget()
        self.results_layout = QVBoxLayout(self.results)
        self.results_layout.setContentsMargins(8, 0, 0, 0)
        self.banners = QVBoxLayout()
        self.summary = QLabel("Inspection has not been run.")
        self.summary.setWordWrap(True)
        self.summary.setObjectName("muted")
        self.segments = DictTable()
        self.segments.setMinimumHeight(180)
        self.notes = QTextEdit()
        self.notes.setReadOnly(True)
        self.notes.setObjectName("mono")
        self.notes.setMinimumHeight(120)
        self.registry_table = DictTable()
        self.registry_table.setMinimumHeight(140)
        self.prepared_table = DictTable()
        self.prepared_table.setMinimumHeight(140)
        self.prepared_table.setObjectName("preparedTable")
        self.results_layout.addLayout(self.banners)
        self.results_layout.addWidget(Section("Inspection"))
        self.results_layout.addWidget(self.summary)
        self.results_layout.addWidget(self.segments)
        self.results_layout.addWidget(Section("Notes"))
        self.results_layout.addWidget(self.notes)
        self.results_layout.addWidget(Section("Registry"))
        self.results_layout.addWidget(self.registry_table)
        self.results_layout.addWidget(Section("Prepared data"))
        self.results_layout.addWidget(self.prepared_table)
        splitter.addWidget(form)
        splitter.addWidget(self.results)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([420, 900])
        root.addWidget(splitter, 1)
        self.root_path.edit.setObjectName("dataRootEdit")
        self.src_path.edit.setObjectName("dataSrcEdit")
        self.ticker.setObjectName("dataTickerEdit")

    def _path(self, label, placeholder, kind="dir"):
        from ..widgets import PathField

        return PathField(label, placeholder, kind=kind)

    def set_root(self, text: str) -> None:
        self.root_path.set_text(text)

    def set_src(self, text: str) -> None:
        self.src_path.set_text(text)

    def set_ticker(self, text: str) -> None:
        self.ticker.setText(text)

    def set_registry(self, text: str) -> None:
        self.registry_path.set_text(text)

    def set_price_kind(self, kind: str) -> None:
        index = self.price.findData(kind)
        if index >= 0:
            self.price.setCurrentIndex(index)

    def set_profile(self, name: str) -> None:
        index = self.profile.findData(name)
        if index >= 0:
            self.profile.setCurrentIndex(index)

    def inspect(self) -> None:
        src = self.src_path.text()
        ticker = self.ticker.text().strip()
        price = self.price.currentData()
        minutes = int(self.minutes.value())
        phase = self.phase_map.text()
        splits = self.splits.text()

        def work():
            return inspect_series(
                src,
                ticker,
                bar_minutes=minutes,
                price_kind=price,
                phase_map_text=phase,
                splits_text=splits,
            )

        self.host.start_task(work, self.show_inspection, self._failed)

    def record(self) -> None:
        src = self.src_path.text()
        ticker = self.ticker.text().strip()
        registry = self.registry_path.text()
        profile = self.profile.currentData()
        price = self.price.currentData()
        minutes = int(self.minutes.value())
        phase = self.phase_map.text()
        splits = self.splits.text()
        borrow_text = self.borrow.text().strip()
        borrow = None if borrow_text == "" else borrow_text

        def work():
            rate = None if borrow is None else float(borrow)
            recorded = record_series(
                src,
                ticker,
                registry,
                profile=profile,
                price_kind=price,
                bar_minutes=minutes,
                phase_map_text=phase,
                splits_text=splits,
                borrow_rate=rate,
            )
            recorded["document"] = list_registry(registry)
            recorded["inspection_payload"] = inspect_series(
                src,
                ticker,
                bar_minutes=minutes,
                price_kind=price,
                phase_map_text=phase,
                splits_text=splits,
            )
            return recorded

        self.host.start_task(work, self._recorded, self._failed)

    def reload_registry(self) -> None:
        path = self.registry_path.text()
        if not path:
            self._failed("Set a registry path first.")
            return

        def work():
            return list_registry(path)

        self.host.start_task(work, self.show_registry, self._failed)

    def refresh_prepared(self) -> None:
        from ...prepared import list_entries

        def work():
            return list_entries()

        self.host.start_task(work, self.show_prepared, self._failed)

    def _selected_prepared_key(self) -> str:
        index = self.prepared_table.currentIndex()
        position = self.prepared_table.source_row(index)
        rows = self.prepared_table.raw_rows()
        if position < 0 or position >= len(rows):
            return ""
        return str(rows[position].get("key") or "")

    def delete_prepared(self) -> None:
        from ...prepared import delete_entry

        key = self._selected_prepared_key()
        if not key:
            self._failed("Select a prepared series first.")
            return

        def work():
            delete_entry(key)
            return key

        self.host.start_task(work, lambda _key: self.refresh_prepared(), self._failed)

    def rebuild_prepared(self) -> None:
        from ...prepared import rebuild_entry

        key = self._selected_prepared_key()
        if not key:
            self._failed("Select a prepared series first.")
            return

        def work():
            rebuild_entry(key)
            return key

        self.host.start_task(work, lambda _key: self.refresh_prepared(), self._failed)

    def show_prepared(self, rows: list) -> None:
        self.prepared_table.set_records(
            ("key", "kind", "symbol", "rows", "bytes"),
            list(rows),
        )
        self.host.set_status(f"Prepared series: {len(rows)}")

    def _recorded(self, payload: dict) -> None:
        self.host.set_status(f"Recorded {payload['entry']['symbol']} in {payload['registry']}")
        if payload.get("document"):
            self.show_registry(payload["document"])
        if payload.get("inspection_payload"):
            self.show_inspection(payload["inspection_payload"])

    def show_registry(self, document: dict) -> None:
        rows = []
        for entry in document.get("series") or []:
            rows.append(
                {
                    "symbol": entry.get("symbol"),
                    "profile": entry.get("profile"),
                    "sessions": entry.get("n_sessions"),
                    "tick": entry.get("native_tick"),
                    "prices": entry.get("price_adjustment"),
                    "runnable": entry.get("runnable"),
                    "hours": hours_text(entry),
                }
            )
        self.registry_table.set_records(
            ("symbol", "profile", "sessions", "tick", "prices", "runnable", "hours"),
            rows,
        )

    def show_inspection(self, payload: dict) -> None:
        self._payload = payload
        info = payload["inspection"]
        clear_layout(self.banners)
        if not payload.get("runnable_if_recorded"):
            reason = "not runnable"
            if payload.get("declared_splits"):
                reason = "declared splits are recorded and not applied, so the series is not runnable"
            elif info.get("price_adjustment") != "raw":
                reason = (
                    f"price kind is {info.get('price_adjustment')}. "
                    "The raw price basis was refused. "
                    "Pass --price-basis total_return to run this series when the split list is empty."
                )
            self.banners.addWidget(Banner("error", f"NOT RUNNABLE. {info.get('symbol')}: {reason}."))
        for gap in payload.get("gaps") or []:
            self.banners.addWidget(Banner("error", f"GAP. {gap}."))
        loud = [note for note in info.get("notes") or [] if _note_severity(note) == "error"]
        for note in loud:
            self.banners.addWidget(Banner("error", note))
        offsets = ", ".join(info.get("timezone_offsets") or []) or "none"
        dst = "daylight-time pair" if info.get("timezone_dst_pair") else "single offset"
        lines = [
            f"{info.get('symbol')}    {info.get('first_timestamp_et')}  →  {info.get('last_timestamp_et')}",
            f"Native rows {info.get('native_rows')}    delivered rows {info.get('n_rows')}    sessions {info.get('n_sessions')}    bar minutes {info.get('bar_minutes')}",
            f"Tick {info.get('native_tick')}    fill {info.get('fill_policy')}    filled rows {info.get('filled_rows')}",
            f"Timezone offsets {offsets} ({dst})",
            f"Prices {info.get('price_adjustment')}    share factor {info.get('share_factor')} (raw files; anonymized scale is not in this inspection)",
            f"Source {info.get('source_glob')}",
        ]
        if payload.get("declared_splits"):
            rendered = ", ".join(f"{item['date']} ×{item['ratio']}" for item in payload["declared_splits"])
            lines.append(f"Declared splits, not applied: {rendered}")
        self.summary.setText("\n".join(lines))
        self.summary.setObjectName("summary")
        segments = []
        for segment in info.get("session_segments") or []:
            label = segment.get("label")
            phase = segment.get("engine_phase")
            shown = label if phase == label else f"{label} → {phase}"
            segments.append(
                {
                    "segment": shown,
                    "open": segment.get("start_clock"),
                    "close": segment.get("end_clock"),
                    "modal bars": segment.get("modal_bars"),
                    "sessions": segment.get("n_sessions"),
                    "variants": segment.get("variant_sessions"),
                    "gaps": segment.get("gapped_sessions"),
                }
            )
        self.segments.set_records(
            ("segment", "open", "close", "modal bars", "sessions", "variants", "gaps"),
            segments,
        )
        self.notes.setPlainText("\n".join(info.get("notes") or []))
        self.host.set_status(f"Inspected {info.get('symbol')}: {info.get('n_sessions')} sessions, tick {info.get('native_tick')}")
        self.host.remember(last_src=self.src_path.text(), last_ticker=self.ticker.text().strip())

    def _failed(self, failure) -> None:
        clear_layout(self.banners)
        sentence = failure_message(failure)
        if sentence == "run failed":
            sentence = "inspection failed"
        self.banners.addWidget(Banner("error", sentence))
        self.notes.setPlainText(failure_detail(failure))
        self.host.set_status("Inspection failed")

    def banner_texts(self) -> list[str]:
        texts = []
        for index in range(self.banners.count()):
            widget = self.banners.itemAt(index).widget()
            if widget is not None:
                label = widget.findChild(QLabel)
                if label is not None:
                    texts.append(label.text())
        return texts
