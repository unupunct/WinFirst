"""Software catalog and verified installation."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLineEdit,
    QMessageBox,
    QWidget,
)

from winfirst.services import software_catalog_service as catalog_service
from winfirst.services import software_install_service as install_service
from winfirst.ui.base_page import BasePage
from winfirst.ui.dialogs import run_operations
from winfirst.ui.widgets import Badge, Banner, Card, DataTable, hint, make_button


class SoftwarePage(BasePage):
    page_key = "software"
    page_title = "Software Installation"
    page_subtitle = "Install only packages from the validated catalog, followed by independent verification."

    def __init__(self, ctx: Any, *, parent: QWidget | None = None) -> None:
        super().__init__(ctx, parent=parent)
        self.catalog = None
        self.winget = None
        self.packages: list[Any] = []
        self._built = False
        self._installed: dict[str, str] = {}

    def load(self) -> None:
        if not self._built:
            self._build()
            self._built = True
        self.refresh()

    def _build(self) -> None:
        status_card = Card(
            "Package manager",
            "WinGet is the only remote installer. Local catalog entries must point at an existing installer "
            "and define their own verification method.",
        )
        row = QHBoxLayout()
        row.setSpacing(8)
        self.winget_badge = Badge("Checking WinGet…", "muted")
        row.addWidget(self.winget_badge)
        self.catalog_badge = Badge("Catalog not loaded", "muted")
        row.addWidget(self.catalog_badge)
        row.addStretch(1)
        row.addWidget(make_button("Reload catalog", variant="ghost", slot=self.refresh))
        row.addWidget(
            make_button("Import catalog…", variant="ghost", slot=self.import_catalog)
        )
        row.addWidget(
            make_button("Reset to built-in", variant="ghost", slot=self.reset_catalog)
        )
        status_card.add_layout(row)
        self.add_widget(status_card)

        filters = Card()
        filter_row = QHBoxLayout()
        filter_row.setSpacing(8)
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Search packages…")
        self.search_box.setClearButtonEnabled(True)
        self.search_box.textChanged.connect(self._apply_filter)
        filter_row.addWidget(self.search_box, 2)
        self.category_combo = QComboBox()
        self.category_combo.addItem("All categories", "")
        self.category_combo.currentIndexChanged.connect(self._apply_filter)
        filter_row.addWidget(self.category_combo, 1)
        filter_row.addWidget(make_button("Select shown", variant="ghost", slot=self.select_shown))
        filter_row.addWidget(make_button("Clear selection", variant="ghost", slot=self.clear_selection))
        filters.add_layout(filter_row)
        self.add_widget(filters)

        self.table = DataTable(
            ["", "Package", "Publisher", "Category", "Source", "Version", "Installed"],
            height=420,
            stretch_last=False,
        )
        self.table.resize_columns([34, 300, 190, 170, 100, 110, 180])
        self.add_widget(self.table)

        consent_row = QHBoxLayout()
        self.consent = QCheckBox(
            "I have the right to install these packages and accept their licence terms."
        )
        consent_row.addWidget(self.consent, 1)
        self.add_layout(consent_row)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        actions.addWidget(
            make_button("Install selected…", variant="primary", slot=self.install_selected)
        )
        actions.addWidget(
            make_button("Check selected…", variant="ghost", slot=self.check_selected)
        )
        actions.addWidget(
            make_button("Save selection…", variant="ghost", slot=self.save_selection)
        )
        actions.addWidget(
            make_button("Load selection…", variant="ghost", slot=self.load_selection)
        )
        actions.addStretch(1)
        self.selection_label = hint("")
        actions.addWidget(self.selection_label)
        self.add_layout(actions)

        self.add_widget(
            hint(
                "WinFirst does not download scripts or execute arbitrary commands. A package is only "
                "reported as installed when its catalog verification method confirms it independently."
            )
        )

    # --------------------------------------------------------------------- data
    def refresh(self) -> None:
        self.clear_error()

        def work():
            catalog = catalog_service.load_catalog()
            winget = install_service.find_winget(self.ctx.runner)
            return catalog, winget

        self.run_async(work, self._show_data, busy="Loading software catalog…")

    def _show_data(self, data) -> None:
        catalog, winget = data
        self.catalog = catalog
        self.winget = winget
        self.packages = list(catalog.packages)
        self.catalog_badge.setText(f"{len(self.packages)} catalog packages")
        self.catalog_badge.set_status("info")
        self.catalog_badge.setToolTip(catalog.path)
        if winget.available:
            self.winget_badge.setText(f"WinGet {winget.version}")
            self.winget_badge.set_status("ok")
            self.winget_badge.setToolTip(winget.path)
        else:
            self.winget_badge.setText("WinGet unavailable")
            self.winget_badge.set_status("critical")
            self.winget_badge.setToolTip(winget.error)
            self.show_error(
                "WinGet is not available, so remote packages cannot be installed. Local catalog entries "
                "may still work if their installers exist."
            )
        if catalog.warnings:
            self.show_error("; ".join(catalog.warnings))
        categories = sorted({package.category for package in self.packages if package.category})
        current = self.category_combo.currentData()
        self.category_combo.blockSignals(True)
        self.category_combo.clear()
        self.category_combo.addItem("All categories", "")
        for category in categories:
            self.category_combo.addItem(category, category)
        if current:
            index = self.category_combo.findData(current)
            if index >= 0:
                self.category_combo.setCurrentIndex(index)
        self.category_combo.blockSignals(False)
        self._apply_filter()

    def _apply_filter(self) -> None:
        needle = self.search_box.text().strip().casefold()
        category = self.category_combo.currentData() or ""
        packages = [
            package
            for package in self.packages
            if (not category or package.category == category)
            and (
                not needle
                or needle
                in f"{package.name} {package.publisher} {package.id} {package.description}".casefold()
            )
        ]
        rows = []
        for package in packages:
            rows.append(
                [
                    "",
                    package.name,
                    package.publisher or "—",
                    package.category or "—",
                    package.source_label,
                    package.display_version,
                    self._installed.get(package.id, "Not checked"),
                ]
            )
        self.table.set_rows(rows, payloads=packages)
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is not None:
                item.setFlags(
                    item.flags()
                    | Qt.ItemFlag.ItemIsUserCheckable
                    | Qt.ItemFlag.ItemIsEnabled
                    | Qt.ItemFlag.ItemIsSelectable
                )
                item.setCheckState(Qt.CheckState.Unchecked)
        self._update_selection_label()

    def _checked_packages(self) -> list[Any]:
        result = []
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is not None and item.checkState() == Qt.CheckState.Checked:
                package = self.table.payload_at(row)
                if package is not None:
                    result.append(package)
        return result

    def _update_selection_label(self) -> None:
        if not hasattr(self, "selection_label"):
            return
        selected = self._checked_packages()
        self.selection_label.setText(f"{len(selected)} package(s) selected")

    def select_shown(self) -> None:
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is not None:
                item.setCheckState(Qt.CheckState.Checked)
        self._update_selection_label()

    def clear_selection(self) -> None:
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is not None:
                item.setCheckState(Qt.CheckState.Unchecked)
        self._update_selection_label()

    # ------------------------------------------------------------------- install
    def install_selected(self) -> None:
        packages = self._checked_packages()
        if not packages:
            QMessageBox.information(self, "No packages selected", "Select at least one package to install.")
            return
        if not self.consent.isChecked():
            QMessageBox.warning(
                self,
                "Consent required",
                "Confirm that you are allowed to install these packages and accept their licence terms.",
            )
            return
        specs = []
        errors = []
        for package in packages:
            try:
                specs.append(
                    install_service.build_install_spec(
                        package,
                        accept_agreements=True,
                        winget_status=self.winget,
                    )
                )
            except Exception as exc:
                errors.append(f"{package.name}: {exc}")
        if errors:
            QMessageBox.warning(
                self,
                "Some packages cannot be installed",
                "\n".join(errors[:20]),
            )
        if not specs:
            return
        results = run_operations(
            self.ctx,
            self,
            specs,
            title="Install selected software",
            intro=(
                "WinGet runs in silent agreement mode for the selected catalog packages. Each install is "
                "verified with the package's own verification method."
            ),
        )
        if results:
            self.check_packages(packages)

    def check_selected(self) -> None:
        selected = self.table.selected_payloads()
        checked = self._checked_packages()
        packages = checked or selected
        if not packages:
            QMessageBox.information(self, "No packages selected", "Select packages to check.")
            return
        self.check_packages(packages)

    def check_packages(self, packages: list[Any]) -> None:
        if self.winget is None:
            return
        status = self.winget

        def work():
            results = {}
            for package in packages[:40]:
                try:
                    check = install_service.check_installed(package, status)
                    results[package.id] = check.detail if check.installed else "Not installed"
                except Exception as exc:
                    results[package.id] = f"Check failed: {exc}"
            return results

        self.run_async(work, self._show_checks, busy="Checking installed packages…")

    def _show_checks(self, results: dict[str, str]) -> None:
        self._installed.update(results)
        self._apply_filter()

    # ------------------------------------------------------------------ catalog
    def import_catalog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Import software catalog", str(Path.home()), "JSON files (*.json)"
        )
        if not path:
            return
        try:
            catalog = catalog_service.import_catalog(path)
        except Exception as exc:
            QMessageBox.critical(self, "Catalog rejected", str(exc))
            return
        QMessageBox.information(
            self,
            "Catalog imported",
            f"Imported {len(catalog.packages)} validated packages. The previous catalog was backed up.",
        )
        self.refresh()

    def reset_catalog(self) -> None:
        answer = QMessageBox.question(
            self,
            "Reset catalog?",
            "Remove the user catalog override and return to the built-in catalog? A backup is kept.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            catalog_service.reset_catalog()
        except Exception as exc:
            QMessageBox.critical(self, "Catalog reset failed", str(exc))
            return
        self.refresh()

    def save_selection(self) -> None:
        if self.catalog is None:
            return
        ids = [package.id for package in self._checked_packages()]
        if not ids:
            QMessageBox.information(self, "No selection", "Select packages before saving a selection.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save software selection",
            str(Path.home() / "winfirst-software-selection.json"),
            "JSON files (*.json)",
        )
        if not path:
            return
        try:
            written = catalog_service.save_selection(path, ids, self.catalog)
        except Exception as exc:
            QMessageBox.critical(self, "Selection not saved", str(exc))
            return
        self.ctx.status(f"Selection saved to {written}")

    def load_selection(self) -> None:
        if self.catalog is None:
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Load software selection", str(Path.home()), "JSON files (*.json)"
        )
        if not path:
            return
        try:
            packages, warnings = catalog_service.load_selection(path, self.catalog)
        except Exception as exc:
            QMessageBox.critical(self, "Selection not loaded", str(exc))
            return
        if warnings:
            QMessageBox.information(self, "Selection warnings", "\n".join(warnings[:20]))
        wanted = {package.id for package in packages}
        self.clear_selection()
        for row in range(self.table.rowCount()):
            package = self.table.payload_at(row)
            item = self.table.item(row, 0)
            if package is not None and item is not None and package.id in wanted:
                item.setCheckState(Qt.CheckState.Checked)
        self._update_selection_label()
