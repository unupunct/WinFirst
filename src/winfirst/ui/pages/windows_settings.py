"""Windows Settings: every registry-backed setting in the central registry."""
from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import QLineEdit, QVBoxLayout, QWidget

from winfirst.core import settings_registry
from winfirst.ui.base_page import BasePage
from winfirst.ui.settings_panel import SettingsPanel
from winfirst.ui.widgets import Banner, hint


class WindowsSettingsPage(BasePage):
    page_key = "windows_settings"
    page_title = "Windows Settings"
    page_subtitle = "Supported Explorer, desktop and privacy settings with read-back verification."

    def __init__(self, ctx: Any, *, parent: QWidget | None = None) -> None:
        super().__init__(ctx, parent=parent)
        self._built = False

    def load(self) -> None:
        if not self._built:
            self._build()
            self._built = True
        self.panel.refresh()

    def _build(self) -> None:
        self.add_widget(
            Banner(
                "Security posture values are shown on the Privacy & Security page and cannot be "
                "weakened from WinFirst. These settings are reversible where Windows provides a "
                "reliable rollback value.",
                "info",
            )
        )
        search = QLineEdit()
        search.setPlaceholderText("Filter settings by name or description…")
        search.setClearButtonEnabled(True)
        search.textChanged.connect(self._filter)
        self.add_widget(search)
        self.search_box = search

        keys = [
            definition.key
            for definition in settings_registry.all_definitions()
            if definition.group != "Security status"
        ]
        self.panel = SettingsPanel(self.ctx, keys, owner=self)
        self.add_widget(self.panel)
        self.add_widget(
            hint(
                "Windows Settings applies one value at a time. To apply a coordinated set, use Quick "
                "Setup or a profile. Policy-managed values may be read-only in practice; detection says so."
            )
        )

    def _filter(self, text: str) -> None:
        needle = text.strip().casefold()
        for key, row in self.panel.rows.items():
            definition = row.definition
            haystack = f"{definition.label} {definition.description} {definition.group}".casefold()
            row.setVisible(not needle or needle in haystack)
