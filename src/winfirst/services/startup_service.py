"""Startup application inventory and safe enable/disable.

Disabling uses the documented StartupApproved registry state that Task Manager
uses, so entries are not deleted and can be re-enabled.  WinFirst never labels
an unknown publisher as malicious.
"""
from __future__ import annotations

import ctypes
import os
import struct
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import winreg
except ImportError:  # pragma: no cover
    winreg = None  # type: ignore[assignment]

from winfirst.core.command_runner import CommandRunner, as_list, ps_quote
from winfirst.core.operation import ApplyOutcome, OperationSpec, VerifyOutcome
from winfirst.core.risk_engine import RiskLevel
from winfirst.services.common import ServiceError, ps_json
from winfirst.services.win_registry import (
    RegValue,
    list_values,
    read_value,
    write_value,
)

RUN_KEYS = (
    ("HKCU", r"Software\Microsoft\Windows\CurrentVersion\Run", "Current user", "", "Run"),
    ("HKLM", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run", "All users", "", "Run"),
    ("HKLM", r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Run", "All users (32-bit)", "", "Run32"),
)

RUN_ONCE_KEYS = (
    ("HKCU", r"Software\Microsoft\Windows\CurrentVersion\RunOnce", "Current user", "", "RunOnce"),
    ("HKLM", r"SOFTWARE\Microsoft\Windows\CurrentVersion\RunOnce", "All users", "", "RunOnce"),
)

_APPROVED_ROOT = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved"

_VERSION_CACHE: dict[str, dict[str, str]] = {}


@dataclass
class StartupEntry:
    id: str
    name: str
    command: str
    source: str
    scope: str
    enabled: bool
    can_toggle: bool = True
    executable: str = ""
    publisher: str = ""
    description: str = ""
    hive: str = "HKCU"
    approved_path: str = ""
    value_name: str = ""
    entry_type: str = "registry"
    location: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def status_label(self) -> str:
        return "Enabled" if self.enabled else "Disabled"

    @property
    def publisher_label(self) -> str:
        return self.publisher or "Unknown publisher"


@dataclass
class StartupSnapshot:
    entries: list[StartupEntry] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def startup_folders() -> list[tuple[Path, str, str]]:
    folders: list[tuple[Path, str, str]] = []
    appdata = os.environ.get("APPDATA")
    programdata = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
    if appdata:
        folders.append(
            (Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup", "Current user", "HKCU")
        )
    folders.append(
        (Path(programdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup", "All users", "HKLM")
    )
    return folders


def _approved_state(hive: str, approved_path: str, value_name: str) -> bool | None:
    if not approved_path:
        return None
    reg = read_value(hive, approved_path, value_name)
    if not reg.exists:
        return None
    data = reg.value
    if isinstance(data, int):
        return data == 0
    if isinstance(data, (bytes, bytearray)) and data:
        return data[0] not in {3, 7}
    return None


def _write_approved_state(
    hive: str, approved_path: str, value_name: str, enabled: bool
) -> RegValue:
    if not approved_path:
        raise ServiceError("No StartupApproved location is available for this entry.")
    if enabled:
        data = struct.pack("<I", 2) + b"\x00" * 8
    else:
        filetime = int(time.time() * 10_000_000) + 116444736000000000
        data = struct.pack("<I", 3) + struct.pack("<Q", filetime)
    return write_value(hive, approved_path, value_name, data, winreg.REG_BINARY)


def _parse_executable(command: str) -> str:
    if not command:
        return ""
    try:
        from winfirst.services.app_removal_service import split_windows_command_line

        parts = split_windows_command_line(command)
    except Exception:
        return ""
    if not parts:
        return ""
    candidate = parts[0].strip().strip('"')
    expanded = os.path.expandvars(candidate)
    if expanded and os.path.isfile(expanded):
        return expanded
    return expanded


def _version_strings(path: str) -> dict[str, str]:
    if not path or not os.path.isfile(path):
        return {}
    cached = _VERSION_CACHE.get(path.lower())
    if cached is not None:
        return cached
    if sys.platform != "win32":
        return {}
    result: dict[str, str] = {}
    try:
        version = ctypes.windll.version
        handle = ctypes.c_uint()
        size = version.GetFileVersionInfoSizeW(ctypes.c_wchar_p(path), ctypes.byref(handle))
        if not size:
            _VERSION_CACHE[path.lower()] = result
            return result
        buffer = ctypes.create_string_buffer(size)
        if not version.GetFileVersionInfoW(
            ctypes.c_wchar_p(path), 0, size, ctypes.cast(buffer, ctypes.c_void_p)
        ):
            _VERSION_CACHE[path.lower()] = result
            return result
        pointer = ctypes.c_void_p()
        length = ctypes.c_uint()
        translations: list[tuple[int, int]] = []
        if version.VerQueryValueW(
            ctypes.cast(buffer, ctypes.c_void_p),
            ctypes.c_wchar_p(r"\VarFileInfo\Translation"),
            ctypes.byref(pointer),
            ctypes.byref(length),
        ) and pointer.value and length.value >= 4:
            raw = ctypes.string_at(pointer.value, length.value)
            for index in range(0, len(raw) - 3, 4):
                lang, codepage = struct.unpack_from("<HH", raw, index)
                translations.append((lang, codepage))
        if not translations:
            translations = [(0x0409, 0x04B0)]
        for lang, codepage in translations[:6]:
            for field_name in ("CompanyName", "FileDescription", "ProductName"):
                sub = f"\\StringFileInfo\\{lang:04x}{codepage:04x}\\{field_name}"
                if version.VerQueryValueW(
                    ctypes.cast(buffer, ctypes.c_void_p),
                    ctypes.c_wchar_p(sub),
                    ctypes.byref(pointer),
                    ctypes.byref(length),
                ) and pointer.value and length.value:
                    value = ctypes.wstring_at(pointer.value, length.value // 2).split("\x00", 1)[0]
                    if value:
                        result.setdefault(field_name, value.strip())
    except Exception:
        result = {}
    _VERSION_CACHE[path.lower()] = result
    return result


def _publisher_for(path: str) -> tuple[str, str]:
    info = _version_strings(path)
    return info.get("CompanyName", ""), info.get("FileDescription", "") or info.get("ProductName", "")


def _resolve_shortcuts(runner: CommandRunner, folder: Path) -> dict[str, dict[str, str]]:
    if not folder.is_dir() or not runner.powershell_available:
        return {}
    script = f"""
$folder = {ps_quote(str(folder))}
$shell = New-Object -ComObject WScript.Shell
Get-ChildItem -LiteralPath $folder -Filter *.lnk -ErrorAction SilentlyContinue | ForEach-Object {{
    $shortcut = $shell.CreateShortcut($_.FullName)
    [pscustomobject]@{{ Name = $_.Name; Target = [string]$shortcut.TargetPath; Arguments = [string]$shortcut.Arguments }}
}} | ConvertTo-Json -Depth 3 -Compress
"""
    try:
        data = ps_json(runner, script, timeout=60, default=[])
    except Exception:
        return {}
    resolved: dict[str, dict[str, str]] = {}
    for row in as_list(data):
        name = str(row.get("Name") or "")
        if name:
            resolved[name.casefold()] = {
                "target": str(row.get("Target") or ""),
                "arguments": str(row.get("Arguments") or ""),
            }
    return resolved


def get_startup_entries(runner: CommandRunner | None = None) -> StartupSnapshot:
    snapshot = StartupSnapshot()
    runner = runner or CommandRunner()
    for hive, path, scope, _unused, run_kind in RUN_KEYS:
        approved_path = f"{_APPROVED_ROOT}\\{run_kind}"
        try:
            values = list_values(hive, path)
        except Exception as exc:
            snapshot.errors.append(f"Could not read {hive}\\{path}: {exc}")
            continue
        for name, value, _kind in values:
            if not isinstance(value, str) or not value.strip():
                continue
            executable = _parse_executable(value)
            publisher, description = _publisher_for(executable)
            approved = _approved_state(hive, approved_path, name)
            snapshot.entries.append(
                StartupEntry(
                    id=f"run:{hive}:{path}:{name}",
                    name=name,
                    command=value,
                    source="Registry Run",
                    scope=scope,
                    enabled=True if approved is None else approved,
                    can_toggle=True,
                    executable=executable,
                    publisher=publisher,
                    description=description,
                    hive=hive,
                    approved_path=approved_path,
                    value_name=name,
                    entry_type="registry",
                    location=f"{hive}\\{path}",
                )
            )
    for hive, path, scope, _unused, kind in RUN_ONCE_KEYS:
        try:
            values = list_values(hive, path)
        except Exception:
            continue
        for name, value, _kind in values:
            if not isinstance(value, str) or not value.strip():
                continue
            executable = _parse_executable(value)
            publisher, description = _publisher_for(executable)
            snapshot.entries.append(
                StartupEntry(
                    id=f"runonce:{hive}:{path}:{name}",
                    name=name,
                    command=value,
                    source="Registry RunOnce",
                    scope=scope,
                    enabled=True,
                    can_toggle=False,
                    executable=executable,
                    publisher=publisher,
                    description=description,
                    hive=hive,
                    entry_type="registry_once",
                    location=f"{hive}\\{path}",
                    warnings=["RunOnce entries run once at the next sign-in and cannot be disabled here."],
                )
            )
    for folder, scope, hive in startup_folders():
        if not folder.is_dir():
            continue
        shortcuts = _resolve_shortcuts(runner, folder)
        approved_path = f"{_APPROVED_ROOT}\\StartupFolder"
        try:
            files = sorted(folder.iterdir(), key=lambda path: path.name.casefold())
        except OSError as exc:
            snapshot.errors.append(f"Could not read {folder}: {exc}")
            continue
        for file in files:
            if not file.is_file() or file.name.lower() == "desktop.ini":
                continue
            info = shortcuts.get(file.name.casefold(), {})
            target = info.get("target", "")
            args = info.get("arguments", "")
            command = f'"{target}" {args}'.strip() if target else file.name
            publisher, description = _publisher_for(target)
            approved = _approved_state(hive, approved_path, file.name)
            snapshot.entries.append(
                StartupEntry(
                    id=f"folder:{hive}:{file.name}",
                    name=file.stem,
                    command=command,
                    source="Startup folder",
                    scope=scope,
                    enabled=True if approved is None else approved,
                    can_toggle=True,
                    executable=target,
                    publisher=publisher,
                    description=description,
                    hive=hive,
                    approved_path=approved_path,
                    value_name=file.name,
                    entry_type="folder",
                    location=str(file),
                )
            )
    snapshot.entries.sort(key=lambda item: (not item.enabled, item.scope, item.name.casefold()))
    return snapshot


def build_toggle_spec(entry: StartupEntry, enable: bool) -> OperationSpec:
    if not entry.can_toggle:
        raise ValueError("This startup entry cannot be toggled by WinFirst.")
    requires_admin = entry.hive == "HKLM"
    action = "enable" if enable else "disable"

    def apply(progress, cancel) -> ApplyOutcome:
        progress(f"Updating startup approval for {entry.name}")
        _write_approved_state(entry.hive, entry.approved_path, entry.value_name, enable)
        after = _approved_state(entry.hive, entry.approved_path, entry.value_name)
        return ApplyOutcome(
            success=after is True if enable else after is False,
            previous_state="Enabled" if entry.enabled else "Disabled",
            requested_state="Enabled" if enable else "Disabled",
            actual_state="Enabled" if after else "Disabled",
            warnings=[
                "The startup entry was not deleted. It can be re-enabled from this page.",
                "Review unknown publishers rather than assuming they are malicious.",
            ],
            rollback={
                "strategy": "startup_toggle",
                "hive": entry.hive,
                "approved_path": entry.approved_path,
                "value_name": entry.value_name,
                "entry_name": entry.name,
                "previous": entry.enabled,
                "summary": f"Restore startup entry {entry.name}",
            },
            details={"approved_path": f"{entry.hive}\\{entry.approved_path}", "value": entry.value_name},
        )

    def verify() -> VerifyOutcome:
        state = _approved_state(entry.hive, entry.approved_path, entry.value_name)
        actual = True if state is None else state
        return VerifyOutcome(
            ok=actual == enable,
            actual_state="Enabled" if actual else "Disabled",
            message="Read the StartupApproved state back from the registry.",
        )

    return OperationSpec(
        operation_id=f"startup.{action}",
        name=f"{action.capitalize()} startup entry: {entry.name}",
        category="startup",
        description=f"{action.capitalize()}s the startup entry using the StartupApproved registry state.",
        risk=RiskLevel.LOW,
        requires_admin=requires_admin,
        reversible=True,
        apply=apply,
        verify=verify,
        before=lambda: "Enabled" if entry.enabled else "Disabled",
        preview_text=f"{entry.name}: {'Enabled' if entry.enabled else 'Disabled'} -> {'Enabled' if enable else 'Disabled'}",
        warnings=[
            "Disabling a startup entry does not uninstall the application.",
            "Do not disable security or update helpers without understanding their purpose.",
        ],
        verification_method="StartupApproved registry state read-back",
        details={"entry": entry.name, "location": entry.location},
    )
