"""User and machine environment variable inspection and controlled editing.

Values for variables whose names look like credentials are never displayed,
logged or stored in the audit database.  Changes to machine scope require
administrator rights.  Core operating-system variables are protected from
deletion.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from winfirst.core.operation import ApplyOutcome, OperationSpec, VerifyOutcome
from winfirst.core.redaction import should_redact_env_name
from winfirst.core.risk_engine import RiskLevel
from winfirst.services import win_registry
from winfirst.services.common import require_windows

USER_ENV_KEY = r"Environment"
MACHINE_ENV_KEY = r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"
_NAME_RE = re.compile(r"^[^=\x00-\x1f]{1,255}$")
_PROTECTED_NAMES = {
    "systemroot",
    "windir",
    "comspec",
    "pathext",
    "programfiles",
    "programfiles(x86)",
    "programw6432",
    "programdata",
    "userprofile",
    "appdata",
    "localappdata",
    "public",
    "allusersprofile",
    "processor_architecture",
    "processor_identifier",
    "os",
    "number_of_processors",
}
_PATH_NAMES = {"path"}


@dataclass
class EnvironmentVariable:
    name: str
    scope: str
    value: Any = None
    value_type: str = ""
    secret: bool = False
    error: str = ""
    multi_value: bool = False

    @property
    def display_value(self) -> str:
        if self.error:
            return f"<unreadable: {self.error}>"
        if self.secret:
            return "(hidden: variable name looks sensitive)"
        if isinstance(self.value, (list, tuple)):
            return "; ".join(str(item) for item in self.value)
        if self.value is None:
            return ""
        return str(self.value)

    @property
    def scope_label(self) -> str:
        return "Current user" if self.scope == "user" else "All users (machine)"

    @property
    def editable(self) -> bool:
        return not self.secret

    @property
    def deletable(self) -> bool:
        return self.name.casefold() not in _PROTECTED_NAMES


@dataclass
class EnvironmentSnapshot:
    variables: list[EnvironmentVariable] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def find(self, scope: str, name: str) -> EnvironmentVariable | None:
        for item in self.variables:
            if item.scope == scope and item.name.casefold() == name.casefold():
                return item
        return None


def _key_for(scope: str) -> tuple[str, str]:
    normalized = scope.strip().lower()
    if normalized in {"user", "current user", "hkcu"}:
        return "HKCU", USER_ENV_KEY
    if normalized in {"machine", "system", "all users", "hklm"}:
        return "HKLM", MACHINE_ENV_KEY
    raise ValueError("Environment scope must be 'user' or 'machine'.")


def get_environment_variables() -> EnvironmentSnapshot:
    require_windows("Environment variable inspection")
    snapshot = EnvironmentSnapshot()
    for scope, hive, key in (
        ("user", "HKCU", USER_ENV_KEY),
        ("machine", "HKLM", MACHINE_ENV_KEY),
    ):
        try:
            values = win_registry.list_values(hive, key)
        except Exception as exc:
            snapshot.errors.append(f"Could not read {scope} environment: {exc}")
            continue
        for name, value, value_type in values:
            if not name:
                continue
            type_name = win_registry.REG_TYPE_NAMES.get(value_type, str(value_type))
            snapshot.variables.append(
                EnvironmentVariable(
                    name=name,
                    scope=scope,
                    value=value,
                    value_type=type_name,
                    secret=should_redact_env_name(name),
                    multi_value=isinstance(value, (list, tuple)),
                )
            )
    snapshot.variables.sort(key=lambda item: (item.scope, item.name.casefold()))
    return snapshot


def validate_environment_name(name: str) -> str:
    cleaned = (name or "").strip()
    if not cleaned:
        raise ValueError("Enter an environment variable name.")
    if not _NAME_RE.match(cleaned):
        raise ValueError("The name contains unsupported characters. '=' and control characters are not allowed.")
    if cleaned.upper().startswith("="):
        raise ValueError("Names beginning with '=' are reserved for drive variables.")
    return cleaned


def validate_environment_value(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("The value must be text.")
    if "\x00" in value:
        raise ValueError("The value contains a NUL character.")
    if len(value) > 32767:
        raise ValueError("The value is longer than the Windows environment variable limit (32,767 characters).")
    return value


def build_set_spec(scope: str, name: str, value: str) -> OperationSpec:
    require_windows("Environment variable changes")
    hive, key = _key_for(scope)
    name = validate_environment_name(name)
    value = validate_environment_value(value)
    previous = win_registry.read_value(hive, key, name)
    secret = should_redact_env_name(name)
    previous_value = previous.value if previous.exists and not secret else None
    value_type = None
    if previous.exists and previous.value_type == 2:  # REG_EXPAND_SZ
        value_type = 2
    is_path = name.casefold() in _PATH_NAMES
    is_machine = hive == "HKLM"
    risk = RiskLevel.CRITICAL if is_path and is_machine else (RiskLevel.HIGH if is_path or is_machine else RiskLevel.MEDIUM)
    requires_admin = is_machine

    def apply(progress, cancel) -> ApplyOutcome:
        progress(f"Writing {name} in the {scope} environment")
        win_registry.write_value(hive, key, name, value, value_type=value_type)
        win_registry.broadcast_setting_change()
        return ApplyOutcome(
            success=True,
            previous_state="Present" if previous.exists else "Not present",
            requested_state=f"Set {name}",
            actual_state="Value written; verification pending",
            warnings=[
                "Running applications may not see the new value until they are restarted.",
                "Do not store passwords or tokens in environment variables.",
            ]
            + (["PATH changes can affect command resolution. A previous value was recorded when possible."] if is_path else []),
            details={"scope": scope, "name": name, "value_type": win_registry.REG_TYPE_NAMES.get(value_type or 1, "REG_SZ")},
            rollback=(
                {
                    "strategy": "env_set",
                    "scope": scope,
                    "name": name,
                    "previous_exists": previous.exists,
                    "previous_value": previous_value,
                    "previous_type": previous.value_type,
                    "summary": f"Restore environment variable {name}",
                }
                if not secret
                else None
            ),
        )

    def verify() -> VerifyOutcome:
        current = win_registry.read_value(hive, key, name)
        return VerifyOutcome(
            ok=current.exists and str(current.value) == value,
            actual_state="Value matches" if current.exists and str(current.value) == value else "Value differs",
            message="Read the environment variable back from the registry. Running processes may still hold the old value.",
        )

    def before() -> str:
        if not previous.exists:
            return "Not present"
        return "Present (value hidden)" if secret else f"{len(str(previous.value))} characters"

    return OperationSpec(
        operation_id=f"env.set.{scope}.{name}",
        name=f"Set {scope} environment variable: {name}",
        category="environment",
        description="Writes a user or machine environment variable and notifies Windows.",
        risk=risk,
        requires_admin=requires_admin,
        reversible=not secret,
        apply=apply,
        verify=verify,
        before=before,
        preview_text=f"{name} ({scope}) will be {'replaced' if previous.exists else 'created'}",
        warnings=[
            "Running applications may need to be restarted.",
            "Do not store passwords, tokens or other secrets in environment variables.",
        ]
        + (["This is a machine-wide change and requires administrator rights."] if is_machine else [])
        + (["PATH is a critical variable. Review the complete value carefully before applying."] if is_path else []),
        verification_method="Reads the registry value back and compares it with the requested value.",
        details={"scope": scope, "name": name},
    )


def build_delete_spec(scope: str, name: str) -> OperationSpec:
    require_windows("Environment variable changes")
    hive, key = _key_for(scope)
    name = validate_environment_name(name)
    if name.casefold() in _PROTECTED_NAMES:
        raise ValueError(
            f"{name} is a core Windows environment variable and cannot be deleted with WinFirst."
        )
    if name.casefold() in _PATH_NAMES:
        raise ValueError("PATH cannot be deleted with WinFirst. You may replace its value instead.")
    previous = win_registry.read_value(hive, key, name)
    if not previous.exists:
        raise ValueError(f"{name} is not present in the {scope} environment.")
    secret = should_redact_env_name(name)
    is_machine = hive == "HKLM"

    def apply(progress, cancel) -> ApplyOutcome:
        progress(f"Deleting {name} from the {scope} environment")
        win_registry.delete_value(hive, key, name)
        win_registry.broadcast_setting_change()
        return ApplyOutcome(
            success=True,
            previous_state="Present",
            requested_state=f"Delete {name}",
            actual_state="Value deleted; verification pending",
            warnings=[
                "Applications that depend on this variable may fail until it is restored.",
                "Running applications may keep the old value until they restart.",
            ],
            details={"scope": scope, "name": name},
            rollback=(
                {
                    "strategy": "env_set",
                    "scope": scope,
                    "name": name,
                    "previous_exists": True,
                    "previous_value": previous.value,
                    "previous_type": previous.value_type,
                    "summary": f"Restore environment variable {name}",
                }
                if not secret
                else None
            ),
        )

    def verify() -> VerifyOutcome:
        current = win_registry.read_value(hive, key, name)
        return VerifyOutcome(
            ok=not current.exists,
            actual_state="Absent" if not current.exists else "Still present",
            message="Read the registry key again to confirm the variable is absent.",
        )

    return OperationSpec(
        operation_id=f"env.delete.{scope}.{name}",
        name=f"Delete {scope} environment variable: {name}",
        category="environment",
        description="Deletes an environment variable from the registry.",
        risk=RiskLevel.HIGH,
        requires_admin=is_machine,
        reversible=not secret,
        apply=apply,
        verify=verify,
        before=lambda: "Present",
        preview_text=f"Delete {name} from the {scope} environment",
        warnings=[
            "Deleting an environment variable can break applications that depend on it.",
            "This does not remove the variable from processes that are already running.",
        ],
        verification_method="Reads the registry key again and confirms the value is absent.",
        details={"scope": scope, "name": name},
    )


def build_restore_spec(payload: dict[str, Any]) -> OperationSpec:
    scope = str(payload.get("scope") or "user")
    name = str(payload.get("name") or "")
    if not name:
        raise ValueError("The environment rollback record has no variable name.")
    hive, key = _key_for(scope)
    existed = bool(payload.get("previous_exists"))
    previous = payload.get("previous_value")
    previous_type = payload.get("previous_type")
    if name.casefold() in _PROTECTED_NAMES or should_redact_env_name(name):
        raise ValueError("This environment variable cannot be restored automatically.")
    write_type = int(previous_type) if previous_type else None
    expected_value = [previous] if write_type == 7 and isinstance(previous, str) else previous

    def apply(progress, cancel) -> ApplyOutcome:
        if existed:
            win_registry.write_value(
                hive,
                key,
                name,
                expected_value if expected_value is not None else "",
                value_type=write_type,
            )
        else:
            win_registry.delete_value(hive, key, name)
        win_registry.broadcast_setting_change()
        return ApplyOutcome(
            success=True,
            requested_state="Restore previous environment value" if existed else "Remove newly created variable",
            actual_state="Registry updated; verification pending",
            warnings=["Running applications may need to restart to see the restored value."],
            details={"scope": scope, "name": name},
        )

    def verify() -> VerifyOutcome:
        current = win_registry.read_value(hive, key, name)
        if not existed:
            return VerifyOutcome(not current.exists, "Absent" if not current.exists else "Present", "Checked absence.")
        ok = current.exists and str(current.value) == str(expected_value)
        return VerifyOutcome(ok, "Matches previous value" if ok else "Differs", "Compared with the recorded previous value.")

    return OperationSpec(
        operation_id=f"rollback.env.{scope}.{name}",
        name=f"Restore environment variable: {name}",
        category="environment",
        description="Restores a recorded environment variable value.",
        risk=RiskLevel.MEDIUM,
        requires_admin=hive == "HKLM",
        reversible=False,
        apply=apply,
        verify=verify,
        preview_text=f"Restore {name} in the {scope} environment",
        warnings=["Restores only this variable; unrelated changes are not reverted."],
        verification_method="Reads the variable back and compares it with the recorded value.",
    )
