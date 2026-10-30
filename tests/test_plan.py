from __future__ import annotations

import json

from winfirst.core.audit_logger import AuditLogger
from winfirst.core.operation import ApplyOutcome, OperationController, OperationSpec, VerifyOutcome
from winfirst.core.operation_result import OperationStatus
from winfirst.core.plan import PlanStep, action_step, build_plan, command_step
from winfirst.core.risk_engine import RiskLevel


def _spec_with_steps(calls: list[str] | None = None) -> OperationSpec:
    calls = calls if calls is not None else []

    def apply(progress, _cancel) -> ApplyOutcome:
        calls.append("apply")
        return ApplyOutcome(success=True, actual_state="Changed")

    def verify() -> VerifyOutcome:
        calls.append("verify")
        return VerifyOutcome(ok=True, actual_state="Changed", message="read back")

    def plan_steps():
        return [
            command_step(["sc.exe", "config", "demo", "start=", "disabled"], title="Disable demo"),
            action_step("Read the startup mode back", detail="Win32_Service read-back"),
        ]

    return OperationSpec(
        operation_id="test.plan",
        name="Plan demo",
        apply=apply,
        verify=verify,
        before=lambda: "Automatic",
        plan_steps=plan_steps,
        preview_text="demo: Automatic -> Disabled",
        verification_method="Win32_Service read-back",
        risk=RiskLevel.MEDIUM,
    )


def test_plan_describes_steps_without_applying(controller: OperationController, audit: AuditLogger) -> None:
    calls: list[str] = []
    spec = _spec_with_steps(calls)
    plan = controller.plan(spec)
    assert calls == []
    assert plan.operation_id == "test.plan"
    assert plan.previous_state == "Automatic"
    assert plan.supported is True
    assert plan.risk is RiskLevel.MEDIUM
    assert [step.kind for step in plan.steps] == ["command", "action"]
    assert plan.steps[0].command == "sc.exe config demo start= disabled"
    assert plan.steps[1].detail == "Win32_Service read-back"
    assert audit.count() == 0
    assert "sc.exe config demo start= disabled" in plan.to_markdown()


def test_plan_marks_irreversible_high_risk_destructive() -> None:
    spec = OperationSpec(
        operation_id="cleanup.demo",
        name="Delete demo cache",
        apply=lambda progress, cancel: ApplyOutcome(success=True),
        risk=RiskLevel.HIGH,
        reversible=False,
        plan_steps=lambda: [action_step("Delete files", destructive=False)],
    )
    plan = build_plan(spec)
    assert plan.destructive is True
    assert plan.to_dict()["destructive"] is True


def test_plan_without_steps_explains_itself() -> None:
    spec = OperationSpec(
        operation_id="setting.demo",
        name="Set demo",
        apply=lambda progress, cancel: ApplyOutcome(success=True),
        preview_text="demo: on -> off",
        verification_method="Registry read-back",
    )
    plan = build_plan(spec)
    assert plan.steps == []
    assert any("No command-level steps are declared" in note for note in plan.notes)


def test_plan_failure_degrades_to_a_note(controller: OperationController) -> None:
    def broken_steps():
        raise RuntimeError("cannot build steps")

    spec = OperationSpec(
        operation_id="test.broken_plan",
        name="Broken plan",
        apply=lambda progress, cancel: ApplyOutcome(success=True),
        plan_steps=broken_steps,
    )
    plan = controller.plan(spec)
    assert plan.steps == []
    assert any("could not be generated" in note for note in plan.notes)
    assert plan.supported is True


def test_plan_reports_unsupported_operation(controller: OperationController) -> None:
    spec = OperationSpec(
        operation_id="test.unsupported",
        name="Unsupported",
        apply=lambda progress, cancel: ApplyOutcome(success=True),
        support_check=lambda: (False, "Requires Windows 11"),
    )
    plan = controller.plan(spec)
    assert plan.supported is False
    assert "Windows 11" in plan.support_reason


def test_plan_document_redacts_secrets(controller: OperationController) -> None:
    spec = OperationSpec(
        operation_id="test.secret",
        name="Secret operation",
        apply=lambda progress, cancel: ApplyOutcome(success=True),
        plan_steps=lambda: [
            command_step(
                ["tool.exe", "--password", "hunter2"],
                title="Sign in with password=hunter2",
            )
        ],
        preview_text="token=abc123",
    )
    document = controller.plan_many([spec], host="LAB-01", profile_name="Baseline")
    payload = document.to_json()
    assert "hunter2" not in payload
    assert "abc123" not in payload
    assert "LAB-01" in payload
    assert "Baseline" in payload
    assert "***REDACTED***" in payload
    assert "test.secret" in document.to_markdown()


def test_dry_run_includes_the_plan(controller: OperationController, audit: AuditLogger) -> None:
    calls: list[str] = []
    result = controller.execute(_spec_with_steps(calls), dry_run=True)
    assert result.status is OperationStatus.SKIPPED
    assert calls == []
    assert result.dry_run is True
    assert result.details["plan"]["steps"][0]["command"] == "sc.exe config demo start= disabled"
    assert result.details["planned_steps"][0] == "sc.exe config demo start= disabled"
    assert result.previous_state == "Automatic"
    assert audit.count() == 1


def test_plan_document_json_round_trip(controller: OperationController) -> None:
    document = controller.plan_many([_spec_with_steps()])
    parsed = json.loads(document.to_json())
    assert parsed["operation_count"] == 1
    assert parsed["highest_risk"] == "medium"
    assert parsed["requires_admin"] is False
    assert parsed["plans"][0]["steps"][0]["argv"][0] == "sc.exe"


def test_plan_step_rejects_unknown_kind() -> None:
    try:
        PlanStep(title="x", kind="execute-everything")
    except ValueError as exc:
        assert "Unsupported plan step kind" in str(exc)
    else:  # pragma: no cover - the constructor must reject it
        raise AssertionError("An unknown step kind must be rejected")
