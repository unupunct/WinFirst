"""Rollback of supported, reversible operations.

WinFirst only offers rollback when a previous state was recorded and a safe
inverse operation exists.  Removed applications, deleted files, driver changes
and other irreversible actions are never presented as rollback-capable.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

from winfirst.core import settings_registry
from winfirst.core.audit_logger import AuditLogger
from winfirst.core.operation import OperationController, OperationSpec
from winfirst.core.operation_result import OperationResult, unsupported_result
from winfirst.core.risk_engine import RiskLevel
from winfirst.core.settings_registry import build_spec as build_setting_spec
from winfirst.services import godmode_service, power_service

_log = logging.getLogger("winfirst.rollback")

Handler = Callable[[dict[str, Any]], OperationSpec]
_HANDLERS: dict[str, Handler] = {}


def register_handler(strategy: str, handler: Handler) -> None:
    _HANDLERS[strategy] = handler


@dataclass
class RollbackPreview:
    entry_id: int
    summary: str
    strategy: str
    available: bool
    reason: str = ""


def _setting_handler(payload: dict[str, Any]) -> OperationSpec:
    key = str(payload.get("key", ""))
    if not key:
        raise ValueError("The rollback entry has no setting key.")
    if "applied" in payload:
        definition = settings_registry.get_definition(key)
        current = definition.detect()
        if current.supported and not settings_registry.values_match(payload["applied"], current.value):
            raise ValueError(
                f"{definition.label} has changed since WinFirst applied it "
                f"(now {definition.format_value(current.value)}); rolling back would overwrite that change."
            )
    spec = build_setting_spec(key, payload.get("previous"))
    return dataclasses.replace(
        spec,
        operation_id=f"rollback.setting.{key}",
        name=f"Rollback: {spec.name}",
        description=f"Restore the previously recorded value for {key}.",
        risk=RiskLevel.MEDIUM,
        reversible=False,
        warnings=list(spec.warnings) + ["This restores a recorded value; it does not undo unrelated changes."],
    )


def _power_plan_handler(payload: dict[str, Any]) -> OperationSpec:
    guid = str(payload.get("guid", ""))
    if not guid:
        raise ValueError("The rollback entry has no power plan identifier.")
    service = power_service.PowerService()
    plans = service.list_plans()
    for plan in plans:
        if plan.guid == guid.lower():
            spec = service.build_plan_spec(plan)
            return dataclasses.replace(
                spec,
                operation_id="rollback.power_plan",
                name=f"Rollback: {spec.name}",
                reversible=False,
            )
    raise ValueError("The previous power plan is no longer available on this system.")


def _godmode_handler(payload: dict[str, Any]) -> OperationSpec:
    action = str(payload.get("action", ""))
    if action == "remove":
        spec = godmode_service.build_remove_spec(force=False)
    elif action == "create":
        spec = godmode_service.build_create_spec()
    else:
        raise ValueError("Unknown God Mode rollback action.")
    return dataclasses.replace(
        spec,
        operation_id=f"rollback.godmode.{action}",
        name=f"Rollback: {spec.name}",
        reversible=False,
    )


def _environment_handler(payload: dict[str, Any]) -> OperationSpec:
    from winfirst.services import environment_service

    return environment_service.build_restore_spec(payload)


def _user_account_handler(payload: dict[str, Any]) -> OperationSpec:
    from winfirst.services import users_service

    name = str(payload.get("name", ""))
    if not name:
        raise ValueError("The account rollback record is incomplete.")
    snapshot = users_service.get_local_users()
    user = snapshot.find(name)
    if user is None:
        raise ValueError(f"Local account {name} no longer exists.")
    spec = users_service.build_toggle_user_spec(user, bool(payload.get("previous", True)), snapshot)
    return dataclasses.replace(
        spec,
        operation_id=f"rollback.user.{name}",
        name=f"Rollback: {spec.name}",
        reversible=False,
    )


def _startup_handler(payload: dict[str, Any]) -> OperationSpec:
    from winfirst.services import startup_service

    hive = str(payload.get("hive", "HKCU"))
    approved_path = str(payload.get("approved_path", ""))
    value_name = str(payload.get("value_name", ""))
    if not approved_path or not value_name:
        raise ValueError("The startup rollback record is incomplete.")
    entry = startup_service.StartupEntry(
        id="rollback",
        name=str(payload.get("entry_name") or value_name),
        command="",
        source="StartupApproved",
        scope="All users" if hive == "HKLM" else "Current user",
        enabled=bool(payload.get("previous", True)),
        hive=hive,
        approved_path=approved_path,
        value_name=value_name,
    )
    spec = startup_service.build_toggle_spec(entry, bool(payload.get("previous", True)))
    return dataclasses.replace(
        spec,
        operation_id="rollback.startup",
        name=f"Rollback: {spec.name}",
        reversible=False,
    )


def _service_startup_handler(payload: dict[str, Any]) -> OperationSpec:
    from winfirst.services import service_audit_service

    name = str(payload.get("name", ""))
    mode = str(payload.get("previous", ""))
    if not name or not mode:
        raise ValueError("The service rollback record is incomplete.")
    snapshot = service_audit_service.get_services()
    service = snapshot.find(name)
    if service is None:
        raise ValueError(f"Service {name} no longer exists.")
    spec = service_audit_service.build_startup_mode_spec(service, mode)
    return dataclasses.replace(
        spec,
        operation_id=f"rollback.service.startup.{name}",
        name=f"Rollback: {spec.name}",
        reversible=False,
    )


def _service_state_handler(payload: dict[str, Any]) -> OperationSpec:
    from winfirst.services import service_audit_service

    name = str(payload.get("name", ""))
    previous = str(payload.get("previous_state", ""))
    if not name or not previous:
        raise ValueError("The service rollback record is incomplete.")
    snapshot = service_audit_service.get_services()
    service = snapshot.find(name)
    if service is None:
        raise ValueError(f"Service {name} no longer exists.")
    start = previous.lower() == "running"
    spec = service_audit_service.build_start_stop_spec(service, start)
    return dataclasses.replace(
        spec,
        operation_id=f"rollback.service.state.{name}",
        name=f"Rollback: {spec.name}",
        reversible=False,
    )


def _task_toggle_handler(payload: dict[str, Any]) -> OperationSpec:
    from winfirst.services import scheduled_task_service

    path = str(payload.get("path", ""))
    name = str(payload.get("name", ""))
    if not path or not name:
        raise ValueError("The scheduled task rollback record is incomplete.")
    snapshot = scheduled_task_service.get_scheduled_tasks()
    task = snapshot.find(path, name)
    if task is None:
        raise ValueError(f"Scheduled task {path}{name} no longer exists.")
    spec = scheduled_task_service.build_toggle_spec(task, bool(payload.get("previous", True)))
    return dataclasses.replace(
        spec,
        operation_id=f"rollback.task.{name}",
        name=f"Rollback: {spec.name}",
        reversible=False,
    )


def _software_install_handler(payload: dict[str, Any]) -> OperationSpec:
    from winfirst.services import software_install_service

    package_id = str(payload.get("package", ""))
    name = str(payload.get("name") or package_id)
    if not package_id:
        raise ValueError("The install rollback record is incomplete.")
    spec = software_install_service.build_uninstall_spec(package_id, name)
    return dataclasses.replace(
        spec,
        operation_id=f"rollback.software.{package_id}",
        name=f"Rollback: {spec.name}",
        reversible=False,
    )


register_handler("setting", _setting_handler)
register_handler("software_install", _software_install_handler)
register_handler("power_plan", _power_plan_handler)
register_handler("godmode", _godmode_handler)
register_handler("startup_toggle", _startup_handler)
register_handler("service_startup", _service_startup_handler)
register_handler("service_start_stop", _service_state_handler)
register_handler("task_toggle", _task_toggle_handler)
register_handler("env_set", _environment_handler)
register_handler("user_account", _user_account_handler)


class RollbackManager:
    PREVIEW_TTL_SECONDS = 30.0

    def __init__(self, audit: AuditLogger, controller: OperationController) -> None:
        self.audit = audit
        self.controller = controller
        self._preview_cache: dict[int, tuple[float, RollbackPreview]] = {}

    def available_entries(self) -> list[dict[str, Any]]:
        return self.audit.rollback_entries(available_only=True)

    def preview(self, entry: dict[str, Any]) -> RollbackPreview:
        # Handlers query live system state, so reuse a recent answer instead of
        # re-running those queries each time the history page redraws.
        entry_id = int(entry.get("id", 0))
        cached = self._preview_cache.get(entry_id)
        now = time.monotonic()
        if cached and now - cached[0] < self.PREVIEW_TTL_SECONDS:
            return cached[1]
        preview = self._build_preview(entry)
        self._preview_cache[entry_id] = (now, preview)
        return preview

    def _build_preview(self, entry: dict[str, Any]) -> RollbackPreview:
        try:
            payload = json.loads(entry.get("payload_json") or "{}")
        except json.JSONDecodeError:
            return RollbackPreview(
                int(entry.get("id", 0)), "Unreadable rollback record", "unknown", False,
                "The stored rollback data is invalid.",
            )
        strategy = str(payload.get("strategy") or entry.get("strategy") or "")
        handler = _HANDLERS.get(strategy)
        summary = str(entry.get("summary") or payload.get("summary") or "Restore previous state")
        if handler is None:
            return RollbackPreview(
                int(entry.get("id", 0)), summary, strategy, False,
                f"Rollback strategy '{strategy}' is not supported by this build.",
            )
        try:
            handler(payload)
        except Exception as exc:
            return RollbackPreview(
                int(entry.get("id", 0)), summary, strategy, False, str(exc)
            )
        return RollbackPreview(int(entry.get("id", 0)), summary, strategy, True)

    def build_spec(self, entry: dict[str, Any]) -> OperationSpec:
        if str(entry.get("status")) != "available":
            raise ValueError("This rollback entry is no longer available.")
        payload = json.loads(entry.get("payload_json") or "{}")
        strategy = str(payload.get("strategy") or entry.get("strategy") or "")
        handler = _HANDLERS.get(strategy)
        if handler is None:
            raise ValueError(f"Rollback strategy '{strategy}' is not supported.")
        return handler(payload)

    def restore(
        self,
        entry: dict[str, Any],
        *,
        progress: Callable[[str], None] | None = None,
        cancel_event=None,
    ) -> OperationResult:
        try:
            spec = self.build_spec(entry)
        except Exception as exc:
            return unsupported_result(
                "rollback.unsupported",
                "Rollback unavailable",
                str(exc),
                category="rollback",
                risk="medium",
            )
        result = self.controller.execute(
            spec, progress=progress, cancel_event=cancel_event, profile_name="rollback"
        )
        entry_id = int(entry.get("id", 0))
        self._preview_cache.pop(entry_id, None)
        if result.status.is_success:
            self.audit.mark_restored(entry_id, success=True, note=result.summary())
        else:
            _log.warning("Rollback %s did not succeed: %s", entry_id, result.error)
        return result
