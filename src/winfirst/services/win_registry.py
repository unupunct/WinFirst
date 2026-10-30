"""Small, validated Windows registry helpers.

Only documented values are written by WinFirst services.  Every write records
the previous value so supported settings can be rolled back.
"""
from __future__ import annotations

import ctypes
import os
import sys
from dataclasses import dataclass
from typing import Any

try:  # pragma: no cover - platform specific
    import winreg
except ImportError:  # pragma: no cover
    winreg = None  # type: ignore[assignment]

from winfirst.services.common import ServiceError, require_windows

HIVES: dict[str, int] = {}
if winreg is not None:
    HIVES = {
        "HKCU": winreg.HKEY_CURRENT_USER,
        "HKLM": winreg.HKEY_LOCAL_MACHINE,
        "HKCR": winreg.HKEY_CLASSES_ROOT,
        "HKU": winreg.HKEY_USERS,
    }

REG_TYPE_NAMES = {
    getattr(winreg, "REG_SZ", 1): "REG_SZ",
    getattr(winreg, "REG_EXPAND_SZ", 2): "REG_EXPAND_SZ",
    getattr(winreg, "REG_BINARY", 3): "REG_BINARY",
    getattr(winreg, "REG_DWORD", 4): "REG_DWORD",
    getattr(winreg, "REG_QWORD", 11): "REG_QWORD",
    getattr(winreg, "REG_MULTI_SZ", 7): "REG_MULTI_SZ",
} if winreg is not None else {}


@dataclass
class RegValue:
    exists: bool
    value: Any = None
    value_type: int | None = None
    error: str = ""

    @property
    def type_name(self) -> str:
        if self.value_type is None:
            return "absent"
        return REG_TYPE_NAMES.get(self.value_type, str(self.value_type))

    def display(self) -> str:
        if not self.exists:
            return "Not present"
        if isinstance(self.value, bytes):
            return f"<{len(self.value)} bytes>"
        if isinstance(self.value, (list, tuple)):
            return "; ".join(str(v) for v in self.value)
        return str(self.value)


def require_registry() -> None:
    require_windows("Registry access")
    if winreg is None:
        raise ServiceError("The Windows registry module is unavailable.")


def hive_handle(hive: str) -> int:
    require_registry()
    try:
        return HIVES[hive.upper()]
    except KeyError as exc:
        raise ServiceError(f"Unknown registry hive: {hive}") from exc


def read_value(hive: str, path: str, name: str) -> RegValue:
    require_registry()
    try:
        with winreg.OpenKey(hive_handle(hive), path, 0, winreg.KEY_READ) as key:
            value, value_type = winreg.QueryValueEx(key, name)
            return RegValue(True, value, value_type)
    except FileNotFoundError:
        return RegValue(False)
    except OSError as exc:
        return RegValue(False, error=str(exc))


def key_exists(hive: str, path: str) -> bool:
    require_registry()
    try:
        with winreg.OpenKey(hive_handle(hive), path, 0, winreg.KEY_READ):
            return True
    except OSError:
        return False


def write_value(
    hive: str,
    path: str,
    name: str,
    value: Any,
    value_type: int | None = None,
) -> RegValue:
    """Write a value and return the previous state for rollback."""
    require_registry()
    previous = read_value(hive, path, name)
    if value_type is None:
        if isinstance(value, bool):
            value_type = winreg.REG_DWORD
            value = int(value)
        elif isinstance(value, int):
            value_type = winreg.REG_DWORD
        elif isinstance(value, bytes):
            value_type = winreg.REG_BINARY
        else:
            value_type = winreg.REG_SZ
    try:
        with winreg.CreateKeyEx(
            hive_handle(hive), path, 0, winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE
        ) as key:
            winreg.SetValueEx(key, name, 0, value_type, value)
    except OSError as exc:
        raise ServiceError(f"Could not write {hive}\\{path}\\{name}: {exc}") from exc
    return previous


def delete_value(hive: str, path: str, name: str) -> RegValue:
    require_registry()
    previous = read_value(hive, path, name)
    if not previous.exists:
        return previous
    try:
        with winreg.OpenKey(
            hive_handle(hive), path, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.DeleteValue(key, name)
    except OSError as exc:
        raise ServiceError(f"Could not delete {hive}\\{path}\\{name}: {exc}") from exc
    return previous


def create_key(hive: str, path: str) -> None:
    require_registry()
    try:
        with winreg.CreateKeyEx(hive_handle(hive), path, 0, winreg.KEY_SET_VALUE):
            pass
    except OSError as exc:
        raise ServiceError(f"Could not create {hive}\\{path}: {exc}") from exc


def list_values(hive: str, path: str) -> list[tuple[str, Any, int]]:
    require_registry()
    items: list[tuple[str, Any, int]] = []
    try:
        with winreg.OpenKey(hive_handle(hive), path, 0, winreg.KEY_READ) as key:
            index = 0
            while True:
                try:
                    name, value, value_type = winreg.EnumValue(key, index)
                except OSError:
                    break
                items.append((name, value, value_type))
                index += 1
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise ServiceError(f"Could not enumerate {hive}\\{path}: {exc}") from exc
    return items


def broadcast_setting_change() -> None:
    """Notify running applications that user settings changed."""
    if sys.platform != "win32":
        return
    try:
        HWND_BROADCAST = 0xFFFF
        WM_SETTINGCHANGE = 0x001A
        SMTO_ABORTIFHUNG = 0x0002
        result = ctypes.c_ulong()
        ctypes.windll.user32.SendMessageTimeoutW(
            HWND_BROADCAST,
            WM_SETTINGCHANGE,
            0,
            ctypes.c_wchar_p("Immediate"),
            SMTO_ABORTIFHUNG,
            2000,
            ctypes.byref(result),
        )
    except Exception:
        pass


def notify_shell_associations() -> None:
    if sys.platform != "win32":
        return
    try:
        SHCNE_ASSOCCHANGED = 0x08000000
        SHCNF_IDLIST = 0x0000
        ctypes.windll.shell32.SHChangeNotify(SHCNE_ASSOCCHANGED, SHCNF_IDLIST, None, None)
    except Exception:
        pass


def expand_env(value: str) -> str:
    return os.path.expandvars(value) if value else value


def reg_type_for_ui(value: Any) -> int | None:
    if winreg is None:
        return None
    if isinstance(value, bool):
        return winreg.REG_DWORD
    if isinstance(value, int):
        return winreg.REG_DWORD
    if isinstance(value, str):
        return winreg.REG_SZ
    return None
