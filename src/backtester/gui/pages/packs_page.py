"""Build a named or anonymized pack. The sidecar is a path, never a document."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ..jobs import build_pack_job
from ..widgets import Banner, DictTable, PageHeader, PathField, Section, clear_layout


class PacksPage(QWidget):
    def __init__(self, host):
        super().__init__()
        self.host = host
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 16)
        root.setSpacing(12)
        root.addWidget(
            PageHeader(
                "02  Packs",
                "Named or anonymized",
                "The pack builder writes the designer files and a sidecar outside that folder. "
                "Scale factors, the date map, the ticker, and the operator note stay in the sidecar. "
                "This page does not open that file.",
            )
        )
        root.addWidget(
            Banner(
                "info",
                "Operator file. The sidecar path is shown so you can point a run at it. Its contents are not displayed.",
            )
        )
        actions = QHBoxLayout()
        build = QPushButton("Build pack")
        build.setObjectName("primary")
        build.clicked.connect(self.build)
        use = QPushButton("Use the inspected folder")
        use.clicked.connect(self.use_inspected)
        actions.addWidget(build)
        actions.addWidget(use)
        actions.addStretch(1)
        root.addLayout(actions)
        splitter = QSplitter()
        form = QWidget()
        layout = QVBoxLayout(form)
        layout.setContentsMargins(0, 0, 8, 0)
        self.registry = PathField("Registry", "Registry that already records this series", kind="save")
        self.src = PathField("Filled-file folder", "Same folder you inspected")
        self.ticker = QLineEdit()
        self.ticker.setPlaceholderText("Ticker, matched exactly")
        self.out = PathField("Pack directory", "Empty folder for the designer-facing pack")
        self.sidecar = PathField("Sidecar file", "Must not be inside the pack directory", kind="save")
        self.profile = QComboBox()
        self.profile.addItem("Registry profile", "")
        self.profile.addItem("Arena", "arena")
        self.profile.addItem("Desk", "desk")
        self.price = QComboBox()
        self.price.addItem("Raw prints", "raw")
        self.price.addItem("Total return (will be refused)", "total_return")
        self.minutes = QSpinBox()
        self.minutes.setRange(1, 1440)
        self.minutes.setValue(1)
        self.named = QRadioButton("Named. Real dates, share scale 1.")
        self.anon = QRadioButton("Anonymized. Rescale, hide the calendar, call the asset RANDOM.")
        self.mode_group = QButtonGroup(self)
        self.mode_group.addButton(self.named)
        self.mode_group.addButton(self.anon)
        self.mode_group.setExclusive(True)
        layout.addWidget(self.registry)
        layout.addWidget(self.src)
        layout.addWidget(QLabel("Ticker"))
        layout.addWidget(self.ticker)
        layout.addWidget(self.out)
        layout.addWidget(self.sidecar)
        layout.addWidget(QLabel("Profile check"))
        layout.addWidget(self.profile)
        layout.addWidget(QLabel("Price kind"))
        layout.addWidget(self.price)
        layout.addWidget(QLabel("Bar minutes (fixed into the pack)"))
        layout.addWidget(self.minutes)
        layout.addWidget(self.named)
        layout.addWidget(self.anon)
        layout.addStretch(1)
        side = QWidget()
        side_layout = QVBoxLayout(side)
        side_layout.setContentsMargins(8, 0, 0, 0)
        self.banners = QVBoxLayout()
        self.summary = QLabel("No pack has been built in this session.")
        self.summary.setWordWrap(True)
        self.summary.setObjectName("muted")
        self.chunks = DictTable()
        side_layout.addLayout(self.banners)
        side_layout.addWidget(Section("Designer-facing manifest"))
        side_layout.addWidget(self.summary)
        side_layout.addWidget(self.chunks, 1)
        splitter.addWidget(form)
        splitter.addWidget(side)
        splitter.setSizes([440, 880])
        root.addWidget(splitter, 1)

    def use_inspected(self) -> None:
        data = self.host.data_page
        if data.src_path.text():
            self.src.set_text(data.src_path.text())
        if data.ticker.text().strip():
            self.ticker.setText(data.ticker.text().strip())
        if data.registry_path.text():
            self.registry.set_text(data.registry_path.text())
        self.minutes.setValue(int(data.minutes.value()))
        if data.price.currentData():
            index = self.price.findData(data.price.currentData())
            if index >= 0:
                self.price.setCurrentIndex(index)

    def build(self) -> None:
        if self.named.isChecked():
            anonymize = False
        elif self.anon.isChecked():
            anonymize = True
        else:
            anonymize = None
        src = self.src.text()
        ticker = self.ticker.text().strip()
        out = self.out.text()
        sidecar = self.sidecar.text()
        registry = self.registry.text()
        profile = self.profile.currentData() or None
        price = self.price.currentData()
        minutes = int(self.minutes.value())
        phase = self.host.data_page.phase_map.text()

        def work():
            return build_pack_job(
                src,
                ticker,
                out,
                sidecar,
                registry,
                anonymize=anonymize,
                profile=profile,
                bar_minutes=minutes,
                price_kind=price,
                phase_map_text=phase,
            )

        self.host.start_task(work, self.show_build, self._failed)

    def show_build(self, payload: dict) -> None:
        forbidden = {"price_factor", "volume_factor", "date_map", "share_scale", "operator_note", "symbol"}
        leaked = forbidden & set(payload)
        clear_layout(self.banners)
        if leaked:
            self.banners.addWidget(Banner("error", "Pack result contained sidecar fields and was not shown."))
            return
        manifest = payload.get("manifest") or {}
        kind = "Anonymized" if payload.get("anonymized") else "Named"
        self.banners.addWidget(
            Banner(
                "ok",
                f"{kind} pack written. {payload.get('n_sessions')} sessions in {payload.get('n_chunks')} file(s). "
                "Sidecar contents were not loaded.",
            )
        )
        lines = [
            f"Asset name: {manifest.get('asset_name', '—')}",
            f"Pack id: {payload.get('pack_id')}",
            f"Bar minutes: {payload.get('bar_minutes')}",
            f"Pack directory: {payload.get('pack_dir')}",
            f"Sidecar path: {payload.get('sidecar_path')}",
            "Sidecar displayed: no",
        ]
        self.summary.setText("\n".join(lines))
        rows = []
        for chunk in manifest.get("chunks") or []:
            if isinstance(chunk, dict):
                rows.append(
                    {
                        "file": chunk.get("file"),
                        "first day": chunk.get("first_day"),
                        "last day": chunk.get("last_day"),
                        "sessions": chunk.get("sessions"),
                    }
                )
        self.chunks.set_records(("file", "first day", "last day", "sessions"), rows)
        self.host.remember(last_pack=str(payload.get("pack_dir") or ""), last_sidecar=str(payload.get("sidecar_path") or ""))
        self.host.set_status(f"{kind} pack ready. Sidecar not displayed.")

    def _failed(self, text: str) -> None:
        clear_layout(self.banners)
        first = text.strip().splitlines()[-1] if text.strip() else "pack build failed"
        self.banners.addWidget(Banner("error", first))
        self.summary.setText(text)
        self.host.set_status("Pack build failed")
