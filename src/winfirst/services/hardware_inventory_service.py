"""Hardware inventory collected from documented WMI/CIM classes and psutil."""
from __future__ import annotations

import platform
from dataclasses import dataclass, field
from typing import Any

import psutil

from winfirst.core.command_runner import CommandRunner
from winfirst.core.os_detection import WindowsInfo, detect_windows_info
from winfirst.services.common import ServiceError, ps_json, require_windows

_LAPTOP_CHASSIS = {8, 9, 10, 14, 18, 30, 31, 32}


@dataclass
class MemoryModule:
    manufacturer: str = ""
    part_number: str = ""
    capacity_gb: float = 0.0
    speed_mhz: int | None = None
    form_factor: str = ""
    slot: str = ""


@dataclass
class GpuInfo:
    name: str = ""
    adapter_ram_gb: float | None = None
    driver_version: str = ""
    driver_date: str = ""
    video_processor: str = ""


@dataclass
class HardwareInfo:
    os_info: WindowsInfo
    manufacturer: str = ""
    model: str = ""
    system_type: str = ""
    chassis: str = ""
    is_laptop: bool = False
    cpu_model: str = ""
    cpu_cores: int | None = None
    cpu_logical: int | None = None
    cpu_clock_mhz: int | None = None
    ram_total_gb: float = 0.0
    ram_available_gb: float = 0.0
    ram_modules: list[MemoryModule] = field(default_factory=list)
    motherboard_manufacturer: str = ""
    motherboard_model: str = ""
    bios_vendor: str = ""
    bios_version: str = ""
    bios_date: str = ""
    firmware_type: str = ""
    gpus: list[GpuInfo] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def summary_rows(self) -> list[tuple[str, str]]:
        rows = [
            ("Computer", f"{self.manufacturer} {self.model}".strip() or "Unknown"),
            ("System type", self.system_type or "Unknown"),
            ("Form factor", self.chassis or ("Laptop" if self.is_laptop else "Desktop / unknown")),
            ("CPU", self.cpu_model or "Unknown"),
            (
                "CPU cores",
                f"{self.cpu_cores or '?'} physical / {self.cpu_logical or '?'} logical",
            ),
            ("Memory", f"{self.ram_total_gb:.1f} GB installed, {self.ram_available_gb:.1f} GB available"),
            ("Motherboard", f"{self.motherboard_manufacturer} {self.motherboard_model}".strip() or "Unknown"),
            ("BIOS", f"{self.bios_vendor} {self.bios_version} ({self.bios_date})".strip()),
            ("Firmware", self.firmware_type or "Unknown"),
        ]
        for gpu in self.gpus:
            detail = gpu.name
            if gpu.adapter_ram_gb:
                detail += f" ({gpu.adapter_ram_gb:.1f} GB)"
            rows.append(("GPU", detail))
        return rows


_SCRIPT = r"""
$result = [ordered]@{
    Computer = $null; Cpu = $null; Bios = $null; Board = $null; Memory = @(); Gpu = @();
    Chassis = @(); Firmware = ''; Errors = @()
}
try {
    $cs = Get-CimInstance -ClassName Win32_ComputerSystem -ErrorAction Stop
    $result.Computer = [pscustomobject]@{
        Manufacturer = [string]$cs.Manufacturer; Model = [string]$cs.Model;
        SystemType = [string]$cs.SystemType; TotalPhysicalMemory = [double]$cs.TotalPhysicalMemory
    }
} catch { $result.Errors += "Computer system: $($_.Exception.Message)" }
try {
    $cpu = Get-CimInstance -ClassName Win32_Processor -ErrorAction Stop | Select-Object -First 1
    $result.Cpu = [pscustomobject]@{
        Name = [string]$cpu.Name; NumberOfCores = $cpu.NumberOfCores;
        NumberOfLogicalProcessors = $cpu.NumberOfLogicalProcessors;
        MaxClockSpeed = $cpu.MaxClockSpeed
    }
} catch { $result.Errors += "Processor: $($_.Exception.Message)" }
try {
    $bios = Get-CimInstance -ClassName Win32_BIOS -ErrorAction Stop
    $result.Bios = [pscustomobject]@{
        Manufacturer = [string]$bios.Manufacturer; Version = [string]$bios.SMBIOSBIOSVersion;
        ReleaseDate = if ($bios.ReleaseDate) { $bios.ReleaseDate.ToString('yyyy-MM-dd') } else { '' }
    }
} catch { $result.Errors += "BIOS: $($_.Exception.Message)" }
try {
    $board = Get-CimInstance -ClassName Win32_BaseBoard -ErrorAction Stop
    $result.Board = [pscustomobject]@{ Manufacturer = [string]$board.Manufacturer; Product = [string]$board.Product }
} catch { $result.Errors += "Motherboard: $($_.Exception.Message)" }
try {
    $result.Memory = @(Get-CimInstance -ClassName Win32_PhysicalMemory -ErrorAction Stop | ForEach-Object {
        [pscustomobject]@{
            Manufacturer = [string]$_.Manufacturer; PartNumber = [string]$_.PartNumber;
            Capacity = [double]$_.Capacity; Speed = $_.Speed;
            FormFactor = $_.FormFactor; Slot = [string]$_.DeviceLocator
        }
    })
} catch { $result.Errors += "Memory modules: $($_.Exception.Message)" }
try {
    $result.Gpu = @(Get-CimInstance -ClassName Win32_VideoController -ErrorAction Stop | ForEach-Object {
        [pscustomobject]@{
            Name = [string]$_.Name; AdapterRAM = $_.AdapterRAM;
            DriverVersion = [string]$_.DriverVersion;
            DriverDate = if ($_.DriverDate) { $_.DriverDate.ToString('yyyy-MM-dd') } else { '' };
            VideoProcessor = [string]$_.VideoProcessor
        }
    })
} catch { $result.Errors += "GPU: $($_.Exception.Message)" }
try {
    $enclosure = Get-CimInstance -ClassName Win32_SystemEnclosure -ErrorAction Stop
    $result.Chassis = @($enclosure | ForEach-Object { $_.ChassisTypes })
} catch { $result.Errors += "Chassis: $($_.Exception.Message)" }
try {
    $fw = (Get-ComputerInfo -Property BiosFirmwareType -ErrorAction Stop).BiosFirmwareType
    $result.Firmware = [string]$fw
} catch { }
$result | ConvertTo-Json -Depth 6 -Compress
"""

_CHASSIS_LABELS = {
    3: "Desktop",
    4: "Low-profile desktop",
    5: "Pizza box",
    6: "Mini tower",
    7: "Tower",
    8: "Portable",
    9: "Laptop",
    10: "Notebook",
    11: "Handheld",
    12: "Docking station",
    13: "All-in-one",
    14: "Sub-notebook",
    15: "Space-saving",
    16: "Lunch box",
    17: "Main system chassis",
    23: "Rack mount",
    30: "Tablet",
    31: "Convertible",
    32: "Detachable",
    35: "Mini PC",
}


def _form_factor(value: Any) -> str:
    mapping = {
        8: "DIMM",
        12: "SODIMM",
        13: "SRIMM",
        11: "RIMM",
    }
    try:
        return mapping.get(int(value), str(value) if value else "")
    except (TypeError, ValueError):
        return ""


def get_hardware_info(runner: CommandRunner | None = None, os_info: WindowsInfo | None = None) -> HardwareInfo:
    require_windows("Hardware inventory")
    runner = runner or CommandRunner()
    info = HardwareInfo(os_info=os_info or detect_windows_info())
    try:
        data = ps_json(runner, _SCRIPT, timeout=90, default={}) or {}
    except ServiceError as exc:
        info.errors.append(str(exc))
        data = {}
    except Exception as exc:
        info.errors.append(f"Hardware query failed: {exc}")
        data = {}

    computer = data.get("Computer") or {}
    info.manufacturer = str(computer.get("Manufacturer") or "")
    info.model = str(computer.get("Model") or "")
    info.system_type = str(computer.get("SystemType") or "")
    cpu = data.get("Cpu") or {}
    info.cpu_model = " ".join(str(cpu.get("Name") or "").split())
    try:
        info.cpu_cores = int(cpu.get("NumberOfCores")) if cpu.get("NumberOfCores") else None
        info.cpu_logical = int(cpu.get("NumberOfLogicalProcessors")) if cpu.get("NumberOfLogicalProcessors") else None
        info.cpu_clock_mhz = int(cpu.get("MaxClockSpeed")) if cpu.get("MaxClockSpeed") else None
    except (TypeError, ValueError):
        pass
    bios = data.get("Bios") or {}
    info.bios_vendor = str(bios.get("Manufacturer") or "")
    info.bios_version = str(bios.get("Version") or "")
    info.bios_date = str(bios.get("ReleaseDate") or "")
    board = data.get("Board") or {}
    info.motherboard_manufacturer = str(board.get("Manufacturer") or "")
    info.motherboard_model = str(board.get("Product") or "")
    chassis_values: list[int] = []
    for value in data.get("Chassis") or []:
        try:
            chassis_values.append(int(value))
        except (TypeError, ValueError):
            continue
    if chassis_values:
        info.chassis = ", ".join(_CHASSIS_LABELS.get(code, f"Type {code}") for code in chassis_values)
        info.is_laptop = any(code in _LAPTOP_CHASSIS for code in chassis_values)
    else:
        info.is_laptop = info.os_info.is_laptop
        info.chassis = "Laptop" if info.is_laptop else "Desktop / unknown"

    for row in data.get("Memory") or []:
        if not isinstance(row, dict):
            continue
        capacity = float(row.get("Capacity") or 0)
        try:
            speed = int(row.get("Speed")) if row.get("Speed") else None
        except (TypeError, ValueError):
            speed = None
        info.ram_modules.append(
            MemoryModule(
                manufacturer=str(row.get("Manufacturer") or ""),
                part_number=str(row.get("PartNumber") or "").strip(),
                capacity_gb=round(capacity / (1024**3), 2) if capacity else 0.0,
                speed_mhz=speed,
                form_factor=_form_factor(row.get("FormFactor")),
                slot=str(row.get("Slot") or ""),
            )
        )
    for row in data.get("Gpu") or []:
        if not isinstance(row, dict):
            continue
        ram = row.get("AdapterRAM")
        try:
            ram_gb = round(float(ram) / (1024**3), 2) if ram and float(ram) > 0 else None
        except (TypeError, ValueError):
            ram_gb = None
        info.gpus.append(
            GpuInfo(
                name=str(row.get("Name") or "Unknown GPU"),
                adapter_ram_gb=ram_gb,
                driver_version=str(row.get("DriverVersion") or ""),
                driver_date=str(row.get("DriverDate") or ""),
                video_processor=str(row.get("VideoProcessor") or ""),
            )
        )
    for message in data.get("Errors") or []:
        if message:
            info.errors.append(str(message))

    try:
        memory = psutil.virtual_memory()
        info.ram_total_gb = round(memory.total / (1024**3), 2)
        info.ram_available_gb = round(memory.available / (1024**3), 2)
    except Exception as exc:
        info.errors.append(f"Memory usage: {exc}")
    if not info.cpu_logical:
        try:
            info.cpu_logical = psutil.cpu_count(logical=True)
            info.cpu_cores = psutil.cpu_count(logical=False)
        except Exception:
            pass
    if not info.cpu_model:
        info.cpu_model = platform.processor() or "Unknown"
    info.firmware_type = str(data.get("Firmware") or "") or info.firmware_type
    if not info.firmware_type:
        info.firmware_type = "Unavailable"
    return info


def _has_efi() -> bool:
    import os

    # Retained for callers that need a best-effort firmware hint.  The main
    # inventory prefers Get-ComputerInfo because it is authoritative.
    return bool(os.environ.get("firmware_type", "").lower() == "uefi")
