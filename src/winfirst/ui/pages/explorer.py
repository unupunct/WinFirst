"""Explorer and desktop customisation, including God Mode management."""
from __future__ import annotations

import os
from typing import Any

from PySide6.QtWidgets import QHBoxLayout, QMessageBox, QWidget

from winfirst.core import settings_registry
from winfirst.services import explorer_service, godmode_service
from winfirst.ui.base_page import BasePage
from winfirst.ui.dialogs import run_operations
from winfirst.ui.settings_panel import SettingsPanel
from winfirst.ui.widgets import Badge, Card, hint, make_button


class ExplorerPage(BasePage):
    page_key = "explorer"
    page_title = "Explorer & Desktop"
    page_subtitle = "File Explorer behaviour, desktop icons and the God Mode shortcuts folder."

    def __init__(self, ctx: Any, *, parent: QWidget | None = None) -> None:
        super().__init__(ctx, parent=parent)
        self._built = False

    def load(self) -> None:
        if not self._built:
            self._build()
            self._built = True
        self.refresh_godmode()
        self.panel.refresh()

    def _build(self) -> None:
        self.add_action_button("Restart Explorer…", slot=self.restart_explorer, variant="ghost")
        gm_card = Card(
            "God Mode",
            "Creates a folder containing shortcuts to existing Windows management tools. It does not unlock hidden features or bypass security.",
        )
        row = QHBoxLayout()
        row.setSpacing(8)
        self.godmode_badge = Badge("Checking…", "muted")
        row.addWidget(self.godmode_badge)
        self.godmode_path = hint("")
        row.addWidget(self.godmode_path, 1)
        row.addWidget(
            make_button("Create…", variant="ghost", slot=self.create_godmode)
        )
        row.addWidget(
            make_button("Remove…", variant="danger", slot=self.remove_godmode)
        )
        row.addWidget(
            make_button("Open Desktop", variant="ghost", slot=self.open_desktop)
        )
        gm_card.add_layout(row)
        self.add_widget(gm_card)

        keys = [
            definition.key
            for definition in settings_registry.all_definitions()
            if definition.group in {"Explorer & Desktop", "Desktop utilities"}
        ]
        self.panel = SettingsPanel(self.ctx, keys, owner=self)
        self.add_widget(self.panel)
        self.add_widget(
            hint(
                "Explorer changes are broadcast to running Explorer windows where Windows supports it. "
                "Some changes only appear in newly opened windows or after Explorer is restarted."
            )
        )

    # ------------------------------------------------------------------ godmode
    def refresh_godmode(self) -> None:
        self.run_async(godmode_service.get_state, self._show_godmode, busy="Checking God Mode…")

    def _show_godmode(self, state) -> None:
        self._godmode_state = state
        self.godmode_badge.setText(state.status)
        if state.exists and state.is_directory:
            self.godmode_badge.set_status("ok")
        elif state.exists:
            self.godmode_badge.set_status("warning")
        else:
            self.godmode_badge.set_status("muted")
        self.godmode_path.setText(str(state.folder))
        if state.error:
            self.show_error(f"God Mode state could not be fully read: {state.error}")

    def create_godmode(self) -> None:
        state = getattr(self, "_godmode_state", None)
        if state is not None and state.exists:
            QMessageBox.information(self, "God Mode already exists", "The managed folder already exists on the Desktop.")
            return
        results = run_operations(
            self.ctx,
            self,
            [godmode_service.build_create_spec()],
            title="Create God Mode folder",
            intro="Only the managed folder at the resolved Desktop path is created.",
        )
        if results:
            self.refresh_godmode()

    def remove_godmode(self) -> None:
        state = getattr(self, "_godmode_state", None)
        if state is None or not state.exists:
            QMessageBox.information(self, "God Mode not present", "There is no managed God Mode folder to remove.")
            return
        if state.strays:
            QMessageBox.warning(
                self,
                "Related folders found",
                "Other God Mode-like folders exist on the Desktop and will not be touched:\n\n"
                + "\n".join(path.name for path in state.strays[:12]),
            )
        results = run_operations(
            self.ctx,
            self,
            [godmode_service.build_remove_spec()],
            title="Remove God Mode folder",
            intro="Only the exact managed folder is removed; unrelated Desktop content is untouched.",
        )
        if results:
            self.refresh_godmode()

    def open_desktop(self) -> None:
        try:
            desk = os.path.join(os.path.expanduser("~"), "Desktop")
            if os.path.isdir(desk):
                os.startfile(desk)  # type: ignore[attr-defined]
        except Exception as exc:
            self.show_error(f"The Desktop folder could not be opened: {exc}")

    # ----------------------------------------------------------------- explorer
    def restart_explorer(self) -> None:
        answer = QMessageBox.question(
            self,
            "Restart File Explorer?",
            "All open Explorer windows and the taskbar will close briefly and then restart. Unsaved "
            "File Explorer views will be lost. Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.run_async(
            lambda: explorer_service.restart_explorer(self.ctx.runner),
            lambda message: self.ctx.status(message),
            busy="Restarting File Explorer…",
        )
