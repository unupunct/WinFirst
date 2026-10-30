"""File Explorer and Desktop preferences stored in documented HKCU values."""
from __future__ import annotations

import time
from dataclasses import dataclass

from winfirst.core.command_runner import CommandRunner
from winfirst.services.common import ServiceError, require_windows, run_ok
from winfirst.services.win_registry import (
    RegValue,
    broadcast_setting_change,
    key_exists,
    notify_shell_associations,
    read_value,
    write_value,
)

ADVANCED = r"Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced"
DESKTOP_ICONS = r"Software\Microsoft\Windows\CurrentVersion\Explorer\HideDesktopIcons\NewStartPanel"
EXPLORER_POLICY = r"SOFTWARE\Policies\Microsoft\Windows\Explorer"
CLSID_THIS_PC = "{20D04FE0-3AEA-1069-A2D8-08002B30309D}"
CLSID_USER_FILES = "{59031a47-3f72-44a7-89c5-5595fe6b30ee}"

HIDE_FILE_EXT = "HideFileExt"
HIDDEN = "Hidden"
SHOW_SUPER_HIDDEN = "ShowSuperHidden"
LAUNCH_TO = "LaunchTo"


@dataclass
class ExplorerReading:
    value: bool
    exists: bool
    raw: str = ""
    note: str = ""


def policy_present() -> bool:
    try:
        return key_exists("HKLM", EXPLORER_POLICY) or key_exists(
            "HKCU", r"Software\Policies\Microsoft\Windows\Explorer"
        )
    except Exception:
        return False


def _policy_note() -> str:
    if policy_present():
        return "Explorer policy keys are present; an organization may manage some preferences."
    return ""


def read_show_file_extensions() -> ExplorerReading:
    reg = read_value("HKCU", ADVANCED, HIDE_FILE_EXT)
    if not reg.exists:
        return ExplorerReading(False, False, note="Not configured; Windows hides known extensions by default.")
    return ExplorerReading(
        value=int(reg.value or 0) == 0,
        exists=True,
        raw=str(reg.value),
        note=_policy_note(),
    )


def read_show_hidden_files() -> ExplorerReading:
    reg = read_value("HKCU", ADVANCED, HIDDEN)
    if not reg.exists:
        return ExplorerReading(False, False, note="Not configured; hidden files are hidden by default.")
    return ExplorerReading(
        value=int(reg.value or 0) == 1, exists=True, raw=str(reg.value), note=_policy_note()
    )


def read_show_super_hidden() -> ExplorerReading:
    reg = read_value("HKCU", ADVANCED, SHOW_SUPER_HIDDEN)
    if not reg.exists:
        return ExplorerReading(False, False, note="Not configured; protected operating system files are hidden.")
    return ExplorerReading(
        value=int(reg.value or 0) == 1, exists=True, raw=str(reg.value), note=_policy_note()
    )


def read_open_to_this_pc() -> ExplorerReading:
    reg = read_value("HKCU", ADVANCED, LAUNCH_TO)
    if not reg.exists:
        return ExplorerReading(False, False, note="Not configured; File Explorer opens to Quick Access/Home.")
    return ExplorerReading(
        value=int(reg.value or 0) == 1, exists=True, raw=str(reg.value), note=_policy_note()
    )


def read_desktop_icon_value(name: str, default_visible: bool) -> ExplorerReading:
    reg = read_value("HKCU", DESKTOP_ICONS, name)
    if not reg.exists:
        return ExplorerReading(
            default_visible,
            False,
            note="Not configured; Windows default applies.",
        )
    return ExplorerReading(
        value=int(reg.value or 0) == 0, exists=True, raw=str(reg.value), note=_policy_note()
    )


def _write_dword(name: str, value: int, path: str = ADVANCED) -> RegValue:
    previous = write_value("HKCU", path, name, int(value))
    broadcast_setting_change()
    return previous


def set_show_file_extensions(enabled: bool) -> RegValue:
    previous = _write_dword(HIDE_FILE_EXT, 0 if enabled else 1)
    if read_show_file_extensions().value != enabled:
        raise ServiceError("The file extension preference did not change.")
    return previous


def set_show_hidden_files(enabled: bool) -> RegValue:
    previous = _write_dword(HIDDEN, 1 if enabled else 2)
    if read_show_hidden_files().value != enabled:
        raise ServiceError("The hidden files preference did not change.")
    return previous


def set_show_super_hidden(enabled: bool) -> RegValue:
    previous = _write_dword(SHOW_SUPER_HIDDEN, 1 if enabled else 0)
    if read_show_super_hidden().value != enabled:
        raise ServiceError("The protected operating system files preference did not change.")
    return previous


def set_open_to_this_pc(enabled: bool) -> RegValue:
    previous = _write_dword(LAUNCH_TO, 1 if enabled else 2)
    if read_open_to_this_pc().value != enabled:
        raise ServiceError("The File Explorer startup location did not change.")
    return previous


def set_desktop_icon(name: str, visible: bool, *, default_visible: bool) -> RegValue:
    if name not in {CLSID_THIS_PC, CLSID_USER_FILES}:
        raise ServiceError("Unsupported desktop icon identifier.")
    previous = write_value("HKCU", DESKTOP_ICONS, name, 0 if visible else 1)
    broadcast_setting_change()
    notify_shell_associations()
    if read_desktop_icon_value(name, default_visible).value != visible:
        raise ServiceError("The desktop icon preference did not change.")
    return previous


def refresh_explorer() -> str:
    broadcast_setting_change()
    notify_shell_associations()
    return "Settings were broadcast to Explorer. Some changes appear only in newly opened windows."


def restart_explorer(runner: CommandRunner | None = None) -> str:
    """Restart Explorer after explicit confirmation from the UI."""
    require_windows("Restarting File Explorer")
    runner = runner or CommandRunner()
    run_ok(
        runner,
        ["taskkill.exe", "/IM", "explorer.exe", "/F"],
        timeout=30,
        error_prefix="Could not stop File Explorer",
    )
    time.sleep(1.0)
    run_ok(
        runner,
        [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "Start-Process explorer.exe",
        ],
        timeout=30,
        error_prefix="Could not start File Explorer",
    )
    for _ in range(20):
        time.sleep(0.25)
        result = runner.run(["tasklist.exe", "/FI", "IMAGENAME eq explorer.exe"], timeout=15)
        if "explorer.exe" in result.stdout.lower():
            return "File Explorer was restarted."
    raise ServiceError("Explorer was stopped but could not be confirmed as running again.")
