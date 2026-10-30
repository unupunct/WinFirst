"""Deployment checklist templates and sessions.

Templates are declarative JSON.  Tasks are completed by the technician or by a
small set of verifiable automatic checks; an automatic check never marks a
manual task complete.  Session snapshots are stored so later template edits do
not rewrite history.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from winfirst.core import paths, privilege_manager, settings_registry
from winfirst.core.database import Database
from winfirst.core.profile_manager import ProfileManager
from winfirst.models.profile import Profile

_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_]{0,79}$")
_TASK_STATUSES = {"pending", "passed", "failed", "not_applicable", "skipped"}
_TOP_KEYS = {"schema_version", "templates"}
_TEMPLATE_KEYS = {"name", "description", "tasks"}
_TASK_KEYS = {"key", "title", "description", "required", "auto_check"}
_AUTO_TYPES = {"profile_applied", "setting", "setting_value", "capability", "software", "manual"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class ChecklistTask:
    key: str
    title: str
    description: str = ""
    required: bool = True
    auto_check: dict[str, Any] = field(default_factory=dict)
    status: str = "pending"
    detail: str = ""
    updated_at: str = ""

    @property
    def auto_label(self) -> str:
        kind = str(self.auto_check.get("type") or "manual")
        return {
            "profile_applied": "Auto: profile settings match",
            "setting": "Auto: profile setting",
            "setting_value": "Auto: specific value",
            "capability": "Auto: system capability",
            "software": "Auto: software installed",
            "manual": "Manual",
        }.get(kind, "Manual")


@dataclass
class ChecklistTemplate:
    name: str
    description: str = ""
    tasks: list[ChecklistTask] = field(default_factory=list)
    is_builtin: bool = False

    @property
    def required_count(self) -> int:
        return sum(1 for task in self.tasks if task.required)


@dataclass
class DeploymentSession:
    id: int
    template_name: str
    profile_name: str = ""
    status: str = "active"
    notes: str = ""
    started_at: str = ""
    updated_at: str = ""
    completed_at: str = ""
    tasks: list[ChecklistTask] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def required_tasks(self) -> list[ChecklistTask]:
        return [task for task in self.tasks if task.required]

    @property
    def required_passed(self) -> int:
        return sum(1 for task in self.required_tasks if task.status in {"passed", "not_applicable"})

    @property
    def progress_label(self) -> str:
        return f"{self.required_passed}/{len(self.required_tasks)} required tasks complete"

    @property
    def complete(self) -> bool:
        return bool(self.required_tasks) and self.required_passed == len(self.required_tasks)


@dataclass
class AutoCheckResult:
    task_key: str
    checked: bool
    passed: bool = False
    detail: str = ""
    error: str = ""


def templates_path() -> Path:
    return paths.resources_dir() / "checklists" / "templates.json"


def parse_templates(data: Any) -> list[ChecklistTemplate]:
    errors: list[str] = []
    if not isinstance(data, dict):
        raise ValueError("Checklist templates must be a JSON object.")
    if set(data) - _TOP_KEYS:
        errors.append(f"Unsupported top-level fields: {', '.join(sorted(set(data) - _TOP_KEYS))}")
    if data.get("schema_version") != 1:
        errors.append("Checklist template schema_version must be 1.")
    raw_templates = data.get("templates")
    if not isinstance(raw_templates, list):
        raise ValueError("Checklist templates must contain a 'templates' array.")
    templates: list[ChecklistTemplate] = []
    seen_names: set[str] = set()
    for t_index, raw in enumerate(raw_templates):
        prefix = f"templates[{t_index}]"
        if not isinstance(raw, dict):
            errors.append(f"{prefix} must be an object.")
            continue
        if set(raw) - _TEMPLATE_KEYS:
            errors.append(f"{prefix} has unsupported fields: {', '.join(sorted(set(raw) - _TEMPLATE_KEYS))}")
            continue
        name = str(raw.get("name") or "").strip()
        if not name or len(name) > 120:
            errors.append(f"{prefix}.name is required and must be at most 120 characters.")
            continue
        if name.casefold() in seen_names:
            errors.append(f"Duplicate template name: {name}")
            continue
        seen_names.add(name.casefold())
        tasks_raw = raw.get("tasks")
        if not isinstance(tasks_raw, list) or not tasks_raw:
            errors.append(f"{prefix}.tasks must be a non-empty array.")
            continue
        tasks: list[ChecklistTask] = []
        seen_keys: set[str] = set()
        for task_index, task_raw in enumerate(tasks_raw):
            task_prefix = f"{prefix}.tasks[{task_index}]"
            if not isinstance(task_raw, dict):
                errors.append(f"{task_prefix} must be an object.")
                continue
            if set(task_raw) - _TASK_KEYS:
                errors.append(
                    f"{task_prefix} has unsupported fields: {', '.join(sorted(set(task_raw) - _TASK_KEYS))}"
                )
                continue
            key = str(task_raw.get("key") or "")
            if not _KEY_RE.match(key):
                errors.append(f"{task_prefix}.key is invalid.")
                continue
            if key in seen_keys:
                errors.append(f"{task_prefix}.key is duplicated.")
                continue
            seen_keys.add(key)
            title = str(task_raw.get("title") or "").strip()
            if not title or len(title) > 200:
                errors.append(f"{task_prefix}.title is required and must be at most 200 characters.")
                continue
            description = str(task_raw.get("description") or "").strip()
            if len(description) > 1000:
                errors.append(f"{task_prefix}.description is too long.")
                continue
            required = task_raw.get("required", True)
            if not isinstance(required, bool):
                errors.append(f"{task_prefix}.required must be true or false.")
                continue
            auto = task_raw.get("auto_check")
            if auto is None:
                auto_dict: dict[str, Any] = {"type": "manual"}
            elif not isinstance(auto, dict):
                errors.append(f"{task_prefix}.auto_check must be an object.")
                continue
            else:
                auto_dict = dict(auto)
                kind = auto_dict.get("type")
                if kind not in _AUTO_TYPES:
                    errors.append(f"{task_prefix}.auto_check.type is not supported.")
                    continue
                if kind in {"setting", "setting_value"}:
                    setting_key = auto_dict.get("key")
                    if not isinstance(setting_key, str) or not settings_registry.is_supported_key(setting_key):
                        errors.append(f"{task_prefix}.auto_check.key is not a supported setting.")
                        continue
                    if kind == "setting_value":
                        try:
                            auto_dict["value"] = settings_registry.get_definition(setting_key).validate(
                                auto_dict.get("value")
                            )
                        except ValueError as exc:
                            errors.append(f"{task_prefix}.auto_check.value: {exc}")
                            continue
                elif kind == "capability":
                    if auto_dict.get("name") not in {"admin", "winget", "powershell"}:
                        errors.append(f"{task_prefix}.auto_check.name must be admin, winget or powershell.")
                        continue
                elif kind == "software":
                    package_id = auto_dict.get("id")
                    if not isinstance(package_id, str) or not re.match(
                        r"^[A-Za-z0-9][A-Za-z0-9._+\-]{0,199}$", package_id
                    ):
                        errors.append(f"{task_prefix}.auto_check.id is invalid.")
                        continue
            tasks.append(
                ChecklistTask(
                    key=key,
                    title=title,
                    description=description,
                    required=required,
                    auto_check=auto_dict,
                )
            )
        templates.append(
            ChecklistTemplate(
                name=name,
                description=str(raw.get("description") or "").strip()[:1000],
                tasks=tasks,
                is_builtin=False,
            )
        )
    if errors:
        raise ValueError("; ".join(errors[:8]))
    return templates


def load_builtin_templates() -> list[ChecklistTemplate]:
    path = templates_path()
    if not path.is_file():
        raise ValueError(f"Checklist templates are missing: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read checklist templates: {exc}") from exc
    templates = parse_templates(data)
    for template in templates:
        template.is_builtin = True
    return templates


class DeploymentService:
    def __init__(self, db: Database, profiles: ProfileManager) -> None:
        self.db = db
        self.profiles = profiles

    # -------------------------------------------------------------- templates
    def ensure_templates(self) -> int:
        templates = load_builtin_templates()
        added = 0
        for template in templates:
            existing = self.db.query_one("SELECT id FROM checklist_templates WHERE name = ?", (template.name,))
            data = json.dumps(template_to_dict(template), ensure_ascii=False)
            if existing:
                self.db.execute(
                    "UPDATE checklist_templates SET description = ?, is_builtin = 1, data_json = ?, updated_at = ? WHERE name = ?",
                    (template.description, data, _now(), template.name),
                )
            else:
                self.db.execute(
                    "INSERT INTO checklist_templates(name, description, is_builtin, data_json, created_at, updated_at) VALUES (?, ?, 1, ?, ?, ?)",
                    (template.name, template.description, data, _now(), _now()),
                )
                added += 1
        return added

    def list_templates(self) -> list[ChecklistTemplate]:
        if not self.db.scalar("SELECT COUNT(*) FROM checklist_templates", default=0):
            self.ensure_templates()
        rows = self.db.query("SELECT * FROM checklist_templates ORDER BY is_builtin DESC, name COLLATE NOCASE")
        templates: list[ChecklistTemplate] = []
        for row in rows:
            try:
                data = json.loads(row.get("data_json") or "{}")
            except json.JSONDecodeError:
                continue
            try:
                parsed = parse_templates({"schema_version": 1, "templates": [data]})
            except ValueError:
                continue
            template = parsed[0]
            template.is_builtin = bool(row.get("is_builtin"))
            templates.append(template)
        return templates

    def get_template(self, name: str) -> ChecklistTemplate:
        for template in self.list_templates():
            if template.name.casefold() == name.casefold():
                return template
        raise KeyError(f"Checklist template not found: {name}")

    # --------------------------------------------------------------- sessions
    def create_session(self, template_name: str, profile_name: str = "") -> DeploymentSession:
        template = self.get_template(template_name)
        if profile_name:
            self.profiles.get_profile(profile_name)  # raises if missing
        data = {
            "template": template_to_dict(template),
            "profile_name": profile_name,
        }
        session_id = self.db.execute(
            """
            INSERT INTO deployment_sessions(template_name, profile_name, status, notes, data_json, started_at, updated_at)
            VALUES (?, ?, 'active', '', ?, ?, ?)
            """,
            (template.name, profile_name, json.dumps(data, ensure_ascii=False), _now(), _now()),
        )
        for task in template.tasks:
            self.db.execute(
                """
                INSERT INTO deployment_task_states(session_id, task_key, title, status, detail, updated_at)
                VALUES (?, ?, ?, 'pending', '', ?)
                """,
                (session_id, task.key, task.title, _now()),
            )
        session = self.get_session(session_id)
        assert session is not None
        return session

    def list_sessions(self, status: str | None = None) -> list[dict[str, Any]]:
        where = "WHERE status = ?" if status else ""
        params: tuple[Any, ...] = (status,) if status else ()
        return self.db.query(
            f"SELECT * FROM deployment_sessions {where} ORDER BY updated_at DESC, id DESC", params
        )

    def get_session(self, session_id: int) -> DeploymentSession | None:
        row = self.db.query_one("SELECT * FROM deployment_sessions WHERE id = ?", (int(session_id),))
        if not row:
            return None
        try:
            data = json.loads(row.get("data_json") or "{}")
        except json.JSONDecodeError:
            data = {}
        template_data = data.get("template") if isinstance(data.get("template"), dict) else {}
        template = ChecklistTemplate(
            name=str(template_data.get("name") or row.get("template_name") or ""),
            description=str(template_data.get("description") or ""),
            tasks=[],
        )
        try:
            parsed = parse_templates({"schema_version": 1, "templates": [template_data]})
            if parsed:
                template = parsed[0]
        except ValueError:
            pass
        by_key = {task.key: task for task in template.tasks}
        rows = self.db.query(
            "SELECT * FROM deployment_task_states WHERE session_id = ? ORDER BY id", (int(session_id),)
        )
        tasks: list[ChecklistTask] = []
        for task_row in rows:
            key = str(task_row.get("task_key") or "")
            definition = by_key.get(key)
            if definition:
                task = ChecklistTask(
                    key=definition.key,
                    title=definition.title,
                    description=definition.description,
                    required=definition.required,
                    auto_check=definition.auto_check,
                )
            else:
                task = ChecklistTask(key=key, title=str(task_row.get("title") or key), required=False)
            task.status = str(task_row.get("status") or "pending")
            task.detail = str(task_row.get("detail") or "")
            task.updated_at = str(task_row.get("updated_at") or "")
            tasks.append(task)
        return DeploymentSession(
            id=int(row.get("id") or 0),
            template_name=str(row.get("template_name") or ""),
            profile_name=str(row.get("profile_name") or ""),
            status=str(row.get("status") or "active"),
            notes=str(row.get("notes") or ""),
            started_at=str(row.get("started_at") or ""),
            updated_at=str(row.get("updated_at") or ""),
            completed_at=str(row.get("completed_at") or ""),
            tasks=tasks,
            data=data,
        )

    def update_task(self, session_id: int, task_key: str, status: str, detail: str = "") -> None:
        if status not in _TASK_STATUSES:
            raise ValueError(f"Unsupported task status: {status}")
        row = self.db.query_one(
            "SELECT id FROM deployment_task_states WHERE session_id = ? AND task_key = ?",
            (int(session_id), task_key),
        )
        if not row:
            raise KeyError(f"Checklist task not found: {task_key}")
        self.db.execute(
            "UPDATE deployment_task_states SET status = ?, detail = ?, updated_at = ? WHERE session_id = ? AND task_key = ?",
            (status, detail[:2000], _now(), int(session_id), task_key),
        )
        self.db.execute(
            "UPDATE deployment_sessions SET updated_at = ? WHERE id = ?", (_now(), int(session_id))
        )

    def update_notes(self, session_id: int, notes: str) -> None:
        self.db.execute(
            "UPDATE deployment_sessions SET notes = ?, updated_at = ? WHERE id = ?",
            (notes[:8000], _now(), int(session_id)),
        )

    def complete_session(self, session_id: int, *, force: bool = False) -> DeploymentSession:
        session = self.get_session(session_id)
        if session is None:
            raise KeyError("Deployment session not found.")
        if not force and not session.complete:
            incomplete = [
                task.title
                for task in session.required_tasks
                if task.status not in {"passed", "not_applicable"}
            ]
            raise ValueError(
                "Required checklist tasks are incomplete: " + ", ".join(incomplete[:8])
            )
        self.db.execute(
            "UPDATE deployment_sessions SET status = 'completed', completed_at = ?, updated_at = ? WHERE id = ?",
            (_now(), _now(), int(session_id)),
        )
        updated = self.get_session(session_id)
        assert updated is not None
        return updated

    def reopen_session(self, session_id: int) -> None:
        self.db.execute(
            "UPDATE deployment_sessions SET status = 'active', completed_at = '', updated_at = ? WHERE id = ?",
            (_now(), int(session_id)),
        )

    def delete_session(self, session_id: int) -> None:
        self.db.execute("DELETE FROM deployment_sessions WHERE id = ?", (int(session_id),))

    # ------------------------------------------------------------- auto-checks
    def auto_check_session(self, session_id: int) -> list[AutoCheckResult]:
        session = self.get_session(session_id)
        if session is None:
            raise KeyError("Deployment session not found.")
        results: list[AutoCheckResult] = []
        for task in session.tasks:
            result = self._check_task(task, session)
            results.append(result)
            if result.checked:
                self.update_task(
                    session_id,
                    task.key,
                    "passed" if result.passed else "failed",
                    result.detail or result.error,
                )
        return results

    def _check_task(self, task: ChecklistTask, session: DeploymentSession) -> AutoCheckResult:
        kind = str(task.auto_check.get("type") or "manual")
        if kind == "manual":
            return AutoCheckResult(task.key, False, detail="Manual task; no automatic check is available.")
        try:
            if kind in {"profile_applied", "setting", "setting_value"}:
                if not session.profile_name:
                    return AutoCheckResult(task.key, False, detail="No configuration profile is attached to this session.")
                try:
                    profile = self.profiles.get_profile(session.profile_name)
                except KeyError:
                    return AutoCheckResult(task.key, False, error="The attached profile no longer exists.")
                if kind == "profile_applied":
                    if not profile.settings:
                        return AutoCheckResult(task.key, False, detail="The profile does not define any settings.")
                    detected = settings_registry.detect_all(profile.settings.keys())
                    failures = []
                    for key, expected in profile.settings.items():
                        actual = detected.get(key)
                        if actual is None or not actual.supported or not settings_registry.values_match(expected, actual.value):
                            label = settings_registry.get_definition(key).label
                            failures.append(label)
                    passed = not failures
                    return AutoCheckResult(
                        task.key,
                        True,
                        passed,
                        (
                            "All profile settings match the current system."
                            if passed
                            else "Settings that differ: " + ", ".join(failures[:10])
                        ),
                    )
                key = str(task.auto_check.get("key") or "")
                expected = (
                    task.auto_check.get("value")
                    if kind == "setting_value"
                    else profile.settings.get(key)
                )
                if expected is None:
                    return AutoCheckResult(task.key, False, detail=f"The profile does not define {key}.")
                detected = settings_registry.detect_all([key]).get(key)
                if detected is None or not detected.supported:
                    return AutoCheckResult(task.key, True, False, detail=f"{key} could not be detected.")
                passed = settings_registry.values_match(expected, detected.value)
                return AutoCheckResult(
                    task.key,
                    True,
                    passed,
                    f"{key}: {'matches' if passed else 'differs from'} the expected value.",
                )
            if kind == "capability":
                name = str(task.auto_check.get("name") or "")
                if name == "admin":
                    elevated = privilege_manager.is_elevated()
                    return AutoCheckResult(task.key, True, elevated, "Running elevated." if elevated else "Not elevated.")
                if name == "winget":
                    from winfirst.services.software_install_service import find_winget

                    status = find_winget()
                    return AutoCheckResult(task.key, True, status.available, status.summary)
                if name == "powershell":
                    from winfirst.core.command_runner import CommandRunner

                    return AutoCheckResult(
                        task.key, True, CommandRunner().powershell_available,
                        "Windows PowerShell is available." if CommandRunner().powershell_available else "Windows PowerShell is unavailable.",
                    )
                return AutoCheckResult(task.key, False, error="Unknown capability check.")
            if kind == "software":
                from winfirst.services.software_catalog_service import load_catalog
                from winfirst.services.software_install_service import check_installed

                package_id = str(task.auto_check.get("id") or "")
                catalog = load_catalog()
                package = catalog.find(package_id)
                if package is None:
                    return AutoCheckResult(task.key, False, error=f"Package {package_id} is not in the catalog.")
                check = check_installed(package)
                if check.error:
                    return AutoCheckResult(task.key, False, error=check.error)
                return AutoCheckResult(
                    task.key,
                    True,
                    check.installed,
                    f"{package.name}: {'installed' if check.installed else 'not installed'}.",
                )
            return AutoCheckResult(task.key, False, error=f"Unsupported auto-check type: {kind}")
        except Exception as exc:
            return AutoCheckResult(task.key, False, error=str(exc))


def template_to_dict(template: ChecklistTemplate) -> dict[str, Any]:
    return {
        "name": template.name,
        "description": template.description,
        "tasks": [
            {
                "key": task.key,
                "title": task.title,
                "description": task.description,
                "required": task.required,
                "auto_check": dict(task.auto_check or {"type": "manual"}),
            }
            for task in template.tasks
        ],
    }


def session_export_document(session: DeploymentSession) -> dict[str, Any]:
    return {
        "id": session.id,
        "template": session.template_name,
        "profile": session.profile_name,
        "status": session.status,
        "started_at": session.started_at,
        "completed_at": session.completed_at,
        "notes": session.notes,
        "tasks": [
            {
                "key": task.key,
                "title": task.title,
                "required": task.required,
                "status": task.status,
                "detail": task.detail,
                "updated_at": task.updated_at,
            }
            for task in session.tasks
        ],
    }
