"""Privilege detection and safe elevation.

Elevation is requested only when the user starts an action that needs it.
Parameters for supported declarative setting changes are preserved in a small,
validated JSON request file; the elevated instance shows a preview and requires
confirmation before applying anything.  Arbitrary commands are never placed in
an elevation request.
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from pathlib import Path

from winfirst.core import elevation_request
from winfirst.core.paths import install_dir, is_frozen


def is_elevated() -> bool:
    if sys.platform != "win32":
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def is_admin() -> bool:
    return is_elevated()


def integrity_note() -> str:
    if sys.platform != "win32":
        return "Not running on Windows."
    if is_elevated():
        return "WinFirst is running with administrator privileges."
    return "WinFirst is running as a standard user. Read-only inspection is available; changes that require administrator rights will ask before elevation."


def _launch_argv(extra_args: list[str]) -> tuple[str, str, str]:
    """Return (executable, parameter_string, working_directory)."""
    if is_frozen():
        executable = sys.executable
        args: list[str] = []
        workdir = str(Path(executable).parent)
    else:
        executable = sys.executable
        entry = Path(sys.argv[0]).resolve()
        if entry.suffix.lower() != ".py" or not entry.is_file():
            entry = install_dir() / "main.py"
        if not entry.is_file():
            raise FileNotFoundError("Unable to locate the WinFirst entry point for elevation")
        args = [str(entry)]
        workdir = str(entry.parent)
    args.extend(extra_args)
    return executable, subprocess.list2cmdline(args), workdir


def relaunch_elevated(extra_args: list[str] | None = None) -> tuple[bool, str]:
    """Start an elevated copy of WinFirst.  Returns (started, message)."""
    if sys.platform != "win32":
        return False, "Elevation is only supported on Windows."
    if is_elevated():
        return False, "WinFirst is already running with administrator privileges."
    try:
        executable, parameters, workdir = _launch_argv(list(extra_args or []))
    except OSError as exc:
        return False, str(exc)
    try:
        shell32 = ctypes.windll.shell32
        shell32.ShellExecuteW.restype = ctypes.c_void_p
        result = shell32.ShellExecuteW(
            None, "runas", executable, parameters, workdir, 1
        )
        code = int(result) if result is not None else 0
    except Exception as exc:
        return False, f"Windows refused the elevation request: {exc}"
    if code <= 32:
        messages = {
            5: "Access denied by Windows or organizational policy.",
            31: "The elevation request could not be associated with a desktop.",
            1223: "Elevation was cancelled.",
        }
        return False, messages.get(code, f"Elevation failed (ShellExecute code {code}).")
    return True, "An elevated WinFirst window was started. This window will close."


def relaunch_with_settings(settings: dict[str, object], *, profile_name: str = "") -> tuple[bool, str]:
    """Persist a validated settings request and restart elevated.

    Only declarative settings are accepted.  The elevated instance validates
    every key again before showing a preview.
    """
    try:
        request_path = elevation_request.write_request(
            {"type": "settings", "settings": settings, "profile_name": profile_name}
        )
    except ValueError as exc:
        return False, f"Could not prepare the elevation request: {exc}"
    started, message = relaunch_elevated(["--elevated-request", str(request_path)])
    if not started:
        elevation_request.discard(request_path)
    return started, message


def current_user_label() -> str:
    return os.environ.get("USERNAME") or os.environ.get("USER") or "unknown"
