"""Storage analysis, cache cleanup and large-folder inspection."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from winfirst.services import cleanup_service, storage_health_service
from winfirst.ui.base_page import BasePage
from winfirst.ui.dialogs import run_operations
from winfirst.ui.widgets import (
    Badge,
    Card,
    DataTable,
    StatCard,
    format_bytes,
    hint,
    make_button,
)


class StoragePage(BasePage):
    page_key = "storage"
    page_title = "Storage Analysis"
    page_subtitle = "Volumes, disks, approved cache cleanup and large-folder inspection."

    def __init__(self, ctx: Any, *, parent: QWidget | None = None) -> None:
        super().__init__(ctx, parent=parent)
        self.snapshot = None
        self.analysis = None
        self._built = False

    def load(self) -> None:
        if not self._built:
            self._build()
            self._built = True
        self.refresh()
        self.analyze_cleanup()

    def _build(self) -> None:
        self.add_action_button("Refresh volumes", slot=self.refresh, variant="ghost")
        stats = QHBoxLayout()
        stats.setSpacing(10)
        self.stat_total = StatCard("Total capacity", "—")
        self.stat_free = StatCard("Free space", "—")
        self.stat_low = StatCard("Low-space volumes", "—")
        for card in (self.stat_total, self.stat_free, self.stat_low):
            stats.addWidget(card, 1)
        self.add_layout(stats)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.add_widget(self.tabs)

        volumes = QWidget()
        volumes_layout = QVBoxLayout(volumes)
        volumes_layout.setContentsMargins(0, 12, 0, 0)
        volumes_layout.setSpacing(9)
        self.smart_label = QLabel("")
        self.smart_label.setWordWrap(True)
        self.smart_label.setObjectName("SmallMuted")
        self.volumes_table = DataTable(
            ["Volume", "Label", "Filesystem", "Total", "Used", "Free", "Used %", "Health"], height=190
        )
        self.disks_table = DataTable(
            ["Disk", "Media", "Bus", "Size", "Health", "Wear", "Temperature", "Read errors", "Write errors"],
            height=170,
        )
        volumes_layout.addWidget(self.smart_label)
        volumes_layout.addWidget(self.volumes_table)
        volumes_layout.addWidget(self.disks_table)
        volumes_layout.addWidget(
            hint("SMART values are reported by Windows and may be unavailable depending on the hardware and driver.")
        )
        self.tabs.addTab(volumes, "Volumes & disks")

        cleanup = QWidget()
        cleanup_layout = QVBoxLayout(cleanup)
        cleanup_layout.setContentsMargins(0, 12, 0, 0)
        cleanup_layout.setSpacing(9)
        cleanup_toolbar = QHBoxLayout()
        cleanup_toolbar.setSpacing(8)
        self.cleanup_badge = Badge("Not analyzed", "muted")
        cleanup_toolbar.addWidget(self.cleanup_badge)
        cleanup_toolbar.addStretch(1)
        cleanup_toolbar.addWidget(QLabel("Only delete files older than"))
        self.age_input = QSpinBox()
        self.age_input.setRange(1, 720)
        self.age_input.setValue(24)
        self.age_input.setSuffix(" hours")
        cleanup_toolbar.addWidget(self.age_input)
        cleanup_toolbar.addWidget(make_button("Rescan", variant="ghost", slot=self.analyze_cleanup))
        cleanup_layout.addLayout(cleanup_toolbar)
        self.cleanup_table = DataTable(
            ["", "Target", "Description", "Estimated size", "Files", "Warnings"], height=270
        )
        self.cleanup_table.resize_columns([34, 220, 360, 130, 90, 260])
        self.cleanup_table.itemChanged.connect(lambda _item: self._cleanup_label())
        cleanup_layout.addWidget(self.cleanup_table)
        cleanup_actions = QHBoxLayout()
        cleanup_actions.setSpacing(8)
        cleanup_actions.addWidget(make_button("Select all available", variant="ghost", slot=self.select_all_cleanup))
        cleanup_actions.addWidget(make_button("Clean selected…", variant="danger", slot=self.clean_selected))
        cleanup_actions.addStretch(1)
        self.cleanup_selection = hint("")
        cleanup_actions.addWidget(self.cleanup_selection)
        cleanup_layout.addLayout(cleanup_actions)
        cleanup_layout.addWidget(
            hint(
                "Only known cache and temporary locations are offered. WinFirst rescans after cleanup and "
                "reports what was actually removed; locked or in-use files are reported, not hidden."
            )
        )
        self.tabs.addTab(cleanup, "Cleanup")

        folders = QWidget()
        folders_layout = QVBoxLayout(folders)
        folders_layout.setContentsMargins(0, 12, 0, 0)
        folders_layout.setSpacing(9)
        folder_toolbar = QHBoxLayout()
        self.folder_label = QLabel("No folder selected")
        self.folder_label.setObjectName("SmallMuted")
        folder_toolbar.addWidget(self.folder_label, 1)
        folder_toolbar.addWidget(QLabel("Minimum size"))
        self.min_size = QSpinBox()
        self.min_size.setRange(10, 100000)
        self.min_size.setValue(50)
        self.min_size.setSuffix(" MB")
        folder_toolbar.addWidget(self.min_size)
        folder_toolbar.addWidget(make_button("Choose folder…", variant="ghost", slot=self.choose_folder))
        folder_layout_btn = make_button("Scan", variant="primary", slot=self.scan_folder)
        self.scan_folder_button = folder_layout_btn
        folder_toolbar.addWidget(folder_layout_btn)
        folders_layout.addLayout(folder_toolbar)
        self.folder_table = DataTable(["Folder", "Size", "Files", "Depth", "Accessible"], height=400)
        folders_layout.addWidget(self.folder_table)
        self.folder_note = QLabel("")
        self.folder_note.setWordWrap(True)
        self.folder_note.setObjectName("SmallMuted")
        folders_layout.addWidget(self.folder_note)
        folders_layout.addWidget(
            hint(
                "The scanner walks real files with a bounded entry budget. It does not follow links and skips "
                "protected system trees."
            )
        )
        self.tabs.addTab(folders, "Large folders")

    # ------------------------------------------------------------------ volumes
    def refresh(self) -> None:
        self.clear_error()
        self.run_async(
            lambda: storage_health_service.get_storage_snapshot(self.ctx.runner),
            self._show_storage,
            busy="Reading volumes and disks…",
        )

    def _show_storage(self, snapshot) -> None:
        self.snapshot = snapshot
        total = sum(volume.total for volume in snapshot.volumes)
        free = sum(volume.free for volume in snapshot.volumes)
        low = [volume.mountpoint for volume in snapshot.volumes if volume.low_space]
        self.stat_total.set_value(format_bytes(total), f"{len(snapshot.volumes)} volume(s)")
        self.stat_free.set_value(format_bytes(free))
        self.stat_low.set_value(len(low), ", ".join(low) if low else "No low-space warning")
        self.volumes_table.set_rows(
            [
                [
                    volume.mountpoint,
                    volume.label or "—",
                    volume.filesystem or "—",
                    format_bytes(volume.total),
                    format_bytes(volume.used),
                    format_bytes(volume.free),
                    f"{volume.percent:.0f}%",
                    volume.health,
                ]
                for volume in snapshot.volumes
            ]
        )
        self.disks_table.set_rows(
            [
                [
                    disk.friendly_name,
                    disk.media_type or "—",
                    disk.bus_type or "—",
                    f"{disk.size_gb:.1f} GB",
                    disk.health_status or "—",
                    f"{disk.wear}%" if disk.wear is not None else "—",
                    f"{disk.temperature_c} °C" if disk.temperature_c is not None else "—",
                    disk.read_errors if disk.read_errors is not None else "—",
                    disk.write_errors if disk.write_errors is not None else "—",
                ]
                for disk in snapshot.disks
            ]
        )
        smart = snapshot.smart
        if smart.available:
            self.smart_label.setText(
                ("SMART predicts a failure. Back up immediately. " if smart.failure_predicted else "SMART: no failure predicted. ")
                + (smart.detail or "")
            )
            if smart.failure_predicted:
                self.show_error("SMART predicts a disk failure. Back up important data immediately.")
        else:
            self.smart_label.setText("SMART status unavailable: " + (smart.reason or smart.detail or "not reported by this device."))
        if snapshot.errors:
            self.show_error("Storage details partially unavailable: " + "; ".join(snapshot.errors[:3]))

    # ------------------------------------------------------------------ cleanup
    def analyze_cleanup(self) -> None:
        self.cleanup_badge.setText("Analyzing…")
        self.cleanup_badge.set_status("info")
        self.run_async(
            cleanup_service.analyze_cleanup,
            self._show_cleanup,
            busy="Measuring approved cache locations…",
        )

    def _show_cleanup(self, analysis) -> None:
        self.analysis = analysis
        available = [target for target in analysis.targets if target.available]
        known = sum(target.estimated_bytes or 0 for target in available)
        self.cleanup_badge.setText(f"{format_bytes(known)} known across {len(available)} target(s)")
        self.cleanup_badge.set_status("info")
        rows = []
        for target in analysis.targets:
            rows.append(
                [
                    "",
                    target.label,
                    target.description,
                    target.size_label if target.available else "Unavailable",
                    target.files_label if target.available else "—",
                    ("; ".join(target.warnings) if target.warnings else target.reason),
                ]
            )
        self.cleanup_table.set_rows(rows, payloads=analysis.targets)
        for row, target in enumerate(analysis.targets):
            item = self.cleanup_table.item(row, 0)
            if item is None:
                continue
            if target.available:
                item.setFlags(
                    item.flags()
                    | Qt.ItemFlag.ItemIsUserCheckable
                    | Qt.ItemFlag.ItemIsEnabled
                    | Qt.ItemFlag.ItemIsSelectable
                )
                item.setCheckState(Qt.CheckState.Unchecked)
            else:
                item.setFlags(Qt.ItemFlag.NoItemFlags)
        self._cleanup_label()
        if analysis.errors:
            self.show_warning(
                "Some cleanup locations were only partially measured (often because files are in use): "
                + "; ".join(analysis.errors[:3])
            )

    def _checked_cleanup(self) -> list[str]:
        keys = []
        for row in range(self.cleanup_table.rowCount()):
            item = self.cleanup_table.item(row, 0)
            target = self.cleanup_table.payload_at(row)
            if item is not None and target is not None and item.checkState() == Qt.CheckState.Checked:
                keys.append(target.key)
        return keys

    def _cleanup_label(self) -> None:
        keys = self._checked_cleanup()
        self.cleanup_selection.setText(f"{len(keys)} target(s) selected" if keys else "Select cleanup targets.")

    def select_all_cleanup(self) -> None:
        for row in range(self.cleanup_table.rowCount()):
            item = self.cleanup_table.item(row, 0)
            target = self.cleanup_table.payload_at(row)
            if item is not None and target is not None and target.available:
                item.setCheckState(Qt.CheckState.Checked)
        self._cleanup_label()

    def clean_selected(self) -> None:
        if self.analysis is None:
            return
        keys = self._checked_cleanup()
        if not keys:
            QMessageBox.information(self, "No targets selected", "Select at least one cleanup target.")
            return
        age = self.age_input.value()
        try:
            specs = cleanup_service.build_cleanup_specs(self.analysis, keys, min_age_hours=age)
        except Exception as exc:
            self.show_error(f"Cleanup operations could not be prepared: {exc}")
            return
        if not specs:
            QMessageBox.information(self, "Nothing to clean", "The selected targets are not available.")
            return
        results = run_operations(
            self.ctx,
            self,
            specs,
            title="Clean approved cache locations",
            intro=(
                f"Only files older than {age} hour(s) are removed. The Recycle Bin is emptied only if selected. "
                "Results are verified by rescanning."
            ),
        )
        if results:
            self.analyze_cleanup()
            self.refresh()

    # ------------------------------------------------------------------ folders
    def choose_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose a folder to inspect")
        if not folder:
            return
        self.folder_label.setText(folder)
        self._scan_root = folder

    def scan_folder(self) -> None:
        root = getattr(self, "_scan_root", "")
        if not root:
            QMessageBox.information(self, "No folder selected", "Choose a folder first.")
            return
        min_size = float(self.min_size.value())
        self.scan_folder_button.setEnabled(False)

        def work():
            return storage_health_service.scan_large_folders(
                root, min_size_mb=min_size, entry_budget=120_000
            )

        def done(data) -> None:
            self.scan_folder_button.setEnabled(True)
            self._show_folders(data)

        def failed(message: str) -> None:
            self.scan_folder_button.setEnabled(True)
            self.show_error(f"The folder scan failed: {message}")

        self.run_async(work, done, on_error=failed, busy="Scanning folder sizes…")

    def _show_folders(self, data) -> None:
        usages, errors = data
        self.folder_table.set_rows(
            [
                [
                    usage.path,
                    format_bytes(usage.size_bytes),
                    usage.file_count,
                    usage.depth,
                    "Yes" if usage.accessible else "No",
                ]
                for usage in usages
            ]
        )
        self.folder_note.setText(
            f"{len(usages)} folder(s) at or above the minimum size."
            + (" " + "; ".join(errors[:5]) if errors else "")
        )
