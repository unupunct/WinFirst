"""Shared service helpers."""
from __future__ import annotations

import sys
from typing import Callable

from winfirst.core.command_runner import CommandResult, CommandRunner, OutputCallback


class ServiceError(RuntimeError):
    """A system operation could not be completed.  Message is user-facing."""


def require_windows(feature: str = "This feature") -> None:
    if sys.platform != "win32":
        raise ServiceError(f"{feature} requires Windows 10 or Windows 11.")


def run_ok(
    runner: CommandRunner,
    argv: list[str],
    *,
    timeout: float = 60,
    on_output: OutputCallback | None = None,
    error_prefix: str = "Command failed",
    cancel_event=None,
) -> CommandResult:
    result = runner.run(
        argv, timeout=timeout, on_output=on_output, cancel_event=cancel_event
    )
    if not result.launched:
        raise ServiceError(result.error or f"{error_prefix}: the tool could not be started")
    if result.timed_out:
        raise ServiceError(f"{error_prefix}: timed out after {int(timeout)} seconds")
    if result.cancelled:
        raise ServiceError(f"{error_prefix}: cancelled")
    if result.exit_code != 0:
        detail = (result.stderr or result.stdout).strip()[:1500]
        raise ServiceError(f"{error_prefix} (exit {result.exit_code}): {detail}")
    return result


def run_streaming(
    runner: CommandRunner,
    argv: list[str],
    *,
    timeout: float,
    on_output: OutputCallback,
    cancel_event=None,
) -> CommandResult:
    return runner.run(
        argv, timeout=timeout, on_output=on_output, cancel_event=cancel_event
    )


def format_bool(value: bool) -> str:
    return "Enabled" if value else "Disabled"


def human_seconds(seconds: int | None) -> str:
    if seconds is None:
        return "Unknown"
    if seconds == 0:
        return "Never"
    minutes, sec = divmod(int(seconds), 60)
    if minutes >= 60 and sec == 0:
        hours, minutes = divmod(minutes, 60)
        return f"{hours} h {minutes} min" if minutes else f"{hours} h"
    return f"{minutes} min" if sec == 0 else f"{minutes} min {sec} s"


def bool_from(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}
    return default


def clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def ps_json(runner: CommandRunner, script: str, *, timeout: float = 60, default=None):
    """Run a PowerShell script that emits JSON, raising a user-facing error."""
    if not runner.powershell_available:
        raise ServiceError("Windows PowerShell is not available.")
    result = runner.powershell_json(script, timeout=timeout, default=default)
    return result


def ps_text(runner: CommandRunner, script: str, *, timeout: float = 60) -> str:
    if not runner.powershell_available:
        raise ServiceError("Windows PowerShell is not available.")
    result = runner.powershell(script, timeout=timeout)
    if not result.launched:
        raise ServiceError(result.error or "PowerShell could not be started.")
    if result.exit_code != 0:
        raise ServiceError((result.stderr or result.stdout).strip()[:1500] or "PowerShell failed.")
    return result.stdout
