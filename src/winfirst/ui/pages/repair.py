"""Repair tools: event review, SFC/DISM and read-only CHKDSK."""
from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from winfirst.services import repair_service
from winfirst.ui.base_page import BasePage
from winfirst.ui.dialogs import TextDialog, run_operations
from winfirst.ui.widgets import Badge, Card, DataTable, hint, make_button


class RepairPage(BasePage):
    page_key = "repair"
    page_title = "Repair Tools"
    page_subtitle = "Event review, protected-file repair and read-only filesystem checks."

    def __init__(self, ctx: Any, *, parent: QWidget | None = None) -> None:
        super().__init__(ctx, parent=parent)
        self._built = False

    def load(self) -> None:
        if not self._built:
            self._build()
            self._built = True
        self.load_findings()
        self.load_events()

    def _build(self) -> None:
        self.add_action_button("Refresh findings", slot=self.load_findings, variant="ghost")

        badges = QHBoxLayout()
        badges.setSpacing(8)
        self.reboot_badge = Badge("Restart: checking…", "muted")
        self.storage_badge = Badge("Storage: checking…", "muted")
        self.events_badge = Badge("Critical events: —", "muted")
        self.crash_badge = Badge("Crashes: —", "muted")
        for badge in (self.reboot_badge, self.storage_badge, self.events_badge, self.crash_badge):
            badges.addWidget(badge)
        badges.addStretch(1)
        self.add_layout(badges)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.add_widget(self.tabs)

        events = QWidget()
        events_layout = QVBoxLayout(events)
        events_layout.setContentsMargins(0, 12, 0, 0)
        events_layout.setSpacing(9)
        event_toolbar = QHBoxLayout()
        event_toolbar.setSpacing(8)
        self.log_combo = QComboBox()
        self.log_combo.addItems(["System", "Application", "Setup"])
        self.days_input = QSpinBox()
        self.days_input.setRange(1, 60)
        self.days_input.setValue(7)
        self.days_input.setSuffix(" days")
        event_toolbar.addWidget(QLabel("Log"))
        event_toolbar.addWidget(self.log_combo)
        event_toolbar.addWidget(self.days_input)
        event_toolbar.addWidget(make_button("Load events", variant="ghost", slot=self.load_events))
        event_toolbar.addStretch(1)
        self.events_note = QLabel("")
        self.events_note.setObjectName("SmallMuted")
        event_toolbar.addWidget(self.events_note)
        events_layout.addLayout(event_toolbar)
        self.events_table = DataTable(["Time", "Level", "Provider", "Event", "Message"], height=300)
        self.events_table.resize_columns([150, 90, 180, 70, 460])
        self.events_table.itemSelectionChanged.connect(self._show_event_details)
        events_layout.addWidget(self.events_table)
        self.event_detail = QPlainTextEdit()
        self.event_detail.setObjectName("LogView")
        self.event_detail.setReadOnly(True)
        self.event_detail.setMaximumHeight(140)
        self.event_detail.setPlaceholderText("Select an event to read its message.")
        events_layout.addWidget(self.event_detail)
        self.tabs.addTab(events, "System events")

        crashes = QWidget()
        crashes_layout = QVBoxLayout(crashes)
        crashes_layout.setContentsMargins(0, 12, 0, 0)
        crashes_layout.setSpacing(9)
        crashes_toolbar = QHBoxLayout()
        crashes_toolbar.addWidget(make_button("Load crashes", variant="ghost", slot=self.load_crashes))
        crashes_toolbar.addStretch(1)
        self.crashes_note = QLabel("")
        self.crashes_note.setObjectName("SmallMuted")
        crashes_toolbar.addWidget(self.crashes_note)
        crashes_layout.addLayout(crashes_toolbar)
        self.crashes_table = DataTable(["Time", "Provider", "Event", "Message"], height=400)
        self.crashes_table.resize_columns([150, 200, 70, 520])
        crashes_layout.addWidget(self.crashes_table)
        self.tabs.addTab(crashes, "Application crashes")

        repairs = QWidget()
        repairs_layout = QVBoxLayout(repairs)
        repairs_layout.setContentsMargins(0, 12, 0, 0)
        repairs_layout.setSpacing(10)
        repairs_layout.addWidget(
            hint(
                "SFC and DISM are long-running Windows tools (often 10–60 minutes). They have no independent "
                "verification API; WinFirst reports the tool exit code and output, not a guaranteed repair. "
                "Do not close the application while they run."
            )
        )
        sfc_card = Card("System File Checker", "Scans and repairs protected Windows system files.")
        sfc_card.add(
            make_button("Run SFC /scannow…", variant="primary", slot=self.run_sfc)
        )
        repairs_layout.addWidget(sfc_card)
        dism_card = Card(
            "Deployment Image Servicing",
            "Repairs the component store. Internet access may be needed for source files.",
        )
        dism_card.add(make_button("Run DISM RestoreHealth…", variant="primary", slot=self.run_dism))
        repairs_layout.addWidget(dism_card)
        chkdsk_card = Card("Filesystem check", "Runs CHKDSK without /F or /R; it never repairs automatically.")
        self.drive_combo = QComboBox()
        for letter in "CDEFGH":
            self.drive_combo.addItem(f"{letter}:")
        chkdsk_card.add(self.drive_combo)
        chkdsk_card.add(
            make_button("Run read-only CHKDSK…", variant="ghost", slot=self.run_chkdsk)
        )
        repairs_layout.addWidget(chkdsk_card)
        repairs_layout.addStretch(1)
        self.tabs.addTab(repairs, "Repairs")

    # ------------------------------------------------------------------ findings
    def load_findings(self) -> None:
        self.run_async(repair_service.get_repair_findings, self._show_findings, busy="Reading repair findings…")

    def _show_findings(self, data: dict[str, Any]) -> None:
        if data.get("pending_reboot"):
            self.reboot_badge.setText("Restart pending")
            self.reboot_badge.set_status("warning")
            self.reboot_badge.setToolTip("; ".join(data.get("pending_reasons") or []))
        else:
            self.reboot_badge.setText("No restart pending")
            self.reboot_badge.set_status("ok")
        problems = [
            volume.mountpoint
            for volume in data.get("volumes", [])
            if volume.low_space or volume.health.lower() not in {"healthy", "ok", ""}
        ]
        smart = data.get("smart")
        if smart and smart.failure_predicted:
            problems.append("SMART failure predicted")
        if problems:
            self.storage_badge.setText("Storage needs attention")
            self.storage_badge.set_status("critical")
            self.storage_badge.setToolTip(", ".join(problems))
        else:
            self.storage_badge.setText("Storage OK")
            self.storage_badge.set_status("ok")
        events = data.get("critical_events") or []
        self.events_badge.setText(f"Critical events: {len(events)}")
        self.events_badge.set_status("warning" if events else "muted")
        crashes = data.get("crashes") or []
        self.crash_badge.setText(f"Crashes: {len(crashes)}")
        self.crash_badge.set_status("warning" if crashes else "muted")
        if data.get("event_error"):
            self.events_note.setText(f"Event log: {data['event_error']}")

    # -------------------------------------------------------------------- events
    def load_events(self) -> None:
        log_name = self.log_combo.currentText() if hasattr(self, "log_combo") else "System"
        days = self.days_input.value() if hasattr(self, "days_input") else 7
        self.run_async(
            lambda: repair_service.get_event_log(days=days, limit=200, log_name=log_name),
            lambda data: self._show_events(data, None),
            busy="Reading Windows event log…",
        )

    def _show_events(self, data, _unused=None) -> None:
        entries, error = data
        self.events_note.setText(f"{len(entries)} entries" + (f" · {error}" if error else ""))
        self.events_table.set_rows(
            [
                [
                    entry.time,
                    entry.level,
                    entry.provider,
                    entry.event_id,
                    entry.message[:300],
                ]
                for entry in entries
            ],
            payloads=entries,
        )

    def _show_event_details(self) -> None:
        entries = self.events_table.selected_payloads()
        if entries:
            entry = entries[0]
            self.event_detail.setPlainText(
                f"{entry.time} · {entry.level} · {entry.provider} · event {entry.event_id}\n\n{entry.message}"
            )

    def load_crashes(self) -> None:
        self.run_async(
            lambda: repair_service.get_application_crashes(days=self.days_input.value() if hasattr(self, "days_input") else 7),
            self._show_crashes,
            busy="Reading application crash events…",
        )

    def _show_crashes(self, data) -> None:
        entries, error = data
        self.crashes_note.setText(f"{len(entries)} crash event(s)" + (f" · {error}" if error else ""))
        self.crashes_table.set_rows(
            [[entry.time, entry.provider, entry.event_id, entry.message[:400]] for entry in entries],
            payloads=entries,
        )

    # ------------------------------------------------------------------- repairs
    def run_sfc(self) -> None:
        self._confirm_long("SFC /scannow", repair_service.build_sfc_spec())

    def run_dism(self) -> None:
        self._confirm_long("DISM RestoreHealth", repair_service.build_dism_spec())

    def _confirm_long(self, name: str, spec) -> None:
        answer = QMessageBox.question(
            self,
            f"Run {name}?",
            f"{name} can take 10–60 minutes and may use significant disk and CPU. Keep the computer "
            "connected to power. Do you want to run it now?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        run_operations(self.ctx, self, [spec], title=name)

    def run_chkdsk(self) -> None:
        drive = self.drive_combo.currentText().rstrip(":")

        def work():
            return repair_service.run_chkdsk_readonly(drive)

        self.run_async(work, self._show_report, busy=f"Running read-only CHKDSK {drive}:…")

    def _show_report(self, report) -> None:
        text = (
            f"{report.name}\n"
            f"Success: {report.success} · Exit code: {report.exit_code}\n"
            f"{report.summary}\n"
        )
        if report.warnings:
            text += "\nWarnings:\n" + "\n".join(f"• {item}" for item in report.warnings)
        text += "\n\n" + (report.output or "No output.")
        TextDialog("CHKDSK result", text, parent=self).exec()
