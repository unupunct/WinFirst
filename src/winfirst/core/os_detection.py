"""Windows operating-system inspection.

Values come from the registry, ``platform`` and ``psutil``.  Nothing is
invented: unavailable values are returned as ``None`` or an explanatory
string.
"""
from __future__ import annotations

import os
import platform
import socket
import sys
from dataclasses import dataclass, field
from datetime import datetime

try:  # pragma: no cover - platform specific
    import winreg
except ImportError:  # pragma: no cover
    winreg = None  # type: ignore[assignment]

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None  # type: ignore[assignment]

_CURRENT_VERSION = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"


@dataclass
class WindowsInfo:
    is_windows: bool
    product_name: str = "Unknown"
    edition: str = ""
    display_version: str = ""
    build: int = 0
    ubr: int = 0
    architecture: str = ""
    install_date: str = ""
    hostname: str = ""
    username: str = ""
    domain: str = ""
    uptime_seconds: int = 0
    is_laptop: bool = False
    pending_reboot: bool = False
    pending_reasons: list[str] = field(default_factory=list)
    is_windows_11: bool = False
    is_server: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def build_label(self) -> str:
        return f"{self.build}.{self.ubr}" if self.ubr else str(self.build)

    @property
    def version_label(self) -> str:
        bits = [self.product_name]
        if self.display_version:
            bits.append(self.display_version)
        if self.build:
            bits.append(f"build {self.build_label}")
        return " ".join(bits)

    @property
    def architecture_label(self) -> str:
        mapping = {
            "AMD64": "64-bit (x64)",
            "ARM64": "64-bit (ARM64)",
            "x86": "32-bit (x86)",
        }
        return mapping.get(self.architecture.upper(), self.architecture or "Unknown")

    @property
    def uptime_label(self) -> str:
        if self.uptime_seconds <= 0:
            return "Unknown"
        days, remainder = divmod(self.uptime_seconds, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes = remainder // 60
        parts = []
        if days:
            parts.append(f"{days}d")
        if hours or days:
            parts.append(f"{hours}h")
        parts.append(f"{minutes}m")
        return " ".join(parts)

    @property
    def supported(self) -> bool:
        return self.is_windows and self.build >= 10240


def _read_reg(path: str, name: str):
    if winreg is None:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as key:
            value, _ = winreg.QueryValueEx(key, name)
            return value
    except OSError:
        return None


def _as_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _format_install_date(raw) -> str:
    seconds = _as_int(raw, 0)
    if seconds <= 0:
        return ""
    try:
        return datetime.fromtimestamp(seconds).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        return ""


def detect_pending_reboot() -> tuple[bool, list[str]]:
    """Best-effort pending restart indicators from documented registry keys."""
    if winreg is None or sys.platform != "win32":
        return False, []
    reasons: list[str] = []
    checks = (
        (
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending",
            "Component Based Servicing requires a restart",
        ),
        (
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired",
            "Windows Update requires a restart",
        ),
        (
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\PackagesPending",
            "CBS package operations are pending",
        ),
    )
    for path, label in checks:
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path):
                reasons.append(label)
        except OSError:
            continue
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager",
        ) as key:
            value, _ = winreg.QueryValueEx(key, "PendingFileRenameOperations")
            if value:
                reasons.append("Pending file rename operations are queued")
    except OSError:
        pass
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\PostRebootReporting",
        ):
            reasons.append("Windows Update post-reboot reporting is pending")
    except OSError:
        pass
    return bool(reasons), reasons


def _is_laptop() -> bool:
    if psutil is None:
        return False
    try:
        return psutil.sensors_battery() is not None
    except Exception:
        return False


def detect_windows_info() -> WindowsInfo:
    is_windows = sys.platform == "win32"
    hostname = socket.gethostname()
    username = os.environ.get("USERNAME") or os.environ.get("USER") or ""
    domain = os.environ.get("USERDOMAIN", "")
    if not is_windows:
        return WindowsInfo(
            is_windows=False,
            product_name=f"{platform.system()} {platform.release()}",
            architecture=platform.machine(),
            hostname=hostname,
            username=username,
            notes=["WinFirst system operations are designed for Windows 10 and Windows 11."],
        )

    product = str(_read_reg(_CURRENT_VERSION, "ProductName") or "Windows")
    edition = str(_read_reg(_CURRENT_VERSION, "EditionID") or "")
    display = str(
        _read_reg(_CURRENT_VERSION, "DisplayVersion")
        or _read_reg(_CURRENT_VERSION, "ReleaseId")
        or ""
    )
    build = _as_int(_read_reg(_CURRENT_VERSION, "CurrentBuildNumber"), 0)
    ubr = _as_int(_read_reg(_CURRENT_VERSION, "UBR"), 0)
    install_date = _format_install_date(_read_reg(_CURRENT_VERSION, "InstallDate"))
    is_win11 = build >= 22000
    if is_win11 and product.startswith("Windows 10"):
        product = product.replace("Windows 10", "Windows 11", 1)
    pending, reasons = detect_pending_reboot()
    uptime = 0
    if psutil is not None:
        try:
            uptime = max(0, int(datetime.now().timestamp() - psutil.boot_time()))
        except Exception:
            uptime = 0
    notes: list[str] = []
    if build and build < 10240:
        notes.append("This Windows build predates Windows 10 and is not supported.")
    if build and 10240 <= build < 22000:
        notes.append("Windows 10 is supported; some Windows 11-only features are unavailable.")
    return WindowsInfo(
        is_windows=True,
        product_name=product,
        edition=edition,
        display_version=display,
        build=build,
        ubr=ubr,
        architecture=platform.machine(),
        install_date=install_date,
        hostname=hostname,
        username=username,
        domain=domain,
        uptime_seconds=uptime,
        is_laptop=_is_laptop(),
        pending_reboot=pending,
        pending_reasons=reasons,
        is_windows_11=is_win11,
        is_server="Server" in product or "Server" in edition,
        notes=notes,
    )
