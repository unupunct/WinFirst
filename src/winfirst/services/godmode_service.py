"""God Mode folder creation and removal.

God Mode is a Windows shell folder identified by a documented CLSID.  It is a
convenience view of existing Control Panel applets, not a security bypass.
WinFirst only ever removes the exact folder it manages and refuses to delete
unrelated content.
"""
from __future__ import annotations

import ctypes
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

from winfirst.core.operation import ApplyOutcome, OperationSpec, VerifyOutcome
from winfirst.core.risk_engine import RiskLevel, assess_action
from winfirst.services.common import ServiceError, require_windows
from winfirst.services.win_registry import expand_env, read_value

CLSID = "{ED7BA470-8E54-465E-825C-99712043E01C}"
GOD_MODE_NAME = f"God Mode.{CLSID}"
_SHORT_NAME = f"GodMode.{CLSID}"
_ANY_GOD_MODE = re.compile(r"^.+\.\{" + re.escape(CLSID.strip("{}")) + r"\}$", re.IGNORECASE)


def _desktop_from_api() -> Path | None:
    if sys.platform != "win32":
        return None
    try:
        CSIDL_DESKTOPDIRECTORY = 0x0010
        buffer = ctypes.create_unicode_buffer(1024)
        result = ctypes.windll.shell32.SHGetFolderPathW(None, CSIDL_DESKTOPDIRECTORY, None, 0, buffer)
        if result == 0 and buffer.value:
            return Path(buffer.value)
    except Exception:
        pass
    return None


def _desktop_from_registry() -> Path | None:
    value = read_value(
        "HKCU",
        r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
        "Desktop",
    )
    if value.exists and isinstance(value.value, str) and value.value:
        return Path(expand_env(value.value))
    return None


def desktop_directory() -> Path:
    """Resolve the current user's Desktop, honouring redirection."""
    for candidate in (_desktop_from_api(), _desktop_from_registry()):
        if candidate is not None:
            try:
                return candidate.expanduser().resolve()
            except OSError:
                return candidate
    fallback = Path.home() / "Desktop"
    return fallback


@dataclass
class GodModeState:
    desktop: Path
    folder: Path
    exists: bool
    is_directory: bool
    managed: bool
    writable: bool
    strays: list[Path] = field(default_factory=list)
    error: str = ""

    @property
    def status(self) -> str:
        if self.exists and self.is_directory:
            return "Present"
        if self.exists:
            return "A file with the managed name exists"
        return "Not present"


def get_state() -> GodModeState:
    desktop = desktop_directory()
    folder = desktop / GOD_MODE_NAME
    exists = folder.exists()
    is_dir = folder.is_dir() if exists else False
    writable = os.access(desktop, os.W_OK) if desktop.exists() else False
    strays: list[Path] = []
    try:
        for entry in desktop.iterdir():
            if entry.name in {GOD_MODE_NAME, _SHORT_NAME}:
                continue
            if _ANY_GOD_MODE.match(entry.name):
                strays.append(entry)
    except OSError as exc:
        return GodModeState(
            desktop, folder, exists, is_dir, True, writable, error=str(exc)
        )
    return GodModeState(
        desktop=desktop,
        folder=folder,
        exists=exists,
        is_directory=is_dir,
        managed=True,
        writable=writable,
        strays=strays,
    )


def create() -> tuple[bool, str]:
    require_windows("God Mode")
    state = get_state()
    if not state.desktop.exists():
        raise ServiceError(f"The Desktop directory does not exist: {state.desktop}")
    if not state.desktop.is_dir():
        raise ServiceError(f"The Desktop path is not a directory: {state.desktop}")
    if state.exists and not state.is_directory:
        raise ServiceError(
            "A file with the God Mode name already exists on the Desktop. "
            "Remove or rename it manually before creating the folder."
        )
    if state.is_directory:
        return False, "God Mode was already present."
    try:
        state.folder.mkdir(mode=0o755)
    except OSError as exc:
        raise ServiceError(f"Could not create the God Mode folder: {exc}") from exc
    if not state.folder.is_dir():
        raise ServiceError("The God Mode folder could not be verified after creation.")
    return True, f"Created {state.folder}"


def remove(*, force: bool = False) -> tuple[bool, str]:
    require_windows("God Mode")
    state = get_state()
    if not state.exists:
        return False, "God Mode was not present."
    if state.folder.name != GOD_MODE_NAME:
        raise ServiceError("Refusing to remove an item that WinFirst does not manage.")
    if os.path.islink(state.folder):
        raise ServiceError("Refusing to remove a reparse point or symbolic link.")
    try:
        if state.folder.resolve().parent != state.desktop:
            raise ServiceError("Refusing to remove an item outside the Desktop directory.")
    except OSError as exc:
        raise ServiceError(f"Could not validate the God Mode path: {exc}") from exc
    if not state.is_directory:
        raise ServiceError("The managed path is not a directory; remove it manually.")
    contents = list(state.folder.iterdir()) if state.folder.exists() else []
    if contents and not force:
        raise ServiceError(
            "The God Mode folder contains items. WinFirst will not delete them. "
            "Move or remove the contents first, or confirm forced removal explicitly."
        )
    try:
        if contents and force:
            shutil.rmtree(state.folder)
        else:
            state.folder.rmdir()
    except OSError as exc:
        raise ServiceError(f"Could not remove the God Mode folder: {exc}") from exc
    if state.folder.exists():
        raise ServiceError("The God Mode folder still exists after removal.")
    return True, "God Mode folder removed."


def build_create_spec() -> OperationSpec:
    action = assess_action("godmode.create")
    return OperationSpec(
        operation_id="godmode.create",
        name="Create God Mode folder",
        category="desktop",
        description="Creates a God Mode folder on the current user's Desktop.",
        risk=action.level,
        requires_admin=False,
        reversible=action.reversible,
        apply=_create_apply,
        verify=_create_verify,
        before=lambda: get_state().status,
        preview_text=f"Create {GOD_MODE_NAME}",
        warnings=["God Mode is a shortcut view of existing Windows settings, not a security bypass."],
        verification_method="Checks that the managed folder exists on the resolved Desktop",
    )


def build_remove_spec(*, force: bool = False) -> OperationSpec:
    risk = RiskLevel.HIGH if force else RiskLevel.LOW
    return OperationSpec(
        operation_id="godmode.remove",
        name="Remove God Mode folder (forced)" if force else "Remove God Mode folder",
        category="desktop",
        description="Removes only the God Mode folder created at the managed Desktop path.",
        risk=risk,
        requires_admin=False,
        reversible=False,
        apply=_remove_apply(force),
        verify=_remove_verify,
        before=lambda: get_state().status,
        preview_text=f"Delete {GOD_MODE_NAME}" + (" including any contents" if force else ""),
        warnings=[
            "Only the exact WinFirst-managed folder is removed.",
            "Unrelated Desktop items are never touched.",
        ]
        + (["Forced removal deletes any files placed inside the God Mode folder."] if force else []),
        verification_method="Checks that the managed folder no longer exists",
    )


def _create_apply(progress, _cancel) -> ApplyOutcome:
    state_before = get_state()
    progress("Creating the God Mode folder")
    created, message = create()
    state_after = get_state()
    return ApplyOutcome(
        success=state_after.exists and state_after.is_directory,
        previous_state=state_before.status,
        requested_state="Present",
        actual_state=state_after.status,
        message=message,
        details={"desktop": str(state_before.desktop), "created": created},
        rollback={"strategy": "godmode", "action": "remove", "force": False,
                  "summary": "Remove the WinFirst-created God Mode folder"},
    )


def _create_verify() -> VerifyOutcome:
    state = get_state()
    return VerifyOutcome(
        ok=bool(state.exists and state.is_directory),
        actual_state=state.status,
        message=f"Desktop: {state.desktop}; folder exists: {state.exists}",
    )


def _remove_apply(force: bool):
    def apply(progress, _cancel) -> ApplyOutcome:
        state_before = get_state()
        progress("Removing the God Mode folder")
        removed, message = remove(force=force)
        state_after = get_state()
        return ApplyOutcome(
            success=not state_after.exists,
            previous_state=state_before.status,
            requested_state="Not present",
            actual_state=state_after.status,
            message=message,
            details={"desktop": str(state_before.desktop), "removed": removed},
        )

    return apply


def _remove_verify() -> VerifyOutcome:
    state = get_state()
    return VerifyOutcome(
        ok=not state.exists,
        actual_state=state.status,
        message=f"Managed folder exists after removal: {state.exists}",
    )
