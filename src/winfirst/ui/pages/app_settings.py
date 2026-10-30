"""Application preferences, storage locations and local data maintenance."""
from __future__ import annotations

import os
from datetime import datetime
from typing import Any

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QSpinBox,
    QWidget,
)

from winfirst.core import paths
from winfirst.ui.base_page import BasePage
from winfirst.ui.widgets import Card, KeyValueGrid, hint, make_button


class AppSettingsPage(BasePage):
    page_key = "app_settings"
    page_title = "Application Settings"
    page_subtitle = "WinFirst preferences, local storage locations and database maintenance."

    def __init__(self, ctx: Any, *, parent: QWidget | None = None) -> None:
        super().__init__(ctx, parent=parent)
        self._built = False

    def load(self) -> None:
        if not self._built:
            self._build()
            self._built = True
            self.reload_values()

    def _build(self) -> None:
        prefs = Card("Preferences", "These settings apply to WinFirst itself, not to Windows.")
        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setHorizontalSpacing(18)
        form.setVerticalSpacing(9)

        self.theme_combo = QComboBox()
        self.theme_combo.addItem("Dark", "dark")
        self.theme_combo.addItem("Light", "light")
        form.addRow("Theme", self.theme_combo)

        self.retention_input = QSpinBox()
        self.retention_input.setRange(30, 3650)
        self.retention_input.setSuffix(" days")
        form.addRow("History retention", self.retention_input)

        self.auto_refresh = QCheckBox("Refresh the dashboard when it is opened")
        form.addRow("Dashboard", self.auto_refresh)

        self.confirm_close = QCheckBox("Ask before closing while an operation is running")
        form.addRow("Closing", self.confirm_close)

        self.show_info_diffs = QCheckBox("Show informational differences in drift reports")
        form.addRow("Drift", self.show_info_diffs)

        self.restore_default = QCheckBox(
            "Offer to create a system restore point before applying changes"
        )
        form.addRow("Safety net", self.restore_default)

        self.author_input = QLineEdit()
        self.author_input.setPlaceholderText("Optional name printed on generated reports")
        form.addRow("Report author", self.author_input)

        self.template_combo = QComboBox()
        form.addRow("Default checklist", self.template_combo)

        prefs.add_layout(form)
        prefs_row = QHBoxLayout()
        prefs_row.addWidget(make_button("Save preferences", variant="primary", slot=self.save_values))
        prefs_row.addWidget(make_button("Reload", variant="ghost", slot=self.reload_values))
        prefs_row.addStretch(1)
        prefs.add_layout(prefs_row)
        self.add_widget(prefs)

        locations = Card("Storage locations", "WinFirst never writes outside these folders except when you export.")
        self.locations_grid = KeyValueGrid()
        locations.add(self.locations_grid)
        location_row = QHBoxLayout()
        location_row.setSpacing(8)
        location_row.addWidget(make_button("Open data folder", variant="ghost", slot=lambda: self._open(paths.data_dir())))
        location_row.addWidget(make_button("Open logs", variant="ghost", slot=lambda: self._open(paths.logs_dir())))
        location_row.addWidget(make_button("Open reports", variant="ghost", slot=lambda: self._open(paths.reports_dir())))
        location_row.addWidget(make_button("Copy paths", variant="ghost", slot=self.copy_paths))
        location_row.addStretch(1)
        locations.add_layout(location_row)
        self.add_widget(locations)

        safety = Card(
            "System restore",
            "A restore point makes a batch of configuration changes recoverable. "
            "System Protection must be enabled in Windows; WinFirst never enables it silently.",
        )
        self.restore_status = QLabel("Checking System Restore availability...")
        self.restore_status.setWordWrap(True)
        self.restore_status.setObjectName("SmallMuted")
        safety.add(self.restore_status)
        safety_row = QHBoxLayout()
        safety_row.setSpacing(8)
        safety_row.addWidget(
            make_button("Create restore point now", variant="ghost", slot=self.create_restore_point)
        )
        safety_row.addWidget(
            make_button("Refresh status", variant="ghost", slot=self.refresh_restore_status)
        )
        safety_row.addStretch(1)
        safety.add_layout(safety_row)
        self.add_widget(safety)

        database = Card("Local database", "Profiles, audit history, drift snapshots and report records live here.")
        self.database_grid = KeyValueGrid()
        database.add(self.database_grid)
        db_row = QHBoxLayout()
        db_row.setSpacing(8)
        db_row.addWidget(make_button("Run integrity check", variant="ghost", slot=self.integrity_check))
        db_row.addWidget(make_button("Apply retention now", variant="ghost", slot=self.apply_retention))
        db_row.addWidget(make_button("Clear operation historyâ€¦", variant="danger", slot=self.clear_history))
        db_row.addStretch(1)
        database.add_layout(db_row)
        self.add_widget(database)

        self.add_widget(
            hint(
                "WinFirst stores no passwords or document contents. Environment values that look like secrets "
                "are masked before they are logged or reported."
            )
        )

    # --------------------------------------------------------------------- data
    def reload_values(self) -> None:
        settings = self.ctx.settings
        index = self.theme_combo.findData(self.ctx.current_theme())
        self.theme_combo.setCurrentIndex(max(0, index))
        self.retention_input.setValue(settings.get_int("history_retention_days", 365))
        self.auto_refresh.setChecked(settings.get_bool("dashboard_auto_refresh", True))
        self.confirm_close.setChecked(settings.get_bool("confirm_before_close", False))
        self.show_info_diffs.setChecked(settings.get_bool("show_informational_diffs", True))
        self.restore_default.setChecked(settings.get_bool("restore_point_before_apply", True))
        self.author_input.setText(settings.get("report_author", ""))
        try:
            templates = self.ctx.deployment.list_templates()
        except Exception:
            templates = []
        current = settings.get("checklist_template", "")
        self.template_combo.clear()
        self.template_combo.addItem("No default", "")
        for template in templates:
            self.template_combo.addItem(template.name, template.name)
        template_index = self.template_combo.findData(current)
        self.template_combo.setCurrentIndex(max(0, template_index))
        self.refresh_restore_status()

        locations = self.ctx.locations()
        self.locations_grid.set_rows(
            [
                ("Installation", locations.get("install", "")),
                ("Data", locations.get("data", "")),
                ("Database", locations.get("database", "")),
                ("Logs", locations.get("logs", "")),
                ("Reports", locations.get("reports", "")),
                ("Portable data mode", locations.get("portable_mode", "no")),
            ]
        )
        try:
            stats = self.ctx.db.stats()
        except Exception as exc:
            stats = {"path": str(exc)}
        self.database_grid.set_rows(
            [
                ("Database", str(stats.get("path") or "")),
                ("Schema version", stats.get("schema_version", "")),
                ("Operations", stats.get("operations", "")),
                ("Profiles", stats.get("profiles", "")),
                ("Rollback entries", stats.get("rollback_entries", "")),
                ("Reports", stats.get("reports", "")),
            ]
        )

    def save_values(self) -> None:
        settings = self.ctx.settings
        try:
            settings.set_many(
                {
                    "theme": self.theme_combo.currentData() or "dark",
                    "history_retention_days": self.retention_input.value(),
                    "dashboard_auto_refresh": self.auto_refresh.isChecked(),
                    "confirm_before_close": self.confirm_close.isChecked(),
                    "show_informational_diffs": self.show_info_diffs.isChecked(),
                    "restore_point_before_apply": self.restore_default.isChecked(),
                    "report_author": self.author_input.text().strip(),
                    "checklist_template": self.template_combo.currentData() or "",
                }
            )
        except Exception as exc:
            self.show_error(f"Preferences could not be saved: {exc}")
            return
        self.ctx.set_theme(self.theme_combo.currentData() or "dark")
        self.ctx.status("Preferences saved.")
        self.reload_values()

    # ------------------------------------------------------------------ safety
    def refresh_restore_status(self) -> None:
        try:
            support = self.ctx.restore_points.support()
            self.restore_status.setText(support.summary())
        except Exception as exc:
            self.restore_status.setText(f"System Restore status could not be read: {exc}")

    def create_restore_point(self) -> None:
        from winfirst.ui.dialogs import run_operations

        try:
            support = self.ctx.restore_points.support()
        except Exception as exc:
            self.show_error(f"System Restore status could not be read: {exc}")
            return
        if not support.can_create:
            self.show_error(support.summary())
            return
        if not self.ctx.elevated:
            self.show_error(
                "Creating a restore point requires administrator rights. "
                "Restart WinFirst as administrator and try again."
            )
            return
        description = f"WinFirst manual checkpoint {datetime.now():%Y-%m-%d %H:%M}"
        try:
            spec = self.ctx.restore_points.build_spec(description)
        except Exception as exc:
            self.show_error(f"The restore point operation could not be prepared: {exc}")
            return
        run_operations(
            self.ctx,
            self,
            [spec],
            title="Create a system restore point",
            intro="WinFirst will ask Windows for a checkpoint and confirm it is listed afterwards.",
            offer_restore_point=False,
        )
        self.refresh_restore_status()

    # -------------------------------------------------------------------- paths
    def _open(self, path) -> None:
        try:
            path.mkdir(parents=True, exist_ok=True)
            os.startfile(str(path))  # type: ignore[attr-defined]
        except Exception as exc:
            self.show_error(f"The folder could not be opened: {exc}")

    def copy_paths(self) -> None:
        from PySide6.QtWidgets import QApplication

        locations = self.ctx.locations()
        text = "\n".join(f"{key}: {value}" for key, value in locations.items())
        QApplication.clipboard().setText(text)
        self.ctx.status("Paths copied to the clipboard.")

    # ---------------------------------------------------------------- database
    def integrity_check(self) -> None:
        try:
            result = self.ctx.db.integrity_check()
        except Exception as exc:
            self.show_error(f"The integrity check failed: {exc}")
            return
        if result.lower() == "ok":
            QMessageBox.information(self, "Database integrity", "The local database passed SQLite's integrity check.")
        else:
            QMessageBox.warning(
                self,
                "Database integrity",
                f"SQLite reported:\n\n{result}\n\nCreate a diagnostic bundle and back up the data folder.",
            )

    def apply_retention(self) -> None:
        days = self.retention_input.value()
        try:
            removed = self.ctx.audit.apply_retention(days)
        except Exception as exc:
            self.show_error(f"Retention could not be applied: {exc}")
            return
        QMessageBox.information(
            self,
            "Retention applied",
            f"Removed {removed} operation record(s) older than {days} days. "
            "Rollback entries that were attached to those operations are removed with them.",
        )
        self.reload_values()
        self.ctx.notify_history_changed()

    def clear_history(self) -> None:
        answer = QMessageBox.question(
            self,
            "Clear operation history?",
            "This permanently deletes every local operation record and its rollback entries. "
            "Profiles, reports and generated files are not deleted. This cannot be undone.\n\n"
            "Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            count = self.ctx.audit.clear()
        except Exception as exc:
            self.show_error(f"History could not be cleared: {exc}")
            return
        QMessageBox.information(self, "History cleared", f"Removed {count} operation record(s).")
        self.reload_values()
        self.ctx.notify_history_changed()
