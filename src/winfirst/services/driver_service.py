"""Driver inventory, export and import using documented Windows tools.

Exported third-party driver packages are a convenience for reinstalling a
device, not a complete system backup.  Import is always previewed and confirmed.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from winfirst.core.command_runner import CommandRunner, as_list, ps_quote
from winfirst.core.operation import ApplyOutcome, OperationSpec, VerifyOutcome
from winfirst.core.risk_engine import RiskLevel
from winfirst.services.common import ServiceError, ps_json, require_windows, run_ok

_OEM_INF = re.compile(r"^oem\d+\.inf$", re.IGNORECASE)


@dataclass
class DriverInfo:
    device_name: str
    provider: str = ""
    version: str = ""
    date: str = ""
    inf_name: str = ""
    device_id: str = ""
    manufacturer: str = ""
    device_class: str = ""
    signed: str = ""

    @property
    def identity(self) -> str:
        return f"{self.device_name}|{self.inf_name}|{self.version}"


@dataclass
class ExportableDriver:
    published_name: str
    provider: str = ""
    class_name: str = ""
    version: str = ""
    date: str = ""

    @property
    def label(self) -> str:
        return f"{self.published_name} - {self.provider} {self.version}".strip()


@dataclass
class ExportResult:
    destination: str
    exported: list[dict[str, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    cancelled: bool = False


_DRIVER_SCRIPT = r"""
try {
    $items = @(Get-CimInstance -ClassName Win32_PnPSignedDriver -ErrorAction Stop |
        Where-Object { $_.DriverVersion -or $_.DeviceName } | ForEach-Object {
        [pscustomobject]@{
            Device = [string]$_.DeviceName
            Provider = [string]$_.DriverProviderName
            Version = [string]$_.DriverVersion
            Date = if ($_.DriverDate) { $_.DriverDate.ToString('yyyy-MM-dd') } else { '' }
            Inf = [string]$_.InfName
            DeviceId = [string]$_.DeviceID
            Manufacturer = [string]$_.Manufacturer
            Class = [string]$_.DeviceClass
            Signed = if ($_.IsSigned) { 'Yes' } else { 'No or unknown' }
        }
    })
    $items | ConvertTo-Json -Depth 4 -Compress
} catch { '' }
"""

_PACKAGE_SCRIPT = r"""
try {
    $items = @(Get-WindowsDriver -Online -ErrorAction Stop | ForEach-Object {
        [pscustomobject]@{
            Driver = [string]$_.Driver; Provider = [string]$_.ProviderName;
            Class = [string]$_.ClassName; Version = [string]$_.Version;
            Date = if ($_.Date) { $_.Date.ToString('yyyy-MM-dd') } else { '' };
            BootCritical = [bool]$_.BootCritical
        }
    })
    $items | ConvertTo-Json -Depth 4 -Compress
} catch { '' }
"""


def list_installed_drivers(runner: CommandRunner | None = None) -> tuple[list[DriverInfo], str]:
    runner = runner or CommandRunner()
    try:
        data = ps_json(runner, _DRIVER_SCRIPT, timeout=90, default=[])
    except Exception as exc:
        return [], str(exc)
    drivers: list[DriverInfo] = []
    for row in as_list(data):
        name = str(row.get("Device") or "").strip()
        if not name:
            continue
        drivers.append(
            DriverInfo(
                device_name=name,
                provider=str(row.get("Provider") or ""),
                version=str(row.get("Version") or ""),
                date=str(row.get("Date") or ""),
                inf_name=str(row.get("Inf") or ""),
                device_id=str(row.get("DeviceId") or ""),
                manufacturer=str(row.get("Manufacturer") or ""),
                device_class=str(row.get("Class") or ""),
                signed=str(row.get("Signed") or ""),
            )
        )
    drivers.sort(key=lambda item: (item.device_class.casefold(), item.device_name.casefold()))
    return drivers, ""


def list_exportable_drivers(runner: CommandRunner | None = None) -> tuple[list[ExportableDriver], str]:
    runner = runner or CommandRunner()
    try:
        data = ps_json(runner, _PACKAGE_SCRIPT, timeout=90, default=[])
    except Exception as exc:
        return [], str(exc)
    drivers = []
    for row in as_list(data):
        name = str(row.get("Driver") or "")
        if not _OEM_INF.match(name):
            continue
        drivers.append(
            ExportableDriver(
                published_name=name,
                provider=str(row.get("Provider") or ""),
                class_name=str(row.get("Class") or ""),
                version=str(row.get("Version") or ""),
                date=str(row.get("Date") or ""),
            )
        )
    return drivers, ""


def check_inf_signature(runner: CommandRunner, inf_path: str) -> str:
    script = (
        f"$s = Get-AuthenticodeSignature -FilePath {ps_quote(inf_path)} -ErrorAction SilentlyContinue; "
        "if ($s) { [string]$s.Status } else { 'Unknown' }"
    )
    try:
        result = runner.powershell(script, timeout=45)
        return result.stdout.strip().splitlines()[-1] if result.stdout.strip() else "Unknown"
    except Exception:
        return "Unknown"


def export_drivers(
    destination: str | Path,
    drivers: list[ExportableDriver],
    *,
    runner: CommandRunner | None = None,
    progress: Callable[[str], None] | None = None,
    cancel_event=None,
) -> ExportResult:
    require_windows("Driver export")
    runner = runner or CommandRunner()
    report = progress or (lambda _text: None)
    target = Path(destination)
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ServiceError(f"Could not create the export directory: {exc}") from exc
    result = ExportResult(destination=str(target))
    for index, driver in enumerate(drivers, start=1):
        if cancel_event is not None and cancel_event.is_set():
            result.cancelled = True
            break
        if not _OEM_INF.match(driver.published_name):
            result.errors.append(f"Skipped an invalid driver name: {driver.published_name}")
            continue
        folder = target / driver.published_name.replace(".inf", "")
        folder.mkdir(parents=True, exist_ok=True)
        report(f"[{index}/{len(drivers)}] Exporting {driver.published_name}")
        try:
            completed = run_ok(
                runner,
                ["pnputil.exe", "/export-driver", driver.published_name, str(folder)],
                timeout=180,
                error_prefix=f"Could not export {driver.published_name}",
            )
            result.exported.append(
                {
                    "driver": driver.published_name,
                    "provider": driver.provider,
                    "version": driver.version,
                    "path": str(folder),
                    "output": completed.stdout.strip()[:500],
                }
            )
        except ServiceError as exc:
            result.errors.append(str(exc))
    return result


def build_export_spec(
    destination: str,
    drivers: list[ExportableDriver],
) -> tuple[OperationSpec, Callable[[], ExportResult]]:
    holder: dict[str, ExportResult] = {}

    def apply(progress, cancel) -> ApplyOutcome:
        export = export_drivers(destination, drivers, progress=progress, cancel_event=cancel)
        holder["result"] = export
        return ApplyOutcome(
            success=not export.errors and not export.cancelled,
            previous_state=f"Destination: {destination}",
            requested_state=f"Export {len(drivers)} driver package(s)",
            actual_state=f"{len(export.exported)} exported, {len(export.errors)} error(s)",
            warnings=["Exported third-party driver packages are not a complete system backup."],
            details={
                "exported": export.exported,
                "errors": export.errors,
                "destination": destination,
            },
            rollback=None,
            error="; ".join(export.errors[:4]) if export.errors else "",
        )

    spec = OperationSpec(
        operation_id="driver.export",
        name="Export third-party drivers",
        category="drivers",
        description="Exports published third-party driver packages with pnputil.",
        risk=RiskLevel.LOW,
        requires_admin=True,
        reversible=False,
        apply=apply,
        before=lambda: f"{len(drivers)} package(s) selected",
        preview_text=f"Export {len(drivers)} driver package(s) to {destination}",
        warnings=["Driver export writes files to the selected folder. It does not change installed drivers."],
        verification_method="Checks pnputil results and exported folder paths",
        details={"destination": destination, "count": len(drivers)},
    )
    return spec, lambda: holder.get("result", ExportResult(destination=destination))


def find_inf_files(folder: str | Path) -> list[Path]:
    base = Path(folder)
    if not base.is_dir():
        raise ServiceError(f"The selected driver folder does not exist: {base}")
    infs = []
    for path in sorted(base.rglob("*.inf")):
        if len(infs) >= 500:
            break
        infs.append(path)
    return infs


def build_import_spec(folder: str, inf_files: list[Path]) -> OperationSpec:
    def apply(progress, cancel) -> ApplyOutcome:
        runner = CommandRunner()
        completed: list[dict[str, Any]] = []
        errors: list[str] = []
        for index, inf in enumerate(inf_files, start=1):
            if cancel_event_is_set(cancel):
                break
            progress(f"[{index}/{len(inf_files)}] Installing {inf.name}")
            result = runner.run(
                ["pnputil.exe", "/add-driver", str(inf), "/install"],
                timeout=300,
                cancel_event=cancel,
            )
            if result.timed_out:
                errors.append(f"{inf.name}: timed out")
                continue
            if result.exit_code != 0:
                errors.append(f"{inf.name}: {(result.stderr or result.stdout).strip()[:400]}")
            else:
                completed.append({"inf": str(inf), "output": result.stdout.strip()[:400]})
        return ApplyOutcome(
            success=not errors,
            previous_state="Drivers before import",
            requested_state=f"Install {len(inf_files)} INF package(s)",
            actual_state=f"{len(completed)} installed, {len(errors)} failed",
            warnings=[
                "Installing a driver that does not match the hardware can make a device unusable.",
                "A restart may be required before the driver is active.",
            ],
            restart_required=bool(completed),
            details={"installed": completed, "errors": errors},
            error="; ".join(errors[:4]) if errors else "",
        )

    return OperationSpec(
        operation_id="driver.import",
        name="Install drivers from a local folder",
        category="drivers",
        description="Adds INF driver packages with pnputil /add-driver /install.",
        risk=RiskLevel.HIGH,
        requires_admin=True,
        reversible=False,
        restart_required=True,
        apply=apply,
        before=lambda: f"Selected folder: {folder}",
        preview_text=f"Install {len(inf_files)} INF file(s) from {folder}",
        warnings=[
            "Only use drivers from the device manufacturer or Windows Update.",
            "WinFirst cannot automatically roll back a driver installation.",
        ],
        verification_method="pnputil exit status for each INF; no independent device functional test",
        details={"folder": folder, "inf_count": len(inf_files)},
    )


def cancel_event_is_set(event) -> bool:
    return bool(event is not None and event.is_set())
