from __future__ import annotations

import threading

from winfirst.core.audit_logger import AuditLogger
from winfirst.core.operation import (
    ApplyOutcome,
    OperationController,
    OperationSpec,
    VerifyOutcome,
)
from winfirst.core.operation_result import OperationStatus
from winfirst.core.risk_engine import RiskLevel


def _spec(**overrides) -> OperationSpec:
    calls = overrides.pop("calls", [])

    def apply(progress, _cancel) -> ApplyOutcome:
        calls.append("apply")
        progress("half way")
        return ApplyOutcome(
            success=True,
            previous_state="Disabled",
            requested_state="Enabled",
            actual_state="Enabled",
            message="applied",
        )

    def verify() -> VerifyOutcome:
        calls.append("verify")
        return VerifyOutcome(ok=True, actual_state="Enabled", message="read back")

    return OperationSpec(
        operation_id="test.operation",
        name="Test operation",
        apply=apply,
        verify=verify,
        **overrides,
    )


def test_success_requires_verification(controller: OperationController, audit: AuditLogger) -> None:
    calls: list[str] = []
    result = controller.execute(_spec(calls=calls))
    assert result.status is OperationStatus.SUCCESS
    assert result.ok
    assert result.actual_state == "Enabled"
    assert result.verification == "read back"
    assert calls == ["apply", "verify"]
    assert audit.count() == 1
    row = audit.list_operations()[0]
    assert row["operation_id"] == "test.operation"
    assert row["status"] == "success"


def test_failed_verification_is_partial(controller: OperationController) -> None:
    def apply(progress, _cancel) -> ApplyOutcome:
        return ApplyOutcome(success=True, actual_state="Looks changed")

    def verify() -> VerifyOutcome:
        return VerifyOutcome(ok=False, actual_state="Unchanged", message="read back differs")

    result = controller.execute(
        OperationSpec(
            operation_id="test.partial",
            name="Partial",
            apply=apply,
            verify=verify,
        )
    )
    assert result.status is OperationStatus.PARTIAL
    assert result.ok is False
    assert "verification did not confirm" in result.error
    assert result.actual_state == "Unchanged"


def test_apply_failure_stops_before_verification(controller: OperationController) -> None:
    calls: list[str] = []

    def apply(progress, _cancel) -> ApplyOutcome:
        return ApplyOutcome(success=False, error="vendor tool failed")

    def verify() -> VerifyOutcome:
        calls.append("verify")
        return VerifyOutcome(ok=True)

    result = controller.execute(
        OperationSpec(
            operation_id="test.failure",
            name="Failure",
            apply=apply,
            verify=verify,
        )
    )
    assert result.status is OperationStatus.FAILED
    assert "vendor tool failed" in result.error
    assert calls == []


def test_exception_is_recorded_as_failed(controller: OperationController, audit: AuditLogger) -> None:
    def apply(progress, _cancel) -> ApplyOutcome:
        raise RuntimeError("boom")

    result = controller.execute(
        OperationSpec(operation_id="test.exception", name="Exception", apply=apply)
    )
    assert result.status is OperationStatus.FAILED
    assert "RuntimeError: boom" in result.error
    assert audit.count() == 1


def test_dry_run_never_calls_apply(controller: OperationController) -> None:
    calls: list[str] = []
    result = controller.execute(_spec(calls=calls), dry_run=True)
    assert result.status is OperationStatus.SKIPPED
    assert result.dry_run is True
    assert calls == []


def test_cancel_before_execution(controller: OperationController) -> None:
    calls: list[str] = []
    event = threading.Event()
    event.set()
    result = controller.execute(_spec(calls=calls), cancel_event=event)
    assert result.status is OperationStatus.CANCELLED
    assert calls == []


def test_admin_requirement_is_enforced(audit: AuditLogger) -> None:
    calls: list[str] = []
    controller = OperationController(audit, privilege_check=lambda: False)
    result = controller.execute(_spec(calls=calls, requires_admin=True))
    assert result.status is OperationStatus.DENIED
    assert calls == []
    assert "Administrator" in result.error


def test_unsupported_system_is_reported(controller: OperationController) -> None:
    result = controller.execute(
        _spec(support_check=lambda: (False, "Requires Windows 11"))
    )
    assert result.status is OperationStatus.UNSUPPORTED
    assert "Windows 11" in result.error


def test_rollback_is_only_available_for_reversible_specs(controller: OperationController, audit: AuditLogger) -> None:
    rollback = {
        "strategy": "setting",
        "key": "show_file_extensions",
        "previous": False,
        "summary": "Restore file extensions",
    }

    def apply(progress, _cancel) -> ApplyOutcome:
        return ApplyOutcome(success=True, rollback=rollback)

    reversible = controller.execute(
        OperationSpec(
            operation_id="test.reversible",
            name="Reversible",
            apply=apply,
            reversible=True,
            verify=lambda: VerifyOutcome(ok=True),
        )
    )
    assert reversible.rollback_available is True
    assert len(audit.rollback_entries()) == 1

    not_reversible = controller.execute(
        OperationSpec(
            operation_id="test.not_reversible",
            name="Not reversible",
            apply=apply,
            reversible=False,
            verify=lambda: VerifyOutcome(ok=True),
        )
    )
    assert not_reversible.rollback_available is False
    assert len(audit.rollback_entries()) == 1


def test_execute_many_continues_after_failure(controller: OperationController) -> None:
    def failing(progress, _cancel) -> ApplyOutcome:
        return ApplyOutcome(success=False, error="failed")

    results = controller.execute_many(
        [
            OperationSpec(operation_id="a", name="A", apply=failing),
            OperationSpec(operation_id="b", name="B", apply=lambda p, c: ApplyOutcome()),
        ]
    )
    assert [result.status for result in results] == [
        OperationStatus.FAILED,
        OperationStatus.SUCCESS,
    ]


def test_execute_many_can_stop_on_failure(controller: OperationController) -> None:
    def failing(progress, _cancel) -> ApplyOutcome:
        return ApplyOutcome(success=False, error="failed")

    results = controller.execute_many(
        [
            OperationSpec(operation_id="a", name="A", apply=failing),
            OperationSpec(operation_id="b", name="B", apply=lambda p, c: ApplyOutcome()),
        ],
        stop_on_failure=True,
    )
    assert len(results) == 1
    assert results[0].status is OperationStatus.FAILED


def test_execute_many_marks_remaining_as_cancelled(controller: OperationController) -> None:
    event = threading.Event()
    event.set()
    results = controller.execute_many(
        [
            OperationSpec(operation_id="a", name="A", apply=lambda p, c: ApplyOutcome()),
            OperationSpec(operation_id="b", name="B", apply=lambda p, c: ApplyOutcome()),
        ],
        cancel_event=event,
    )
    assert len(results) == 2
    assert all(result.status is OperationStatus.CANCELLED for result in results)


def test_progress_messages_are_forwarded(controller: OperationController) -> None:
    messages: list[str] = []
    controller.execute(_spec(), progress=messages.append)
    assert any("Applying" in message for message in messages)
    assert any("Verifying" in message for message in messages)
    assert any("half way" in message for message in messages)


def test_risk_and_category_are_recorded(controller: OperationController, audit: AuditLogger) -> None:
    controller.execute(
        _spec(category="power", risk=RiskLevel.HIGH, details={"setting": "x"})
    )
    row = audit.get_operation(1)
    assert row is not None
    assert row["category"] == "power"
    assert row["risk"] == "high"
    assert row["details"]["setting"] == "x"
