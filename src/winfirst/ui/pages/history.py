"""Audit history, operation details and reversible changes."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLineEdit,
    QMessageBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from winfirst.ui.base_page import BasePage
from winfirst.ui.dialogs import TextDialog, run_operations
from winfirst.ui.widgets import Badge, Card, DataTable, format_datetime, hint, make_button


class HistoryPage(BasePage):
    page_key = "history"
    page_title = "History & Rollback"
    page_subtitle = "Every operation WinFirst recorded, plus the changes that can be restored safely."

    def __init__(self, ctx: Any, *, parent: QWidget | None = None) -> None:
        super().__init__(ctx, parent=parent)
        self.operations: list[dict[str, Any]] = []
        self.rollbacks: list[dict[str, Any]] = []
        self._built = False

    def load(self) -> None:
        if not self._built:
            self._build()
            self._built = True
        self.refresh()

    def _build(self) -> None:
        self.add_action_button("Refresh", slot=self.refresh, variant="ghost")
        self.add_action_button("Export history…", slot=self.export_history, variant="ghost")
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.add_widget(self.tabs)

        history = QWidget()
        history_layout = QVBoxLayout(history)
        history_layout.setContentsMargins(0, 12, 0, 0)
        history_layout.setSpacing(9)
        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Search operation name, ID, message or profile…")
        self.search_box.setClearButtonEnabled(True)
        self.search_box.returnPressed.connect(self.refresh)
        toolbar.addWidget(self.search_box, 2)
        self.category_combo = QComboBox()
        self.category_combo.addItem("All categories", "")
        toolbar.addWidget(self.category_combo)
        self.status_combo = QComboBox()
        self.status_combo.addItem("All statuses", "")
        for status in ("success", "partial", "failed", "blocked", "denied", "cancelled", "dry-run"):
            self.status_combo.addItem(status.title(), status)
        toolbar.addWidget(self.status_combo)
        toolbar.addWidget(make_button("Search", variant="primary", slot=self.refresh))
        history_layout.addLayout(toolbar)

        self.history_badge = Badge("Operations: —", "muted")
        history_layout.addWidget(self.history_badge)
        self.history_table = DataTable(
            ["When", "Category", "Operation", "Status", "Risk", "Profile", "Restorable"],
            height=380,
        )
        self.history_table.resize_columns([150, 130, 320, 100, 80, 160, 100])
        self.history_table.itemSelectionChanged.connect(self._history_selection)
        history_layout.addWidget(self.history_table)
        actions = QHBoxLayout()
        actions.setSpacing(8)
        actions.addWidget(make_button("View details…", variant="ghost", slot=self.view_details))
        actions.addWidget(make_button("Restore this change…", variant="ghost", slot=self.restore_selected))
        actions.addStretch(1)
        self.history_selection = hint("Select an operation to inspect it.")
        actions.addWidget(self.history_selection)
        history_layout.addLayout(actions)
        history_layout.addWidget(
            hint(
                "History is stored locally. Values that look like secrets are masked before they are written. "
                "Only changes that recorded a verified previous state can be restored."
            )
        )
        self.tabs.addTab(history, "Change history")

        rollback = QWidget()
        rollback_layout = QVBoxLayout(rollback)
        rollback_layout.setContentsMargins(0, 12, 0, 0)
        rollback_layout.setSpacing(9)
        self.rollback_badge = Badge("Available rollbacks: —", "muted")
        rollback_layout.addWidget(self.rollback_badge)
        self.rollback_table = DataTable(
            ["When", "Operation", "Summary", "Strategy"], height=400
        )
        self.rollback_table.resize_columns([150, 280, 380, 150])
        rollback_layout.addWidget(self.rollback_table)
        rollback_actions = QHBoxLayout()
        rollback_actions.setSpacing(8)
        rollback_actions.addWidget(
            make_button("Preview and restore…", variant="primary", slot=self.restore_rollback)
        )
        rollback_actions.addWidget(make_button("Refresh", variant="ghost", slot=self.refresh_rollbacks))
        rollback_actions.addStretch(1)
        rollback_layout.addLayout(rollback_actions)
        rollback_layout.addWidget(
            hint(
                "Rollback restores the exact previous value recorded at apply time. Some changes cannot be "
                "rolled back (application removal, file deletion, driver installs) and will not appear here."
            )
        )
        self.tabs.addTab(rollback, "Available rollbacks")

    # --------------------------------------------------------------------- data
    def refresh(self) -> None:
        self.clear_error()
        search = self.search_box.text().strip()
        category = self.category_combo.currentData() or ""
        status = self.status_combo.currentData() or ""

        def work():
            categories = self.ctx.audit.categories()
            operations = self.ctx.audit.list_operations(
                search=search, category=category, status=status, limit=500
            )
            return categories, operations

        self.run_async(work, self._show_history, busy="Reading operation history…")
        self.refresh_rollbacks()

    def _show_history(self, data) -> None:
        self.categories, self.operations = data
        current = self.category_combo.currentData()
        self.category_combo.blockSignals(True)
        self.category_combo.clear()
        self.category_combo.addItem("All categories", "")
        for name in self.categories:
            self.category_combo.addItem(name.replace("_", " ").title(), name)
        if current:
            index = self.category_combo.findData(current)
            if index >= 0:
                self.category_combo.setCurrentIndex(index)
        self.category_combo.blockSignals(False)
        self.history_badge.setText(f"Operations: {len(self.operations)}")
        self.history_badge.set_status("info")
        self.history_table.set_rows(
            [
                [
                    format_datetime(str(operation.get("started_at") or "")),
                    str(operation.get("category") or "").replace("_", " ").title(),
                    operation.get("name") or operation.get("operation_id") or "",
                    str(operation.get("status") or "").title(),
                    str(operation.get("risk") or "").title(),
                    operation.get("profile_name") or "—",
                    "Yes" if operation.get("rollback_available") else "No",
                ]
                for operation in self.operations
            ],
            payloads=self.operations,
        )
        self.history_selection.setText("Select an operation to inspect it.")

    def refresh_rollbacks(self) -> None:
        try:
            self.rollbacks = self.ctx.rollback.available_entries()
        except Exception as exc:
            self.show_error(f"Rollback entries could not be loaded: {exc}")
            return
        self.rollback_badge.setText(f"Available rollbacks: {len(self.rollbacks)}")
        self.rollback_table.set_rows(
            [
                [
                    format_datetime(str(entry.get("created_at") or "")),
                    entry.get("operation_name") or "",
                    entry.get("summary") or "",
                    entry.get("strategy") or "",
                ]
                for entry in self.rollbacks
            ],
            payloads=self.rollbacks,
        )

    # ----------------------------------------------------------------- details
    def _history_selection(self) -> None:
        selected = self.history_table.selected_payloads()
        if not selected:
            self.history_selection.setText("Select an operation to inspect it.")
            return
        operation = selected[0]
        restorable = " · a rollback is available" if operation.get("rollback_available") else ""
        self.history_selection.setText(
            f"{operation.get('name') or operation.get('operation_id')} · {operation.get('status')}{restorable}"
        )

    def _current_operation(self) -> dict[str, Any] | None:
        selected = self.history_table.selected_payloads()
        return selected[0] if selected else None

    def view_details(self) -> None:
        operation = self._current_operation()
        if operation is None:
            return
        try:
            details = self.ctx.audit.get_operation(int(operation["id"])) or operation
        except Exception as exc:
            self.show_error(f"Operation details could not be loaded: {exc}")
            return
        lines = [
            f"Operation: {details.get('name') or details.get('operation_id')}",
            f"ID: {details.get('operation_id')}",
            f"Category: {details.get('category')}",
            f"Status: {details.get('status')}",
            f"Risk: {details.get('risk')}",
            f"Started: {details.get('started_at')}",
            f"Finished: {details.get('finished_at')}",
            f"Duration: {details.get('duration_ms')} ms",
            f"Exit code: {details.get('exit_code')}",
            f"Profile: {details.get('profile_name') or '—'}",
            "",
            f"Requested state:\n{details.get('requested_state') or '—'}",
            f"Previous state:\n{details.get('previous_state') or '—'}",
            f"Actual state:\n{details.get('actual_state') or '—'}",
            f"Message:\n{details.get('message') or '—'}",
        ]
        extra = details.get("details") or {}
        if extra:
            lines.append("\nRecorded details:")
            for key, value in extra.items():
                lines.append(f"{key}: {value}")
        rollbacks = details.get("rollback") or []
        if rollbacks:
            lines.append("\nRollback records:")
            for entry in rollbacks:
                lines.append(
                    f"#{entry.get('id')} · {entry.get('strategy')} · {entry.get('status')} · "
                    f"{entry.get('summary') or ''}"
                )
        TextDialog("Operation details", "\n".join(str(line) for line in lines), parent=self).exec()

    # ---------------------------------------------------------------- restore
    def restore_selected(self) -> None:
        operation = self._current_operation()
        if operation is None:
            return
        if not operation.get("rollback_available"):
            QMessageBox.information(
                self,
                "No rollback available",
                "This operation did not record a verified previous state, so it cannot be restored automatically.",
            )
            return
        entry = next(
            (
                item
                for item in self.rollbacks
                if int(item.get("operation_row_id", 0)) == int(operation["id"])
            ),
            None,
        )
        if entry is None:
            self.refresh_rollbacks()
            QMessageBox.information(
                self,
                "Rollback not found",
                "The rollback record for this operation is no longer available. It may already have been restored.",
            )
            return
        self._restore_entry(entry)

    def restore_rollback(self) -> None:
        selected = self.rollback_table.selected_payloads()
        if not selected:
            QMessageBox.information(self, "No rollback selected", "Select a rollback entry first.")
            return
        self._restore_entry(selected[0])

    def _restore_entry(self, entry: dict[str, Any]) -> None:
        preview = self.ctx.rollback.preview(entry)
        if not preview.available:
            QMessageBox.warning(
                self,
                "Rollback not available",
                f"{preview.summary}\n\n{preview.reason or 'The recorded state cannot be restored.'}",
            )
            return
        answer = QMessageBox.question(
            self,
            "Restore previous state?",
            f"Restore: {preview.summary}\n\nStrategy: {preview.strategy}\n\n"
            "The restored value is verified by reading it back. The rollback record is marked as restored "
            "only if verification succeeds.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            spec = self.ctx.rollback.build_spec(entry)
        except Exception as exc:
            QMessageBox.critical(self, "Rollback could not be prepared", str(exc))
            return
        results = run_operations(
            self.ctx,
            self,
            [spec],
            title="Restore previous state",
            intro="This is the exact previous state recorded during the original change.",
        )
        if results and all(result.status.is_success for result in results):
            try:
                self.ctx.audit.mark_restored(int(entry.get("id", 0)), success=True, note="Restored from History page")
            except Exception:
                pass
        self.refresh()

    # ------------------------------------------------------------------ export
    def export_history(self) -> None:
        path, selected_filter = QFileDialog.getSaveFileName(
            self,
            "Export operation history",
            str(Path.home() / "winfirst-history.csv"),
            "CSV files (*.csv);;JSON files (*.json)",
        )
        if not path:
            return
        fmt = "json" if path.lower().endswith(".json") or "JSON" in selected_filter else "csv"
        try:
            written = self.ctx.audit.export(path, fmt=fmt)
        except Exception as exc:
            QMessageBox.critical(self, "History not exported", str(exc))
            return
        QMessageBox.information(self, "History exported", f"Saved to:\n{written}")
