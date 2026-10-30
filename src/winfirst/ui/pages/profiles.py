"""Profile library management."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QWidget,
)

from winfirst.models.profile import Profile
from winfirst.ui.base_page import BasePage
from winfirst.ui.dialogs import TextDialog, run_operations
from winfirst.ui.widgets import Badge, Card, DataTable, hint, make_button


class ProfilesPage(BasePage):
    page_key = "profiles"
    page_title = "Profiles"
    page_subtitle = "Reusable, validated configuration documents. Built-in profiles are read-only."

    def __init__(self, ctx: Any, *, parent: QWidget | None = None) -> None:
        super().__init__(ctx, parent=parent)
        self.profiles: list[Profile] = []
        self._built = False

    def load(self) -> None:
        if not self._built:
            self._build()
            self._built = True
        self.refresh()

    def _build(self) -> None:
        self.add_action_button("Create from this PC…", slot=self.create_profile, variant="primary")
        self.profiles_table = DataTable(
            ["Profile", "Source", "Settings", "Software", "Checklist", "Updated"], height=250
        )
        self.profiles_table.resize_columns([280, 110, 90, 90, 90, 160])
        self.profiles_table.itemSelectionChanged.connect(self._selection_changed)
        self.add_widget(self.profiles_table)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        actions.addWidget(make_button("Apply selected…", variant="primary", slot=self.apply_profile))
        actions.addWidget(make_button("Duplicate…", variant="ghost", slot=self.duplicate_profile))
        actions.addWidget(make_button("Export…", variant="ghost", slot=self.export_profile))
        actions.addWidget(make_button("Import…", variant="ghost", slot=self.import_profile))
        actions.addWidget(make_button("Versions…", variant="ghost", slot=self.show_versions))
        actions.addWidget(make_button("Delete…", variant="danger", slot=self.delete_profile))
        actions.addStretch(1)
        self.add_layout(actions)

        details = Card("Selected profile")
        self.details_label = QLabel("Select a profile to inspect its settings.")
        self.details_label.setWordWrap(True)
        self.details_label.setObjectName("SmallMuted")
        details.add(self.details_label)
        self.details_table = DataTable(["Setting", "Group", "Profile value"], height=280)
        details.add(self.details_table)
        self.add_widget(details)
        self.add_widget(
            hint(
                "Profiles are declarative JSON. They cannot contain scripts or shell commands. Imported "
                "profiles are validated before they enter the library."
            )
        )

    # --------------------------------------------------------------------- data
    def refresh(self) -> None:
        try:
            self.profiles = self.ctx.profiles.list_profiles()
        except Exception as exc:
            self.show_error(f"Profiles could not be loaded: {exc}")
            return
        self.profiles_table.set_rows(
            [
                [
                    profile.name,
                    "Built-in" if profile.is_builtin else "Custom",
                    profile.setting_count,
                    profile.software_count,
                    profile.checklist_count,
                    profile.updated_at.replace("T", " ")[:16] if profile.updated_at else "—",
                ]
                for profile in self.profiles
            ],
            payloads=self.profiles,
        )
        self._selection_changed()

    def _selected(self) -> Profile | None:
        selected = self.profiles_table.selected_payloads()
        return selected[0] if selected else None

    def _selection_changed(self) -> None:
        profile = self._selected()
        if profile is None:
            self.details_label.setText("Select a profile to inspect its settings.")
            self.details_table.set_rows([])
            return
        source = "Built-in profile" if profile.is_builtin else "Custom profile"
        self.details_label.setText(
            f"{profile.name} · {source} · version {profile.version} · {profile.description or 'No description'}"
        )
        self.details_table.set_rows(
            [[label, group, value] for label, value, group in self.ctx.profiles.setting_summary(profile)]
        )

    # ------------------------------------------------------------------- create
    def create_profile(self) -> None:
        name, ok = QInputDialog.getText(self, "New profile", "Profile name:")
        if not ok or not name.strip():
            return
        name = name.strip()
        if any(profile.name.casefold() == name.casefold() for profile in self.profiles):
            QMessageBox.warning(self, "Name already used", "Choose a different profile name.")
            return
        description, ok = QInputDialog.getText(self, "Profile description", "Description (optional):")
        if not ok:
            return

        def work():
            captured, notes = self.ctx.profiles.capture_current()
            return captured, notes

        def done(data) -> None:
            captured, notes = data
            if not captured:
                QMessageBox.warning(
                    self,
                    "Nothing captured",
                    "No supported settings could be read from this computer. The profile was not created.\n\n"
                    + "\n".join(notes[:20]),
                )
                return
            document = Profile(
                name=name,
                description=description.strip(),
                settings=captured,
            ).to_document()
            try:
                self.ctx.profiles.save_profile(document, note="Captured from this computer")
            except Exception as exc:
                QMessageBox.critical(self, "Profile not saved", str(exc))
                return
            if notes:
                QMessageBox.information(
                    self,
                    "Profile created with notes",
                    "Some values were not captured:\n\n" + "\n".join(notes[:20]),
                )
            self.refresh()

        self.run_async(work, done, busy="Capturing current settings…")

    # ------------------------------------------------------------------ actions
    def apply_profile(self) -> None:
        profile = self._selected()
        if profile is None:
            return
        try:
            specs, skipped = self.ctx.profiles.build_apply_specs(profile)
        except Exception as exc:
            self.show_error(str(exc))
            return
        if skipped:
            QMessageBox.information(self, "Some settings cannot be applied", "\n".join(skipped[:30]))
        if not specs:
            QMessageBox.information(self, "Nothing to apply", "This profile has no automatically applicable settings.")
            return
        results = run_operations(
            self.ctx,
            self,
            specs,
            title=f"Apply profile: {profile.name}",
            profile_name=profile.name,
            intro=f"{len(specs)} setting(s) will be applied and read back.",
        )
        if results:
            self.refresh()

    def duplicate_profile(self) -> None:
        profile = self._selected()
        if profile is None:
            return
        name, ok = QInputDialog.getText(
            self, "Duplicate profile", "New profile name:", text=f"{profile.name} (copy)"
        )
        if not ok or not name.strip():
            return
        try:
            self.ctx.profiles.duplicate_profile(profile.name, name.strip())
        except Exception as exc:
            QMessageBox.critical(self, "Profile not duplicated", str(exc))
            return
        self.refresh()

    def export_profile(self) -> None:
        profile = self._selected()
        if profile is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Export profile",
            str(Path.home() / f"winfirst-{profile.name.replace(' ', '-').lower()}.json"),
            "JSON files (*.json)",
        )
        if not path:
            return
        try:
            written = self.ctx.profiles.export_profile(profile.name, path)
        except Exception as exc:
            QMessageBox.critical(self, "Profile not exported", str(exc))
            return
        QMessageBox.information(self, "Profile exported", f"Saved to:\n{written}")

    def import_profile(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Import profile", str(Path.home()), "JSON files (*.json)"
        )
        if not path:
            return
        overwrite = False
        answer = QMessageBox.question(
            self,
            "Overwrite existing profile?",
            "If a profile with the same name already exists, should it be overwritten?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Cancel:
            return
        overwrite = answer == QMessageBox.StandardButton.Yes
        try:
            profile = self.ctx.profiles.import_profile(path, overwrite=overwrite)
        except Exception as exc:
            QMessageBox.critical(self, "Profile rejected", str(exc))
            return
        QMessageBox.information(self, "Profile imported", f"Imported “{profile.name}”.")
        self.refresh()

    def show_versions(self) -> None:
        profile = self._selected()
        if profile is None:
            return
        try:
            versions = self.ctx.profiles.version_history(profile.name)
        except Exception as exc:
            self.show_error(str(exc))
            return
        if not versions:
            QMessageBox.information(self, "Version history", "No version records are stored for this profile.")
            return
        lines = [
            f"Version {row.get('version')} · {row.get('created_at')} · {row.get('note') or 'No note'}"
            for row in versions
        ]
        TextDialog(f"Versions · {profile.name}", "\n".join(lines), parent=self).exec()

    def delete_profile(self) -> None:
        profile = self._selected()
        if profile is None:
            return
        if profile.is_builtin:
            QMessageBox.warning(self, "Built-in profile", "Built-in profiles cannot be deleted.")
            return
        answer = QMessageBox.question(
            self,
            "Delete profile?",
            f"Delete the custom profile “{profile.name}”? The profile and its version history are removed. "
            "This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.ctx.profiles.delete_profile(profile.name)
        except Exception as exc:
            QMessageBox.critical(self, "Profile not deleted", str(exc))
            return
        self.refresh()
