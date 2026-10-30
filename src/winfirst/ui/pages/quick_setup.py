"""Quick Setup: pick a profile, inspect the real differences, apply safely."""
from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)

from winfirst.core import settings_registry
from winfirst.core.operation_result import OperationStatus
from winfirst.ui.base_page import BasePage
from winfirst.ui.dialogs import run_operations
from winfirst.ui.widgets import Badge, Card, DataTable, hint, make_button


class QuickSetupPage(BasePage):
    page_key = "quick_setup"
    page_title = "Quick Setup"
    page_subtitle = "Compare a profile with this computer, then apply only the differences you approve."

    def __init__(self, ctx: Any, *, parent: QWidget | None = None) -> None:
        super().__init__(ctx, parent=parent)
        self.profiles: list[Any] = []
        self._last_diff: dict[str, str] = {}
        self._built = False

    def load(self) -> None:
        if not self._built:
            self._build()
            self._built = True
        self._refresh_profiles()

    # -------------------------------------------------------------------- build
    def _build(self) -> None:
        selector = Card("Choose a configuration profile")
        row = QHBoxLayout()
        row.setSpacing(9)
        self.profile_combo = QComboBox()
        self.profile_combo.setMinimumWidth(300)
        self.profile_combo.currentIndexChanged.connect(self._profile_changed)
        row.addWidget(self.profile_combo, 0)
        row.addWidget(
            make_button("Compare with this PC", variant="primary", slot=self.compare)
        )
        row.addStretch(1)
        selector.add_layout(row)
        self.description_label = hint("")
        selector.add(self.description_label)
        self.add_widget(selector)

        self.summary_card = Card("Profile contents", "Expected values from the selected profile.")
        self.summary_table = DataTable(
            ["Setting", "Group", "Profile value"], height=230, stretch_last=False
        )
        self.summary_table.resize_columns([260, 190, 220])
        self.summary_card.add(self.summary_table)
        self.add_widget(self.summary_card)

        diff_card = Card(
            "Differences on this computer",
            "Current values are read from Windows. Unsupported settings are not treated as failures.",
        )
        self.diff_table = DataTable(
            ["Setting", "Profile", "Current", "Status"], height=280, stretch_last=False
        )
        self.diff_table.resize_columns([280, 180, 220, 130])
        diff_card.add(self.diff_table)
        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.apply_diff_button = make_button(
            "Apply differences…", variant="primary", slot=self.apply_differences
        )
        self.apply_all_button = make_button(
            "Apply entire profile…", variant="ghost", slot=self.apply_all
        )
        actions.addWidget(self.apply_diff_button)
        actions.addWidget(self.apply_all_button)
        actions.addStretch(1)
        self.refresh_button = make_button("Refresh", variant="ghost", slot=self.compare)
        actions.addWidget(self.refresh_button)
        diff_card.add_layout(actions)
        self.hint_label = hint(
            "Profiles never include arbitrary scripts or commands. Software and checklist items are "
            "reviewed on their own pages."
        )
        diff_card.add(self.hint_label)
        self.add_widget(diff_card)
        self.add_widget(
            make_button(
                "Manage profile library…",
                variant="link",
                slot=lambda: self.navigate("profiles"),
            )
        )

    # ----------------------------------------------------------------- profiles
    def _refresh_profiles(self) -> None:
        try:
            self.profiles = self.ctx.profiles.list_profiles()
        except Exception as exc:
            self.show_error(f"Profiles could not be loaded: {exc}")
            return
        current = self.profile_combo.currentText()
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        for profile in self.profiles:
            self.profile_combo.addItem(profile.name)
        if current:
            index = self.profile_combo.findText(current)
            if index >= 0:
                self.profile_combo.setCurrentIndex(index)
        self.profile_combo.blockSignals(False)
        self._profile_changed()

    def _selected_profile(self):
        index = self.profile_combo.currentIndex()
        if 0 <= index < len(self.profiles):
            return self.profiles[index]
        return None

    def _profile_changed(self) -> None:
        profile = self._selected_profile()
        if profile is None:
            return
        self.description_label.setText(profile.description or "No description provided.")
        rows = []
        for label, value, group in self.ctx.profiles.setting_summary(profile):
            rows.append([label, group, value])
        self.summary_table.set_rows(rows)
        self._last_diff = {}
        self.diff_table.set_rows([])
        self.compare()

    # ------------------------------------------------------------------ compare
    def compare(self) -> None:
        profile = self._selected_profile()
        if profile is None:
            return
        keys = list(profile.settings)
        self.clear_error()

        def work():
            detected = settings_registry.detect_all(keys)
            return profile, keys, detected

        self.run_async(work, self._show_compare, busy="Comparing profile with this computer…")

    def _show_compare(self, data) -> None:
        profile, keys, detected = data
        rows = []
        diff_keys: dict[str, str] = {}
        for key in keys:
            definition = settings_registry.get_definition(key)
            expected = profile.settings[key]
            detected_value = detected.get(key)
            expected_display = definition.format_value(expected)
            if detected_value is None or not detected_value.supported:
                status = "Unsupported"
                current = detected_value.note if detected_value and detected_value.note else "Unavailable"
            elif not detected_value.exists:
                status = "Not configured"
                current = "Not configured"
                diff_keys[key] = status
            elif settings_registry.values_match(expected, detected_value.value):
                status = "Match"
                current = definition.format_value(detected_value.value)
            else:
                status = "Different"
                current = definition.format_value(detected_value.value)
                diff_keys[key] = status
            rows.append([definition.label, expected_display, current, status])
        self._last_diff = diff_keys
        self.diff_table.set_rows(rows, payloads=keys)
        self.apply_diff_button.setEnabled(bool(diff_keys))
        if not diff_keys:
            self.hint_label.setText(
                "Every supported setting already matches this profile. Nothing needs to be applied."
            )
        else:
            self.hint_label.setText(
                f"{len(diff_keys)} setting(s) differ. Applying is previewed with warnings and verified afterwards."
            )

    # -------------------------------------------------------------------- apply
    def apply_differences(self) -> None:
        profile = self._selected_profile()
        if profile is None or not self._last_diff:
            return
        self._apply(profile, list(self._last_diff), "Apply profile differences")

    def apply_all(self) -> None:
        profile = self._selected_profile()
        if profile is None:
            return
        answer = QMessageBox.question(
            self,
            "Apply the entire profile?",
            f"Apply every supported setting in “{profile.name}”, including values that already match?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._apply(profile, list(profile.settings), "Apply entire profile")

    def _apply(self, profile, keys: list[str], title: str) -> None:
        try:
            specs, skipped = self.ctx.profiles.build_apply_specs(profile, keys)
        except Exception as exc:
            self.show_error(f"The profile could not be converted into operations: {exc}")
            return
        if skipped:
            QMessageBox.information(
                self,
                "Some settings were skipped",
                "These settings cannot be applied automatically:\n\n" + "\n".join(skipped),
            )
        if not specs:
            QMessageBox.information(
                self,
                "Nothing to apply",
                "No supported, automatically verifiable settings were selected.",
            )
            return
        intro = (
            f"Profile: {profile.name}. {len(specs)} setting(s) will be applied. "
            "Each change is verified by reading the value back."
        )
        results = run_operations(
            self.ctx,
            self,
            specs,
            title=title,
            profile_name=profile.name,
            intro=intro,
        )
        if results:
            self.compare()
