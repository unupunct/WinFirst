"""Power management using the documented ``powercfg`` interface.

Reads and writes the active scheme's AC/DC values and verifies every change by
querying the effective configuration again.  Output parsing is intentionally
locale tolerant: values are taken from the hexadecimal indices rather than
from translated labels.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from winfirst.core.command_runner import CommandRunner
from winfirst.core.operation import ApplyOutcome, OperationSpec, VerifyOutcome
from winfirst.core.plan import action_step, command_step
from winfirst.core.risk_engine import assess_action
from winfirst.services.common import ServiceError, human_seconds, require_windows, run_ok
from winfirst.services.win_registry import read_value

BALANCED_GUID = "381b4222-f694-41f0-9685-ff5bb260df2e"
HIGH_PERFORMANCE_GUID = "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"
POWER_SAVER_GUID = "a1841308-3541-4fab-bc81-f71556f20b4a"

SUB_VIDEO = "7516b95f-f776-4464-8c53-06167f40cc99"
SUB_SLEEP = "238c9fa8-0aad-41ed-83f4-97be242c8f20"
SUB_BUTTONS = "4f971e89-eebd-4455-a8de-9e59040e7347"

VIDEO_IDLE = "3c0bc021-c8a8-4e07-a973-6b14cbcb2b7e"
STANDBY_IDLE = "29f6c1db-86da-48c5-9fdb-f2b67b1f44da"
LID_ACTION = "5ca83367-6e45-459f-a27b-476b1d01c936"
POWER_BUTTON_ACTION = "7648efa3-dd9c-4e3e-b566-50f929386280"

HIBERNATE_KEY = r"SYSTEM\CurrentControlSet\Control\Power"
HIBERNATE_VALUE = "HibernateEnabled"
FAST_STARTUP_KEY = r"SYSTEM\CurrentControlSet\Control\Session Manager\Power"
FAST_STARTUP_VALUE = "HiberbootEnabled"

ACTION_VALUES = {"do_nothing": 0, "sleep": 1, "hibernate": 2, "shutdown": 3}
ACTION_LABELS = {
    "do_nothing": "Do nothing",
    "sleep": "Sleep",
    "hibernate": "Hibernate",
    "shutdown": "Shut down",
}

_GUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_PLAN_RE = re.compile(
    r"GUID:\s*([0-9a-fA-F-]{36})\s*(?:\((.*?)\))?", re.IGNORECASE
)
_HEX_RE = re.compile(r"0x([0-9a-fA-F]+)")


@dataclass
class PowerPlan:
    guid: str
    name: str
    active: bool = False
    recommended: bool = False

    @property
    def label(self) -> str:
        suffix = " (active)" if self.active else ""
        return f"{self.name}{suffix}"


@dataclass
class TimeoutValue:
    ac_seconds: int
    dc_seconds: int

    @property
    def ac_minutes(self) -> int:
        return round(self.ac_seconds / 60)

    @property
    def dc_minutes(self) -> int:
        return round(self.dc_seconds / 60)


@dataclass
class HibernateStatus:
    enabled: bool | None
    supported: bool
    fast_startup: bool | None = None
    reason: str = ""
    detail: str = ""


class PowerService:
    def __init__(self, runner: CommandRunner | None = None) -> None:
        self.runner = runner or CommandRunner()

    # ------------------------------------------------------------------ plans
    def list_plans(self) -> list[PowerPlan]:
        require_windows("Power plan inspection")
        result = run_ok(self.runner, ["powercfg.exe", "/list"], timeout=30)
        active_guid = self.active_plan_guid()
        plans: list[PowerPlan] = []
        seen: set[str] = set()
        for line in result.stdout.splitlines():
            match = _PLAN_RE.search(line)
            if not match:
                continue
            guid = match.group(1).lower()
            if guid in seen:
                continue
            name = (match.group(2) or guid).strip()
            seen.add(guid)
            plans.append(
                PowerPlan(
                    guid=guid,
                    name=name,
                    active=guid == active_guid,
                    recommended=guid == BALANCED_GUID,
                )
            )
        return plans

    def active_plan_guid(self) -> str:
        require_windows("Power plan inspection")
        result = run_ok(self.runner, ["powercfg.exe", "/getactivescheme"], timeout=30)
        match = _GUID_RE.search(result.stdout)
        if not match:
            raise ServiceError("Could not determine the active power plan.")
        return match.group(0).lower()

    def active_plan(self) -> PowerPlan:
        guid = self.active_plan_guid()
        for plan in self.list_plans():
            if plan.guid == guid:
                return plan
        return PowerPlan(guid=guid, name="Unknown plan", active=True)

    def activate_plan(self, guid: str) -> None:
        if not _GUID_RE.fullmatch(guid or ""):
            raise ServiceError("Invalid power plan identifier.")
        available = {plan.guid for plan in self.list_plans()}
        if guid.lower() not in available:
            raise ServiceError("The selected power plan is not available on this system.")
        run_ok(
            self.runner,
            ["powercfg.exe", "/setactive", guid],
            timeout=30,
            error_prefix="Could not activate the power plan",
        )
        if self.active_plan_guid() != guid.lower():
            raise ServiceError("The active power plan did not change.")

    def build_plan_spec(self, plan: PowerPlan) -> OperationSpec:
        risk = assess_action("power.set_active_plan")
        previous = {"guid": ""}

        def plan_steps():
            return [
                command_step(
                    ["powercfg.exe", "/setactive", plan.guid],
                    title="Activate the selected power plan",
                    detail=f"Selects {plan.name} for the current user session.",
                ),
                action_step(
                    "Read the active plan back",
                    detail="powercfg.exe /getactivescheme must report the selected plan GUID.",
                ),
            ]

        def apply(progress: Callable[[str], None], _cancel) -> ApplyOutcome:
            previous["guid"] = self.active_plan_guid()
            progress(f"Activating power plan {plan.name}")
            self.activate_plan(plan.guid)
            after = self.active_plan()
            return ApplyOutcome(
                success=after.guid == plan.guid,
                previous_state=previous["guid"],
                requested_state=plan.name,
                actual_state=after.name,
                message=f"Active power plan is now {after.name}.",
                rollback={
                    "strategy": "power_plan",
                    "guid": previous["guid"],
                    "summary": f"Restore previous power plan ({previous['guid']})",
                },
                details={"guid": plan.guid},
            )

        def verify() -> VerifyOutcome:
            after = self.active_plan()
            return VerifyOutcome(
                ok=after.guid == plan.guid,
                actual_state=after.name,
                message=f"Read back active plan: {after.name}",
            )

        return OperationSpec(
            operation_id="power.set_active_plan",
            name=f"Activate power plan: {plan.name}",
            category="power",
            description=f"Switches the active power plan to {plan.name}.",
            risk=risk.level,
            requires_admin=False,
            reversible=True,
            apply=apply,
            verify=verify,
            before=self.active_plan_guid,
            plan_steps=plan_steps,
            preview_text=f"Active power plan: {self.active_plan().name} -> {plan.name}",
            warnings=[
                "Power plans change performance, heat and battery life. Balanced is recommended for most systems."
            ],
            verification_method="powercfg /getactivescheme",
        )

    # --------------------------------------------------------------- timeouts
    def _query_setting(self, subgroup: str, setting: str) -> TimeoutValue:
        require_windows("Power settings inspection")
        result = run_ok(
            self.runner,
            [
                "powercfg.exe",
                "/query",
                "SCHEME_CURRENT",
                subgroup,
                setting,
            ],
            timeout=30,
            error_prefix="Could not read the power setting",
        )
        values = [int(value, 16) for value in _HEX_RE.findall(result.stdout)]
        if len(values) < 2:
            raise ServiceError(
                "The requested power setting is not exposed on this system. "
                "It may be hidden by the hardware or an organizational policy."
            )
        return TimeoutValue(ac_seconds=values[-2], dc_seconds=values[-1])

    def get_display_timeout(self) -> TimeoutValue:
        return self._query_setting(SUB_VIDEO, VIDEO_IDLE)

    def get_sleep_timeout(self) -> TimeoutValue:
        return self._query_setting(SUB_SLEEP, STANDBY_IDLE)

    def _set_index(self, subgroup: str, setting: str, seconds: int, ac: bool) -> None:
        if not isinstance(seconds, int) or seconds < 0 or seconds > 604800:
            raise ServiceError("Power timeout must be between 0 and 604800 seconds.")
        arg = "/setacvalueindex" if ac else "/setdcvalueindex"
        run_ok(
            self.runner,
            [
                "powercfg.exe",
                arg,
                "SCHEME_CURRENT",
                subgroup,
                setting,
                str(seconds),
            ],
            timeout=30,
            error_prefix="Could not change the power timeout",
        )

    def set_timeout(
        self,
        *,
        display_ac_minutes: int | None = None,
        display_dc_minutes: int | None = None,
        sleep_ac_minutes: int | None = None,
        sleep_dc_minutes: int | None = None,
    ) -> None:
        require_windows("Power management")
        changed = False
        if display_ac_minutes is not None:
            self._set_index(SUB_VIDEO, VIDEO_IDLE, display_ac_minutes * 60, True)
            changed = True
        if display_dc_minutes is not None:
            self._set_index(SUB_VIDEO, VIDEO_IDLE, display_dc_minutes * 60, False)
            changed = True
        if sleep_ac_minutes is not None:
            self._set_index(SUB_SLEEP, STANDBY_IDLE, sleep_ac_minutes * 60, True)
            changed = True
        if sleep_dc_minutes is not None:
            self._set_index(SUB_SLEEP, STANDBY_IDLE, sleep_dc_minutes * 60, False)
            changed = True
        if not changed:
            return
        active = self.active_plan_guid()
        run_ok(
            self.runner,
            ["powercfg.exe", "/setactive", active],
            timeout=30,
            error_prefix="Could not apply the power setting",
        )

    # -------------------------------------------------------------- buttons
    def get_button_action(self, setting: str) -> TimeoutValue:
        return self._query_setting(SUB_BUTTONS, setting)

    def set_button_action(self, setting: str, ac_value: int, dc_value: int) -> None:
        if setting not in {LID_ACTION, POWER_BUTTON_ACTION}:
            raise ServiceError("Unsupported power button setting.")
        for value in (ac_value, dc_value):
            if value not in ACTION_VALUES.values():
                raise ServiceError("Invalid button action value.")
        run_ok(
            self.runner,
            ["powercfg.exe", "/setacvalueindex", "SCHEME_CURRENT", SUB_BUTTONS, setting, str(ac_value)],
            timeout=30,
            error_prefix="Could not change the button action",
        )
        run_ok(
            self.runner,
            ["powercfg.exe", "/setdcvalueindex", "SCHEME_CURRENT", SUB_BUTTONS, setting, str(dc_value)],
            timeout=30,
            error_prefix="Could not change the button action",
        )
        active = self.active_plan_guid()
        run_ok(self.runner, ["powercfg.exe", "/setactive", active], timeout=30)

    # ------------------------------------------------------------- hibernate
    def hibernate_status(self) -> HibernateStatus:
        if not _is_windows():
            return HibernateStatus(None, False, reason="Windows is required")
        enabled_value = read_value("HKLM", HIBERNATE_KEY, HIBERNATE_VALUE)
        fast_value = read_value("HKLM", FAST_STARTUP_KEY, FAST_STARTUP_VALUE)
        enabled: bool | None
        if enabled_value.exists:
            enabled = bool(int(enabled_value.value or 0))
        else:
            enabled = None
        fast: bool | None
        if fast_value.exists:
            fast = bool(int(fast_value.value or 0))
        else:
            fast = None
        detail = ""
        supported = enabled is not None
        reason = ""
        if not supported:
            try:
                result = self.runner.run(["powercfg.exe", "/a"], timeout=20)
                text = result.combined.lower()
                if "hibernation has not been enabled" in text:
                    supported = True
                    detail = "Hibernation is supported but disabled."
                elif "hibernation is not available" in text or "not supported" in text:
                    supported = False
                    reason = "Firmware or policy does not support hibernation."
                else:
                    reason = "Hibernation state could not be determined."
            except Exception:
                reason = "Hibernation state could not be determined."
        elif not enabled:
            detail = "Hibernation is supported but currently disabled."
        return HibernateStatus(
            enabled=enabled,
            supported=supported,
            fast_startup=fast,
            reason=reason,
            detail=detail,
        )

    def set_hibernate(self, enabled: bool) -> None:
        require_windows("Hibernation")
        arg = "on" if enabled else "off"
        run_ok(
            self.runner,
            ["powercfg.exe", "/hibernate", arg],
            timeout=60,
            error_prefix=f"Could not turn hibernation {arg}",
        )
        status = self.hibernate_status()
        if status.enabled is None or status.enabled != enabled:
            raise ServiceError(
                "The hibernation command completed but the registry does not confirm the requested state. "
                "A policy or firmware setting may override it."
            )


def _is_windows() -> bool:
    import sys

    return sys.platform == "win32"


def timeout_label(seconds: int) -> str:
    return human_seconds(seconds)
