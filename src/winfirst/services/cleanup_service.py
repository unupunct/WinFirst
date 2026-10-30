"""Bounded, explicit storage cleanup.

Only a fixed set of well-known cache locations is offered.  WinFirst never
deletes user documents, Downloads, browser profiles or arbitrary paths.  Every
target must be measured, previewed and confirmed; deletion is permanent and is
never presented as reversible.
"""
from __future__ import annotations

import glob
import os
import stat as stat_module
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from winfirst.core.command_runner import CommandRunner
from winfirst.core.operation import ApplyOutcome, OperationSpec, VerifyOutcome
from winfirst.core.plan import action_step
from winfirst.core.risk_engine import RiskLevel
from winfirst.services.common import ServiceError, ps_text, require_windows

ProgressFn = Callable[[str], None]

_ENTRY_BUDGET = 300_000
_LARGE_FILE_BYTES = 100 * 1024 * 1024


@dataclass
class CleanupTarget:
    key: str
    label: str
    description: str
    kind: str
    paths: list[str] = field(default_factory=list)
    requires_admin: bool = False
    risk: RiskLevel = RiskLevel.HIGH
    min_age_hours: int = 24
    estimated_bytes: int | None = None
    estimated_files: int | None = None
    available: bool = True
    reason: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def size_label(self) -> str:
        if self.estimated_bytes is None:
            return "Not measured"
        from winfirst.services.storage_health_service import format_bytes

        return format_bytes(self.estimated_bytes)

    @property
    def files_label(self) -> str:
        if self.estimated_files is None:
            return "Unknown"
        return f"{self.estimated_files:,}"


@dataclass
class CleanupAnalysis:
    targets: list[CleanupTarget] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    scanned_at: float = field(default_factory=time.time)

    def find(self, key: str) -> CleanupTarget | None:
        for target in self.targets:
            if target.key == key:
                return target
        return None

    @property
    def known_bytes(self) -> int:
        return sum(target.estimated_bytes or 0 for target in self.targets if target.available)

    @property
    def known_files(self) -> int:
        return sum(target.estimated_files or 0 for target in self.targets if target.available)


@dataclass
class DeleteReport:
    deleted_files: int = 0
    deleted_bytes: int = 0
    skipped_recent: int = 0
    skipped_locked: int = 0
    errors: list[str] = field(default_factory=list)
    cancelled: bool = False


def _dedupe(paths: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in paths:
        if not item:
            continue
        normalized = os.path.normcase(os.path.normpath(item))
        if normalized not in seen:
            seen.add(normalized)
            result.append(item)
    return result


def _target_definitions() -> list[CleanupTarget]:
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    local_appdata = os.environ.get("LOCALAPPDATA", "")
    temp_paths = _dedupe(
        [
            os.environ.get("TEMP", ""),
            os.environ.get("TMP", ""),
            os.path.join(local_appdata, "Temp") if local_appdata else "",
        ]
    )
    thumbnails = (
        os.path.join(local_appdata, "Microsoft", "Windows", "Explorer", "thumbcache_*.db")
        if local_appdata
        else ""
    )
    crash_dumps = os.path.join(local_appdata, "CrashDumps") if local_appdata else ""
    return [
        CleanupTarget(
            key="user_temp",
            label="Current user temporary files",
            description="Files in the current user's temporary folders that are at least 24 hours old.",
            kind="temp",
            paths=temp_paths,
            risk=RiskLevel.HIGH,
            min_age_hours=24,
            warnings=[
                "Deleted files do not go to the Recycle Bin.",
                "Files in use by running applications are skipped.",
                "Some applications store recoverable work in TEMP; close documents before cleaning.",
            ],
        ),
        CleanupTarget(
            key="windows_temp",
            label="Windows temporary files",
            description="Files in %SystemRoot%\\Temp that are at least 24 hours old.",
            kind="temp",
            paths=[os.path.join(system_root, "Temp")],
            requires_admin=True,
            risk=RiskLevel.HIGH,
            min_age_hours=24,
            warnings=["Requires administrator rights.", "Deleted files do not go to the Recycle Bin."],
        ),
        CleanupTarget(
            key="thumbnails",
            label="Explorer thumbnail cache",
            description="Windows Explorer thumbnail cache files. Windows rebuilds them as needed.",
            kind="thumbnails",
            paths=[thumbnails],
            risk=RiskLevel.MEDIUM,
            min_age_hours=0,
            warnings=[
                "Files locked by Explorer are skipped; close File Explorer windows first.",
                "Thumbnails will be regenerated when folders are opened.",
            ],
        ),
        CleanupTarget(
            key="crash_dumps",
            label="Current user crash dumps",
            description="Application crash dumps in the current user's CrashDumps folder.",
            kind="dumps",
            paths=[crash_dumps],
            risk=RiskLevel.MEDIUM,
            min_age_hours=0,
            warnings=[
                "Crash dumps can be useful for diagnosing an application fault. "
                "Delete them only if you do not need to investigate a crash.",
            ],
        ),
        CleanupTarget(
            key="minidumps",
            label="Windows memory dumps",
            description="Kernel minidumps in %SystemRoot%\\Minidump.",
            kind="dumps",
            paths=[os.path.join(system_root, "Minidump")],
            requires_admin=True,
            risk=RiskLevel.HIGH,
            min_age_hours=0,
            warnings=[
                "Requires administrator rights.",
                "Memory dumps are diagnostic evidence for stop errors. Delete them only if you do not need them.",
            ],
        ),
        CleanupTarget(
            key="update_cache",
            label="Windows Update download cache",
            description="Cached update downloads in SoftwareDistribution\\Download. Windows re-downloads anything still needed.",
            kind="update_cache",
            paths=[os.path.join(system_root, "SoftwareDistribution", "Download")],
            requires_admin=True,
            risk=RiskLevel.HIGH,
            min_age_hours=0,
            warnings=[
                "Requires administrator rights.",
                "The Windows Update service is stopped temporarily and restarted afterwards.",
                "Do not run this while updates are installing. A pending update may download again.",
                "This does not remove installed updates or update history.",
            ],
        ),
        CleanupTarget(
            key="recycle_bin",
            label="Recycle Bin",
            description="All items in the current user's Recycle Bin. They are permanently deleted.",
            kind="recycle_bin",
            requires_admin=False,
            risk=RiskLevel.HIGH,
            warnings=[
                "Every item in this user's Recycle Bin is permanently removed.",
                "Files cannot be restored by WinFirst after this action.",
                "Other user's Recycle Bins are not affected.",
            ],
        ),
    ]


def _scan_stats(
    paths: list[str],
    *,
    cutoff: float | None,
    cancel_event: threading.Event | None = None,
    entry_budget: int = _ENTRY_BUDGET,
) -> tuple[int, int, list[str], bool]:
    """Return (bytes, files, errors, truncated) for existing files under paths.

    Symlinks and junctions are never followed.  ``cutoff`` is an epoch second;
    only files with mtime <= cutoff are counted (all files when None).
    """
    total_bytes = 0
    total_files = 0
    errors: list[str] = []
    entries = 0
    truncated = False
    stack: list[Path] = []
    for raw in paths:
        if not raw:
            continue
        expanded = os.path.expandvars(raw)
        if glob.has_magic(expanded):
            for match in glob.glob(expanded):
                candidate = Path(match)
                if candidate.is_file():
                    try:
                        stat = candidate.stat(follow_symlinks=False)
                    except OSError as exc:
                        errors.append(f"{candidate}: {exc}")
                        continue
                    if cutoff is None or stat.st_mtime <= cutoff:
                        total_bytes += stat.st_size
                        total_files += 1
            continue
        candidate = Path(expanded)
        if candidate.is_file():
            stack.append(candidate)
        elif candidate.is_dir():
            stack.append(candidate)
        else:
            continue
    visited: set[str] = set()
    while stack:
        if cancel_event is not None and cancel_event.is_set():
            errors.append("Measurement cancelled; sizes are incomplete.")
            break
        current = stack.pop()
        key = os.path.normcase(os.path.abspath(str(current)))
        if key in visited:
            continue
        visited.add(key)
        try:
            if current.is_file():
                stat = current.stat(follow_symlinks=False)
                if cutoff is None or stat.st_mtime <= cutoff:
                    total_bytes += stat.st_size
                    total_files += 1
                continue
            with os.scandir(current) as iterator:
                for entry in iterator:
                    entries += 1
                    if entries > entry_budget:
                        truncated = True
                        break
                    try:
                        if entry.is_symlink() or getattr(entry, "is_junction", lambda: False)():
                            continue
                        if entry.is_file(follow_symlinks=False):
                            stat = entry.stat(follow_symlinks=False)
                            if cutoff is None or stat.st_mtime <= cutoff:
                                total_bytes += stat.st_size
                                total_files += 1
                        elif entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                    except OSError as exc:
                        if len(errors) < 40:
                            errors.append(f"{entry.path}: {exc}")
        except PermissionError:
            errors.append(f"Access denied: {current}")
        except OSError as exc:
            errors.append(f"Could not read {current}: {exc}")
        if truncated:
            break
    if truncated:
        errors.append("The size scan reached its entry limit and may be incomplete.")
    return total_bytes, total_files, errors, truncated


def _recycle_count(runner: CommandRunner) -> int | None:
    script = (
        "$shell = New-Object -ComObject Shell.Application; "
        "$bin = $shell.NameSpace(10); "
        "if ($bin) { @($bin.Items()).Count } else { 0 }"
    )
    try:
        result = runner.powershell(script, timeout=45)
        if not result.launched or result.exit_code != 0:
            return None
        text = (result.stdout or "").strip()
        return int(text.splitlines()[-1]) if text else None
    except (ValueError, TypeError):
        return None


def analyze_cleanup(
    *,
    min_age_hours: int = 24,
    runner: CommandRunner | None = None,
    cancel_event: threading.Event | None = None,
) -> CleanupAnalysis:
    require_windows("Storage cleanup")
    runner = runner or CommandRunner()
    analysis = CleanupAnalysis()
    now = time.time()
    cutoff = now - max(0, min_age_hours) * 3600
    for definition in _target_definitions():
        target = CleanupTarget(**{**definition.__dict__, "paths": list(definition.paths)})
        if target.kind == "temp":
            target.min_age_hours = max(0, min_age_hours)
            cutoff_for_target = cutoff
        else:
            cutoff_for_target = None
        if target.kind == "recycle_bin":
            count = _recycle_count(runner)
            target.estimated_files = count
            target.estimated_bytes = None
            target.available = True
            if count is None:
                target.reason = "The Recycle Bin could not be measured; contents will still be listed for confirmation."
            elif count == 0:
                target.reason = "The Recycle Bin is empty."
            analysis.targets.append(target)
            continue
        existing = [path for path in target.paths if path and (os.path.exists(os.path.expandvars(path)) or glob.has_magic(path))]
        if not existing:
            target.available = False
            target.estimated_bytes = 0
            target.estimated_files = 0
            target.reason = "This location does not exist on this system."
            analysis.targets.append(target)
            continue
        if target.kind == "thumbnails":
            # Only cache files directly in the Explorer folder are deleted.
            cutoff_for_target = None
        size, files, errors, truncated = _scan_stats(
            existing, cutoff=cutoff_for_target, cancel_event=cancel_event
        )
        target.estimated_bytes = size
        target.estimated_files = files
        if errors:
            target.reason = "; ".join(dict.fromkeys(errors))[:800]
        if target.requires_admin:
            target.reason = (target.reason + " Requires administrator rights to measure and delete.").strip()
        analysis.targets.append(target)
        for error in errors:
            if error not in analysis.errors:
                analysis.errors.append(error)
    return analysis


def _delete_files(
    paths: list[str],
    *,
    cutoff: float | None,
    cancel_event: threading.Event | None,
    progress: ProgressFn,
) -> DeleteReport:
    report = DeleteReport()
    candidates: list[Path] = []
    for raw in paths:
        if not raw:
            continue
        expanded = os.path.expandvars(raw)
        if glob.has_magic(expanded):
            for match in glob.glob(expanded):
                candidate = Path(match)
                if candidate.is_file() and not candidate.is_symlink():
                    candidates.append(candidate)
            continue
        root = Path(expanded)
        if root.is_file():
            candidates.append(root)
            continue
        if not root.is_dir():
            continue
        stack = [root]
        visited: set[str] = set()
        while stack:
            if cancel_event is not None and cancel_event.is_set():
                report.cancelled = True
                break
            directory = stack.pop()
            key = os.path.normcase(os.path.abspath(str(directory)))
            if key in visited:
                continue
            visited.add(key)
            try:
                with os.scandir(directory) as iterator:
                    for entry in iterator:
                        if cancel_event is not None and cancel_event.is_set():
                            report.cancelled = True
                            break
                        try:
                            if entry.is_symlink() or getattr(entry, "is_junction", lambda: False)():
                                continue
                            if entry.is_file(follow_symlinks=False):
                                candidates.append(Path(entry.path))
                            elif entry.is_dir(follow_symlinks=False):
                                stack.append(Path(entry.path))
                        except OSError as exc:
                            if len(report.errors) < 40:
                                report.errors.append(f"{entry.path}: {exc}")
            except PermissionError:
                report.errors.append(f"Access denied: {directory}")
            except OSError as exc:
                report.errors.append(f"Could not read {directory}: {exc}")
            if report.cancelled:
                break
        if report.cancelled:
            break
    for index, path in enumerate(candidates, start=1):
        if cancel_event is not None and cancel_event.is_set():
            report.cancelled = True
            break
        try:
            if path.is_symlink() or path.is_dir():
                continue
            stat = path.stat(follow_symlinks=False)
            if cutoff is not None and stat.st_mtime > cutoff:
                report.skipped_recent += 1
                continue
            try:
                path.unlink()
            except PermissionError:
                try:
                    os.chmod(path, stat_module.S_IWRITE | stat_module.S_IREAD)
                    path.unlink()
                except OSError as exc:
                    report.skipped_locked += 1
                    if len(report.errors) < 40:
                        report.errors.append(f"Could not delete {path}: {exc}")
                    continue
            except OSError as exc:
                if len(report.errors) < 40:
                    report.errors.append(f"Could not delete {path}: {exc}")
                continue
            report.deleted_files += 1
            report.deleted_bytes += stat.st_size
        except OSError as exc:
            if len(report.errors) < 40:
                report.errors.append(f"Could not inspect {path}: {exc}")
        if index % 300 == 0:
            progress(f"Deleted {report.deleted_files:,} files...")
    return report


def _service_is_running(runner: CommandRunner, name: str) -> bool:
    script = f"$s = Get-Service -Name {name} -ErrorAction Stop; $s.Status.ToString()"
    text = ps_text(runner, script, timeout=20).strip().splitlines()
    return bool(text and text[-1].strip().lower() == "running")


def _stop_service(runner: CommandRunner, name: str) -> None:
    script = (
        f"$s = Get-Service -Name {name} -ErrorAction Stop; "
        "if ($s.Status -eq 'Running') { Stop-Service -Name " + name + " -Force -ErrorAction Stop; "
        "$s.WaitForStatus('Stopped','00:01:00') }"
    )
    result = runner.powershell(script, timeout=90)
    if not result.launched or result.exit_code != 0:
        raise ServiceError((result.stderr or result.stdout).strip()[:800] or "Could not stop Windows Update.")


def _start_service(runner: CommandRunner, name: str) -> None:
    script = (
        f"$s = Get-Service -Name {name} -ErrorAction Stop; "
        f"if ($s.Status -ne 'Running') {{ Start-Service -Name {name} -ErrorAction Stop }}"
    )
    result = runner.powershell(script, timeout=90)
    if not result.launched or result.exit_code != 0:
        raise ServiceError(
            (result.stderr or result.stdout).strip()[:800]
            or "The Windows Update service could not be restarted. Start it manually in Services."
        )


def build_cleanup_spec(target: CleanupTarget, *, min_age_hours: int | None = None) -> OperationSpec:
    if not target.available:
        raise ValueError(f"{target.label} is not available on this system.")
    if target.kind == "recycle_bin":
        return _build_recycle_spec(target)
    paths = [
        path
        for path in target.paths
        if path and (os.path.exists(os.path.expandvars(path)) or glob.has_magic(path))
    ]
    if not paths:
        raise ValueError(f"{target.label} does not exist on this system.")
    age = target.min_age_hours if min_age_hours is None else max(0, min_age_hours)
    start_time = time.time()
    cutoff = start_time - age * 3600
    delete_cutoff: float | None = cutoff
    verify_cutoff: float | None = cutoff
    service_state_info = {"was_running": False}
    if target.kind in {"update_cache", "thumbnails", "dumps"}:
        # Delete everything currently present, but do not count files created
        # by Windows or an application after the operation started.
        delete_cutoff = None
        verify_cutoff = start_time

    def apply(progress: ProgressFn, cancel) -> ApplyOutcome:
        runner = CommandRunner()
        was_running = False
        service_state = ""
        service_restart_failed = False
        report = DeleteReport()
        if target.kind == "update_cache":
            was_running = _service_is_running(runner, "wuauserv")
            service_state_info["was_running"] = was_running
            if was_running:
                progress("Stopping Windows Update service")
                _stop_service(runner, "wuauserv")
        try:
            progress(f"Deleting files for {target.label}")
            report = _delete_files(paths, cutoff=delete_cutoff, cancel_event=cancel, progress=progress)
        finally:
            if target.kind == "update_cache":
                if was_running:
                    try:
                        progress("Restarting Windows Update service")
                        _start_service(runner, "wuauserv")
                        service_state = "Windows Update service restarted"
                    except Exception as exc:
                        service_restart_failed = True
                        report.errors.append(f"Service restart failed: {exc}")
                        service_state = "Windows Update service could not be restarted"
                else:
                    service_state = "Windows Update service was already stopped"
        from winfirst.services.storage_health_service import format_bytes

        deleted_label = f"Deleted {report.deleted_files:,} files ({format_bytes(report.deleted_bytes)})"
        warnings = list(target.warnings)
        if report.skipped_locked:
            warnings.append(f"{report.skipped_locked:,} locked or in-use files were skipped.")
        if report.errors:
            warnings.append(f"{len(report.errors):,} file-level errors occurred; see details.")
        if service_restart_failed:
            warnings.append(
                "The Windows Update service was running before this cleanup and could not be restarted. "
                "Start it manually in Services (wuauserv) or restart Windows."
            )
        if report.cancelled:
            warnings.append("The operation was cancelled partway through; some files may already be deleted.")
        return ApplyOutcome(
            success=True,
            previous_state=f"{target.estimated_files or 0:,} files, {target.size_label}",
            requested_state="Delete cache files permanently",
            actual_state=deleted_label + (f"; {service_state}" if service_state else ""),
            warnings=warnings,
            details={
                "target": target.key,
                "deleted_files": report.deleted_files,
                "deleted_bytes": report.deleted_bytes,
                "skipped_recent": report.skipped_recent,
                "skipped_locked": report.skipped_locked,
                "service_was_running": was_running,
                "service_restart_failed": service_restart_failed,
                "errors": report.errors[:40],
            },
            exit_code=0,
        )

    def verify() -> VerifyOutcome:
        remaining_bytes, remaining_files, errors, _ = _scan_stats(
            paths, cutoff=verify_cutoff, cancel_event=None
        )
        ok = remaining_files == 0
        if ok:
            message = "No matching files remain in the measured locations."
        else:
            message = (
                f"{remaining_files:,} matching files ({remaining_bytes:,} bytes) still remain. "
                "They may be locked, recently created, or outside this user's permissions."
            )
        actual_state = f"{remaining_files:,} files remain"
        if target.kind == "update_cache":
            try:
                running = _service_is_running(CommandRunner(), "wuauserv")
                if service_state_info["was_running"] and not running:
                    # The cleanup stopped the service. Never report success while
                    # leaving it down: this is an actionable partial result.
                    ok = False
                    actual_state += "; Windows Update service stopped"
                    message += (
                        " The Windows Update service was running before the cleanup but is not running now. "
                        "Start it in Services or restart Windows."
                    )
                else:
                    actual_state += f"; Windows Update service running: {'yes' if running else 'no'}"
                    message += f" Windows Update service running: {'yes' if running else 'no'}."
            except Exception:
                ok = False
                actual_state += "; Windows Update service state unknown"
                message += " The Windows Update service state could not be read back."
        return VerifyOutcome(ok=ok, actual_state=actual_state, message=message)

    def before() -> str:
        return f"{target.estimated_files or 0:,} files, {target.size_label}"

    def plan_steps():
        expanded = [os.path.expandvars(path) for path in paths]
        shown = "; ".join(expanded[:5])
        if len(expanded) > 5:
            shown += f"; and {len(expanded) - 5} more location(s)"
        steps = [
            action_step(
                f"Permanently delete matching files in {len(expanded)} location(s)",
                detail=shown,
                destructive=True,
            )
        ]
        if age:
            steps.append(
                action_step(
                    f"Keep files modified in the last {age} hour(s)",
                    detail=f"Files newer than {age} hour(s) are skipped, not deleted.",
                )
            )
        if target.kind == "update_cache":
            steps.append(
                action_step(
                    "Stop Windows Update (wuauserv) if it is running, then restart it",
                    detail="If the service cannot be restarted, the operation reports a partial result.",
                )
            )
        return steps

    return OperationSpec(
        operation_id=f"cleanup.{target.key}",
        name=f"Clean storage: {target.label}",
        category="cleanup",
        description=target.description,
        risk=RiskLevel.HIGH,
        requires_admin=target.requires_admin,
        reversible=False,
        affects_other_users=False,
        apply=apply,
        verify=verify,
        before=before,
        plan_steps=plan_steps,
        preview_text=f"{target.label}: {target.estimated_files or 0:,} files, {target.size_label}",
        warnings=list(target.warnings),
        verification_method="Rescans the same locations after deletion and checks that no matching files remain.",
        details={"target": target.key, "paths_count": len(paths), "min_age_hours": age},
    )


def _build_recycle_spec(target: CleanupTarget) -> OperationSpec:
    def count(runner: CommandRunner) -> int | None:
        return _recycle_count(runner)

    def apply(progress: ProgressFn, cancel) -> ApplyOutcome:
        runner = CommandRunner()
        progress("Emptying the Recycle Bin")
        result = runner.powershell("Clear-RecycleBin -Force -ErrorAction Stop", timeout=180)
        if not result.launched:
            raise ServiceError(result.error or "PowerShell could not be started.")
        if result.exit_code != 0:
            raise ServiceError((result.stderr or result.stdout).strip()[:800] or "Clear-RecycleBin failed.")
        return ApplyOutcome(
            success=True,
            previous_state=f"{target.estimated_files or 0:,} items",
            requested_state="Recycle Bin empty",
            actual_state="Clear-RecycleBin accepted; verification pending",
            warnings=list(target.warnings),
            details={"target": target.key, "previous_items": target.estimated_files},
            exit_code=result.exit_code,
        )

    def verify() -> VerifyOutcome:
        remaining = count(CommandRunner())
        if remaining is None:
            return VerifyOutcome(False, "Unknown", "The Recycle Bin item count could not be read back.")
        return VerifyOutcome(
            ok=remaining == 0,
            actual_state=f"{remaining:,} items remain",
            message="Read the Recycle Bin item count back through the Windows shell.",
        )

    def before() -> str:
        return f"{target.estimated_files or 0:,} items"

    def plan_steps():
        return [
            action_step(
                "Empty the Recycle Bin permanently",
                detail=f"{target.estimated_files or 0:,} item(s) are currently reported by the shell.",
                destructive=True,
            )
        ]

    return OperationSpec(
        operation_id="cleanup.recycle_bin",
        name="Empty the Recycle Bin",
        category="cleanup",
        description=target.description,
        risk=RiskLevel.HIGH,
        requires_admin=False,
        reversible=False,
        apply=apply,
        verify=verify,
        before=before,
        plan_steps=plan_steps,
        preview_text=f"Recycle Bin: {target.estimated_files or 0:,} items will be permanently deleted",
        warnings=list(target.warnings),
        verification_method="Reads the Recycle Bin item count back through the Windows shell.",
        details={"target": target.key},
    )


def build_cleanup_specs(
    analysis: CleanupAnalysis,
    keys: list[str],
    *,
    min_age_hours: int = 24,
) -> list[OperationSpec]:
    specs: list[OperationSpec] = []
    for key in keys:
        target = analysis.find(key)
        if target is None:
            raise ValueError(f"Unknown cleanup target: {key}")
        if not target.available:
            continue
        specs.append(build_cleanup_spec(target, min_age_hours=min_age_hours))
    return specs
