"""Startup entry management with per-entry verification."""
from __future__ import annotations

import os
from typing import Any

from PySide6.QtWidgets import QHBoxLayout, QLabel, QMessageBox, QWidget

from winfirst.services import startup_service
from winfirst.ui.base_page import BasePage
from winfirst.ui.dialogs import run_operations
from winfirst.ui.widgets import Badge, Card, DataTable, hint, make_button


class StartupPage(BasePage):
    page_key = "startup"
    page_title = "Startup Manager"
    page_subtitle = "Programs and shortcuts Windows launches at sign-in, with explicit enable/disable verification."

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
        stats = QHBoxLayout()
        stats.setSpacing(8)
        self.count_badge = Badge("Entries: —", "muted")
        self.enabled_badge = Badge("Enabled: —", "muted")
        stats.addWidget(self.count_badge)
        stats.addWidget(self.enabled_badge)
        stats.addStretch(1)
        stats.addWidget(make_button("Refresh", variant="ghost", slot=self.refresh))
        stats.addWidget(make_button("Open startup folder", variant="ghost", slot=self.open_startup_folder))
        self.add_layout(stats)

        self.table = DataTable(
            ["Name", "State", "Source", "Scope", "Publisher", "Executable", "Notes"],
            height=430,
        )
        self.table.resize_columns([280, 90, 150, 130, 180, 300, 220])
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.add_widget(self.table)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        actions.addWidget(make_button("Disable selected…", variant="danger", slot=self.disable_selected))
        actions.addWidget(make_button("Enable selected…", variant="ghost", slot=self.enable_selected))
        actions.addStretch(1)
        self.selection_label = hint("")
        actions.addWidget(self.selection_label)
        self.add_layout(actions)

        self.add_widget(
            hint(
                "Startup entries can be re-enabled later. Some vendor entries are protected by their own "
                "services and Windows may override the startup approval state."
            )
        )

    def refresh(self) -> None:
        self.clear_error()
        self.run_async(
            lambda: startup_service.get_startup_entries(self.ctx.runner),
            self._show,
            busy="Reading startup entries…",
        )

    def _show(self, snapshot) -> None:
        self.snapshot = snapshot
        entries = snapshot.entries
        self.count_badge.setText(f"Entries: {len(entries)}")
        enabled = sum(1 for entry in entries if entry.enabled)
        self.enabled_badge.setText(f"Enabled: {enabled}")
        self.enabled_badge.set_status("info")
        if snapshot.errors:
            self.show_error("Some startup locations could not be read: " + "; ".join(snapshot.errors[:3]))
        self.table.set_rows(
            [
                [
                    entry.name,
                    "Enabled" if entry.enabled else "Disabled",
                    entry.source,
                    entry.scope,
                    entry.publisher or "—",
                    entry.executable or "—",
                    "; ".join(entry.warnings[:2]) or ("Protected" if not entry.can_toggle else ""),
                ]
                for entry in entries
            ],
            payloads=entries,
        )
        self._selection_changed()

    def _selection_changed(self) -> None:
        selected = self.table.selected_payloads()
        self.selection_label.setText(f"{len(selected)} selected" if selected else "Select entries.")

    def _toggle(self, enable: bool) -> None:
        selected = self.table.selected_payloads()
        if not selected:
            return
        specs = []
        errors = []
        for entry in selected:
            if not entry.can_toggle:
                errors.append(f"{entry.name}: {entry.protection_reason or 'protected'}")
                continue
            try:
                specs.append(startup_service.build_toggle_spec(entry, enable))
            except Exception as exc:
                errors.append(f"{entry.name}: {exc}")
        if errors:
            QMessageBox.warning(self, "Some entries were skipped", "\n".join(errors[:20]))
        if not specs:
            return
        results = run_operations(
            self.ctx,
            self,
            specs,
            title=("Enable startup entries" if enable else "Disable startup entries"),
            intro="Each startup entry is read back after the change.",
        )
        if results:
            self.refresh()

    def disable_selected(self) -> None:
        self._toggle(False)

    def enable_selected(self) -> None:
        self._toggle(True)

    def open_startup_folder(self) -> None:
        try:
            folders = startup_service.startup_folders()
            target = folders[0][0] if folders else None
            if target and target.is_dir():
                os.startfile(str(target))  # type: ignore[attr-defined]
        except Exception as exc:
            self.show_error(f"The startup folder could not be opened: {exc}")
