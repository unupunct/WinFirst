"""System health: hardware, storage, battery, devices and security findings."""
from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from winfirst.core.report_manager import build_system_report
from winfirst.services import (
    battery_service,
    device_health_service,
    hardware_inventory_service,
    security_service,
    storage_health_service,
)
from winfirst.ui.base_page import BasePage
from winfirst.ui.widgets import (
    Badge,
    Card,
    DataTable,
    KeyValueGrid,
    StatCard,
    format_bytes,
    hint,
    make_button,
)


class SystemHealthPage(BasePage):
    page_key = "health"
    page_title = "System Health"
    page_subtitle = "Hardware, storage, battery, device problems and security posture from real Windows sources."

    def __init__(self, ctx: Any, *, parent: QWidget | None = None) -> None:
        super().__init__(ctx, parent=parent)
        self.data: dict[str, Any] = {}
        self._built = False

    def load(self) -> None:
        if not self._built:
            self._build()
            self._built = True
        self.refresh()

    def _build(self) -> None:
        self.add_action_button("Generate report", slot=self.generate_report, variant="ghost")
        stats = QHBoxLayout()
        stats.setSpacing(10)
        self.stat_model = StatCard("Computer model", "—")
        self.stat_cpu = StatCard("Processor", "—")
        self.stat_ram = StatCard("Memory", "—")
        self.stat_storage = StatCard("Storage free", "—")
        self.stat_devices = StatCard("Device problems", "—")
        for card in (
            self.stat_model,
            self.stat_cpu,
            self.stat_ram,
            self.stat_storage,
            self.stat_devices,
        ):
            stats.addWidget(card, 1)
        self.add_layout(stats)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.add_widget(self.tabs)

        overview = QWidget()
        overview_layout = QHBoxLayout(overview)
        overview_layout.setContentsMargins(0, 12, 0, 0)
        overview_layout.setSpacing(14)
        hardware_card = Card("Hardware")
        self.hardware_grid = KeyValueGrid()
        hardware_card.add(self.hardware_grid)
        overview_layout.addWidget(hardware_card, 3)
        modules_card = Card("Memory modules")
        self.modules_table = DataTable(
            ["Slot", "Size", "Speed", "Manufacturer", "Part"], height=210
        )
        modules_card.add(self.modules_table)
        gpu_card = Card("Graphics adapters")
        self.gpu_table = DataTable(["Adapter", "VRAM", "Driver", "Date"], height=160)
        gpu_card.add(self.gpu_table)
        right = QVBoxLayout()
        right.setSpacing(12)
        right.addWidget(modules_card)
        right.addWidget(gpu_card)
        overview_layout.addLayout(right, 4)
        self.tabs.addTab(overview, "Hardware")

        storage = QWidget()
        storage_layout = QVBoxLayout(storage)
        storage_layout.setContentsMargins(0, 12, 0, 0)
        storage_layout.setSpacing(10)
        self.storage_note = QLabel("")
        self.storage_note.setObjectName("SmallMuted")
        self.volumes_table = DataTable(
            ["Volume", "Label", "Filesystem", "Total", "Free", "Used %", "Health"], height=180
        )
        self.disks_table = DataTable(
            ["Disk", "Media", "Bus", "Size", "Health", "Wear", "Temperature"], height=150
        )
        storage_layout.addWidget(self.storage_note)
        storage_layout.addWidget(self.volumes_table)
        storage_layout.addWidget(self.disks_table)
        storage_layout.addWidget(
            make_button(
                "Open storage analysis…",
                variant="link",
                slot=lambda: self.navigate("storage"),
            )
        )
        self.tabs.addTab(storage, "Storage")

        battery = QWidget()
        battery_layout = QVBoxLayout(battery)
        battery_layout.setContentsMargins(0, 12, 0, 0)
        self.battery_note = QLabel("")
        self.battery_note.setWordWrap(True)
        self.battery_grid = KeyValueGrid()
        self.battery_table = DataTable(
            ["Battery", "Manufacturer", "Chemistry", "Design", "Full charge", "Cycles", "Wear"],
            height=170,
        )
        battery_layout.addWidget(self.battery_note)
        battery_layout.addWidget(self.battery_grid)
        battery_layout.addWidget(self.battery_table)
        battery_layout.addStretch(1)
        self.tabs.addTab(battery, "Battery")

        devices = QWidget()
        devices_layout = QVBoxLayout(devices)
        devices_layout.setContentsMargins(0, 12, 0, 0)
        self.device_note = QLabel("")
        self.device_note.setObjectName("SmallMuted")
        self.device_table = DataTable(
            ["Device", "Class", "Status", "Problem code", "Manufacturer"], height=260
        )
        devices_layout.addWidget(self.device_note)
        devices_layout.addWidget(self.device_table)
        devices_layout.addStretch(1)
        self.tabs.addTab(devices, "Devices")

        security = QWidget()
        security_layout = QVBoxLayout(security)
        security_layout.setContentsMargins(0, 12, 0, 0)
        self.security_note = QLabel("")
        self.security_note.setObjectName("SmallMuted")
        self.security_table = DataTable(["Finding", "Status", "Detail"], height=260)
        security_layout.addWidget(self.security_note)
        security_layout.addWidget(self.security_table)
        security_layout.addWidget(
            make_button(
                "Open Privacy & Security…",
                variant="link",
                slot=lambda: self.navigate("privacy"),
            )
        )
        security_layout.addStretch(1)
        self.tabs.addTab(security, "Security")

        self.add_widget(
            hint(
                "Battery reports and SMART data are provided by Windows and may be unavailable on some "
                "hardware. Missing data is reported as unavailable, not inferred."
            )
        )

    # ------------------------------------------------------------------ refresh
    def refresh(self) -> None:
        self.clear_error()

        def work():
            info = self.ctx.os_info
            hardware = hardware_inventory_service.get_hardware_info(self.ctx.runner, info)
            storage = storage_health_service.get_storage_snapshot(self.ctx.runner)
            battery = battery_service.get_battery_info(self.ctx.runner)
            devices = device_health_service.get_device_health(self.ctx.runner)
            findings = security_service.security_findings()
            return {
                "hardware": hardware,
                "storage": storage,
                "battery": battery,
                "devices": devices,
                "findings": findings,
            }

        self.run_async(work, self._show, busy="Collecting hardware and security information…")

    def _show(self, data: dict[str, Any]) -> None:
        self.data = data
        hardware = data["hardware"]
        storage = data["storage"]
        battery = data["battery"]
        devices = data["devices"]
        findings = data["findings"]

        self.stat_model.set_value(hardware.model or "Unknown", hardware.manufacturer or "Manufacturer unknown")
        self.stat_cpu.set_value(
            hardware.cpu_model or "Unknown",
            f"{hardware.cpu_cores or '?'} cores / {hardware.cpu_logical or '?'} threads",
        )
        self.stat_ram.set_value(
            f"{hardware.ram_total_gb:.1f} GB",
            f"{hardware.ram_available_gb:.1f} GB available",
        )
        if storage.volumes:
            free = sum(volume.free for volume in storage.volumes)
            self.stat_storage.set_value(format_bytes(free), f"{len(storage.volumes)} volume(s)")
        else:
            self.stat_storage.set_value("Unavailable", storage.errors[0] if storage.errors else "")
        self.stat_devices.set_value(
            len(devices.problems) if not devices.error else "Unavailable",
            f"{devices.present_devices} devices present" if devices.present_devices else "",
        )

        self.hardware_grid.set_rows(
            [
                ("Manufacturer", hardware.manufacturer or "Unknown"),
                ("Model", hardware.model or "Unknown"),
                ("System type", hardware.system_type or "Unknown"),
                ("Chassis", hardware.chassis or "Unknown"),
                ("Motherboard", " ".join(filter(None, [hardware.motherboard_manufacturer, hardware.motherboard_model]))),
                ("BIOS", f"{hardware.bios_vendor} {hardware.bios_version}".strip() or "Unknown"),
                ("Firmware", hardware.firmware_type or "Unknown"),
                ("Windows", hardware.os_info.version_label if hardware.os_info else "Unknown"),
            ]
        )
        self.modules_table.set_rows(
            [
                [
                    module.slot or "—",
                    f"{module.capacity_gb:.1f} GB",
                    f"{module.speed_mhz} MHz" if module.speed_mhz else "—",
                    module.manufacturer or "—",
                    module.part_number or "—",
                ]
                for module in hardware.ram_modules
            ]
        )
        self.gpu_table.set_rows(
            [
                [
                    gpu.name,
                    f"{gpu.adapter_ram_gb:.1f} GB" if gpu.adapter_ram_gb else "—",
                    gpu.driver_version or "—",
                    gpu.driver_date or "—",
                ]
                for gpu in hardware.gpus
            ]
        )

        self.volumes_table.set_rows(
            [
                [
                    volume.mountpoint,
                    volume.label or "—",
                    volume.filesystem or "—",
                    format_bytes(volume.total),
                    format_bytes(volume.free),
                    f"{volume.percent:.0f}%",
                    volume.health,
                ]
                for volume in storage.volumes
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
                ]
                for disk in storage.disks
            ]
        )
        smart_bits = [storage.smart.detail or "SMART status unavailable"]
        if storage.smart.failure_predicted:
            smart_bits.append("A failure is predicted by the drive.")
        if storage.smart.reason:
            smart_bits.append(storage.smart.reason)
        self.storage_note.setText(" ".join(smart_bits) + (" " + "; ".join(storage.errors) if storage.errors else ""))

        self._show_battery(battery)
        if devices.error:
            self.device_note.setText(f"Device health could not be collected: {devices.error}")
            self.device_table.set_rows([])
        else:
            self.device_note.setText(
                f"{devices.present_devices} present device(s); {len(devices.problems)} problem(s) reported by Windows."
            )
            self.device_table.set_rows(
                [
                    [
                        problem.name,
                        problem.pnp_class,
                        problem.status,
                        problem.problem_code,
                        problem.manufacturer or "—",
                    ]
                    for problem in devices.problems
                ]
            )

        self.security_note.setText(
            "Security controls are inspected only. WinFirst never disables Defender, Firewall, UAC or updates."
        )
        security_rows = []
        for finding in findings:
            security_rows.append(
                [
                    finding.get("title", ""),
                    str(finding.get("status", "")).title(),
                    finding.get("detail", ""),
                ]
            )
        self.security_table.set_rows(security_rows)

    def _show_battery(self, battery) -> None:
        if not battery.available:
            self.battery_note.setText(battery.reason or "Battery information is unavailable.")
            self.battery_grid.set_rows([])
            self.battery_table.set_rows([])
            return
        bits = []
        if battery.estimated:
            bits.append("Some values are estimated from Windows' report.")
        if battery.note:
            bits.append(battery.note)
        self.battery_note.setText(" ".join(bits))
        self.battery_grid.set_rows(
            [
                ("Status", battery.status or "Unknown"),
                ("Power source", "AC power" if battery.power_online else "Battery"),
                ("Current charge", f"{battery.current_charge_percent}%" if battery.current_charge_percent is not None else "Unavailable"),
                ("Overall wear", f"{battery.wear_percent:.1f}%" if battery.wear_percent is not None else "Unavailable"),
            ]
        )
        self.battery_table.set_rows(
            [
                [
                    unit.name,
                    unit.manufacturer or "—",
                    unit.chemistry or "—",
                    f"{unit.design_capacity_mwh} mWh" if unit.design_capacity_mwh else "—",
                    f"{unit.full_charge_capacity_mwh} mWh" if unit.full_charge_capacity_mwh else "—",
                    unit.cycle_count if unit.cycle_count is not None else "—",
                    f"{unit.wear_percent:.1f}%" if unit.wear_percent is not None else "—",
                ]
                for unit in battery.units
            ]
        )

    # ------------------------------------------------------------------- report
    def generate_report(self) -> None:
        if not self.data:
            return
        data = dict(self.data)

        def work():
            document = build_system_report(
                os_info=self.ctx.os_info,
                hardware=data.get("hardware"),
                storage=data.get("storage"),
                battery=data.get("battery"),
                security_findings=data.get("findings"),
                device_health=data.get("devices"),
            )
            return self.ctx.reports.save(document, "html")

        def done(path) -> None:
            self.ctx.status(f"System report saved to {path}")
            QMessageBox.information(self, "Report generated", f"Saved to:\n{path}")

        self.run_async(
            work,
            done,
            busy="Saving the system health report…",
            done_message="System report saved.",
        )
