"""Execution controller implementing the operation lifecycle.

Detect -> Preview -> Validate -> Confirm -> Apply -> Verify -> Log -> Report.

The controller is deliberately conservative: success is only returned when the
apply step reports success *and* the independent verification step agrees.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Sequence

from winfirst.core import privilege_manager
from winfirst.core.audit_logger import AuditLogger
from winfirst.core.operation_result import (
    OperationResult,
    OperationStatus,
    cancelled_result,
    utc_now,
)
from winfirst.core.plan import (
    OperationPlan,
    PlanDocument,
    PlanStep,
    build_document,
    build_plan,
)
from winfirst.core.redaction import redact_secrets
from winfirst.core.risk_engine import RiskLevel

ProgressFn = Callable[[str], None]
CancelEvent = threading.Event | None


def _noop(_: str) -> None:
    return None


@dataclass
class VerifyOutcome:
    ok: bool
    actual_state: str = ""
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class ApplyOutcome:
    success: bool = True
    previous_state: str = ""
    requested_state: str = ""
    actual_state: str = ""
    message: str = ""
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    warnings: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)
    rollback: dict[str, Any] | None = None
    restart_required: bool = False
    error: str = ""


ApplyFn = Callable[[ProgressFn, CancelEvent], ApplyOutcome]
VerifyFn = Callable[[], VerifyOutcome]
BeforeFn = Callable[[], str]
SupportFn = Callable[[], tuple[bool, str]]
PlanStepsFn = Callable[[], Sequence[PlanStep | str]]


@dataclass
class OperationSpec:
    operation_id: str
    name: str
    apply: ApplyFn
    category: str = "general"
    description: str = ""
    risk: RiskLevel = RiskLevel.MEDIUM
    requires_admin: bool = False
    reversible: bool = False
    restart_required: bool = False
    can_interrupt_connectivity: bool = False
    affects_other_users: bool = False
    verify: VerifyFn | None = None
    before: BeforeFn | None = None
    support_check: SupportFn | None = None
    plan_steps: PlanStepsFn | None = None
    preview_text: str = ""
    warnings: list[str] = field(default_factory=list)
    verification_method: str = "Reads back the affected state"
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def requires_confirmation(self) -> bool:
        return self.risk.rank >= RiskLevel.MEDIUM.rank

    @property
    def requires_typed_confirmation(self) -> bool:
        return self.risk is RiskLevel.CRITICAL


class OperationController:
    def __init__(
        self,
        audit: AuditLogger,
        *,
        privilege_check: Callable[[], bool] = privilege_manager.is_elevated,
        logger: logging.Logger | None = None,
    ) -> None:
        self.audit = audit
        self.privilege_check = privilege_check
        self.log = logger or logging.getLogger("winfirst.operations")

    # ------------------------------------------------------------------ plans
    def plan(self, spec: OperationSpec) -> OperationPlan:
        """Describe what ``spec`` would do without changing anything.

        Only read-only detection runs here: the support check and the
        ``before`` reader.  A failing read becomes a note, not an error, so a
        review can still be produced for an inaccessible setting.
        """
        supported, reason = True, ""
        if spec.support_check is not None:
            try:
                supported, reason = spec.support_check()
            except Exception as exc:  # a broken check must not block the review
                supported, reason = False, f"Support check failed: {exc}"
        previous = ""
        notes: list[str] = []
        if spec.before is not None:
            try:
                previous = str(spec.before() or "")
            except Exception as exc:
                notes.append(f"The current state could not be read: {exc}")
        steps: Sequence[PlanStep | str] = []
        if spec.plan_steps is not None:
            try:
                steps = spec.plan_steps()
            except Exception as exc:
                notes.append(f"Command-level steps could not be generated: {exc}")
        return build_plan(
            spec,
            previous_state=previous,
            steps=steps,
            supported=supported,
            support_reason=reason,
            notes=notes,
        )

    def plan_many(
        self,
        specs: Sequence[OperationSpec],
        *,
        title: str = "WinFirst change plan",
        host: str = "",
        profile_name: str = "",
    ) -> PlanDocument:
        return build_document(
            [self.plan(spec) for spec in specs],
            title=title,
            host=host,
            profile_name=profile_name,
        )

    def execute(
        self,
        spec: OperationSpec,
        *,
        progress: ProgressFn | None = None,
        cancel_event: CancelEvent = None,
        dry_run: bool = False,
        profile_name: str = "",
    ) -> OperationResult:
        report = progress or _noop
        result = OperationResult(
            operation_id=spec.operation_id,
            name=spec.name,
            category=spec.category,
            requires_admin=spec.requires_admin,
            risk=spec.risk.value,
            restart_required=spec.restart_required,
            dry_run=dry_run,
            details=dict(spec.details),
        )
        result.elevated = self.privilege_check()
        started = time.monotonic()

        if cancel_event is not None and cancel_event.is_set():
            result.status = OperationStatus.CANCELLED
            result.error = "Cancelled before execution."
            result.finished_at = utc_now()
            self._record(result, None, profile_name)
            return result

        if spec.support_check is not None:
            try:
                supported, reason = spec.support_check()
            except Exception as exc:  # defensive: a broken check means unsupported
                supported, reason = False, f"Support check failed: {exc}"
            if not supported:
                result.status = OperationStatus.UNSUPPORTED
                result.error = reason or "This operation is not supported on this system."
                result.finished_at = utc_now()
                self._record(result, None, profile_name)
                return result

        if spec.requires_admin and not result.elevated:
            result.status = OperationStatus.DENIED
            result.error = (
                "Administrator privileges are required. Restart WinFirst as administrator "
                "and try again."
            )
            result.finished_at = utc_now()
            self._record(result, None, profile_name)
            return result

        if dry_run:
            plan = self.plan(spec)
            result.status = OperationStatus.SKIPPED
            result.requested_state = plan.preview or plan.name
            result.previous_state = plan.previous_state
            result.verification = plan.verification
            result.error = "Dry run: no changes were applied."
            result.details["planned"] = plan.preview
            result.details["plan"] = plan.to_dict()
            result.details["planned_steps"] = [
                step.command or step.title for step in plan.steps
            ]
            result.finished_at = utc_now()
            result.duration_ms = int((time.monotonic() - started) * 1000)
            self._record(result, None, profile_name)
            return result

        if spec.before is not None:
            try:
                result.previous_state = str(spec.before() or "")
            except Exception as exc:
                result.warnings.append(f"Could not read the previous state: {exc}")

        outcome: ApplyOutcome | None = None
        try:
            report(f"Applying: {spec.name}")
            outcome = spec.apply(report, cancel_event)
        except Exception as exc:
            self.log.exception("Operation failed: %s", spec.operation_id)
            result.status = OperationStatus.FAILED
            result.error = f"{type(exc).__name__}: {exc}"
            result.finished_at = utc_now()
            result.duration_ms = int((time.monotonic() - started) * 1000)
            self._record(result, None, profile_name)
            return result

        if outcome is None:
            outcome = ApplyOutcome(success=False, error="The operation returned no result.")

        result.previous_state = result.previous_state or outcome.previous_state
        result.requested_state = redact_secrets(outcome.requested_state)
        result.actual_state = redact_secrets(outcome.actual_state)
        result.stdout = outcome.stdout
        result.stderr = outcome.stderr
        result.exit_code = outcome.exit_code
        result.warnings.extend(outcome.warnings)
        result.details.update(outcome.details)
        if outcome.restart_required:
            result.restart_required = True
        if not outcome.success:
            result.error = outcome.error or outcome.message or "The operation reported failure."

        if cancel_event is not None and cancel_event.is_set():
            result.status = OperationStatus.CANCELLED
            result.error = "The operation was cancelled."
            result.warnings.append(
                "A cancelled operation may have applied part of its change; review the current state."
            )
            result.finished_at = utc_now()
            result.duration_ms = int((time.monotonic() - started) * 1000)
            self._record(result, None, profile_name)
            return result

        verified = True
        if outcome.success and spec.verify is not None:
            try:
                report(f"Verifying: {spec.name}")
                check = spec.verify()
                verified = bool(check.ok)
                if check.actual_state:
                    result.actual_state = redact_secrets(check.actual_state)
                result.verification = check.message or spec.verification_method
                if check.details:
                    result.details.update(check.details)
            except Exception as exc:
                verified = False
                result.verification = f"Verification failed to run: {exc}"
                result.warnings.append(result.verification)
        elif outcome.success:
            result.verification = "Reported by the operation; no independent read-back is available."

        if not outcome.success:
            result.status = OperationStatus.FAILED
        elif not verified:
            result.status = OperationStatus.PARTIAL
            result.error = result.error or "The change was applied but verification did not confirm the requested state."
        else:
            result.status = OperationStatus.SUCCESS

        if outcome.rollback and spec.reversible:
            result.rollback_available = True

        result.finished_at = utc_now()
        result.duration_ms = int((time.monotonic() - started) * 1000)
        self._record(result, outcome.rollback if result.rollback_available else None, profile_name)
        report(f"{spec.name}: {result.status.label}")
        return result

    def execute_many(
        self,
        specs: Sequence[OperationSpec],
        *,
        progress: ProgressFn | None = None,
        cancel_event: CancelEvent = None,
        dry_run: bool = False,
        profile_name: str = "",
        stop_on_failure: bool = False,
    ) -> list[OperationResult]:
        report = progress or _noop
        results: list[OperationResult] = []
        total = len(specs)
        for index, spec in enumerate(specs, start=1):
            if cancel_event is not None and cancel_event.is_set():
                remaining = specs[index - 1 :]
                for leftover in remaining:
                    results.append(
                        cancelled_result(
                            leftover.operation_id,
                            leftover.name,
                            "Cancelled before this operation started.",
                        )
                    )
                break
            report(f"[{index}/{total}] {spec.name}")
            result = self.execute(
                spec,
                progress=report,
                cancel_event=cancel_event,
                dry_run=dry_run,
                profile_name=profile_name,
            )
            results.append(result)
            if stop_on_failure and result.status.is_failure:
                break
        return results

    def _record(self, result: OperationResult, rollback: dict | None, profile_name: str) -> None:
        try:
            self.audit.record(result, rollback=rollback, profile_name=profile_name)
        except Exception as exc:
            self.log.error("Failed to record operation history: %s", exc)
            result.details["audit_error"] = str(exc)


def local_timestamp() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
