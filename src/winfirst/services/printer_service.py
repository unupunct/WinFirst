"""Printer, queue and print-spooler inspection with narrowly scoped repairs."""
from __future__ import annotations

import socket
from dataclasses import dataclass, field
from typing import Any, Callable

from winfirst.core.command_runner import CommandRunner, as_list
from winfirst.core.operation import ApplyOutcome, OperationSpec, VerifyOutcome
from winfirst.core.risk_engine import RiskLevel
from winfirst.services.common import ServiceError, bool_from, ps_json, require_windows

ProgressFn = Callable[[str], None]

_PRINTER_SCRIPT = r"""
$result = [ordered]@{ Printers = @(); Jobs = @(); Ports = @(); Spooler = $null; Default = ''; Errors = @() }
try {
    $result.Printers = @(Get-Printer -ErrorAction Stop | ForEach-Object {
        [pscustomobject]@{
            Name = [string]$_.Name; Driver = [string]$_.DriverName; Port = [string]$_.PortName;
            Status = [string]$_.PrinterStatus; Shared = [bool]$_.Shared; Published = [bool]$_.Published;
            Type = [string]$_.Type; Comment = [string]$_.Comment; Location = [string]$_.Location
        }
    })
} catch { $result.Errors += "Printers: $($_.Exception.Message)" }
try {
    $result.Jobs = @(Get-Printer -ErrorAction Stop | ForEach-Object {
        $p = $_
        try {
            Get-PrintJob -PrinterName $p.Name -ErrorAction Stop | ForEach-Object {
                [pscustomobject]@{
                    Printer = [string]$p.Name; Id = $_.Id; Document = [string]$_.DocumentName;
                    Status = [string]$_.JobStatus; Submitted = if ($_.SubmittedTime) { $_.SubmittedTime.ToString('o') } else { '' };
                    Size = $_.Size; Pages = $_.TotalPages
                }
            }
        } catch { }
    })
} catch { $result.Errors += "Jobs: $($_.Exception.Message)" }
try {
    $result.Ports = @(Get-PrinterPort -ErrorAction Stop | ForEach-Object {
        [pscustomobject]@{
            Name = [string]$_.Name; Description = [string]$_.Description;
            Host = [string]$_.PrinterHostAddress; PortNumber = $_.PortNumber; Protocol = [string]$_.Protocol
        }
    })
} catch { $result.Errors += "Ports: $($_.Exception.Message)" }
try {
    $s = Get-Service -Name Spooler -ErrorAction Stop
    $result.Spooler = [pscustomobject]@{ Status = [string]$s.Status; StartType = [string]$s.StartType }
} catch { $result.Errors += "Spooler: $($_.Exception.Message)" }
try {
    $d = Get-CimInstance Win32_Printer -ErrorAction Stop | Where-Object { $_.Default } | Select-Object -First 1
    if ($d) { $result.Default = [string]$d.Name }
} catch { }
[pscustomobject]$result | ConvertTo-Json -Depth 5 -Compress
"""


@dataclass
class PrinterInfo:
    name: str
    driver: str = ""
    port: str = ""
    status: str = ""
    shared: bool = False
    published: bool = False
    printer_type: str = ""
    comment: str = ""
    location: str = ""
    is_default: bool = False
    job_count: int = 0
    problem: str = ""


@dataclass
class PrintJob:
    printer: str
    job_id: int | None = None
    document: str = ""
    status: str = ""
    submitted: str = ""
    size: int | None = None
    pages: int | None = None


@dataclass
class PrinterPort:
    name: str
    description: str = ""
    host: str = ""
    port_number: int | None = None
    protocol: str = ""


@dataclass
class SpoolerInfo:
    status: str = "Unknown"
    start_type: str = ""

    @property
    def running(self) -> bool:
        return self.status.lower() == "running"


@dataclass
class PrinterSnapshot:
    printers: list[PrinterInfo] = field(default_factory=list)
    jobs: list[PrintJob] = field(default_factory=list)
    ports: list[PrinterPort] = field(default_factory=list)
    spooler: SpoolerInfo = field(default_factory=SpoolerInfo)
    default_printer: str = ""
    errors: list[str] = field(default_factory=list)

    def findings(self) -> list[dict[str, str]]:
        findings: list[dict[str, str]] = []
        if not self.spooler.running:
            findings.append({
                "key": "spooler",
                "title": "Print Spooler is not running",
                "status": "critical",
                "detail": f"Spooler status is {self.spooler.status}. Printing will fail until it is started.",
            })
        for printer in self.printers:
            status = printer.status.lower()
            if status and status not in {"normal", "idle", "printing", "ready"}:
                findings.append({
                    "key": f"printer_{printer.name}",
                    "title": f"Printer status: {printer.name}",
                    "status": "warning",
                    "detail": f"Windows reports {printer.status} on port {printer.port or 'unknown'}.",
                })
            if printer.problem:
                findings.append({
                    "key": f"printer_problem_{printer.name}",
                    "title": printer.name,
                    "status": "warning",
                    "detail": printer.problem,
                })
        stuck = [job for job in self.jobs if _job_has_error(job)]
        if stuck:
            findings.append({
                "key": "print_jobs",
                "title": f"{len(stuck)} print job(s) need attention",
                "status": "warning",
                "detail": "Queued jobs report an error or offline state. Review before clearing the queue.",
            })
        if not findings:
            findings.append({
                "key": "printers_ok",
                "title": "No printer problems detected",
                "status": "ok",
                "detail": "The spooler is running and no printer reports an error state.",
            })
        return findings


def _job_has_error(job: PrintJob) -> bool:
    lowered = (job.status or "").lower()
    return any(token in lowered for token in ("error", "offline", "blocked", "paused", "deleted"))


def _as_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None and str(value).strip() != "" else None
    except (TypeError, ValueError):
        return None


def get_printer_snapshot(runner: CommandRunner | None = None) -> PrinterSnapshot:
    require_windows("Printer diagnostics")
    runner = runner or CommandRunner()
    try:
        data = ps_json(runner, _PRINTER_SCRIPT, timeout=90, default={})
    except Exception as exc:
        raise ServiceError(f"Could not read printer information: {exc}") from exc
    if not isinstance(data, dict):
        data = {}
    snapshot = PrinterSnapshot()
    snapshot.default_printer = str(data.get("Default") or "")
    snapshot.errors = [str(item) for item in as_list(data.get("Errors")) if item]
    for row in as_list(data.get("Printers")):
        if not isinstance(row, dict):
            continue
        name = str(row.get("Name") or "")
        if not name:
            continue
        snapshot.printers.append(
            PrinterInfo(
                name=name,
                driver=str(row.get("Driver") or ""),
                port=str(row.get("Port") or ""),
                status=str(row.get("Status") or ""),
                shared=bool_from(row.get("Shared")),
                published=bool_from(row.get("Published")),
                printer_type=str(row.get("Type") or ""),
                comment=str(row.get("Comment") or ""),
                location=str(row.get("Location") or ""),
                is_default=name.casefold() == snapshot.default_printer.casefold(),
            )
        )
    for row in as_list(data.get("Jobs")):
        if not isinstance(row, dict):
            continue
        snapshot.jobs.append(
            PrintJob(
                printer=str(row.get("Printer") or ""),
                job_id=_as_int(row.get("Id")),
                document=str(row.get("Document") or ""),
                status=str(row.get("Status") or ""),
                submitted=str(row.get("Submitted") or ""),
                size=_as_int(row.get("Size")),
                pages=_as_int(row.get("Pages")),
            )
        )
    for row in as_list(data.get("Ports")):
        if not isinstance(row, dict):
            continue
        snapshot.ports.append(
            PrinterPort(
                name=str(row.get("Name") or ""),
                description=str(row.get("Description") or ""),
                host=str(row.get("Host") or ""),
                port_number=_as_int(row.get("PortNumber")),
                protocol=str(row.get("Protocol") or ""),
            )
        )
    spooler = data.get("Spooler")
    if isinstance(spooler, dict):
        snapshot.spooler = SpoolerInfo(
            status=str(spooler.get("Status") or "Unknown"),
            start_type=str(spooler.get("StartType") or ""),
        )
    counts: dict[str, int] = {}
    for job in snapshot.jobs:
        counts[job.printer.casefold()] = counts.get(job.printer.casefold(), 0) + 1
    for printer in snapshot.printers:
        printer.job_count = counts.get(printer.name.casefold(), 0)
    snapshot.printers.sort(key=lambda item: (not item.is_default, item.name.casefold()))
    return snapshot


def test_printer_port(port: PrinterPort, timeout: float = 1.5) -> tuple[bool | None, str]:
    """Test a TCP print port.  Returns (reachable, explanation)."""
    if not port.host:
        return None, "No host address is exposed for this port."
    if not (port.host.replace(".", "").isdigit() or ":" in port.host):
        return None, f"Port host '{port.host}' is not a numeric IP address."
    target_port = port.port_number or 9100
    try:
        with socket.create_connection((port.host, target_port), timeout=timeout):
            return True, f"TCP {port.host}:{target_port} accepted a connection."
    except OSError as exc:
        return False, f"TCP {port.host}:{target_port} did not accept a connection: {exc}"


def get_printer_drivers(runner: CommandRunner | None = None) -> list[dict[str, str]]:
    runner = runner or CommandRunner()
    script = (
        "@(Get-PrinterDriver -ErrorAction SilentlyContinue | ForEach-Object { "
        "[pscustomobject]@{ Name = [string]$_.Name; Manufacturer = [string]$_.Manufacturer; "
        "Version = [string]$_.DriverVersion; Environment = [string]$_.PrinterEnvironment } }) | "
        "ConvertTo-Json -Depth 3 -Compress"
    )
    data = ps_json(runner, script, timeout=60, default=[])
    return [
        {
            "name": str(row.get("Name") or ""),
            "manufacturer": str(row.get("Manufacturer") or ""),
            "version": str(row.get("Version") or ""),
            "environment": str(row.get("Environment") or ""),
        }
        for row in as_list(data)
        if isinstance(row, dict) and row.get("Name")
    ]


def _job_count(runner: CommandRunner, printer_name: str | None = None) -> int:
    if printer_name:
        script = f"@(Get-PrintJob -PrinterName {_ps_quote(printer_name)} -ErrorAction Stop).Count"
    else:
        script = (
            "$n = 0; Get-Printer -ErrorAction SilentlyContinue | ForEach-Object { "
            "$n += @(Get-PrintJob -PrinterName $_.Name -ErrorAction SilentlyContinue).Count }; $n"
        )
    result = runner.powershell(script, timeout=60)
    if not result.launched or result.exit_code != 0:
        raise ServiceError((result.stderr or result.stdout).strip()[:500] or "Could not count print jobs.")
    try:
        return int((result.stdout or "0").strip().splitlines()[-1])
    except (ValueError, IndexError):
        return 0


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _spooler_running(runner: CommandRunner) -> bool:
    result = runner.powershell(
        "$s = Get-Service -Name Spooler -ErrorAction Stop; $s.Status.ToString()", timeout=30
    )
    if not result.launched or result.exit_code != 0:
        raise ServiceError((result.stderr or result.stdout).strip()[:500] or "Could not read the spooler state.")
    return (result.stdout or "").strip().splitlines()[-1].strip().lower() == "running"


def build_clear_jobs_spec(printer_name: str, snapshot: PrinterSnapshot | None = None) -> OperationSpec:
    if not printer_name:
        raise ValueError("A printer name is required.")
    count = _job_count(CommandRunner(), printer_name)

    def apply(progress: ProgressFn, cancel) -> ApplyOutcome:
        runner = CommandRunner()
        progress(f"Removing print jobs for {printer_name}")
        script = (
            f"$jobs = @(Get-PrintJob -PrinterName {_ps_quote(printer_name)} -ErrorAction Stop); "
            "if ($jobs.Count -gt 0) { $jobs | Remove-PrintJob -ErrorAction Stop }; "
            "$jobs.Count"
        )
        result = runner.powershell(script, timeout=120, cancel_event=cancel)
        if not result.launched:
            raise ServiceError(result.error or "PowerShell could not be started.")
        if result.exit_code != 0:
            raise ServiceError((result.stderr or result.stdout).strip()[:800] or "Removing print jobs failed.")
        removed = 0
        try:
            removed = int((result.stdout or "0").strip().splitlines()[-1])
        except (ValueError, IndexError):
            pass
        return ApplyOutcome(
            success=True,
            previous_state=f"{count:,} queued job(s)",
            requested_state="Queue empty",
            actual_state=f"Removed {removed:,} job(s); verification pending",
            warnings=[
                "Queued jobs are deleted and must be printed again.",
                "The document content is not inspected or logged.",
            ],
            details={"printer": printer_name, "removed": removed},
            exit_code=result.exit_code,
        )

    def verify() -> VerifyOutcome:
        remaining = _job_count(CommandRunner(), printer_name)
        return VerifyOutcome(
            ok=remaining == 0,
            actual_state=f"{remaining:,} job(s) remain",
            message="Counted the printer queue after removal.",
        )

    def before() -> str:
        return f"{count:,} queued job(s)"

    return OperationSpec(
        operation_id=f"printer.clear_jobs.{printer_name}",
        name=f"Clear print queue: {printer_name}",
        category="printers",
        description="Removes all jobs queued for this printer.",
        risk=RiskLevel.MEDIUM,
        requires_admin=False,
        reversible=False,
        apply=apply,
        verify=verify,
        before=before,
        preview_text=f"{printer_name}: {count:,} queued job(s) will be deleted",
        warnings=[
            "Queued jobs are deleted and must be printed again.",
            "If the queue is stuck, restarting the Print Spooler may also be required.",
        ],
        verification_method="Recounts the printer queue with Get-PrintJob.",
        details={"printer": printer_name},
    )


def build_restart_spooler_spec() -> OperationSpec:
    def apply(progress: ProgressFn, cancel) -> ApplyOutcome:
        runner = CommandRunner()
        progress("Restarting the Print Spooler")
        script = (
            "$s = Get-Service -Name Spooler -ErrorAction Stop; "
            "if ($s.Status -eq 'Running') { Stop-Service -Name Spooler -Force -ErrorAction Stop; "
            "$s.WaitForStatus('Stopped','00:01:00') }; "
            "Start-Service -Name Spooler -ErrorAction Stop; "
            "$s.WaitForStatus('Running','00:01:00')"
        )
        result = runner.powershell(script, timeout=120, cancel_event=cancel)
        if not result.launched:
            raise ServiceError(result.error or "PowerShell could not be started.")
        if result.exit_code != 0:
            raise ServiceError(
                (result.stderr or result.stdout).strip()[:800]
                or "The Print Spooler could not be restarted. Check Services and permissions."
            )
        return ApplyOutcome(
            success=True,
            previous_state="Unknown",
            requested_state="Running",
            actual_state="Restart command accepted; verification pending",
            warnings=[
                "Printing is unavailable while the service restarts.",
                "Restarting the spooler does not delete queued jobs.",
            ],
            details={},
            exit_code=result.exit_code,
        )

    def verify() -> VerifyOutcome:
        running = _spooler_running(CommandRunner())
        return VerifyOutcome(
            ok=running,
            actual_state="Running" if running else "Stopped",
            message="Read the Print Spooler service state back with Get-Service.",
        )

    return OperationSpec(
        operation_id="printer.restart_spooler",
        name="Restart the Print Spooler",
        category="printers",
        description="Stops and starts the Windows Print Spooler service.",
        risk=RiskLevel.MEDIUM,
        requires_admin=True,
        reversible=True,
        apply=apply,
        verify=verify,
        before=lambda: "Spooler state will be read back",
        preview_text="Restart the Print Spooler service",
        warnings=[
            "Printing is briefly unavailable.",
            "If the service does not restart, start it manually in Services.",
        ],
        verification_method="Reads the Print Spooler service state back with Get-Service.",
    )


def build_restart_spooler_clear_spec(printer_name: str | None = None) -> OperationSpec:
    label = printer_name or "all printers"
    count = _job_count(CommandRunner(), printer_name)

    def apply(progress: ProgressFn, cancel) -> ApplyOutcome:
        runner = CommandRunner()
        progress("Stopping the Print Spooler")
        stop_script = (
            "$s = Get-Service -Name Spooler -ErrorAction Stop; "
            "if ($s.Status -eq 'Running') { Stop-Service -Name Spooler -Force -ErrorAction Stop; "
            "$s.WaitForStatus('Stopped','00:01:00') }"
        )
        started = False
        try:
            result = runner.powershell(stop_script, timeout=90, cancel_event=cancel)
            if not result.launched or result.exit_code != 0:
                raise ServiceError((result.stderr or result.stdout).strip()[:800] or "Could not stop the spooler.")
            progress(f"Removing queued jobs for {label}")
            if printer_name:
                clear = (
                    f"Get-PrintJob -PrinterName {_ps_quote(printer_name)} -ErrorAction SilentlyContinue | "
                    "Remove-PrintJob -ErrorAction SilentlyContinue"
                )
            else:
                clear = (
                    "Get-Printer -ErrorAction SilentlyContinue | ForEach-Object { "
                    "Get-PrintJob -PrinterName $_.Name -ErrorAction SilentlyContinue | "
                    "Remove-PrintJob -ErrorAction SilentlyContinue }"
                )
            runner.powershell(clear, timeout=120, cancel_event=cancel)
            progress("Starting the Print Spooler")
            start = (
                "Start-Service -Name Spooler -ErrorAction Stop; "
                "$s = Get-Service -Name Spooler; $s.WaitForStatus('Running','00:01:00')"
            )
            result = runner.powershell(start, timeout=90, cancel_event=cancel)
            if not result.launched or result.exit_code != 0:
                raise ServiceError(
                    (result.stderr or result.stdout).strip()[:800]
                    or "The spooler was stopped but could not be restarted. Start it manually in Services."
                )
            started = True
            return ApplyOutcome(
                success=True,
                previous_state=f"{count:,} job(s)",
                requested_state="Spooler running with an empty queue",
                actual_state="Restart and clear commands accepted; verification pending",
                warnings=[
                    "All selected queued jobs are deleted and must be printed again.",
                    "A failed restart leaves printing unavailable; start the spooler manually if needed.",
                ],
                details={"printer": printer_name, "previous_jobs": count},
            )
        finally:
            if not started:
                runner.powershell("Start-Service -Name Spooler -ErrorAction SilentlyContinue", timeout=60)

    def verify() -> VerifyOutcome:
        runner = CommandRunner()
        running = _spooler_running(runner)
        remaining = _job_count(runner, printer_name)
        return VerifyOutcome(
            ok=running and remaining == 0,
            actual_state=f"Spooler {'running' if running else 'stopped'}, {remaining:,} job(s) remain",
            message="Read back the spooler state and queue count.",
        )

    def before() -> str:
        return f"{count:,} queued job(s)"

    return OperationSpec(
        operation_id="printer.restart_spooler_clear",
        name=f"Restart spooler and clear {label}",
        category="printers",
        description="Stops the spooler, removes queued jobs, and starts the spooler again.",
        risk=RiskLevel.HIGH,
        requires_admin=True,
        reversible=False,
        apply=apply,
        verify=verify,
        before=before,
        preview_text=f"Restart the Print Spooler and delete {count:,} queued job(s)",
        warnings=[
            "All selected queued jobs are deleted and must be printed again.",
            "Printing is unavailable until the spooler restarts.",
            "This does not delete printer drivers or ports.",
        ],
        verification_method="Reads back the spooler state and queue count.",
        details={"printer": printer_name},
    )
