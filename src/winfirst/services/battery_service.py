"""Battery health from the Windows battery report and WMI.

Wear is calculated only when both design and full-charge capacities are valid,
and is always labelled as an estimate.
"""
from __future__ import annotations

import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from winfirst.core.command_runner import CommandRunner, as_list
from winfirst.services.common import ServiceError, ps_json, require_windows, run_ok


@dataclass
class BatteryUnit:
    name: str = ""
    manufacturer: str = ""
    chemistry: str = ""
    design_capacity_mwh: int | None = None
    full_charge_capacity_mwh: int | None = None
    cycle_count: int | None = None
    wear_percent: float | None = None


@dataclass
class BatteryInfo:
    available: bool
    reason: str = ""
    units: list[BatteryUnit] = field(default_factory=list)
    current_charge_percent: int | None = None
    status: str = ""
    power_online: bool | None = None
    wear_percent: float | None = None
    estimated: bool = True
    note: str = ""

    def summary_rows(self) -> list[tuple[str, str]]:
        if not self.available:
            return [("Battery", self.reason or "Not available")]
        rows = [
            ("Charge level", f"{self.current_charge_percent}%" if self.current_charge_percent is not None else "Unknown"),
            ("Power", self.status or "Unknown"),
            (
                "Design capacity",
                f"{self.units[0].design_capacity_mwh:,} mWh" if self.units and self.units[0].design_capacity_mwh else "Unavailable",
            ),
            (
                "Full-charge capacity",
                f"{self.units[0].full_charge_capacity_mwh:,} mWh" if self.units and self.units[0].full_charge_capacity_mwh else "Unavailable",
            ),
            ("Estimated wear", f"{self.wear_percent:.0f}%" if self.wear_percent is not None else "Unavailable"),
        ]
        if self.note:
            rows.append(("Note", self.note))
        return rows


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find_text(node: ET.Element, name: str) -> str:
    for child in node.iter():
        if _local(child.tag) == name and child.text:
            return child.text.strip()
    return ""


def _int_or_none(value: str) -> int | None:
    try:
        number = int(value)
        return number if number > 0 else None
    except (TypeError, ValueError):
        return None


def _read_report(path: Path) -> list[BatteryUnit]:
    tree = ET.parse(path)
    root = tree.getroot()
    units: list[BatteryUnit] = []
    for node in root.iter():
        if _local(node.tag) != "Battery":
            continue
        name = _find_text(node, "Id") or _find_text(node, "Name")
        manufacturer = _find_text(node, "Manufacturer")
        chemistry = _find_text(node, "Chemistry")
        design = _int_or_none(_find_text(node, "DesignCapacity"))
        full = _int_or_none(_find_text(node, "FullChargeCapacity"))
        cycles = _int_or_none(_find_text(node, "CycleCount"))
        wear = None
        if design and full:
            wear = max(0.0, round((1 - (full / design)) * 100, 1))
        units.append(
            BatteryUnit(
                name=name,
                manufacturer=manufacturer,
                chemistry=chemistry,
                design_capacity_mwh=design,
                full_charge_capacity_mwh=full,
                cycle_count=cycles,
                wear_percent=wear,
            )
        )
    return units


def _wmi_battery(runner: CommandRunner) -> dict[str, Any]:
    script = r"""
$b = @(Get-CimInstance -ClassName Win32_Battery -ErrorAction SilentlyContinue | Select-Object -First 1)
if ($b.Count -eq 0) { '{}' } else {
    [pscustomobject]@{
        Name = [string]$b[0].Name
        EstimatedChargeRemaining = $b[0].EstimatedChargeRemaining
        BatteryStatus = $b[0].BatteryStatus
        Chemistry = [string]$b[0].Chemistry
    } | ConvertTo-Json -Compress
}
"""
    try:
        data = ps_json(runner, script, timeout=30, default={})
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


_STATUS = {
    1: "Discharging",
    2: "Connected to AC power",
    3: "Fully charged",
    4: "Low",
    5: "Critical",
    6: "Charging",
    7: "Charging and high",
    8: "Charging and low",
    9: "Charging and critical",
    10: "Undefined",
    11: "Partially charged",
}


def get_battery_info(runner: CommandRunner | None = None) -> BatteryInfo:
    if __import__("sys").platform != "win32":
        return BatteryInfo(False, "Battery information requires Windows.")
    runner = runner or CommandRunner()
    wmi = _wmi_battery(runner)
    if not wmi and not _device_present():
        return BatteryInfo(False, "No battery was detected on this computer.")
    units: list[BatteryUnit] = []
    reason = ""
    with tempfile.TemporaryDirectory(prefix="winfirst-battery-") as temp:
        report = Path(temp) / "battery-report.xml"
        try:
            result = runner.run(
                ["powercfg.exe", "/batteryreport", "/xml", "/output", str(report)],
                timeout=90,
            )
            if result.ok and report.is_file():
                units = _read_report(report)
            else:
                reason = (result.stderr or result.stdout).strip()[:500] or "The battery report could not be generated."
        except Exception as exc:
            reason = f"Battery report failed: {exc}"
    if not units and not wmi:
        return BatteryInfo(False, reason or "No battery data is available.")
    status_code = None
    try:
        status_code = int(wmi.get("BatteryStatus")) if wmi.get("BatteryStatus") is not None else None
    except (TypeError, ValueError):
        status_code = None
    charge = wmi.get("EstimatedChargeRemaining")
    try:
        charge_percent = int(charge) if charge is not None else None
    except (TypeError, ValueError):
        charge_percent = None
    wear = units[0].wear_percent if units else None
    note = ""
    if units and units[0].design_capacity_mwh and units[0].full_charge_capacity_mwh:
        if units[0].full_charge_capacity_mwh > units[0].design_capacity_mwh:
            note = "Reported full-charge capacity exceeds design capacity; wear is reported as 0%."
        note = (note + " " if note else "") + "Wear is an estimate based on the Windows battery report."
    elif not units:
        note = "Capacity data is unavailable; wear cannot be calculated."
    return BatteryInfo(
        available=True,
        reason=reason,
        units=units,
        current_charge_percent=charge_percent,
        status=_STATUS.get(status_code or 0, "Unknown"),
        power_online=True if status_code in {2, 3, 6, 7, 8, 9, 11} else (False if status_code == 1 else None),
        wear_percent=wear,
        note=note,
    )


def _device_present() -> bool:
    try:
        import psutil

        return psutil.sensors_battery() is not None
    except Exception:
        return False
