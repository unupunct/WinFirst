"""Windows repair and troubleshooting.

SFC and DISM run with explicit confirmation, streamed output and long timeouts.
Neither is presented as a universal fix.  Event-log inspection is read-only.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Any, Callable

from winfirst.core.command_runner import CommandRunner, as_list
from winfirst.core.operation import ApplyOutcome, OperationSpec, VerifyOutcome
from winfirst.core.os_detection import detect_pending_reboot
from winfirst.core.risk_engine import assess_action
from winfirst.services.common import ps_json, require_windows
from winfirst.services.storage_health_service import get_storage_snapshot

_EVENT_SCRIPT = r"""
$start = (Get-Date).AddDays(-__DAYS__)
try {
    $items = @(Get-WinEvent -FilterHashtable @{ LogName = '__LOG__'; Level = 1,2; StartTime = $start } -MaxEvents __LIMIT__ -ErrorAction Stop |
        ForEach-Object {
            [pscustomobject]@{
                Time = $_.TimeCreated.ToString('yyyy-MM-dd HH:mm:ss')
                Id = $_.Id
                Level = [string]$_.LevelDisplayName
                Provider = [string]$_.ProviderName
                Message = if ($_.Message) { $_.Message.Substring(0, [Math]::Min(600, $_.Message.Length)) } else { '' }
            }
        })
    $items | ConvertTo-Json -Depth 3 -Compress
} catch { '' }
"""

_CRASH_SCRIPT = r"""
$start = (Get-Date).AddDays(-30)
try {
    $items = @(Get-WinEvent -FilterHashtable @{ LogName = 'Application'; Id = 1000,1001,1002; StartTime = $start } -MaxEvents 100 -ErrorAction Stop |
        ForEach-Object {
            [pscustomobject]@{
                Time = $_.TimeCreated.ToString('yyyy-MM-dd HH:mm:ss')
                Id = $_.Id
                Provider = [string]$_.ProviderName
                Message = if ($_.Message) { $_.Message.Substring(0, [Math]::Min(700, $_.Message.Length)) } else { '' }
            }
        })
    $items | ConvertTo-Json -Depth 3 -Compress
} catch { '' }
"""


@dataclass
class EventEntry:
    time: str
    event_id: int
    level: str
    provider: str
    message: str


@dataclass
class CrashEntry:
    time: str
    event_id: int
    provider: str
    message: str


@dataclass
class RepairReport:
    name: str
    success: bool
    exit_code: int | None
    output: str
    summary: str
    restart_required: bool = False
    warnings: list[str] = field(default_factory=list)


def get_event_log(days: int = 7, limit: int = 100, log_name: str = "System") -> tuple[list[EventEntry], str]:
    runner = CommandRunner()
    if log_name not in {"System", "Application", "Setup"}:
        return [], "Unsupported event log."
    script = _EVENT_SCRIPT.replace("__DAYS__", str(max(1, min(days, 60)))).replace(
        "__LIMIT__", str(max(1, min(limit, 500)))
    ).replace("__LOG__", log_name)
    try:
        data = ps_json(runner, script, timeout=90, default=[])
    except Exception as exc:
        return [], str(exc)
    entries = []
    for row in as_list(data):
        try:
            event_id = int(row.get("Id") or 0)
        except (TypeError, ValueError):
            event_id = 0
        entries.append(
            EventEntry(
                time=str(row.get("Time") or ""),
                event_id=event_id,
                level=str(row.get("Level") or ""),
                provider=str(row.get("Provider") or ""),
                message=str(row.get("Message") or ""),
            )
        )
    return entries, ""


def get_application_crashes(days: int = 30) -> tuple[list[CrashEntry], str]:
    runner = CommandRunner()
    try:
        data = ps_json(runner, _CRASH_SCRIPT, timeout=90, default=[])
    except Exception as exc:
        return [], str(exc)
    crashes = []
    for row in as_list(data):
        try:
            event_id = int(row.get("Id") or 0)
        except (TypeError, ValueError):
            event_id = 0
        crashes.append(
            CrashEntry(
                time=str(row.get("Time") or ""),
                event_id=event_id,
                provider=str(row.get("Provider") or ""),
                message=str(row.get("Message") or ""),
            )
        )
    return crashes, ""


def get_repair_findings() -> dict[str, Any]:
    pending, reasons = detect_pending_reboot()
    storage = get_storage_snapshot()
    events, event_error = get_event_log(days=7, limit=60)
    crashes, crash_error = get_application_crashes()
    return {
        "pending_reboot": pending,
        "pending_reasons": reasons,
        "volumes": storage.volumes,
        "disks": storage.disks,
        "smart": storage.smart,
        "storage_errors": storage.errors,
        "critical_events": events,
        "event_error": event_error,
        "crashes": crashes,
        "crash_error": crash_error,
    }


def build_sfc_spec() -> OperationSpec:
    action = assess_action("repair.sfc")

    def apply(progress, cancel) -> ApplyOutcome:
        runner = CommandRunner()
        progress("Running sfc /scannow. This can take 10-60 minutes; output is shown as it arrives.")
        result = runner.run(
            ["sfc.exe", "/scannow"], timeout=3600, on_output=progress, cancel_event=cancel
        )
        text = result.combined
        success = result.launched and result.exit_code == 0 and not result.timed_out
        summary = _sfc_summary(text)
        return ApplyOutcome(
            success=success,
            previous_state="Windows protected system files",
            requested_state="Integrity scan and repair",
            actual_state=summary or ("Completed" if success else "Failed"),
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.exit_code,
            warnings=[
                "SFC repairs protected Windows files when it can. It does not fix every Windows problem.",
                "Review the full output above; some repairs take effect only after a restart.",
            ]
            + (["The scan may have been cancelled."] if result.cancelled else []),
            restart_required=_restart_from_text(text),
            error="" if success else (result.error or "sfc /scannow did not complete successfully."),
            details={"summary": summary},
        )

    return OperationSpec(
        operation_id="repair.sfc",
        name="Run System File Checker",
        category="repair",
        description="Runs sfc /scannow to scan and repair protected Windows system files.",
        risk=action.level,
        requires_admin=True,
        reversible=action.reversible,
        restart_required=False,
        apply=apply,
        verify=None,
        before=lambda: "Protected system files (state not directly readable)",
        preview_text="sfc /scannow",
        warnings=[
            "This can take 10-60 minutes.",
            "SFC does not expose an independent verification API; success is based on the documented exit status and output.",
        ],
        verification_method="sfc exit status and output summary; no separate integrity API is available",
    )


def build_dism_spec() -> OperationSpec:
    action = assess_action("repair.dism")

    def apply(progress, cancel) -> ApplyOutcome:
        runner = CommandRunner()
        progress(
            "Running DISM /Online /Cleanup-Image /RestoreHealth. "
            "This can take a long time and may require internet access."
        )
        result = runner.run(
            ["dism.exe", "/Online", "/Cleanup-Image", "/RestoreHealth"],
            timeout=3600,
            on_output=progress,
            cancel_event=cancel,
        )
        success = result.launched and result.exit_code == 0 and not result.timed_out
        text = result.combined
        return ApplyOutcome(
            success=success,
            previous_state="Windows component store",
            requested_state="Repaired component store",
            actual_state="DISM reported success" if success else "DISM reported failure",
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.exit_code,
            warnings=[
                "DISM may download replacement components from Windows Update.",
                "A restart may be required.",
            ],
            restart_required=_restart_from_text(text) or success,
            error="" if success else (result.error or "DISM did not complete successfully."),
        )

    def verify() -> VerifyOutcome:
        runner = CommandRunner()
        result = runner.run(
            ["dism.exe", "/Online", "/Cleanup-Image", "/CheckHealth"], timeout=300
        )
        text = result.combined.strip()
        healthy = result.ok and (
            "no component store corruption" in text.lower() or "operation completed successfully" in text.lower()
        )
        return VerifyOutcome(
            ok=healthy,
            actual_state="Component store health checked" if healthy else "CheckHealth did not confirm health",
            message=text[-800:] or "No CheckHealth output.",
        )

    return OperationSpec(
        operation_id="repair.dism",
        name="Repair Windows component store",
        category="repair",
        description="Runs DISM /Online /Cleanup-Image /RestoreHealth.",
        risk=action.level,
        requires_admin=True,
        reversible=action.reversible,
        restart_required=True,
        apply=apply,
        verify=verify,
        before=lambda: "Component store health not yet checked",
        preview_text="DISM /Online /Cleanup-Image /RestoreHealth",
        warnings=[
            "This can take 15-60 minutes or longer.",
            "Internet access or installation media may be required.",
            "DISM does not repair every Windows problem.",
        ],
        verification_method="DISM /CheckHealth after RestoreHealth",
    )


def _sfc_summary(text: str) -> str:
    lowered = text.lower()
    if "did not find any integrity violations" in lowered:
        return "No integrity violations found."
    if "successfully repaired" in lowered:
        return "Windows Resource Protection repaired files."
    if "unable to fix" in lowered or "could not repair" in lowered:
        return "Some corrupt files could not be repaired."
    if "verification" in lowered and "completed" in lowered:
        return "Verification completed; review the output."
    return ""


def _restart_from_text(text: str) -> bool:
    lowered = (text or "").lower()
    return any(phrase in lowered for phrase in ("restart", "reboot", "pending"))


def run_chkdsk_readonly(drive: str = "C") -> RepairReport:
    """Run chkdsk without repair switches (read-only)."""
    require_windows("CHKDSK")
    letter = (drive or "C").strip().rstrip(":").upper()
    if len(letter) != 1 or not letter.isalpha():
        raise ValueError("Invalid drive letter.")
    runner = CommandRunner()
    result = runner.run(["chkdsk.exe", f"{letter}:"], timeout=900)
    if not result.launched:
        return RepairReport("CHKDSK", False, result.exit_code, "", result.error)
    text = result.combined.strip()
    return RepairReport(
        name=f"CHKDSK {letter}: (read-only)",
        success=bool(result.ok),
        exit_code=result.exit_code,
        output=text[-8000:],
        summary="Read-only filesystem check completed." if result.ok else "CHKDSK reported a problem.",
        warnings=["This check does not repair the filesystem."],
    )
