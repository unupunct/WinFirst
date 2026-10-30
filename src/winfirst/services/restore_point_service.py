"""System restore points as a pre-apply safety net.

Creating a restore point is the one cheap, built-in way to make a batch of
configuration changes recoverable.  This service:

* detects whether System Protection is enabled (read-only);
* lists existing restore points through the documented ``root/default``
  ``SystemRestore`` CIM class;
* reuses a recent restore point instead of fighting the Windows frequency
  limit, so a second batch in the same day does not fail;
* creates a checkpoint with ``Checkpoint-Computer`` and verifies that the new
  point is actually listed before reporting success.

WinFirst never changes the restore-point frequency or enables System
Protection silently.  When protection is off, the caller is told exactly what
to change and the operation is refused.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from winfirst.core.command_runner import CommandRunner, as_list, parse_json_output, ps_quote
from winfirst.core.operation import ApplyOutcome, OperationSpec, VerifyOutcome
from winfirst.core.plan import action_step
from winfirst.core.privilege_manager import is_elevated
from winfirst.core.risk_engine import RiskLevel
from winfirst.services.common import ServiceError, require_windows
from winfirst.services.win_registry import read_value

RESTORE_KEY = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\SystemRestore"
DISABLE_VALUE = "DisableSR"
FREQUENCY_VALUE = "SystemRestorePointCreationFrequency"
DEFAULT_FREQUENCY_MINUTES = 1440

_DESCRIPTION_RE = re.compile(r"[^\x20-\x7E]")
_MAX_DESCRIPTION = 200

_LIST_SCRIPT = (
    "$points = @(Get-CimInstance -ClassName SystemRestore -Namespace root/default "
    "-ErrorAction Stop | Sort-Object SequenceNumber -Descending | "
    "Select-Object -First {limit} SequenceNumber, Description, RestorePointType, "
    "@{{n='CreatedAt';e={{$_.CreationTime.ToString('o')}}}}); "
    "if ($points.Count -gt 0) {{ $points | ConvertTo-Json -Depth 3 }}"
)


@dataclass(frozen=True)
class RestorePointInfo:
    sequence: int
    description: str
    created_at: str = ""
    point_type: str = ""

    def created_datetime(self) -> datetime | None:
        text = (self.created_at or "").strip()
        if not text:
            return None
        candidates = [text]
        if text.endswith("Z"):
            candidates.append(text[:-1] + "+00:00")
        for candidate in candidates:
            try:
                parsed = datetime.fromisoformat(candidate)
            except ValueError:
                continue
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        # WMI DATETIME, e.g. 20240115123045.000000-000
        match = re.match(r"^(\d{14})\.\d+([+-]\d{3})$", text)
        if match:
            base = datetime.strptime(match.group(1), "%Y%m%d%H%M%S")
            offset_minutes = int(match.group(2)[:3])
            return (base - timedelta(minutes=offset_minutes)).replace(tzinfo=timezone.utc)
        return None

    def label(self) -> str:
        when = self.created_at or "unknown time"
        return f"#{self.sequence} {self.description} ({when})"


@dataclass(frozen=True)
class RestoreSupport:
    available: bool
    enabled: bool
    reason: str = ""
    frequency_minutes: int = DEFAULT_FREQUENCY_MINUTES
    elevated: bool = True

    @property
    def can_create(self) -> bool:
        return self.available and self.enabled and self.elevated

    def summary(self) -> str:
        if not self.available:
            return self.reason or "System Restore is not available."
        if not self.enabled:
            return self.reason or "System Protection is disabled."
        if not self.elevated:
            return (
                "System Restore is available, but creating or reading restore points "
                "requires administrator rights. Restart WinFirst as administrator to use it."
            )
        if self.frequency_minutes <= 0:
            return "System Restore is available; Windows does not throttle checkpoint creation."
        return (
            "System Restore is available. Windows allows a new checkpoint every "
            f"{self.frequency_minutes} minutes by default."
        )


def sanitize_description(description: str) -> str:
    cleaned = _DESCRIPTION_RE.sub(" ", str(description or ""))
    cleaned = " ".join(cleaned.split())
    if not cleaned:
        raise ValueError("A restore point description is required.")
    return cleaned[:_MAX_DESCRIPTION]


class RestorePointService:
    def __init__(
        self,
        runner: CommandRunner | None = None,
        *,
        elevation_check: Callable[[], bool] = is_elevated,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.runner = runner or CommandRunner()
        self.elevation_check = elevation_check
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    # --------------------------------------------------------------- support
    def support(self) -> RestoreSupport:
        if not _is_windows():
            return RestoreSupport(False, False, "System Restore requires Windows 10 or Windows 11.")
        try:
            disabled = read_value("HKLM", RESTORE_KEY, DISABLE_VALUE)
            frequency = read_value("HKLM", RESTORE_KEY, FREQUENCY_VALUE)
        except Exception as exc:  # registry access is expected to work; be explicit if not
            return RestoreSupport(False, False, f"System Restore settings could not be read: {exc}")
        enabled = not (disabled.exists and bool(int(disabled.value or 0)))
        minutes = DEFAULT_FREQUENCY_MINUTES
        if frequency.exists:
            try:
                minutes = max(0, int(frequency.value or 0))
            except (TypeError, ValueError):
                minutes = DEFAULT_FREQUENCY_MINUTES
        reason = ""
        if not enabled:
            reason = (
                "System Protection is disabled, so Windows cannot create restore points. "
                "Enable it in System Properties > System Protection, then try again."
            )
        return RestoreSupport(
            True,
            enabled,
            reason,
            minutes,
            elevated=bool(self.elevation_check()),
        )

    # ----------------------------------------------------------------- points
    def list_points(self, limit: int = 10) -> list[RestorePointInfo]:
        require_windows("System Restore")
        limit = max(1, min(int(limit), 100))
        result = self.runner.powershell(_LIST_SCRIPT.format(limit=limit), timeout=120)
        if not result.launched:
            raise ServiceError(result.error or "PowerShell could not be started.")
        if result.exit_code != 0:
            detail = _clean_powershell_error(result.stderr or result.stdout)
            if "access denied" in detail.casefold():
                raise ServiceError(
                    "Restore points cannot be read without administrator rights. "
                    "Restart WinFirst as administrator and try again."
                )
            raise ServiceError(
                "Restore points could not be read. System Protection may be disabled "
                f"or the account may lack access. {detail}".strip()
            )
        points: list[RestorePointInfo] = []
        for row in as_list(parse_json_output(result.stdout)):
            try:
                sequence = int(row.get("SequenceNumber") or 0)
            except (TypeError, ValueError):
                continue
            if sequence <= 0:
                continue
            points.append(
                RestorePointInfo(
                    sequence=sequence,
                    description=str(row.get("Description") or "").strip(),
                    created_at=str(row.get("CreatedAt") or "").strip(),
                    point_type=str(row.get("RestorePointType") or "").strip(),
                )
            )
        points.sort(key=lambda item: item.sequence, reverse=True)
        return points

    def newest_point(self) -> RestorePointInfo | None:
        points = self.list_points(limit=1)
        return points[0] if points else None

    def throttle_remaining(self, point: RestorePointInfo | None = None) -> timedelta | None:
        support = self.support()
        if support.frequency_minutes <= 0:
            return None
        point = point or self.newest_point()
        if point is None:
            return None
        created = point.created_datetime()
        if created is None:
            return None
        elapsed = self._clock() - created
        remaining = timedelta(minutes=support.frequency_minutes) - elapsed
        return remaining if remaining > timedelta(0) else None

    # ---------------------------------------------------------------- create
    def create(
        self,
        description: str,
        *,
        progress: Callable[[str], None] | None = None,
        cancel_event=None,
        wait_seconds: int = 120,
        poll_interval: float = 3.0,
    ) -> RestorePointInfo:
        require_windows("System Restore")
        if not self.elevation_check():
            raise ServiceError(
                "Creating a restore point requires administrator rights. "
                "Restart WinFirst as administrator and try again."
            )
        support = self.support()
        if not support.available:
            raise ServiceError(support.summary())
        if not support.enabled:
            raise ServiceError(support.summary())
        cleaned = sanitize_description(description)
        before = self.newest_point()
        before_sequence = before.sequence if before else 0
        report = progress or (lambda _text: None)
        report(f"Creating a restore point: {cleaned}")
        script = (
            f"Checkpoint-Computer -Description {ps_quote(cleaned)} "
            "-RestorePointType 'MODIFY_SETTINGS' -ErrorAction Stop"
        )
        result = self.runner.powershell(script, timeout=1800, cancel_event=cancel_event)
        if not result.launched:
            raise ServiceError(result.error or "PowerShell could not be started.")
        if result.timed_out:
            raise ServiceError("Creating the restore point timed out after 30 minutes.")
        if result.cancelled:
            raise ServiceError("Creating the restore point was cancelled.")
        if result.exit_code != 0:
            detail = _clean_powershell_error(result.stderr or result.stdout)
            lowered = detail.casefold()
            if "already been created" in lowered or "24 hours" in lowered:
                remaining = self.throttle_remaining(before)
                wait = f" Try again in about {_humanize(remaining)}." if remaining else ""
                raise ServiceError(
                    "Windows refused a new restore point because one was created recently. "
                    f"{wait}".strip()
                )
            if "access denied" in lowered:
                raise ServiceError(
                    "Checkpoint-Computer was denied. Restart WinFirst as administrator and try again."
                )
            raise ServiceError(f"Checkpoint-Computer failed: {detail or 'no error text was returned.'}")
        report("Waiting for Windows to list the new restore point")
        point = self._wait_for_point(cleaned, before_sequence, wait_seconds, poll_interval)
        if point is None:
            raise ServiceError(
                "The checkpoint command completed but no new restore point appeared. "
                "Check System Protection settings before relying on rollback."
            )
        return point

    def ensure_point(
        self,
        description: str,
        *,
        max_age_minutes: int | None = None,
        progress: Callable[[str], None] | None = None,
        cancel_event=None,
    ) -> tuple[RestorePointInfo, bool]:
        """Return a usable restore point and whether one was created.

        A checkpoint created within the Windows frequency window is reused
        instead of failing, because it still protects the current batch.
        """
        support = self.support()
        if not support.can_create:
            raise ServiceError(support.summary())
        window = support.frequency_minutes if max_age_minutes is None else max(0, max_age_minutes)
        newest = self.newest_point()
        if newest is not None and window > 0:
            created = newest.created_datetime()
            if created is not None:
                age = self._clock() - created
                if timedelta(0) <= age <= timedelta(minutes=window):
                    if progress:
                        progress(
                            "Reusing the restore point created "
                            f"{_humanize(age)} ago; no new checkpoint was needed."
                        )
                    return newest, False
        point = self.create(
            description,
            progress=progress,
            cancel_event=cancel_event,
        )
        return point, True

    def _wait_for_point(
        self,
        description: str,
        before_sequence: int,
        wait_seconds: int,
        poll_interval: float,
    ) -> RestorePointInfo | None:
        deadline = time.monotonic() + max(0, wait_seconds)
        while True:
            try:
                points = self.list_points(limit=10)
            except ServiceError:
                points = []
            for point in points:
                if point.sequence > before_sequence and point.description == description:
                    return point
            if time.monotonic() >= deadline:
                return None
            time.sleep(max(0.1, poll_interval))

    # ------------------------------------------------------------------ spec
    def build_spec(self, description: str) -> OperationSpec:
        cleaned = sanitize_description(description)
        state: dict[str, object] = {"sequence": 0, "created": False, "point": "", "reused": ""}

        def apply(progress, cancel) -> ApplyOutcome:
            point, created = self.ensure_point(
                cleaned,
                progress=progress,
                cancel_event=cancel,
            )
            state["sequence"] = point.sequence
            state["created"] = created
            state["point"] = point.label()
            support = self.support()
            warnings = [
                "Restore points do not replace a backup and can be removed by Windows "
                "when disk space is low.",
            ]
            if not created:
                warnings.append(
                    "An existing restore point was reused because Windows limits how often "
                    "a new checkpoint can be created."
                )
            return ApplyOutcome(
                success=True,
                previous_state="No restore point was created by WinFirst",
                requested_state="A verified restore point exists",
                actual_state=point.label(),
                message=(
                    f"Created restore point {point.label()}"
                    if created
                    else f"Reused recent restore point {point.label()}"
                ),
                warnings=warnings,
                details={
                    "sequence": point.sequence,
                    "created": created,
                    "frequency_minutes": support.frequency_minutes,
                },
            )

        def verify() -> VerifyOutcome:
            sequence = int(state.get("sequence") or 0)
            try:
                points = self.list_points(limit=10)
            except ServiceError as exc:
                return VerifyOutcome(False, "Unknown", str(exc))
            match = next((point for point in points if point.sequence == sequence), None)
            return VerifyOutcome(
                ok=match is not None,
                actual_state=match.label() if match else "No matching restore point",
                message=(
                    f"Read the restore point list back: {match.label()}"
                    if match
                    else "The restore point is not listed after creation."
                ),
            )

        def plan_steps():
            support = self.support()
            steps = [
                action_step(
                    f"Ensure a restore point exists: {cleaned}",
                    detail=(
                        f"Checkpoint-Computer -Description \"{cleaned}\" "
                        "-RestorePointType MODIFY_SETTINGS"
                    ),
                )
            ]
            if support.frequency_minutes > 0:
                steps.append(
                    action_step(
                        "Reuse a recent checkpoint when Windows throttles creation",
                        detail=(
                            "A restore point newer than "
                            f"{support.frequency_minutes} minutes counts as sufficient; "
                            "WinFirst does not change the frequency setting."
                        ),
                    )
                )
            steps.append(
                action_step(
                    "Confirm the point is listed",
                    detail="Get-CimInstance SystemRestore is read back after creation.",
                )
            )
            return steps

        return OperationSpec(
            operation_id="safety.restore_point",
            name="Create a system restore point",
            category="safety",
            description=f"Creates a verified System Restore checkpoint: {cleaned}",
            risk=RiskLevel.LOW,
            requires_admin=True,
            reversible=False,
            apply=apply,
            verify=verify,
            before=lambda: _describe_newest(self),
            plan_steps=plan_steps,
            preview_text=f"Restore point: {cleaned}",
            warnings=[
                "System Protection must be enabled; WinFirst does not enable it silently.",
                "Restore points consume disk space and are not a substitute for a backup.",
            ],
            verification_method="Reads the checkpoint list back through Get-CimInstance SystemRestore",
            details={"description": cleaned},
        )


def _describe_newest(service: RestorePointService) -> str:
    try:
        point = service.newest_point()
    except ServiceError as exc:
        return str(exc)
    return point.label() if point else "No restore points are currently listed"


def _clean_powershell_error(text: str) -> str:
    """Turn PowerShell CLIXML error noise into one readable line."""
    cleaned = re.sub(r"<[^>]*>", " ", str(text or ""))
    cleaned = cleaned.replace("_x000D_", " ").replace("_x000A_", " ")
    return " ".join(cleaned.split())[:800]


def _humanize(delta: timedelta | None) -> str:
    if delta is None:
        return "a while"
    seconds = max(0, int(delta.total_seconds()))
    if seconds < 90:
        return f"{seconds} seconds"
    minutes = round(seconds / 60)
    if minutes < 90:
        return f"{minutes} minutes"
    hours = round(minutes / 60)
    return f"{hours} hours"


def _is_windows() -> bool:
    import sys

    return sys.platform == "win32"
