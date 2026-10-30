"""Local user and group inspection with guarded enable/disable actions.

WinFirst never reads, sets or clears passwords, never creates hidden accounts,
and refuses to disable the signed-in account or the last enabled administrator.
Group membership is read-only.  System information is collected with CIM.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any

from winfirst.core.command_runner import CommandRunner, as_list, ps_quote
from winfirst.core.operation import ApplyOutcome, OperationSpec, VerifyOutcome
from winfirst.core.risk_engine import RiskLevel
from winfirst.services.common import ServiceError, bool_from, ps_json, require_windows

_ADMIN_SID = "S-1-5-32-544"
_BUILTIN_ACCOUNTS = {
    "administrator",
    "guest",
    "defaultaccount",
    "wdagutilityaccount",
    "defaultuser0",
}


@dataclass
class LocalUser:
    name: str
    enabled: bool = True
    description: str = ""
    last_logon: str = ""
    password_required: bool | None = None
    password_last_set: str = ""
    sid: str = ""
    source: str = ""
    is_admin: bool = False
    is_current: bool = False
    can_toggle: bool = True
    protection_reason: str = ""
    error: str = ""

    @property
    def status_label(self) -> str:
        if self.error:
            return "Unknown"
        return "Enabled" if self.enabled else "Disabled"


@dataclass
class UserSnapshot:
    users: list[LocalUser] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def find(self, name: str) -> LocalUser | None:
        for user in self.users:
            if user.name.casefold() == name.casefold():
                return user
        return None


@dataclass
class GroupInfo:
    name: str
    description: str = ""
    sid: str = ""
    member_count: int | None = None
    error: str = ""


@dataclass
class SystemSummary:
    computer_name: str = ""
    domain: str = ""
    part_of_domain: bool = False
    manufacturer: str = ""
    model: str = ""
    total_memory: int = 0
    os_name: str = ""
    os_version: str = ""
    os_build: str = ""
    architecture: str = ""
    install_date: str = ""
    last_boot: str = ""
    uptime_seconds: int = 0
    current_user: str = ""
    errors: list[str] = field(default_factory=list)


_USERS_SCRIPT = r"""
$result = [ordered]@{ Users = @(); Admins = @(); Errors = @() }
try {
    $result.Users = @(Get-LocalUser -ErrorAction Stop | ForEach-Object {
        [pscustomobject]@{
            Name = [string]$_.Name; Enabled = [bool]$_.Enabled;
            Description = [string]$_.Description;
            LastLogon = if ($_.LastLogon) { $_.LastLogon.ToString('o') } else { '' };
            PasswordRequired = $_.PasswordRequired;
            PasswordLastSet = if ($_.PasswordLastSet) { $_.PasswordLastSet.ToString('o') } else { '' };
            SID = [string]$_.SID; Source = [string]$_.PrincipalSource
        }
    })
} catch { $result.Errors += "Local users: $($_.Exception.Message)" }
try {
    $adminGroup = Get-LocalGroup -SID 'S-1-5-32-544' -ErrorAction Stop
    $result.Admins = @(Get-LocalGroupMember -Group $adminGroup.Name -ErrorAction Stop | ForEach-Object {
        [pscustomobject]@{ Name = [string]$_.Name; SID = [string]$_.SID; Class = [string]$_.ObjectClass }
    })
} catch { $result.Errors += "Administrators: $($_.Exception.Message)" }
[pscustomobject]$result | ConvertTo-Json -Depth 5 -Compress
"""

_GROUPS_SCRIPT = r"""
try {
    @(Get-LocalGroup -ErrorAction Stop | ForEach-Object {
        $count = $null
        try { $count = @(Get-LocalGroupMember -Group $_.Name -ErrorAction Stop).Count } catch { }
        [pscustomobject]@{ Name = [string]$_.Name; Description = [string]$_.Description;
            SID = [string]$_.SID; MemberCount = $count }
    }) | ConvertTo-Json -Depth 3 -Compress
} catch { '' }
"""

_SYSTEM_SCRIPT = r"""
$result = [ordered]@{ Computer = $null; Os = $null; CurrentUser = ''; Errors = @() }
try { $result.Computer = Get-CimInstance Win32_ComputerSystem -ErrorAction Stop |
    Select-Object Name, Domain, PartOfDomain, Manufacturer, Model, TotalPhysicalMemory } catch { $result.Errors += "Computer: $($_.Exception.Message)" }
try { $result.Os = Get-CimInstance Win32_OperatingSystem -ErrorAction Stop |
    Select-Object Caption, Version, BuildNumber, OSArchitecture, InstallDate, LastBootUpTime } catch { $result.Errors += "OS: $($_.Exception.Message)" }
try { $result.CurrentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name } catch { }
[pscustomobject]$result | ConvertTo-Json -Depth 4 -Compress
"""


def _format_date(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    try:
        # Convert CIM JSON dates (20240101120000.000000-000) to a readable form.
        if len(text) >= 14 and text[:14].isdigit():
            return f"{text[0:4]}-{text[4:6]}-{text[6:8]} {text[8:10]}:{text[10:12]}"
    except Exception:
        pass
    return text


def _current_username() -> str:
    return os.environ.get("USERNAME", "")


def get_local_users(runner: CommandRunner | None = None) -> UserSnapshot:
    require_windows("Local user inspection")
    runner = runner or CommandRunner()
    try:
        data = ps_json(runner, _USERS_SCRIPT, timeout=90, default={})
    except Exception as exc:
        raise ServiceError(f"Could not read local users: {exc}") from exc
    if not isinstance(data, dict):
        data = {}
    snapshot = UserSnapshot(errors=[str(item) for item in as_list(data.get("Errors")) if item])
    admin_sids = {
        str(row.get("SID") or "")
        for row in as_list(data.get("Admins"))
        if isinstance(row, dict)
    }
    admin_names = {
        str(row.get("Name") or "").split("\\")[-1].casefold()
        for row in as_list(data.get("Admins"))
        if isinstance(row, dict)
    }
    current = _current_username().casefold()
    for row in as_list(data.get("Users")):
        if not isinstance(row, dict):
            continue
        name = str(row.get("Name") or "")
        if not name:
            continue
        sid = str(row.get("SID") or "")
        enabled = bool_from(row.get("Enabled"))
        is_admin = sid in admin_sids or name.casefold() in admin_names
        is_current = bool(current and name.casefold() == current)
        user = LocalUser(
            name=name,
            enabled=enabled,
            description=str(row.get("Description") or ""),
            last_logon=_format_date(row.get("LastLogon")),
            password_required=(
                bool_from(row.get("PasswordRequired")) if row.get("PasswordRequired") is not None else None
            ),
            password_last_set=_format_date(row.get("PasswordLastSet")),
            sid=sid,
            source=str(row.get("Source") or ""),
            is_admin=is_admin,
            is_current=is_current,
        )
        if user.name.casefold() in _BUILTIN_ACCOUNTS:
            user.can_toggle = False
            user.protection_reason = "Built-in Windows accounts are protected from enable/disable changes."
        elif is_current:
            user.can_toggle = False
            user.protection_reason = "You cannot disable the account that is currently signed in."
        elif is_admin and enabled and sum(
            1 for other in snapshot.users if other.is_admin and other.enabled
        ) == 0:
            # First admin encountered: the final check after all users are loaded.
            pass
        snapshot.users.append(user)
    enabled_admins = [user for user in snapshot.users if user.is_admin and user.enabled]
    if len(enabled_admins) <= 1:
        for user in enabled_admins:
            if user.name.casefold() not in _BUILTIN_ACCOUNTS:
                user.can_toggle = False
                user.protection_reason = (
                    "This is the last enabled administrator account; disabling it could lock out administration."
                )
    snapshot.users.sort(key=lambda item: (not item.is_admin, item.name.casefold()))
    return snapshot


def get_local_groups(runner: CommandRunner | None = None) -> tuple[list[GroupInfo], list[str]]:
    require_windows("Local group inspection")
    runner = runner or CommandRunner()
    try:
        data = ps_json(runner, _GROUPS_SCRIPT, timeout=120, default=[])
    except Exception as exc:
        return [], [f"Could not read local groups: {exc}"]
    groups = [
        GroupInfo(
            name=str(row.get("Name") or ""),
            description=str(row.get("Description") or ""),
            sid=str(row.get("SID") or ""),
            member_count=(
                int(row["MemberCount"]) if isinstance(row.get("MemberCount"), (int, float)) else None
            ),
        )
        for row in as_list(data)
        if isinstance(row, dict) and row.get("Name")
    ]
    groups.sort(key=lambda item: item.name.casefold())
    return groups, []


def get_group_members(group_name: str, runner: CommandRunner | None = None) -> list[dict[str, str]]:
    require_windows("Local group inspection")
    if not group_name:
        raise ValueError("A group name is required.")
    runner = runner or CommandRunner()
    script = (
        f"$members = @(Get-LocalGroupMember -Group {ps_quote(group_name)} -ErrorAction Stop | "
        "ForEach-Object { [pscustomobject]@{ Name = [string]$_.Name; SID = [string]$_.SID; "
        "Class = [string]$_.ObjectClass; Source = [string]$_.PrincipalSource } }); "
        "$members | ConvertTo-Json -Depth 3 -Compress"
    )
    data = ps_json(runner, script, timeout=60, default=[])
    return [
        {
            "name": str(row.get("Name") or ""),
            "sid": str(row.get("SID") or ""),
            "class": str(row.get("Class") or ""),
            "source": str(row.get("Source") or ""),
        }
        for row in as_list(data)
        if isinstance(row, dict)
    ]


def get_system_summary(runner: CommandRunner | None = None) -> SystemSummary:
    require_windows("System information")
    runner = runner or CommandRunner()
    try:
        data = ps_json(runner, _SYSTEM_SCRIPT, timeout=60, default={})
    except Exception as exc:
        raise ServiceError(f"Could not read system information: {exc}") from exc
    if not isinstance(data, dict):
        data = {}
    summary = SystemSummary(errors=[str(item) for item in as_list(data.get("Errors")) if item])
    computer = data.get("Computer") if isinstance(data.get("Computer"), dict) else {}
    os_data = data.get("Os") if isinstance(data.get("Os"), dict) else {}
    summary.computer_name = str(computer.get("Name") or "")
    summary.domain = str(computer.get("Domain") or "")
    summary.part_of_domain = bool_from(computer.get("PartOfDomain"))
    summary.manufacturer = str(computer.get("Manufacturer") or "")
    summary.model = str(computer.get("Model") or "")
    try:
        summary.total_memory = int(computer.get("TotalPhysicalMemory") or 0)
    except (TypeError, ValueError):
        summary.total_memory = 0
    summary.os_name = str(os_data.get("Caption") or "")
    summary.os_version = str(os_data.get("Version") or "")
    summary.os_build = str(os_data.get("BuildNumber") or "")
    summary.architecture = str(os_data.get("OSArchitecture") or "")
    summary.install_date = _format_date(os_data.get("InstallDate"))
    summary.last_boot = _format_date(os_data.get("LastBootUpTime"))
    summary.current_user = str(data.get("CurrentUser") or "")
    last_boot_raw = str(os_data.get("LastBootUpTime") or "")
    if len(last_boot_raw) >= 14 and last_boot_raw[:14].isdigit():
        try:
            boot = time.mktime(time.strptime(last_boot_raw[:14], "%Y%m%d%H%M%S"))
            summary.uptime_seconds = max(0, int(time.time() - boot))
        except (OverflowError, ValueError):
            summary.uptime_seconds = 0
    return summary


def _read_user_enabled(name: str, runner: CommandRunner) -> bool | None:
    script = (
        f"$u = Get-LocalUser -Name {ps_quote(name)} -ErrorAction Stop; $u.Enabled.ToString()"
    )
    result = runner.powershell(script, timeout=45)
    if not result.launched or result.exit_code != 0:
        return None
    text = (result.stdout or "").strip().splitlines()
    if not text:
        return None
    return text[-1].strip().lower() == "true"


def validate_user_toggle_batch(
    users: list[LocalUser], enable: bool, snapshot: UserSnapshot | None
) -> None:
    """Refuse a batch that would leave no enabled administrator.

    A per-user check is not sufficient: when several administrators are
    selected together, each individual check still sees the original snapshot
    and would allow the whole batch.  This check subtracts every selected
    enabled administrator before deciding.
    """
    if enable or snapshot is None:
        return
    enabled_admins = {
        user.name.casefold() for user in snapshot.users if user.is_admin and user.enabled
    }
    selected_admins = {
        user.name.casefold() for user in users if user.is_admin and user.enabled
    }
    if enabled_admins and not (enabled_admins - selected_admins):
        raise ValueError(
            "This batch would disable every enabled administrator on the computer. "
            "Keep at least one enabled administrator."
        )


def build_toggle_user_spec(user: LocalUser, enable: bool, snapshot: UserSnapshot | None = None) -> OperationSpec:
    if not user.can_toggle:
        raise ValueError(user.protection_reason or "This account is protected from enable/disable changes.")
    if user.name.casefold() in _BUILTIN_ACCOUNTS:
        raise ValueError("Built-in Windows accounts are protected from enable/disable changes.")
    if snapshot is not None and not enable:
        admins = [item for item in snapshot.users if item.is_admin and item.enabled]
        if user.is_admin and len(admins) <= 1:
            raise ValueError("Disabling this account would leave no enabled administrator.")
    action = "enable" if enable else "disable"
    tool = "Enable-LocalUser" if enable else "Disable-LocalUser"

    def apply(progress, cancel) -> ApplyOutcome:
        runner = CommandRunner()
        progress(f"{action.capitalize()}ing local account {user.name}")
        result = runner.powershell(
            f"{tool} -Name {ps_quote(user.name)} -ErrorAction Stop", timeout=60, cancel_event=cancel
        )
        if not result.launched:
            raise ServiceError(result.error or "PowerShell could not be started.")
        if result.exit_code != 0:
            raise ServiceError((result.stderr or result.stdout).strip()[:800] or f"Could not {action} {user.name}.")
        return ApplyOutcome(
            success=True,
            previous_state="Enabled" if user.enabled else "Disabled",
            requested_state="Enabled" if enable else "Disabled",
            actual_state="Account change applied; verification pending",
            warnings=[
                "This changes the local account only; it does not change the password.",
                "A disabled account may remain signed in until it signs out, but cannot sign in again.",
                "Domain accounts and Microsoft accounts are not changed by this action.",
            ],
            details={"user": user.name, "sid": user.sid, "action": action},
            rollback={
                "strategy": "user_account",
                "name": user.name,
                "previous": user.enabled,
                "summary": f"Restore local account {user.name} to {'enabled' if user.enabled else 'disabled'}",
            },
        )

    def verify() -> VerifyOutcome:
        current = _read_user_enabled(user.name, CommandRunner())
        if current is None:
            return VerifyOutcome(False, "Unknown", "The account state could not be read back.")
        return VerifyOutcome(
            ok=current == enable,
            actual_state="Enabled" if current else "Disabled",
            message="Read the account state back with Get-LocalUser.",
        )

    return OperationSpec(
        operation_id=f"user.{action}.{user.name}",
        name=f"{action.capitalize()} local account: {user.name}",
        category="users",
        description=f"{action.capitalize()}s a local Windows user account. Passwords are not changed.",
        risk=RiskLevel.HIGH,
        requires_admin=True,
        reversible=True,
        affects_other_users=not user.is_current,
        apply=apply,
        verify=verify,
        before=lambda: "Enabled" if user.enabled else "Disabled",
        preview_text=f"{user.name}: {'Disabled -> Enabled' if enable else 'Enabled -> Disabled'}",
        warnings=[
            "This changes only whether the local account can sign in.",
            "It does not delete files, change the password, or remove the account.",
            "Never disable the last administrator account.",
        ],
        verification_method="Reads the account's Enabled property back with Get-LocalUser.",
        details={"user": user.name, "sid": user.sid},
    )
