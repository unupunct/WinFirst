"""Detect which Windows tools and features are actually available."""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from typing import Iterable

from winfirst.core.command_runner import CommandRunner, as_list

_PS_COMMANDS = (
    "Get-AppxPackage",
    "Get-AppxProvisionedPackage",
    "Get-CimInstance",
    "Get-PnpDevice",
    "Get-NetAdapter",
    "Get-NetIPConfiguration",
    "Get-NetIPAddress",
    "Get-DnsClientServerAddress",
    "Get-NetRoute",
    "Get-VpnConnection",
    "Get-MpComputerStatus",
    "Get-NetFirewallProfile",
    "Get-Service",
    "Get-ScheduledTask",
    "Get-ScheduledTaskInfo",
    "Disable-ScheduledTask",
    "Enable-ScheduledTask",
    "Get-Printer",
    "Get-PrintJob",
    "Remove-PrintJob",
    "Get-PrinterDriver",
    "Get-LocalUser",
    "Get-LocalGroup",
    "Get-LocalGroupMember",
    "Get-WindowsOptionalFeature",
    "Get-WindowsCapability",
    "Get-Volume",
    "Get-PhysicalDisk",
    "Get-StorageReliabilityCounter",
    "Get-WindowsDriver",
    "Get-AuthenticodeSignature",
    "Get-HotFix",
    "Get-WinEvent",
    "Get-Tpm",
    "Confirm-SecureBootUEFI",
    "Get-BitLockerVolume",
    "Restart-Service",
    "Set-Service",
    "Get-ComputerInfo",
)

_EXECUTABLES = (
    "winget.exe",
    "pnputil.exe",
    "powercfg.exe",
    "dism.exe",
    "sfc.exe",
    "netsh.exe",
    "ipconfig.exe",
    "ping.exe",
    "tracert.exe",
    "sc.exe",
    "net.exe",
    "msiexec.exe",
    "shutdown.exe",
    "cleanmgr.exe",
    "taskkill.exe",
    "reg.exe",
    "bcdedit.exe",
    "chkdsk.exe",
    "slmgr.vbs",
)

_CAPABILITY_LABELS = {
    "powershell": "Windows PowerShell",
    "appx_inventory": "Appx / Store package inventory",
    "appx_provisioned": "Provisioned Appx packages",
    "cim": "WMI / CIM hardware inventory",
    "pnp_devices": "Plug and Play device inventory",
    "network_cmdlets": "Network cmdlets",
    "defender_status": "Microsoft Defender status",
    "firewall_status": "Windows Firewall status",
    "services": "Service inventory",
    "scheduled_tasks": "Scheduled task inventory",
    "printers": "Printer inventory",
    "local_users": "Local users and groups",
    "optional_features": "Optional Windows features",
    "storage_health": "Storage health cmdlets",
    "driver_export": "Driver export (pnputil)",
    "driver_inventory": "Driver inventory",
    "authenticode": "Digital signature verification",
    "hotfix": "Update history",
    "event_log": "Event log access",
    "tpm": "TPM status",
    "secure_boot": "Secure Boot status",
    "bitlocker": "BitLocker status",
    "winget": "winget package manager",
    "powercfg": "powercfg power management",
    "hibernate": "Hibernation support",
    "battery": "Battery present",
    "admin": "Administrator session",
}


@dataclass
class Capability:
    key: str
    label: str
    available: bool
    reason: str = ""
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "available": self.available,
            "reason": self.reason,
            "detail": self.detail,
        }


class UnsupportedCapability(RuntimeError):
    pass


@dataclass
class CapabilitySet:
    items: dict[str, Capability] = field(default_factory=dict)
    os_build: int = 0
    os_supported: bool = True
    notes: list[str] = field(default_factory=list)

    def has(self, key: str) -> bool:
        item = self.items.get(key)
        return bool(item and item.available)

    def get(self, key: str) -> Capability | None:
        return self.items.get(key)

    def require(self, key: str) -> None:
        if not self.has(key):
            item = self.items.get(key)
            reason = item.reason if item and item.reason else "not available on this system"
            raise UnsupportedCapability(f"{self.label(key)} is {reason}")

    def label(self, key: str) -> str:
        item = self.items.get(key)
        return item.label if item else key

    def reason(self, key: str) -> str:
        item = self.items.get(key)
        return item.reason if item else "Not detected"

    def available_items(self) -> list[Capability]:
        return [c for c in self.items.values() if c.available]

    def unavailable_items(self) -> list[Capability]:
        return [c for c in self.items.values() if not c.available]

    def to_dict(self) -> dict:
        return {
            "os_build": self.os_build,
            "os_supported": self.os_supported,
            "items": {key: cap.to_dict() for key, cap in self.items.items()},
            "notes": list(self.notes),
        }


def _add(caps: dict[str, Capability], key: str, available: bool, reason: str = "", detail: str = "") -> None:
    caps[key] = Capability(
        key=key,
        label=_CAPABILITY_LABELS.get(key, key),
        available=available,
        reason="" if available else reason,
        detail=detail,
    )


def detect_capabilities(
    runner: CommandRunner | None = None,
    *,
    is_elevated: bool | None = None,
    os_build: int | None = None,
    query_powershell: bool = True,
) -> CapabilitySet:
    runner = runner or CommandRunner()
    caps: dict[str, Capability] = {}
    notes: list[str] = []

    if sys.platform != "win32":
        for key, label in _CAPABILITY_LABELS.items():
            _add(caps, key, False, "Windows is required")
        return CapabilitySet(
            items=caps,
            os_build=0,
            os_supported=False,
            notes=["Capability detection requires Windows 10 or Windows 11."],
        )

    if is_elevated is None:
        try:
            import ctypes

            is_elevated = bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            is_elevated = False
    _add(caps, "admin", bool(is_elevated), "The application is not running elevated")

    ps_available = runner.powershell_available
    _add(caps, "powershell", ps_available, "powershell.exe was not found")

    command_support: dict[str, bool] = {}
    if ps_available and query_powershell:
        names = ",\n    ".join(f"'{name}'" for name in _PS_COMMANDS)
        script = (
            "$names = @(\n    " + names + "\n)\n"
            "$names | ForEach-Object {\n"
            "  [pscustomobject]@{ Name = $_; Available = [bool](Get-Command $_ -ErrorAction SilentlyContinue) }\n"
            "} | ConvertTo-Json -Compress\n"
        )
        try:
            raw = runner.powershell_json(script, timeout=90, default=[])
            for row in as_list(raw):
                command_support[str(row.get("Name"))] = bool(row.get("Available"))
        except Exception as exc:
            notes.append(f"PowerShell cmdlet probe failed: {exc}")

    def cmd(name: str) -> bool:
        return command_support.get(name, False)

    _add(caps, "appx_inventory", ps_available and cmd("Get-AppxPackage"), "Get-AppxPackage unavailable")
    _add(
        caps,
        "appx_provisioned",
        ps_available and cmd("Get-AppxProvisionedPackage") and bool(is_elevated),
        "Requires PowerShell and administrator rights",
    )
    _add(caps, "cim", ps_available and cmd("Get-CimInstance"), "Get-CimInstance unavailable")
    _add(caps, "pnp_devices", ps_available and cmd("Get-PnpDevice"), "Get-PnpDevice unavailable")
    _add(
        caps,
        "network_cmdlets",
        ps_available and cmd("Get-NetAdapter") and cmd("Get-NetIPConfiguration"),
        "Get-NetAdapter / Get-NetIPConfiguration unavailable",
    )
    _add(caps, "defender_status", ps_available and cmd("Get-MpComputerStatus"), "Get-MpComputerStatus unavailable")
    _add(caps, "firewall_status", ps_available and cmd("Get-NetFirewallProfile"), "Get-NetFirewallProfile unavailable")
    _add(caps, "services", ps_available and cmd("Get-Service"), "Get-Service unavailable")
    _add(caps, "scheduled_tasks", ps_available and cmd("Get-ScheduledTask"), "Get-ScheduledTask unavailable")
    _add(caps, "printers", ps_available and cmd("Get-Printer"), "Get-Printer unavailable")
    _add(caps, "local_users", ps_available and cmd("Get-LocalUser"), "Get-LocalUser unavailable")
    _add(
        caps,
        "optional_features",
        ps_available and cmd("Get-WindowsOptionalFeature") and bool(is_elevated),
        "Requires PowerShell and administrator rights",
    )
    _add(
        caps,
        "storage_health",
        ps_available and cmd("Get-PhysicalDisk"),
        "Get-PhysicalDisk unavailable",
    )
    _add(
        caps,
        "driver_inventory",
        ps_available and cmd("Get-WindowsDriver") and bool(is_elevated),
        "Get-WindowsDriver requires administrator rights",
    )
    _add(caps, "authenticode", ps_available and cmd("Get-AuthenticodeSignature"), "Get-AuthenticodeSignature unavailable")
    _add(caps, "hotfix", ps_available and cmd("Get-HotFix"), "Get-HotFix unavailable")
    _add(caps, "event_log", ps_available and cmd("Get-WinEvent"), "Get-WinEvent unavailable")
    _add(caps, "tpm", ps_available and cmd("Get-Tpm"), "Get-Tpm unavailable")
    _add(caps, "secure_boot", ps_available and cmd("Confirm-SecureBootUEFI"), "Confirm-SecureBootUEFI unavailable")
    _add(caps, "bitlocker", ps_available and cmd("Get-BitLockerVolume"), "Get-BitLockerVolume unavailable")

    found_executables: dict[str, str] = {}
    for name in _EXECUTABLES:
        path = _resolve_executable(runner, name)
        found_executables[name] = path or ""
    _add(
        caps,
        "winget",
        bool(found_executables.get("winget.exe")),
        "winget is not installed for this user",
    )
    _add(caps, "powercfg", bool(found_executables.get("powercfg.exe")), "powercfg.exe not found")
    _add(caps, "driver_export", bool(found_executables.get("pnputil.exe")), "pnputil.exe not found")
    if found_executables.get("pnputil.exe"):
        caps["driver_inventory"].available = True
        caps["driver_inventory"].reason = ""
        caps["driver_inventory"].detail = "pnputil.exe inventory"

    if os_build is None:
        try:
            from winfirst.core.os_detection import detect_windows_info

            os_build = detect_windows_info().build
        except Exception:
            os_build = 0
    os_supported = bool(os_build and os_build >= 10240)
    if os_build and os_build < 10240:
        notes.append("This Windows build is older than Windows 10.")

    hibernate_available = False
    hibernate_detail = ""
    if found_executables.get("powercfg.exe"):
        try:
            result = runner.run([found_executables["powercfg.exe"], "/a"], timeout=20)
            text = result.combined.lower()
            hibernate_available = result.ok and "hibernate" in text and "not available" not in text
            if "hibernation has not been enabled" in text:
                hibernate_available = True
                hibernate_detail = "Hibernation is supported but currently disabled"
        except Exception:
            pass
    _add(
        caps,
        "hibernate",
        hibernate_available,
        "Hibernation is not supported by this firmware or is disabled by policy",
        hibernate_detail,
    )

    battery_present = False
    try:
        import psutil

        battery_present = psutil.sensors_battery() is not None
    except Exception:
        battery_present = False
    _add(caps, "battery", battery_present, "No battery detected (desktop or virtual machine)")

    return CapabilitySet(items=caps, os_build=int(os_build or 0), os_supported=os_supported, notes=notes)


def _resolve_executable(runner: CommandRunner, name: str) -> str | None:
    try:
        return runner.resolve_executable(name)
    except Exception:
        pass
    if name == "winget.exe":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            candidate = os.path.join(local, "Microsoft", "WindowsApps", "winget.exe")
            if os.path.isfile(candidate):
                return candidate
    return None


def capability_summary(caps: CapabilitySet) -> str:
    available = len(caps.available_items())
    total = len(caps.items)
    return f"{available} of {total} capabilities available"


def capabilities_json(caps: CapabilitySet) -> str:
    return json.dumps(caps.to_dict(), indent=2)
