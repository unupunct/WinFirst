"""Reports and redacted diagnostic bundles."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QInputDialog,
    QMessageBox,
    QWidget,
)

from winfirst.core import paths
from winfirst.core.report_manager import (
    build_deployment_report,
    build_history_report,
    build_system_report,
)
from winfirst.services import (
    battery_service,
    device_health_service,
    hardware_inventory_service,
    security_service,
    storage_health_service,
)
from winfirst.ui.base_page import BasePage
from winfirst.ui.widgets import Badge, Card, DataTable, format_datetime, hint, make_button


class ReportsPage(BasePage):
    page_key = "reports"
    page_title = "Reports"
    page_subtitle = "Self-contained HTML/PDF reports and a redacted diagnostic bundle for support."

    def __init__(self, ctx: Any, *, parent: QWidget | None = None) -> None:
        super().__init__(ctx, parent=parent)
        self.reports: list[dict[str, Any]] = []
        self._built = False

    def load(self) -> None:
        if not self._built:
            self._build()
            self._built = True
        self.refresh()

    def _build(self) -> None:
        self.add_action_button("Open reports folder", slot=self.open_folder, variant="ghost")

        generate = Card(
            "Generate a report",
            "Reports contain only values read from this computer. Unavailable values are labelled, not guessed.",
        )
        row = QHBoxLayout()
        row.setSpacing(8)
        self.format_combo = QComboBox()
        self.format_combo.addItem("HTML (self-contained)", "html")
        self.format_combo.addItem("PDF", "pdf")
        row.addWidget(self.format_combo)
        row.addWidget(make_button("System health report…", variant="primary", slot=self.system_report))
        row.addWidget(make_button("Operation history report…", variant="ghost", slot=self.history_report))
        row.addWidget(make_button("Deployment report…", variant="ghost", slot=self.deployment_report))
        row.addStretch(1)
        generate.add_layout(row)
        self.add_widget(generate)

        diagnostic = Card(
            "Diagnostic bundle",
            "A ZIP containing machine state, recent operations and the tail of the application log.",
        )
        diag_row = QHBoxLayout()
        diag_row.setSpacing(8)
        self.identifier_combo = QComboBox()
        self.identifier_combo.addItem("Redact names and identifiers", False)
        self.identifier_combo.addItem("Include host and user names", True)
        diag_row.addWidget(self.identifier_combo, 1)
        diag_row.addWidget(make_button("Create diagnostic bundle…", variant="ghost", slot=self.diagnostic_bundle))
        diagnostic.add_layout(diag_row)
        self.add_widget(diagnostic)

        self.report_badge = Badge("Reports: —", "muted")
        self.add_widget(self.report_badge)
        self.table = DataTable(["Created", "Title", "Kind", "Format", "File exists", "Path"], height=330)
        self.table.resize_columns([150, 300, 120, 80, 90, 500])
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.table.doubleClicked.connect(lambda _index: self.open_selected())
        self.add_widget(self.table)
        actions = QHBoxLayout()
        actions.setSpacing(8)
        actions.addWidget(make_button("Open selected", variant="ghost", slot=self.open_selected))
        actions.addWidget(make_button("Open containing folder", variant="ghost", slot=self.open_containing))
        actions.addStretch(1)
        self.add_layout(actions)
        self.add_widget(
            hint(
                "Reports are saved under the WinFirst data folder and recorded in the local database. "
                "A report whose file was moved or deleted is flagged rather than silently forgotten."
            )
        )

    # --------------------------------------------------------------------- data
    def refresh(self) -> None:
        try:
            self.reports = self.ctx.reports.list_reports(limit=300)
        except Exception as exc:
            self.show_error(f"Reports could not be listed: {exc}")
            return
        self.report_badge.setText(f"Reports: {len(self.reports)}")
        self.report_badge.set_status("info")
        self.table.set_rows(
            [
                [
                    format_datetime(str(row.get("created_at") or "")),
                    row.get("title") or "",
                    str(row.get("kind") or "").replace("_", " ").title(),
                    str(row.get("format") or "").upper(),
                    "Yes" if row.get("exists") else "Missing",
                    row.get("path") or "",
                ]
                for row in self.reports
            ],
            payloads=self.reports,
        )

    def _selection_changed(self) -> None:
        selected = self.table.selected_payloads()
        self._selected = selected[0] if selected else None

    def _save_async(self, document) -> None:
        fmt = self.format_combo.currentData() or "html"

        def work():
            return self.ctx.reports.save(document, fmt)

        def done(path) -> None:
            self.refresh()
            QMessageBox.information(self, "Report generated", f"Saved to:\n{path}")

        self.run_async(work, done, busy="Saving report…", done_message="Report saved.")

    # ---------------------------------------------------------------- generate
    def system_report(self) -> None:
        def work():
            return build_system_report(
                os_info=self.ctx.os_info,
                hardware=hardware_inventory_service.get_hardware_info(
                    self.ctx.runner, self.ctx.os_info
                ),
                storage=storage_health_service.get_storage_snapshot(self.ctx.runner),
                battery=battery_service.get_battery_info(self.ctx.runner),
                security_findings=security_service.security_findings(),
                device_health=device_health_service.get_device_health(self.ctx.runner),
            )

        self.run_async(work, self._save_async, busy="Collecting system information for the report…")

    def history_report(self) -> None:
        def work():
            return build_history_report(self.ctx.audit.list_operations(limit=1000))

        self.run_async(
            work,
            self._save_async,
            busy="Building the operation history report…",
        )

    def deployment_report(self) -> None:
        try:
            self.ctx.deployment.ensure_templates()
            sessions = self.ctx.deployment.list_sessions()
        except Exception as exc:
            self.show_error(f"Deployment sessions could not be listed: {exc}")
            return
        if not sessions:
            QMessageBox.information(self, "No sessions", "Create a deployment checklist session first.")
            return
        labels = [
            f"{row.get('template_name')} · {row.get('status')} · {str(row.get('updated_at') or '')[:16]}"
            for row in sessions
        ]
        choice, ok = QInputDialog.getItem(
            self, "Deployment report", "Choose a session:", labels, 0, False
        )
        if not ok:
            return
        row = sessions[labels.index(choice)]
        session = self.ctx.deployment.get_session(int(row["id"]))
        if session is None:
            self.show_error("The selected session no longer exists.")
            return
        try:
            document = build_deployment_report(session)
        except Exception as exc:
            self.show_error(f"The deployment report could not be built: {exc}")
            return
        self._save_async(document)

    def diagnostic_bundle(self) -> None:
        include_identifiers = bool(self.identifier_combo.currentData())
        os_info = self.ctx.os_info

        def work():
            data = {
                "application": {
                    "portable_data": paths.portable_mode_active(),
                    "data_dir": str(paths.data_dir()),
                    "reports_dir": str(paths.reports_dir()),
                    "logs_dir": str(paths.logs_dir()),
                },
                "windows": {
                    "product_name": getattr(os_info, "product_name", ""),
                    "edition": getattr(os_info, "edition", ""),
                    "build": getattr(os_info, "build", ""),
                    "architecture": getattr(os_info, "architecture", ""),
                    "hostname": getattr(os_info, "hostname", ""),
                    "username": getattr(os_info, "username", ""),
                    "pending_reboot": getattr(os_info, "pending_reboot", None),
                    "pending_reasons": getattr(os_info, "pending_reasons", []),
                },
                "operations": self.ctx.audit.list_operations(limit=800),
                "rollbacks": self.ctx.audit.rollback_entries(available_only=False)[:300],
                "security_findings": security_service.security_findings(),
                "capabilities": self.ctx.capabilities().to_dict(),
            }
            return data

        def done(data) -> None:
            try:
                path = self.ctx.reports.create_diagnostic_bundle(
                    data=data,
                    include_identifiers=include_identifiers,
                    hostname=getattr(os_info, "hostname", ""),
                    username=getattr(os_info, "username", ""),
                )
            except Exception as exc:
                self.show_error(f"The diagnostic bundle could not be created: {exc}")
                return
            QMessageBox.information(
                self,
                "Diagnostic bundle created",
                f"Saved to:\n{path}\n\n"
                + (
                    "Host and user names were included as requested."
                    if include_identifiers
                    else "Host and user names were redacted."
                ),
            )

        self.run_async(work, done, busy="Collecting and redacting diagnostic data…")

    # -------------------------------------------------------------------- files
    def open_folder(self) -> None:
        try:
            folder = paths.reports_dir()
            os.startfile(str(folder))  # type: ignore[attr-defined]
        except Exception as exc:
            self.show_error(f"The reports folder could not be opened: {exc}")

    def open_selected(self) -> None:
        selected = self.table.selected_payloads()
        if not selected:
            return
        row = selected[0]
        path = Path(str(row.get("path") or ""))
        if not path.is_file():
            QMessageBox.warning(self, "File missing", "The report file no longer exists on disk.")
            return
        try:
            os.startfile(str(path))  # type: ignore[attr-defined]
        except Exception as exc:
            self.show_error(f"The report could not be opened: {exc}")

    def open_containing(self) -> None:
        selected = self.table.selected_payloads()
        if not selected:
            return
        path = Path(str(selected[0].get("path") or ""))
        folder = path.parent if path.parent.is_dir() else paths.reports_dir()
        try:
            os.startfile(str(folder))  # type: ignore[attr-defined]
        except Exception as exc:
            self.show_error(f"The folder could not be opened: {exc}")
