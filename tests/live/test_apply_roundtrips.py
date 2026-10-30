"""End-to-end apply/verify/restore scenarios for a disposable Windows VM.

Every test uses the production controller and service code, then restores the
state it changed in a ``finally`` block.  Run through
``scripts/acceptance/run_acceptance.ps1``; do not run on a production machine.
"""
from __future__ import annotations

import ctypes
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

import pytest

from winfirst.core.command_runner import CommandRunner
from winfirst.core.operation_result import OperationStatus
from winfirst.core import settings_registry
from winfirst.services.cleanup_service import CleanupTarget, build_cleanup_spec
from winfirst.services.common import run_ok
from winfirst.services.power_service import PowerService
from winfirst.services.restore_point_service import RestorePointService
from winfirst.services.scheduled_task_service import build_toggle_spec, get_scheduled_tasks
from winfirst.services.service_audit_service import build_startup_mode_spec, get_services

pytestmark = pytest.mark.live

SERVICE_NAME = "WinFirstAcceptanceSvc"
TASK_FOLDER = "\\WinFirstAcceptance\\"
TASK_NAME = "WinFirstAcceptanceTask"


def _require_windows() -> None:
    if sys.platform != "win32":
        pytest.skip("Windows is required.")


def _is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _require_admin() -> None:
    if not _is_admin():
        pytest.skip("This scenario requires an elevated shell.")


def test_settings_roundtrip_through_the_registry(controller, evidence) -> None:
    _require_windows()
    key = "show_file_extensions"
    definition = settings_registry.get_definition(key)
    original = definition.detect()
    if not original.supported:
        pytest.skip(f"{key} is not supported on this build: {original.note}")
    before = bool(original.value)
    target = not before
    result = controller.execute(settings_registry.build_spec(key, target))
    try:
        assert result.status is OperationStatus.SUCCESS, result.error
        after = definition.detect()
        assert bool(after.value) is target
        evidence(
            "settings",
            setting=key,
            before=before,
            after=bool(after.value),
            verification=result.verification,
        )
    finally:
        restored = controller.execute(settings_registry.build_spec(key, before))
        assert restored.status is OperationStatus.SUCCESS, restored.error
    assert bool(definition.detect().value) is before


def test_settings_plan_does_not_change_anything(controller, evidence) -> None:
    _require_windows()
    key = "show_hidden_files"
    definition = settings_registry.get_definition(key)
    original = definition.detect()
    if not original.supported:
        pytest.skip(f"{key} is not supported on this build: {original.note}")
    plan = controller.plan(settings_registry.build_spec(key, not bool(original.value)))
    assert definition.detect().value == original.value
    assert plan.steps
    evidence("settings-plan", setting=key, steps=[step.title for step in plan.steps])


def test_power_plan_roundtrip(controller, evidence) -> None:
    _require_windows()
    service = PowerService()
    original_guid = service.active_plan_guid()
    alternatives = [plan for plan in service.list_plans() if plan.guid != original_guid]
    if not alternatives:
        pytest.skip("Only one power plan is installed.")
    target = alternatives[0]
    result = controller.execute(service.build_plan_spec(target))
    try:
        assert result.status is OperationStatus.SUCCESS, result.error
        assert service.active_plan_guid() == target.guid
        evidence(
            "power",
            previous=original_guid,
            target=target.guid,
            target_name=target.name,
            verification=result.verification,
        )
    finally:
        service.activate_plan(original_guid)
    assert service.active_plan_guid() == original_guid


def test_service_startup_roundtrip(controller, evidence) -> None:
    _require_windows()
    _require_admin()
    runner = CommandRunner()
    runner.run(["sc.exe", "delete", SERVICE_NAME], timeout=60)
    run_ok(
        runner,
        [
            "sc.exe",
            "create",
            SERVICE_NAME,
            "binPath=",
            r"C:\Windows\System32\cmd.exe /c exit",
            "start=",
            "demand",
            "DisplayName=",
            "WinFirst acceptance service",
        ],
        timeout=60,
        error_prefix="Could not create the test service",
    )
    try:
        info = get_services().find(SERVICE_NAME)
        assert info is not None, "The test service was created but not listed."
        result = controller.execute(build_startup_mode_spec(info, "disabled"))
        assert result.status is OperationStatus.SUCCESS, result.error
        after = get_services().find(SERVICE_NAME)
        assert after is not None and after.start_mode.lower() == "disabled"
        evidence(
            "service",
            name=SERVICE_NAME,
            before=info.start_mode,
            after=after.start_mode,
            verification=result.verification,
        )
    finally:
        runner.run(["sc.exe", "delete", SERVICE_NAME], timeout=60)
    time.sleep(1)
    assert get_services().find(SERVICE_NAME) is None


def test_scheduled_task_toggle_roundtrip(controller, evidence) -> None:
    _require_windows()
    _require_admin()
    runner = CommandRunner()
    full_name = TASK_FOLDER + TASK_NAME
    runner.run(["schtasks.exe", "/Delete", "/TN", full_name, "/F"], timeout=60)
    run_ok(
        runner,
        [
            "schtasks.exe",
            "/Create",
            "/TN",
            full_name,
            "/SC",
            "ONCE",
            "/ST",
            "23:59",
            "/TR",
            "cmd /c exit",
            "/F",
        ],
        timeout=60,
        error_prefix="Could not create the test task",
    )
    try:
        snapshot = get_scheduled_tasks()
        task = snapshot.find(TASK_FOLDER, TASK_NAME)
        assert task is not None, "The test task was created but not listed."
        result = controller.execute(build_toggle_spec(task, False))
        assert result.status is OperationStatus.SUCCESS, result.error
        disabled = get_scheduled_tasks().find(TASK_FOLDER, TASK_NAME)
        assert disabled is not None and disabled.enabled is False
        enabled_result = controller.execute(build_toggle_spec(disabled, True))
        assert enabled_result.status is OperationStatus.SUCCESS, enabled_result.error
        evidence(
            "task",
            task=full_name,
            disabled_state=disabled.state,
            verification=result.verification,
        )
    finally:
        runner.run(["schtasks.exe", "/Delete", "/TN", full_name, "/F"], timeout=60)
    assert get_scheduled_tasks().find(TASK_FOLDER, TASK_NAME) is None


def test_cleanup_of_a_sandbox_folder(controller, evidence) -> None:
    _require_windows()
    sandbox = Path(tempfile.mkdtemp(prefix="winfirst-acceptance-"))
    try:
        payload = sandbox / "cache.bin"
        payload.write_bytes(b"x" * 8192)
        old = time.time() - 7200
        os.utime(payload, (old, old))
        target = CleanupTarget(
            key="acceptance_temp",
            label="Acceptance sandbox",
            description="Test-only folder created by the acceptance suite.",
            kind="temp",
            paths=[str(sandbox)],
            min_age_hours=1,
            estimated_files=1,
            estimated_bytes=8192,
            available=True,
            warnings=["Test-only target; safe to delete."],
        )
        result = controller.execute(build_cleanup_spec(target, min_age_hours=1))
        assert result.status is OperationStatus.SUCCESS, result.error
        assert not payload.exists()
        assert result.details.get("deleted_files") == 1
        evidence(
            "cleanup",
            path=str(sandbox),
            deleted_files=result.details.get("deleted_files"),
            deleted_bytes=result.details.get("deleted_bytes"),
            verification=result.verification,
        )
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)
    assert not sandbox.exists()


@pytest.mark.slow
def test_restore_point_can_be_created_and_listed(controller, evidence) -> None:
    _require_windows()
    _require_admin()
    service = RestorePointService()
    support = service.support()
    if not support.can_create:
        pytest.skip(support.summary())
    description = f"WinFirst acceptance {os.getpid()}"
    result = controller.execute(service.build_spec(description))
    assert result.status is OperationStatus.SUCCESS, result.error
    points = service.list_points(limit=10)
    listed = [point for point in points if point.description == description]
    created = bool(result.details.get("created"))
    assert listed or not created, "A new checkpoint was created but is not listed."
    evidence(
        "restore",
        description=description,
        created=created,
        newest=points[0].label() if points else "",
        verification=result.verification,
    )
