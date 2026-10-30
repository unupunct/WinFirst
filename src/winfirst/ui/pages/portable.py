"""Portable toolkit repository: inventory, hashes and signatures, never execution."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLineEdit,
    QMessageBox,
    QWidget,
)

from winfirst.services import repository_service
from winfirst.ui.base_page import BasePage
from winfirst.ui.dialogs import TextDialog
from winfirst.ui.widgets import Badge, Card, DataTable, format_bytes, hint, make_button


class PortablePage(BasePage):
    page_key = "portable"
    page_title = "Portable Toolkit"
    page_subtitle = "Inventory the tools you carry with you. Files are hashed and inspected, never executed."

    def __init__(self, ctx: Any, *, parent: QWidget | None = None) -> None:
        super().__init__(ctx, parent=parent)
        self.scan = None
        self.items: list[Any] = []
        self._built = False

    def load(self) -> None:
        if not self._built:
            self._build()
            self._built = True
        self.restore_inventory()

    def _build(self) -> None:
        self.add_action_button("Open repository folder", slot=self.open_repository, variant="ghost")

        location = Card(
            "Repository folder",
            "A local folder containing installers, scripts, archives and documentation. "
            "WinFirst never runs anything from this folder.",
        )
        row = QHBoxLayout()
        row.setSpacing(8)
        self.path_input = QLineEdit()
        self.path_input.setPlaceholderText("Folder path")
        row.addWidget(self.path_input, 3)
        row.addWidget(make_button("Browse…", variant="ghost", slot=self.browse))
        row.addWidget(make_button("Set folder", variant="ghost", slot=self.set_folder))
        row.addWidget(make_button("Scan…", variant="primary", slot=self.scan_repository))
        location.add_layout(row)
        self.location_badge = Badge("No inventory", "muted")
        location.add(self.location_badge)
        self.add_widget(location)

        stats = QHBoxLayout()
        stats.setSpacing(8)
        self.count_badge = Badge("Files: —", "muted")
        self.size_badge = Badge("Size: —", "muted")
        self.script_badge = Badge("Scripts: —", "warning")
        stats.addWidget(self.count_badge)
        stats.addWidget(self.size_badge)
        stats.addWidget(self.script_badge)
        stats.addStretch(1)
        self.add_layout(stats)

        self.table = DataTable(
            ["Name", "Kind", "Size", "Signature", "Signer", "SHA256", "Warning"],
            height=380,
        )
        self.table.resize_columns([240, 110, 90, 110, 200, 220, 260])
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.add_widget(self.table)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        actions.addWidget(make_button("Export inventory…", variant="ghost", slot=self.export_inventory))
        actions.addWidget(make_button("Copy checksums", variant="ghost", slot=self.copy_checksums))
        actions.addWidget(make_button("Inspect selected…", variant="ghost", slot=self.inspect_selected))
        actions.addWidget(make_button("Open containing folder", variant="ghost", slot=self.open_containing))
        actions.addStretch(1)
        self.add_layout(actions)

        self.add_widget(
            hint(
                "Script and executable files are labelled so they can be reviewed deliberately. "
                "The portable repository is an inventory and checksum tool, not an installer or launcher."
            )
        )

    # --------------------------------------------------------------------- data
    def restore_inventory(self) -> None:
        try:
            path = repository_service.repository_dir(self.ctx.settings)
        except Exception:
            path = Path("")
        self.path_input.setText(str(path))
        try:
            self.items = self.ctx.repository.load()
        except Exception as exc:
            self.show_error(f"The stored repository inventory could not be loaded: {exc}")
            self.items = []
        if self.items:
            self.location_badge.setText("Stored inventory")
            self.location_badge.set_status("info")
            self._show_items(self.items)
        else:
            self.location_badge.setText("No inventory yet")
            self.location_badge.set_status("muted")
            self._update_stats()

    def _show_items(self, items) -> None:
        self.items = list(items)
        self.table.set_rows(
            [
                [
                    item.name,
                    item.kind,
                    item.size_label,
                    item.signature_label,
                    item.signer or "—",
                    item.sha256[:32] + "…" if len(item.sha256) > 32 else item.sha256,
                    item.warning or item.error or "",
                ]
                for item in self.items
            ],
            payloads=self.items,
        )
        self._update_stats()

    def _update_stats(self) -> None:
        self.count_badge.setText(f"Files: {len(self.items)}")
        total = sum(item.size for item in self.items)
        self.size_badge.setText(f"Size: {format_bytes(total)}")
        scripts = sum(1 for item in self.items if item.is_script)
        self.script_badge.setText(f"Scripts: {scripts}")
        self.script_badge.set_status("warning" if scripts else "muted")

    def _selection_changed(self) -> None:
        selected = self.table.selected_payloads()
        if selected:
            self.ctx.status(selected[0].path)

    # ------------------------------------------------------------------ folder
    def browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Choose repository folder", self.path_input.text().strip() or str(Path.home())
        )
        if not folder:
            return
        self.path_input.setText(folder)
        self.set_folder()

    def set_folder(self) -> None:
        text = self.path_input.text().strip()
        if not text:
            return
        try:
            folder = repository_service.validate_repository_path(text)
            self.ctx.settings.set("repository_dir", str(folder))
        except Exception as exc:
            QMessageBox.warning(self, "Folder rejected", str(exc))
            return
        self.path_input.setText(str(folder))
        self.ctx.status(f"Repository folder set to {folder}")

    def open_repository(self) -> None:
        try:
            folder = repository_service.ensure_repository_dir(self.ctx.settings)
            os.startfile(str(folder))  # type: ignore[attr-defined]
        except Exception as exc:
            self.show_error(f"The repository folder could not be opened: {exc}")

    # -------------------------------------------------------------------- scan
    def scan_repository(self) -> None:
        text = self.path_input.text().strip()
        if not text:
            QMessageBox.information(self, "No folder", "Choose a repository folder first.")
            return
        try:
            folder = repository_service.validate_repository_path(text)
        except Exception as exc:
            QMessageBox.warning(self, "Folder rejected", str(exc))
            return
        self.ctx.settings.set("repository_dir", str(folder))
        self.location_badge.setText("Scanning…")
        self.location_badge.set_status("info")

        def work():
            return repository_service.scan_repository(folder)

        def done(scan) -> None:
            self.scan = scan
            try:
                self.ctx.repository.save(scan)
            except Exception as exc:
                self.show_error(f"The inventory was scanned but could not be stored: {exc}")
            self.location_badge.setText(f"Scanned {str(scan.root)}")
            self.location_badge.set_status("ok")
            if scan.errors:
                self.show_error("Some files could not be inspected: " + "; ".join(scan.errors[:3]))
            if scan.truncated:
                self.show_error("The scan reached its safety limit; some files were not included.")
            self._show_items(scan.items)

        self.run_async(work, done, busy="Hashing and checking repository files…")

    # ----------------------------------------------------------------- exports
    def export_inventory(self) -> None:
        if not self.items:
            QMessageBox.information(self, "Nothing to export", "Scan the repository first.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Export repository inventory",
            str(Path.home() / "winfirst-repository.csv"),
            "CSV files (*.csv)",
        )
        if not path:
            return
        try:
            written = repository_service.export_inventory_csv(self.items, path)
        except Exception as exc:
            QMessageBox.critical(self, "Inventory not exported", str(exc))
            return
        QMessageBox.information(self, "Inventory exported", f"Saved to:\n{written}")

    def copy_checksums(self) -> None:
        if not self.items:
            return
        selected = self.table.selected_payloads()
        names = [item.name for item in selected] or [item.name for item in self.items]
        text = repository_service.copy_checksum(self.items, names)
        QApplication.clipboard().setText(text)
        self.ctx.status(f"Copied {len(text.splitlines())} checksum line(s) to the clipboard.")

    def inspect_selected(self) -> None:
        selected = self.table.selected_payloads()
        if not selected:
            QMessageBox.information(self, "No file selected", "Select a repository file first.")
            return
        item = selected[0]
        lines = [
            f"Name: {item.name}",
            f"Path: {item.path}",
            f"Relative path: {item.relative_path}",
            f"Kind: {item.kind}",
            f"Size: {item.size_label} ({item.size:,} bytes)",
            f"Modified: {item.modified}",
            f"SHA256: {item.sha256}",
            f"Signature: {item.signature_label}",
            f"Signer: {item.signer or 'Not signed or unavailable'}",
            f"Version: {item.version or 'Unknown'}",
            f"Executable/script: {'Yes' if item.is_script else 'No'}",
        ]
        if item.error:
            lines.append(f"Error: {item.error}")
        if item.warning:
            lines.append(f"Warning: {item.warning}")
        TextDialog("Repository file", "\n".join(lines), parent=self).exec()

    def open_containing(self) -> None:
        selected = self.table.selected_payloads()
        if not selected:
            return
        folder = Path(selected[0].path).parent
        try:
            if folder.is_dir():
                os.startfile(str(folder))  # type: ignore[attr-defined]
            else:
                self.show_error("The containing folder no longer exists.")
        except Exception as exc:
            self.show_error(f"The folder could not be opened: {exc}")
