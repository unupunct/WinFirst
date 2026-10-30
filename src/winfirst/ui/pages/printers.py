"""Printer inventory, queues, ports and spooler repair."""
from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from winfirst.services import printer_service
from winfirst.ui.base_page import BasePage
from winfirst.ui.dialogs import run_operations
from winfirst.ui.widgets import Badge, Card, DataTable, hint, make_button


class PrinterPage(BasePage):
    page_key = "printers"
    page_title = "Printer Management"
    page_subtitle = "Printers, queues, ports and narrowly scoped spooler repairs."

    def __init__(self, ctx: Any, *, parent: QWidget | None = None) -> None:
        super().__init__(ctx, parent=parent)
        self.snapshot = None
        self._built = False

    def load(self) -> None:
        if not self._built:
            self._build()
            self._built = True
        self.refresh()

    def _build(self) -> None:
        self.add_action_button("Refresh", slot=self.refresh, variant="ghost")
        stats = QHBoxLayout()
        stats.setSpacing(8)
        self.printer_badge = Badge("Printers: —", "muted")
        self.job_badge = Badge("Queued jobs: —", "muted")
        self.spooler_badge = Badge("Spooler: —", "muted")
        for badge in (self.printer_badge, self.job_badge, self.spooler_badge):
            stats.addWidget(badge)
        stats.addStretch(1)
        self.add_layout(stats)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.add_widget(self.tabs)

        printers = QWidget()
        printers_layout = QVBoxLayout(printers)
        printers_layout.setContentsMargins(0, 12, 0, 0)
        printers_layout.setSpacing(9)
        self.printers_table = DataTable(
            ["Printer", "Default", "Status", "Driver", "Port", "Jobs", "Problem"], height=280
        )
        self.printers_table.resize_columns([240, 70, 120, 220, 140, 60, 260])
        printers_layout.addWidget(self.printers_table)
        actions = QHBoxLayout()
        actions.setSpacing(8)
        actions.addWidget(make_button("Clear selected queue…", variant="ghost", slot=self.clear_selected_queue))
        actions.addWidget(make_button("Restart spooler…", variant="ghost", slot=self.restart_spooler))
        actions.addWidget(
            make_button("Restart spooler and clear queue…", variant="danger", slot=self.restart_and_clear)
        )
        actions.addStretch(1)
        self.printer_selection = hint("")
        actions.addWidget(self.printer_selection)
        printers_layout.addLayout(actions)
        self.printers_table.itemSelectionChanged.connect(self._printer_selection_changed)
        printers_layout.addWidget(
            hint(
                "The Print Spooler is a protected service elsewhere in WinFirst; this page uses dedicated, "
                "queue-aware printer operations instead of a generic service change."
            )
        )
        self.tabs.addTab(printers, "Printers")

        jobs = QWidget()
        jobs_layout = QVBoxLayout(jobs)
        jobs_layout.setContentsMargins(0, 12, 0, 0)
        jobs_layout.setSpacing(9)
        self.jobs_note = QLabel("")
        self.jobs_note.setObjectName("SmallMuted")
        self.jobs_table = DataTable(
            ["Printer", "Document", "Status", "Submitted", "Pages", "Size"], height=380
        )
        self.jobs_table.resize_columns([200, 320, 150, 160, 70, 90])
        jobs_layout.addWidget(self.jobs_note)
        jobs_layout.addWidget(self.jobs_table)
        jobs_layout.addWidget(
            hint("Document names are shown from Windows' queue. WinFirst does not read document contents.")
        )
        self.tabs.addTab(jobs, "Print queue")

        ports = QWidget()
        ports_layout = QVBoxLayout(ports)
        ports_layout.setContentsMargins(0, 12, 0, 0)
        ports_layout.setSpacing(9)
        self.ports_table = DataTable(["Port", "Description", "Host", "Number", "Protocol"], height=340)
        ports_layout.addWidget(self.ports_table)
        ports_layout.addWidget(
            hint("Reachability only tests a TCP connection. A successful connection does not prove printing works.")
        )
        self.tabs.addTab(ports, "Ports")

    # --------------------------------------------------------------------- load
    def refresh(self) -> None:
        self.clear_error()
        self.run_async(
            lambda: printer_service.get_printer_snapshot(self.ctx.runner),
            self._show,
            busy="Reading printers and queue…",
        )

    def _show(self, snapshot) -> None:
        self.snapshot = snapshot
        self.printer_badge.setText(f"Printers: {len(snapshot.printers)}")
        self.job_badge.setText(f"Queued jobs: {len(snapshot.jobs)}")
        self.job_badge.set_status("warning" if snapshot.jobs else "muted")
        spooler = snapshot.spooler
        self.spooler_badge.setText(f"Spooler: {spooler.status or 'unknown'}")
        self.spooler_badge.set_status("ok" if spooler.status.lower() == "running" else "critical")
        self.spooler_badge.setToolTip(f"Start mode: {spooler.start_type or 'unknown'}")
        if snapshot.errors:
            self.show_error("Some printer information could not be read: " + "; ".join(snapshot.errors[:3]))

        self.printers_table.set_rows(
            [
                [
                    printer.name,
                    "Yes" if printer.is_default else "",
                    printer.status or "—",
                    printer.driver or "—",
                    printer.port or "—",
                    printer.job_count,
                    printer.problem or "",
                ]
                for printer in snapshot.printers
            ],
            payloads=snapshot.printers,
        )
        self.jobs_table.set_rows(
            [
                [
                    job.printer,
                    job.document or "—",
                    job.status or "—",
                    job.submitted or "—",
                    job.pages if job.pages is not None else "—",
                    f"{job.size:,} bytes" if job.size is not None else "—",
                ]
                for job in snapshot.jobs
            ],
            payloads=snapshot.jobs,
        )
        self.jobs_note.setText(f"{len(snapshot.jobs)} queued job(s).")
        self.ports_table.set_rows(
            [
                [
                    port.name,
                    port.description or "—",
                    port.host or "—",
                    port.port_number if port.port_number is not None else "—",
                    port.protocol or "—",
                ]
                for port in snapshot.ports
            ],
            payloads=snapshot.ports,
        )
        self._printer_selection_changed()

    def _printer_selection_changed(self) -> None:
        selected = self.printers_table.selected_payloads()
        self.printer_selection.setText(
            f"{len(selected)} printer(s) selected" if selected else "Select a printer."
        )

    # ------------------------------------------------------------------ actions
    def _selected_printer(self):
        selected = self.printers_table.selected_payloads()
        return selected[0] if selected else None

    def clear_selected_queue(self) -> None:
        printer = self._selected_printer()
        if printer is None:
            QMessageBox.information(self, "No printer selected", "Select a printer first.")
            return
        if printer.job_count == 0:
            QMessageBox.information(self, "Queue already empty", f"{printer.name} has no queued jobs.")
            return
        try:
            spec = printer_service.build_clear_jobs_spec(printer.name, self.snapshot)
        except Exception as exc:
            self.show_error(str(exc))
            return
        results = run_operations(self.ctx, self, [spec], title="Clear print queue")
        if results:
            self.refresh()

    def restart_spooler(self) -> None:
        results = run_operations(
            self.ctx,
            self,
            [printer_service.build_restart_spooler_spec()],
            title="Restart Print Spooler",
            intro="Stopping the spooler cancels in-progress printing briefly; it is started again immediately.",
        )
        if results:
            self.refresh()

    def restart_and_clear(self) -> None:
        printer = self._selected_printer()
        if printer is not None and printer.job_count == 0:
            QMessageBox.information(self, "Queue already empty", f"{printer.name} has no queued jobs.")
            return
        name = printer.name if printer is not None else None
        results = run_operations(
            self.ctx,
            self,
            [printer_service.build_restart_spooler_clear_spec(name)],
            title="Restart spooler and clear queue",
            intro="The spooler is stopped, queued jobs are removed, then the spooler is started and verified.",
        )
        if results:
            self.refresh()
