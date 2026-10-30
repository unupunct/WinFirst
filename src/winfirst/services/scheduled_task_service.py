"""Scheduled task inventory and carefully controlled enable/disable."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from winfirst.core.command_runner import CommandRunner, as_list, ps_quote
from winfirst.core.operation import ApplyOutcome, OperationSpec, VerifyOutcome
from winfirst.core.plan import action_step
from winfirst.core.risk_engine import RiskLevel
from winfirst.services.common import ps_json, require_windows

_TASK_PATH_RE = re.compile(r"^\\[A-Za-z0-9 _.\-\\]{0,500}$")
_TASK_NAME_RE = re.compile(r"^[A-Za-z0-9 _.\-()]{1,200}$")
_USER_WRITABLE = ("\\appdata\\", "\\temp\\", "\\users\\public\\", "\\downloads\\")

_SCRIPT = r"""
try {
    $items = @(Get-ScheduledTask -ErrorAction Stop | ForEach-Object {
        $triggers = @($_.Triggers | ForEach-Object { [string]$_.CimClass.CimClassName })
        $actions = @($_.Actions | ForEach-Object {
            [pscustomobject]@{ Execute = [string]$_.Execute; Arguments = [string]$_.Arguments; WorkingDirectory = [string]$_.WorkingDirectory }
        })
        [pscustomobject]@{
            Path = [string]$_.TaskPath; Name = [string]$_.TaskName; State = [string]$_.State;
            Author = [string]$_.Author; Description = [string]$_.Description;
            Triggers = $triggers; Actions = $actions
        }
    })
    $items | ConvertTo-Json -Depth 6 -Compress
} catch { '' }
"""


@dataclass
class ScheduledTask:
    path: str
    name: str
    state: str
    author: str = ""
    description: str = ""
    triggers: list[str] = field(default_factory=list)
    actions: list[dict[str, str]] = field(default_factory=list)
    protected: bool = False
    protection_reason: str = ""
    review_note: str = ""

    @property
    def full_name(self) -> str:
        return f"{self.path}{self.name}"

    @property
    def enabled(self) -> bool:
        return self.state.lower() != "disabled"

    @property
    def executable(self) -> str:
        return self.actions[0].get("Execute", "") if self.actions else ""

    @property
    def trigger_summary(self) -> str:
        if not self.triggers:
            return "No triggers"
        return ", ".join(
            trigger.replace("MSFT_Task", "").replace("Trigger", "") or "Unknown"
            for trigger in self.triggers[:3]
        )

    @property
    def action_summary(self) -> str:
        if not self.actions:
            return "No actions"
        action = self.actions[0]
        text = action.get("Execute", "")
        arguments = action.get("Arguments", "")
        if arguments:
            text += " " + arguments
        return text[:300]


@dataclass
class TaskSnapshot:
    tasks: list[ScheduledTask] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def find(self, path: str, name: str) -> ScheduledTask | None:
        for task in self.tasks:
            if task.path == path and task.name == name:
                return task
        return None

    @property
    def review_items(self) -> list[ScheduledTask]:
        return [task for task in self.tasks if task.review_note]

    def logon_tasks(self) -> list[ScheduledTask]:
        return [
            task
            for task in self.tasks
            if any("Logon" in trigger or "Boot" in trigger for trigger in task.triggers)
        ]


def get_scheduled_tasks(runner: CommandRunner | None = None) -> TaskSnapshot:
    require_windows("Scheduled task inventory")
    runner = runner or CommandRunner()
    try:
        data = ps_json(runner, _SCRIPT, timeout=120, default=[])
    except Exception as exc:
        return TaskSnapshot(errors=[str(exc)])
    tasks: list[ScheduledTask] = []
    for row in as_list(data):
        path = str(row.get("Path") or "\\")
        name = str(row.get("Name") or "").strip()
        if not name:
            continue
        actions = []
        for action in row.get("Actions") or []:
            if isinstance(action, dict):
                actions.append(
                    {
                        "Execute": str(action.get("Execute") or ""),
                        "Arguments": str(action.get("Arguments") or ""),
                        "WorkingDirectory": str(action.get("WorkingDirectory") or ""),
                    }
                )
        protected = path.lower().startswith("\\microsoft\\windows\\")
        reason = "Protected Microsoft Windows task" if protected else ""
        review = ""
        executable = actions[0].get("Execute", "").lower() if actions else ""
        if executable and any(fragment in executable for fragment in _USER_WRITABLE):
            review = "Action runs from a user-writable location; review the task publisher and purpose."
        if not actions:
            review = review or "Task has no readable action."
        trigger_list = [str(item) for item in (row.get("Triggers") or [])]
        tasks.append(
            ScheduledTask(
                path=path,
                name=name,
                state=str(row.get("State") or ""),
                author=str(row.get("Author") or ""),
                description=str(row.get("Description") or ""),
                triggers=trigger_list,
                actions=actions,
                protected=protected,
                protection_reason=reason,
                review_note=review,
            )
        )
    tasks.sort(key=lambda item: (item.path.casefold(), item.name.casefold()))
    return TaskSnapshot(tasks=tasks)


def build_toggle_spec(task: ScheduledTask, enable: bool) -> OperationSpec:
    if task.protected:
        raise ValueError("Protected Windows tasks cannot be changed by WinFirst.")
    if not _TASK_PATH_RE.match(task.path or ""):
        raise ValueError("The task path contains unsupported characters.")
    if not _TASK_NAME_RE.match(task.name or ""):
        raise ValueError("The task name contains unsupported characters.")
    action = "enable" if enable else "disable"
    tool = "Enable-ScheduledTask" if enable else "Disable-ScheduledTask"

    def plan_steps():
        return [
            action_step(
                f"{tool} -TaskPath {task.path} -TaskName {task.name}",
                detail="The task is toggled, never deleted; the state is read back with Get-ScheduledTask.",
            )
        ]

    def apply(progress, cancel) -> ApplyOutcome:
        runner = CommandRunner()
        progress(f"{action.capitalize()}ing task {task.full_name}")
        script = (
            f"{tool} -TaskPath {ps_quote(task.path)} -TaskName {ps_quote(task.name)} "
            "-ErrorAction Stop | Out-Null"
        )
        result = runner.powershell(script, timeout=90, cancel_event=cancel)
        if not result.launched:
            raise RuntimeError(result.error or "PowerShell could not be started.")
        if result.exit_code != 0:
            raise RuntimeError((result.stderr or result.stdout).strip()[:800])
        return ApplyOutcome(
            success=True,
            previous_state="Enabled" if task.enabled else "Disabled",
            requested_state="Enabled" if enable else "Disabled",
            actual_state="Change applied; verification pending",
            warnings=[
                "The task was not deleted; it can be re-enabled later.",
                "Do not disable tasks based only on an unfamiliar name.",
            ],
            rollback={
                "strategy": "task_toggle",
                "path": task.path,
                "name": task.name,
                "previous": task.enabled,
                "summary": f"Restore task {task.full_name}",
            },
            details={"task": task.full_name},
        )

    def verify() -> VerifyOutcome:
        snapshot = get_scheduled_tasks()
        current = snapshot.find(task.path, task.name)
        if current is None:
            return VerifyOutcome(False, "Unknown", "The task could not be read back.")
        return VerifyOutcome(
            ok=current.enabled == enable,
            actual_state=current.state,
            message=f"Read back task state: {current.state}",
        )

    return OperationSpec(
        operation_id=f"task.{action}",
        name=f"{action.capitalize()} scheduled task: {task.name}",
        category="services",
        description=f"{action.capitalize()}s the scheduled task {task.full_name}.",
        risk=RiskLevel.MEDIUM,
        requires_admin=task.path.startswith("\\Microsoft") is False,
        reversible=True,
        apply=apply,
        verify=verify,
        before=lambda: task.state,
        plan_steps=plan_steps,
        preview_text=f"{task.full_name}: {task.state} -> {'Enabled' if enable else 'Disabled'}",
        warnings=[
            "Disabling a task can stop maintenance, updates or vendor functionality.",
            "WinFirst never deletes scheduled tasks.",
        ],
        verification_method="Get-ScheduledTask state read-back",
        details={"task": task.full_name},
    )
