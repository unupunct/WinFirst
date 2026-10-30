"""Plug and Play device health and driver problem detection."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from winfirst.core.command_runner import CommandRunner, as_list
from winfirst.services.common import ps_json, require_windows

PROBLEM_CODES = {
    1: "Device is not configured correctly",
    3: "Driver is corrupted or memory is insufficient",
    10: "Device cannot start",
    12: "Device cannot find enough free resources",
    14: "Device cannot work properly until restarted",
    16: "Device resources could not be identified",
    18: "Drivers must be reinstalled",
    19: "Registry configuration is invalid",
    21: "Windows is removing the device",
    22: "Device is disabled",
    24: "Device is not present or is not working properly",
    28: "Drivers are not installed",
    29: "Device firmware did not provide required resources",
    31: "Device is not working properly",
    32: "Driver for this device is disabled",
    37: "Driver could not be initialised",
    39: "Driver is corrupted or missing",
    40: "Device information is unavailable",
    41: "Device failed to load a driver",
    43: "Device reported a failure",
    44: "Device was stopped by software",
    45: "Device is not connected",
    46: "Device is not available while Windows is shutting down",
    47: "Device is prepared for safe removal",
    48: "Driver blocked because it is known to have problems",
    49: "Device cannot start because of a firmware issue",
    52: "Driver signature could not be verified",
}


@dataclass
class ProblemDevice:
    name: str
    instance_id: str = ""
    pnp_class: str = ""
    status: str = ""
    problem_code: int = 0
    manufacturer: str = ""

    @property
    def problem_text(self) -> str:
        return PROBLEM_CODES.get(self.problem_code, f"Device problem code {self.problem_code}")

    @property
    def is_missing_driver(self) -> bool:
        return self.problem_code == 28

    @property
    def severity(self) -> str:
        if self.is_missing_driver:
            return "warning"
        if self.problem_code in {10, 28, 43, 31, 39, 52}:
            return "critical"
        return "warning"


@dataclass
class DeviceHealth:
    present_devices: int = 0
    problems: list[ProblemDevice] = field(default_factory=list)
    error: str = ""
    detail: str = ""

    @property
    def available(self) -> bool:
        return not self.error

    def findings(self) -> list[dict[str, str]]:
        if self.error:
            return [
                {
                    "key": "device_inventory",
                    "title": "Device inventory unavailable",
                    "status": "info",
                    "detail": self.error,
                }
            ]
        findings = []
        for problem in self.problems:
            findings.append(
                {
                    "key": f"device_{problem.instance_id or problem.name}",
                    "title": problem.name,
                    "status": problem.severity,
                    "detail": f"{problem.problem_text} (code {problem.problem_code}).",
                }
            )
        return findings


_SCRIPT = r"""
$result = [ordered]@{ Present = 0; Items = @(); Error = '' }
try {
    $present = @(Get-PnpDevice -PresentOnly -ErrorAction Stop)
    $result.Present = $present.Count
    $items = @($present | Where-Object { $_.Status -eq 'Error' -or $_.Status -eq 'Unknown' -or $_.Status -eq 'Degraded' } |
        ForEach-Object {
            $code = 0
            try { $code = [int]$_.Problem } catch { }
            [pscustomobject]@{
                Name = [string]$_.FriendlyName
                InstanceId = [string]$_.InstanceId
                Class = [string]$_.Class
                Status = [string]$_.Status
                Problem = $code
                Manufacturer = ''
            }
        })
    # Add devices with a non-zero configuration manager error code even if
    # Get-PnpDevice reports another status.
    $cim = @(Get-CimInstance -ClassName Win32_PnPEntity -ErrorAction SilentlyContinue |
        Where-Object { $_.ConfigManagerErrorCode -ne 0 } | ForEach-Object {
            [pscustomobject]@{
                Name = [string]$_.Name
                InstanceId = [string]$_.PNPDeviceID
                Class = [string]$_.PNPClass
                Status = 'Error'
                Problem = [int]$_.ConfigManagerErrorCode
                Manufacturer = [string]$_.Manufacturer
            }
        })
    $result.Items = @($items + $cim)
} catch { $result.Error = $_.Exception.Message }
$result | ConvertTo-Json -Depth 5 -Compress
"""


def get_device_health(runner: CommandRunner | None = None) -> DeviceHealth:
    require_windows("Device health inspection")
    runner = runner or CommandRunner()
    try:
        data = ps_json(runner, _SCRIPT, timeout=90, default={}) or {}
    except Exception as exc:
        return DeviceHealth(error=str(exc))
    if data.get("Error"):
        return DeviceHealth(error=str(data.get("Error")))
    seen: set[str] = set()
    problems: list[ProblemDevice] = []
    for row in as_list(data.get("Items")):
        instance = str(row.get("InstanceId") or "")
        name = str(row.get("Name") or "Unknown device")
        key = instance or name
        if key in seen:
            continue
        seen.add(key)
        try:
            code = int(row.get("Problem") or 0)
        except (TypeError, ValueError):
            code = 0
        problems.append(
            ProblemDevice(
                name=name,
                instance_id=instance,
                pnp_class=str(row.get("Class") or ""),
                status=str(row.get("Status") or ""),
                problem_code=code,
                manufacturer=str(row.get("Manufacturer") or ""),
            )
        )
    problems.sort(key=lambda item: (not item.is_missing_driver, item.name.casefold()))
    return DeviceHealth(
        present_devices=int(data.get("Present") or 0),
        problems=problems,
        detail=f"{len(problems)} device(s) report a problem" if problems else "No device problems reported",
    )
