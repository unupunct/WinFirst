"""Storage capacity, volume health, physical disk health and large-folder scans."""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import psutil

from winfirst.core.command_runner import CommandRunner, as_list
from winfirst.services.common import ServiceError, ps_json, require_windows

LOW_SPACE_PERCENT = 10.0
LOW_SPACE_BYTES = 10 * 1024**3


def format_bytes(value: float | int | None) -> str:
    if value is None:
        return "Unknown"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(size) < 1024 or unit == "PB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} PB"


@dataclass
class VolumeInfo:
    mountpoint: str
    device: str = ""
    label: str = ""
    filesystem: str = ""
    total: int = 0
    used: int = 0
    free: int = 0
    percent: float = 0.0
    health: str = ""
    low_space: bool = False
    error: str = ""


@dataclass
class PhysicalDiskInfo:
    friendly_name: str
    media_type: str = ""
    health_status: str = ""
    operational_status: str = ""
    size_gb: float = 0.0
    bus_type: str = ""
    wear: int | None = None
    temperature_c: int | None = None
    read_errors: int | None = None
    write_errors: int | None = None
    health_detail: str = ""


@dataclass
class SmartStatus:
    available: bool
    failure_predicted: bool = False
    detail: str = ""
    reason: str = ""


@dataclass
class StorageSnapshot:
    volumes: list[VolumeInfo] = field(default_factory=list)
    disks: list[PhysicalDiskInfo] = field(default_factory=list)
    smart: SmartStatus = field(default_factory=lambda: SmartStatus(False))
    errors: list[str] = field(default_factory=list)
    low_space: bool = False

    def findings(self) -> list[dict[str, str]]:
        findings: list[dict[str, str]] = []
        for volume in self.volumes:
            if volume.error:
                findings.append(
                    {
                        "key": f"volume_{volume.mountpoint}",
                        "title": f"Volume {volume.mountpoint}",
                        "status": "info",
                        "detail": volume.error,
                    }
                )
                continue
            if volume.low_space:
                findings.append(
                    {
                        "key": f"volume_{volume.mountpoint}",
                        "title": f"Low space on {volume.mountpoint}",
                        "status": "warning",
                        "detail": f"{format_bytes(volume.free)} free of {format_bytes(volume.total)} ({volume.percent:.0f}% used).",
                    }
                )
            if volume.health and volume.health.lower() not in {"healthy", ""}:
                findings.append(
                    {
                        "key": f"volume_health_{volume.mountpoint}",
                        "title": f"Volume health: {volume.mountpoint}",
                        "status": "critical",
                        "detail": f"Windows reports {volume.health}.",
                    }
                )
        for disk in self.disks:
            if disk.health_status and disk.health_status.lower() not in {"healthy", ""}:
                findings.append(
                    {
                        "key": f"disk_{disk.friendly_name}",
                        "title": disk.friendly_name,
                        "status": "critical",
                        "detail": f"Physical disk health: {disk.health_status}. {disk.health_detail}".strip(),
                    }
                )
        if self.smart.available and self.smart.failure_predicted:
            findings.append(
                {
                    "key": "smart",
                    "title": "SMART failure predicted",
                    "status": "critical",
                    "detail": self.smart.detail or "A drive reports an imminent failure prediction.",
                }
            )
        if not self.smart.available:
            findings.append(
                {
                    "key": "smart",
                    "title": "SMART status unavailable",
                    "status": "info",
                    "detail": "SMART data is not exposed for this storage configuration. "
                    "This is not evidence that the disk is healthy.",
                }
            )
        return findings


@dataclass
class FolderUsage:
    path: str
    size_bytes: int = 0
    file_count: int = 0
    depth: int = 0
    accessible: bool = True
    skipped: int = 0


_VOLUME_SCRIPT = r"""
try {
    $items = @(Get-Volume -ErrorAction Stop | Where-Object { $_.DriveLetter } | ForEach-Object {
        [pscustomobject]@{
            Letter = [string]$_.DriveLetter; Label = [string]$_.FileSystemLabel;
            FileSystem = [string]$_.FileSystem; Health = [string]$_.HealthStatus;
            Size = $_.Size; Remaining = $_.SizeRemaining
        }
    })
    $items | ConvertTo-Json -Depth 3 -Compress
} catch { '' }
"""

_DISK_SCRIPT = r"""
try {
    $items = @(Get-PhysicalDisk -ErrorAction Stop | ForEach-Object {
        $rel = $null
        try { $rel = $_ | Get-StorageReliabilityCounter -ErrorAction SilentlyContinue } catch { }
        [pscustomobject]@{
            Name = [string]$_.FriendlyName; Media = [string]$_.MediaType;
            Health = [string]$_.HealthStatus; Operational = (@($_.OperationalStatus) -join ', ');
            Size = $_.Size; Bus = [string]$_.BusType;
            Wear = if ($rel) { $rel.Wear } else { $null };
            Temperature = if ($rel) { $rel.Temperature } else { $null };
            ReadErrors = if ($rel) { $rel.ReadErrorsTotal } else { $null };
            WriteErrors = if ($rel) { $rel.WriteErrorsTotal } else { $null }
        }
    })
    $items | ConvertTo-Json -Depth 4 -Compress
} catch { '' }
"""

_SMART_SCRIPT = r"""
try {
    $items = @(Get-CimInstance -Namespace root\wmi -ClassName MSStorageDriver_FailurePredictStatus -ErrorAction Stop |
        ForEach-Object { [pscustomobject]@{ Name = [string]$_.InstanceName; PredictFailure = [bool]$_.PredictFailure; Reason = $_.Reason } })
    $items | ConvertTo-Json -Depth 3 -Compress
} catch { '' }
"""


def get_volumes(runner: CommandRunner | None = None) -> tuple[list[VolumeInfo], list[str]]:
    runner = runner or CommandRunner()
    errors: list[str] = []
    volumes: list[VolumeInfo] = []
    ps_rows: dict[str, dict[str, Any]] = {}
    try:
        data = ps_json(runner, _VOLUME_SCRIPT, timeout=45, default=[])
        for row in as_list(data):
            letter = str(row.get("Letter") or "").upper()
            if letter:
                ps_rows[f"{letter}:"] = row
    except Exception as exc:
        errors.append(f"Get-Volume failed: {exc}")
    seen: set[str] = set()
    for partition in psutil.disk_partitions(all=False):
        mount = partition.mountpoint
        if not mount:
            continue
        seen.add(mount.upper())
        try:
            usage = psutil.disk_usage(mount)
        except (PermissionError, OSError) as exc:
            volumes.append(
                VolumeInfo(mountpoint=mount, device=partition.device, filesystem=partition.fstype, error=str(exc))
            )
            continue
        row = ps_rows.get(mount.upper(), {})
        percent = round(usage.percent, 1)
        low = usage.free < LOW_SPACE_BYTES or percent >= (100 - LOW_SPACE_PERCENT)
        volumes.append(
            VolumeInfo(
                mountpoint=mount,
                device=partition.device,
                label=str(row.get("Label") or ""),
                filesystem=str(row.get("FileSystem") or partition.fstype or ""),
                total=int(usage.total),
                used=int(usage.used),
                free=int(usage.free),
                percent=percent,
                health=str(row.get("Health") or ""),
                low_space=low,
            )
        )
    for mount, row in ps_rows.items():
        if mount.upper() in seen:
            continue
        total = row.get("Size") or 0
        remaining = row.get("Remaining") or 0
        try:
            used = int(total) - int(remaining)
        except (TypeError, ValueError):
            used = 0
        volumes.append(
            VolumeInfo(
                mountpoint=mount,
                label=str(row.get("Label") or ""),
                filesystem=str(row.get("FileSystem") or ""),
                total=int(total or 0),
                used=used,
                free=int(remaining or 0),
                percent=round((used / total) * 100, 1) if total else 0.0,
                health=str(row.get("Health") or ""),
                low_space=bool(total and (int(remaining) < LOW_SPACE_BYTES or (used / total) * 100 >= (100 - LOW_SPACE_PERCENT))),
            )
        )
    return volumes, errors


def get_physical_disks(runner: CommandRunner | None = None) -> tuple[list[PhysicalDiskInfo], list[str]]:
    runner = runner or CommandRunner()
    errors: list[str] = []
    disks: list[PhysicalDiskInfo] = []
    try:
        data = ps_json(runner, _DISK_SCRIPT, timeout=60, default=[])
        for row in as_list(data):
            size = row.get("Size") or 0
            try:
                size_gb = round(float(size) / (1024**3), 1)
            except (TypeError, ValueError):
                size_gb = 0.0
            disks.append(
                PhysicalDiskInfo(
                    friendly_name=str(row.get("Name") or "Disk"),
                    media_type=str(row.get("Media") or ""),
                    health_status=str(row.get("Health") or ""),
                    operational_status=str(row.get("Operational") or ""),
                    size_gb=size_gb,
                    bus_type=str(row.get("Bus") or ""),
                    wear=row.get("Wear"),
                    temperature_c=row.get("Temperature"),
                    read_errors=row.get("ReadErrors"),
                    write_errors=row.get("WriteErrors"),
                )
            )
    except Exception as exc:
        errors.append(f"Physical disk query failed: {exc}")
    return disks, errors


def get_smart_status(runner: CommandRunner | None = None) -> SmartStatus:
    runner = runner or CommandRunner()
    try:
        data = ps_json(runner, _SMART_SCRIPT, timeout=30, default=[])
        rows = as_list(data)
    except Exception as exc:
        return SmartStatus(False, reason=str(exc))
    if not rows:
        return SmartStatus(
            False,
            reason="SMART failure prediction is not exposed for these drives (common for USB, RAID and some NVMe controllers).",
        )
    failed = [row for row in rows if bool(row.get("PredictFailure"))]
    return SmartStatus(
        available=True,
        failure_predicted=bool(failed),
        detail="; ".join(str(row.get("Name") or "Drive") for row in failed) if failed else "",
        reason="" if not failed else "At least one drive predicts failure.",
    )


def get_storage_snapshot(runner: CommandRunner | None = None) -> StorageSnapshot:
    require_windows("Storage health inspection")
    runner = runner or CommandRunner()
    volumes, volume_errors = get_volumes(runner)
    disks, disk_errors = get_physical_disks(runner)
    smart = get_smart_status(runner)
    return StorageSnapshot(
        volumes=volumes,
        disks=disks,
        smart=smart,
        errors=volume_errors + disk_errors,
        low_space=any(volume.low_space for volume in volumes),
    )


_DEFAULT_SKIP = {
    "System Volume Information",
    "$Recycle.Bin",
    "Windows",
    "WinSxS",
    "node_modules",
    ".git",
    "AppData\\Local\\Packages",
}


def scan_large_folders(
    root: str | Path,
    *,
    max_depth: int = 3,
    min_size_mb: float = 50,
    entry_budget: int = 200_000,
    cancel_event: threading.Event | None = None,
) -> tuple[list[FolderUsage], list[str]]:
    """Inspect directory sizes without following links or entering system trees.

    The result is based on actual files examined.  Directories that cannot be
    read are reported rather than silently treated as empty.
    """
    base = Path(root)
    if not base.is_dir():
        raise ServiceError(f"The selected folder does not exist: {base}")
    errors: list[str] = []
    usages: dict[Path, FolderUsage] = {}
    entries_seen = 0
    truncated = False
    cancelled = False
    min_bytes = int(min_size_mb * 1024 * 1024)
    stack: list[tuple[Path, int]] = [(base, 0)]
    while stack:
        if cancel_event is not None and cancel_event.is_set():
            cancelled = True
            break
        directory, depth = stack.pop()
        usage = FolderUsage(path=str(directory), depth=depth)
        usages[directory] = usage
        try:
            with os.scandir(directory) as iterator:
                for entry in iterator:
                    entries_seen += 1
                    if entries_seen > entry_budget:
                        truncated = True
                        break
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_file(follow_symlinks=False):
                            try:
                                stat = entry.stat(follow_symlinks=False)
                            except OSError:
                                usage.skipped += 1
                                continue
                            usage.size_bytes += stat.st_size
                            usage.file_count += 1
                        elif entry.is_dir(follow_symlinks=False):
                            if entry.name in _DEFAULT_SKIP:
                                continue
                            if depth < max_depth:
                                stack.append((Path(entry.path), depth + 1))
                    except OSError:
                        usage.skipped += 1
        except PermissionError:
            usage.accessible = False
            errors.append(f"Access denied: {directory}")
        except OSError as exc:
            usage.accessible = False
            errors.append(f"Could not scan {directory}: {exc}")
        if truncated:
            errors.append("The scan reached its entry budget and was truncated.")
            break
    if cancelled:
        errors.append("The folder scan was cancelled; totals are incomplete.")
    # Aggregate every scanned child into every ancestor, not only large ones.
    for path in sorted(usages, key=lambda item: len(item.parts), reverse=True):
        usage = usages[path]
        parent = path.parent
        if parent in usages and parent != path:
            usages[parent].size_bytes += usage.size_bytes
            usages[parent].file_count += usage.file_count
            usages[parent].skipped += usage.skipped
    results = [item for item in usages.values() if item.depth > 0 and item.size_bytes >= min_bytes]
    results.sort(key=lambda row: row.size_bytes, reverse=True)
    return results, errors
