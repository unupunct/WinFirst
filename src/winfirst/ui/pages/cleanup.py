"""Application inventory and guarded removal."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLineEdit,
    QMessageBox,
    QWidget,
)

from winfirst.core import privilege_manager
from winfirst.services.app_inventory_service import CATEGORY_LABELS, AppInventoryService
from winfirst.services.app_removal_service import build_removal_specs
from winfirst.ui.base_page import BasePage
from winfirst.ui.dialogs import run_operations
from winfirst.ui.widgets import Badge, Card, DataTable, hint, make_button


class CleanupPage(BasePage):
    page_key = "cleanup"
    page_title = "Applications & Bloatware"
    page_subtitle = "Inspect installed packages and remove only apps that are individually selectable."

    def __init__(self, ctx: Any, *, parent: QWidget | None = None) -> None:
        super().__init__(ctx, parent=parent)
        self.service = AppInventoryService(ctx.runner, lambda: ctx.elevated)
        self.snapshot = None
        self._filtered: list[Any] = []
        self._built = False

    def load(self) -> None:
        if not self._built:
            self._build()
            self._built = True
        self.refresh()

    def _build(self) -> None:
        toolbar = Card()
        row = QHBoxLayout()
        row.setSpacing(8)
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Search by name, publisher or package…")
        self.search_box.setClearButtonEnabled(True)
        self.search_box.textChanged.connect(self._apply_filter)
        row.addWidget(self.search_box, 2)
        self.category_combo = QComboBox()
        self.category_combo.addItem("All categories", "")
        for key, label in CATEGORY_LABELS.items():
            self.category_combo.addItem(label, key)
        self.category_combo.currentIndexChanged.connect(self._apply_filter)
        row.addWidget(self.category_combo, 1)
        toolbar.add_layout(row)

        stats = QHBoxLayout()
        stats.setSpacing(7)
        self.count_badge = Badge("Not scanned", "muted")
        self.safe_badge = Badge("Safe candidates: —", "info")
        self.protected_badge = Badge("Protected: —", "muted")
        stats.addWidget(self.count_badge)
        stats.addWidget(self.safe_badge)
        stats.addWidget(self.protected_badge)
        stats.addStretch(1)
        self.scan_button = make_button("Rescan", variant="ghost", slot=self.refresh)
        stats.addWidget(self.scan_button)
        self.export_button = make_button(
            "Export inventory…", variant="ghost", slot=self.export_inventory
        )
        stats.addWidget(self.export_button)
        toolbar.add_layout(stats)
        self.add_widget(toolbar)

        self.table = DataTable(
            [
                "Name",
                "Category",
                "Source",
                "Scope",
                "Publisher",
                "Version",
                "Size (MB)",
                "Removal",
            ],
            height=430,
        )
        self.table.resize_columns([280, 170, 100, 130, 180, 90, 80, 150])
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.add_widget(self.table)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.remove_button = make_button(
            "Remove selected…", variant="danger", slot=self.remove_selected
        )
        self.remove_button.setEnabled(False)
        actions.addWidget(self.remove_button)
        actions.addWidget(
            make_button("Select safe candidates", variant="ghost", slot=self.select_safe)
        )
        actions.addStretch(1)
        self.selection_label = hint("Select one or more applications.")
        actions.addWidget(self.selection_label)
        self.add_layout(actions)

        self.add_widget(
            hint(
                "Protected system components, frameworks and resource packages are never offered for "
                "mass removal. Desktop applications use their registered uninstaller, which may show its "
                "own prompts. Removal is not reversible."
            )
        )

    # --------------------------------------------------------------------- data
    def refresh(self) -> None:
        self.clear_error()
        self.scan_button.setEnabled(False)
        self.count_badge.setText("Scanning…")
        self.count_badge.set_status("info")

        def work():
            return self.service.load()

        self.run_async(work, self._show_inventory, on_error=self._scan_failed, busy="Reading installed applications…")

    def _scan_failed(self, message: str) -> None:
        self.scan_button.setEnabled(True)
        self.count_badge.setText("Scan failed")
        self.count_badge.set_status("critical")
        self.show_error(
            f"Installed applications could not be read: {message}. "
            "Some package sources may be unavailable on this edition of Windows."
        )

    def _show_inventory(self, snapshot) -> None:
        self.scan_button.setEnabled(True)
        self.snapshot = snapshot
        counts = snapshot.counts()
        self.count_badge.setText(f"{len(snapshot.items)} installed items")
        self.count_badge.set_status("ok")
        self.safe_badge.setText(f"Safe candidates: {len(snapshot.safe_removal_candidates)}")
        protected = counts.get("protected_system", 0)
        self.protected_badge.setText(f"Protected: {protected}")
        if snapshot.errors:
            self.show_error("Some sources could not be read: " + "; ".join(snapshot.errors[:3]))
        self._apply_filter()

    def _apply_filter(self) -> None:
        if self.snapshot is None:
            return
        needle = self.search_box.text().strip().casefold()
        category = self.category_combo.currentData() or ""
        items = []
        for item in self.snapshot.items:
            if category and item.category != category:
                continue
            haystack = f"{item.name} {item.publisher} {item.package_full_name} {item.source}".casefold()
            if needle and needle not in haystack:
                continue
            items.append(item)
        self._filtered = items
        rows = []
        for item in items:
            size = f"{item.estimated_size_mb:.1f}" if item.estimated_size_mb else "—"
            method = item.removal_method if item.can_uninstall else (item.protected_reason or "Not removable")
            rows.append(
                [
                    item.name,
                    item.category_label,
                    item.source,
                    item.display_scope,
                    item.publisher or "—",
                    item.version or "—",
                    size,
                    method,
                ]
            )
        self.table.set_rows(rows, payloads=items)
        self._selection_changed()

    # ---------------------------------------------------------------- selection
    def _selection_changed(self) -> None:
        selected = self.table.selected_payloads()
        removable = [item for item in selected if item.can_uninstall]
        protected = len(selected) - len(removable)
        if not selected:
            self.selection_label.setText("Select one or more applications.")
        else:
            text = f"{len(removable)} removable selected"
            if protected:
                text += f" · {protected} protected selection will be refused"
            self.selection_label.setText(text)
        self.remove_button.setEnabled(bool(removable))

    def select_safe(self) -> None:
        if self.snapshot is None:
            return
        self.table.clearSelection()
        safe_ids = {item.id for item in self.snapshot.safe_removal_candidates}
        for row, item in enumerate(self._filtered):
            if item.id in safe_ids:
                self.table.selectRow(row)
        self._selection_changed()

    # ------------------------------------------------------------------ removal
    def remove_selected(self) -> None:
        selected = self.table.selected_payloads()
        if not selected:
            return
        protected = [item for item in selected if not item.can_uninstall]
        candidates = [item for item in selected if item.can_uninstall]
        if protected:
            QMessageBox.warning(
                self,
                "Protected selections ignored",
                "These items are protected and will not be removed:\n\n"
                + "\n".join(f"• {item.name}: {item.protected_reason or 'protected'}" for item in protected[:12]),
            )
        if not candidates:
            return
        message = (
            f"Remove {len(candidates)} application(s)?\n\n"
            + "\n".join(f"• {item.name}" for item in candidates[:20])
        )
        if len(candidates) > 20:
            message += f"\n… and {len(candidates) - 20} more"
        message += (
            "\n\nThis uses each app's registered removal method. Removal cannot be undone by WinFirst. "
            "The result is verified by scanning the inventory again."
        )
        answer = QMessageBox.question(
            self,
            "Confirm application removal",
            message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        specs, errors = build_removal_specs(candidates)
        if errors:
            QMessageBox.information(
                self,
                "Some items cannot be removed",
                "\n".join(errors[:20]),
            )
        if not specs:
            return
        results = run_operations(
            self.ctx,
            self,
            specs,
            title="Remove selected applications",
            intro=(
                "Each removal is checked independently. A package that is still present will be "
                "reported as a failure rather than a success."
            ),
        )
        if results:
            self.refresh()

    # ------------------------------------------------------------------- export
    def export_inventory(self) -> None:
        if self.snapshot is None:
            return
        path, selected_filter = QFileDialog.getSaveFileName(
            self,
            "Export application inventory",
            str(Path.home() / "winfirst-applications.csv"),
            "CSV files (*.csv);;JSON files (*.json)",
        )
        if not path:
            return
        fmt = "json" if path.lower().endswith(".json") or "JSON" in selected_filter else "csv"
        try:
            written = self.service.export(self.snapshot, path, fmt)
        except Exception as exc:
            self.show_error(f"The inventory could not be exported: {exc}")
            return
        self.ctx.status(f"Inventory exported to {written}")
        QMessageBox.information(self, "Inventory exported", f"Saved to:\n{written}")
