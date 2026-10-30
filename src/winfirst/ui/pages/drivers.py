"""Driver inspection, export and explicit INF import."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from winfirst.services import driver_service
from winfirst.ui.base_page import BasePage
from winfirst.ui.dialogs import run_operations
from winfirst.ui.widgets import Card, DataTable, hint, make_button


class DriverPage(BasePage):
    page_key = "drivers"
    page_title = "Driver Management"
    page_subtitle = "Installed drivers, third-party packages and signed INF installation."

    def __init__(self, ctx: Any, *, parent: QWidget | None = None) -> None:
        super().__init__(ctx, parent=parent)
        self.installed: list[Any] = []
        self.exportable: list[Any] = []
        self.inf_files: list[Path] = []
        self._import_folder = ""
        self._built = False

    def load(self) -> None:
        if not self._built:
            self._build()
            self._built = True
        self.refresh()

    def _build(self) -> None:
        self.add_action_button("Refresh", slot=self.refresh, variant="ghost")
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.add_widget(self.tabs)

        installed = QWidget()
        installed_layout = QVBoxLayout(installed)
        installed_layout.setContentsMargins(0, 12, 0, 0)
        installed_layout.setSpacing(9)
        self.installed_note = QLabel("")
        self.installed_note.setObjectName("SmallMuted")
        self.installed_table = DataTable(
            ["Device", "Class", "Provider", "Version", "Date", "INF", "Signature"],
            height=420,
        )
        self.installed_table.resize_columns([280, 150, 180, 110, 100, 110, 110])
        installed_layout.addWidget(self.installed_note)
        installed_layout.addWidget(self.installed_table)
        self.tabs.addTab(installed, "Installed drivers")

        export_tab = QWidget()
        export_layout = QVBoxLayout(export_tab)
        export_layout.setContentsMargins(0, 12, 0, 0)
        export_layout.setSpacing(9)
        export_layout.addWidget(
            hint(
                "Only published third-party driver packages can be exported. The export is a folder of "
                "packages, not a full system backup."
            )
        )
        self.exportable_table = DataTable(
            ["Published name", "Class", "Provider", "Version", "Date"], height=320
        )
        export_layout.addWidget(self.exportable_table)
        export_actions = QHBoxLayout()
        export_actions.addWidget(
            make_button("Export selected…", variant="primary", slot=self.export_selected)
        )
        export_actions.addWidget(
            make_button("Export all…", variant="ghost", slot=self.export_all)
        )
        export_actions.addStretch(1)
        export_layout.addLayout(export_actions)
        self.tabs.addTab(export_tab, "Export packages")

        import_tab = QWidget()
        import_layout = QVBoxLayout(import_tab)
        import_layout.setContentsMargins(0, 12, 0, 0)
        import_layout.setSpacing(9)
        import_layout.addWidget(
            hint(
                "Installing an INF package changes Windows drivers. WinFirst checks the Authenticode "
                "signature first and requires an explicit confirmation. Hardware mismatch can make a "
                "device unusable; create a restore point first."
            )
        )
        folder_row = QHBoxLayout()
        self.import_folder_label = QLabel("No folder selected")
        self.import_folder_label.setObjectName("SmallMuted")
        folder_row.addWidget(self.import_folder_label, 1)
        folder_row.addWidget(
            make_button("Choose driver folder…", variant="ghost", slot=self.choose_import_folder)
        )
        import_layout.addLayout(folder_row)
        self.import_table = DataTable(["INF file", "Signature", "Size"], height=260)
        import_layout.addWidget(self.import_table)
        import_actions = QHBoxLayout()
        import_actions.addWidget(
            make_button("Install selected INF…", variant="danger", slot=self.import_selected)
        )
        import_actions.addStretch(1)
        import_layout.addLayout(import_actions)
        self.tabs.addTab(import_tab, "Install INF package")

    # -------------------------------------------------------------------- data
    def refresh(self) -> None:
        self.clear_error()

        def work():
            installed, installed_error = driver_service.list_installed_drivers(self.ctx.runner)
            exportable, exportable_error = driver_service.list_exportable_drivers(self.ctx.runner)
            return installed, installed_error, exportable, exportable_error

        self.run_async(work, self._show, busy="Reading driver inventory…")

    def _show(self, data) -> None:
        installed, installed_error, exportable, exportable_error = data
        self.installed = list(installed)
        self.exportable = list(exportable)
        errors = [error for error in (installed_error, exportable_error) if error]
        if errors:
            self.show_error("Some driver information could not be read: " + "; ".join(errors[:3]))
        self.installed_note.setText(f"{len(self.installed)} driver(s) reported by Windows.")
        self.installed_table.set_rows(
            [
                [
                    driver.device_name,
                    driver.device_class or "—",
                    driver.provider or "—",
                    driver.version or "—",
                    driver.date or "—",
                    driver.inf_name or "—",
                    driver.signed or "—",
                ]
                for driver in self.installed
            ],
            payloads=self.installed,
        )
        self.exportable_table.set_rows(
            [
                [
                    driver.published_name,
                    driver.class_name or "—",
                    driver.provider or "—",
                    driver.version or "—",
                    driver.date or "—",
                ]
                for driver in self.exportable
            ],
            payloads=self.exportable,
        )

    # ------------------------------------------------------------------ export
    def export_selected(self) -> None:
        drivers = self.exportable_table.selected_payloads()
        if not drivers:
            QMessageBox.information(self, "No drivers selected", "Select one or more driver packages.")
            return
        self._export(drivers)

    def export_all(self) -> None:
        if not self.exportable:
            return
        self._export(self.exportable)

    def _export(self, drivers) -> None:
        destination = QFileDialog.getExistingDirectory(
            self, "Choose an empty export folder", str(Path.home() / "winfirst-drivers")
        )
        if not destination:
            return
        spec, holder = driver_service.build_export_spec(destination, drivers)
        results = run_operations(
            self.ctx,
            self,
            [spec],
            title="Export driver packages",
            intro="pnputil exports the selected published drivers into separate folders.",
        )
        export_result = holder.get("result")
        if results and export_result is not None:
            lines = [f"Exported {len(export_result.exported)} package(s) to {export_result.destination}"]
            if export_result.errors:
                lines.append("Errors:\n" + "\n".join(export_result.errors[:20]))
            QMessageBox.information(self, "Driver export finished", "\n\n".join(lines))

    # ------------------------------------------------------------------ import
    def choose_import_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose a folder containing INF files")
        if not folder:
            return
        self._import_folder = folder
        self.import_folder_label.setText(folder)
        try:
            inf_files = driver_service.find_inf_files(folder)
        except Exception as exc:
            self.show_error(str(exc))
            return
        if not inf_files:
            QMessageBox.information(self, "No INF files", "No .inf files were found in that folder.")
            return
        runner = self.ctx.runner

        def work():
            return [(path, driver_service.check_inf_signature(runner, str(path))) for path in inf_files]

        self.run_async(work, self._show_inf, busy="Checking INF signatures…")

    def _show_inf(self, results) -> None:
        self.inf_files = [path for path, _signature in results]
        rows = []
        for path, signature in results:
            try:
                size = f"{path.stat().st_size:,} bytes"
            except OSError:
                size = "Unavailable"
            rows.append([path.name, signature, size])
        self.import_table.set_rows(rows, payloads=self.inf_files)
        untrusted = [signature for _path, signature in results if signature.lower() not in {"valid", "notSigned"}]
        if untrusted:
            self.show_error(
                "Some INF files do not have a valid signature. Review them carefully; Windows may still "
                "reject unsigned drivers."
            )

    def import_selected(self) -> None:
        selected = self.import_table.selected_payloads()
        if not selected:
            QMessageBox.information(self, "No INF selected", "Select at least one INF file.")
            return
        if not self._import_folder:
            return
        answer = QMessageBox.warning(
            self,
            "Install driver package?",
            "Installing a driver that does not match the hardware can make a device unusable.\n\n"
            "Continue only if you trust the source and have a recovery path.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            spec = driver_service.build_import_spec(self._import_folder, selected)
        except Exception as exc:
            self.show_error(str(exc))
            return
        results = run_operations(
            self.ctx,
            self,
            [spec],
            title="Install INF driver package",
            intro="pnputil adds each selected INF package. A restart may be required.",
        )
        if results:
            self.refresh()
