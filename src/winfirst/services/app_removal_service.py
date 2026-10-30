"""Safe removal of applications from the inventory.

Removal never uses a shell.  Appx operations use validated package identifiers
and the documented PowerShell cmdlets; desktop applications use their registered
uninstaller with a validated argument list.  Protected components are rejected
even if a caller requests them.
"""
from __future__ import annotations

import ctypes
import re
import sys
import time

try:
    import winreg
except ImportError:  # pragma: no cover
    winreg = None  # type: ignore[assignment]

from winfirst.core.command_runner import CommandRunner, CommandResult, ps_quote
from winfirst.core.operation import ApplyOutcome, OperationSpec, VerifyOutcome
from winfirst.core.plan import action_step, command_step
from winfirst.core.risk_engine import RiskLevel
from winfirst.services.app_inventory_service import InventoryItem
from winfirst.services.common import ServiceError
from winfirst.services.win_registry import RegValue, key_exists, read_value

_PACKAGE_NAME_RE = re.compile(r"^[A-Za-z0-9._~+\-]{3,260}$")
_GUID_RE = re.compile(
    r"\{[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\}"
)
_BLOCKED_EXECUTABLES = {
    "cmd.exe",
    "powershell.exe",
    "pwsh.exe",
    "wscript.exe",
    "cscript.exe",
    "mshta.exe",
    "rundll32.exe",
    "regsvr32.exe",
}

_MSI_SUCCESS_CODES = {0, 3010}
_MSI_RESTART_CODES = {3010, 1641}


def validate_package_name(value: str) -> str:
    if not isinstance(value, str) or not _PACKAGE_NAME_RE.match(value.strip()):
        raise ServiceError("The package identifier is not valid for a removal command.")
    return value.strip()


def split_windows_command_line(command: str) -> list[str]:
    """Split a Windows command line into arguments using CommandLineToArgvW."""
    if not command or not command.strip():
        raise ServiceError("The uninstall command is empty.")
    if len(command) > 8192:
        raise ServiceError("The uninstall command is unreasonably long.")
    if sys.platform != "win32":
        raise ServiceError("Uninstall commands require Windows.")
    argc = ctypes.c_int()
    argv = ctypes.windll.shell32.CommandLineToArgvW(ctypes.c_wchar_p(command), ctypes.byref(argc))
    if not argv:
        raise ServiceError("The uninstall command could not be parsed.")
    try:
        parts = [ctypes.wstring_at(argv[i]) for i in range(argc.value)]
    finally:
        ctypes.windll.kernel32.LocalFree(argv)
    if not parts:
        raise ServiceError("The uninstall command could not be parsed.")
    return parts


def _msi_product_code(command: str) -> str:
    match = _GUID_RE.search(command or "")
    return match.group(0) if match else ""


def _desktop_command(item: InventoryItem) -> tuple[list[str], bool]:
    """Return (argv, quiet) for a desktop uninstall operation."""
    quiet_command = (item.quiet_uninstall_string or "").strip()
    command = quiet_command or (item.uninstall_string or "").strip()
    if not command:
        raise ServiceError("This application does not register an uninstall command.")
    product_code = _msi_product_code(command)
    if product_code and ("msiexec" in command.lower() or item.raw.get("windows_installer") == 1):
        return (["msiexec.exe", "/x", product_code, "/qn", "/norestart"], True)
    parts = split_windows_command_line(command)
    executable = parts[0].strip().strip('"')
    basename = executable.rsplit("\\", 1)[-1].lower()
    if basename in _BLOCKED_EXECUTABLES:
        raise ServiceError(
            "This application's uninstall command is wrapped in a script host. "
            "WinFirst does not execute shell-wrapped uninstall strings. "
            "Remove it manually from Windows Settings > Apps."
        )
    if basename.endswith((".bat", ".cmd", ".ps1", ".vbs", ".js")):
        raise ServiceError(
            "The registered uninstall command is a script, not an executable. "
            "Remove this application manually from Windows Settings > Apps."
        )
    if not basename.endswith((".exe", ".msi")):
        raise ServiceError(
            f"The registered uninstaller '{basename}' is not a supported executable type."
        )
    parts[0] = executable
    return parts, bool(quiet_command)


def build_removal_spec(item: InventoryItem) -> OperationSpec:
    if item.category == "protected_system" or not item.removable:
        raise ValueError(
            f"{item.name} is a protected component and cannot be removed through WinFirst."
        )
    if item.source == "provisioned":
        return _provisioned_spec(item)
    if item.source == "appx":
        return _appx_spec(item)
    return _desktop_spec(item)


def _appx_spec(item: InventoryItem) -> OperationSpec:
    package = validate_package_name(item.package_full_name)
    all_users = "All users" in item.scope
    requires_admin = all_users
    operation = "Remove-AppxPackage -AllUsers" if all_users else "Remove-AppxPackage"

    def plan_steps():
        flag = " -AllUsers" if all_users else ""
        return [
            action_step(
                "Uninstall the Appx package",
                detail=f"Remove-AppxPackage -Package {package}{flag} -ErrorAction Stop",
                destructive=True,
            ),
            action_step(
                "Confirm the package is gone",
                detail="Get-AppxPackage must no longer report the exact package full name.",
            ),
        ]

    def apply(progress, cancel) -> ApplyOutcome:
        runner = CommandRunner()
        progress(f"Removing {item.name}")
        flag = " -AllUsers" if all_users else ""
        script = f"Remove-AppxPackage -Package {ps_quote(package)}{flag} -ErrorAction Stop"
        result = runner.powershell(script, timeout=300, cancel_event=cancel)
        if not result.launched:
            raise ServiceError(result.error or "PowerShell could not be started.")
        if result.timed_out:
            raise ServiceError("Removal timed out after 300 seconds.")
        if result.exit_code != 0:
            detail = (result.stderr or result.stdout).strip()[:1200]
            raise ServiceError(f"Remove-AppxPackage failed: {detail}")
        return ApplyOutcome(
            success=True,
            previous_state=f"Installed ({item.version})",
            requested_state="Removed",
            actual_state="Removal command completed; verification pending",
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.exit_code,
            warnings=[
                "WinFirst cannot restore this package. Reinstalling may require the Microsoft Store."
            ],
            details={"package_full_name": package, "scope": item.scope},
        )

    def verify() -> VerifyOutcome:
        runner = CommandRunner()
        flag = " -AllUsers" if all_users else ""
        script = (
            f"$found = @(Get-AppxPackage{flag} -ErrorAction SilentlyContinue | "
            f"Where-Object {{ $_.PackageFullName -eq {ps_quote(package)} }}); "
            f"if ($found.Count -gt 0) {{ 'present' }} else {{ 'absent' }}"
        )
        result = runner.powershell(script, timeout=90)
        if not result.ok:
            raise ServiceError(f"Could not verify package removal: {result.tail(500)}")
        state = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else "unknown"
        return VerifyOutcome(
            ok=state == "absent",
            actual_state="Removed" if state == "absent" else "Still installed",
            message=f"Get-AppxPackage reports the package as {state}.",
        )

    return OperationSpec(
        operation_id=f"appx.remove.{item.package_family}",
        name=f"Remove app: {item.name}",
        category="bloatware",
        description=f"Removes the Appx package {package} for {item.scope.lower()}.",
        risk=RiskLevel.HIGH,
        requires_admin=requires_admin,
        reversible=False,
        apply=apply,
        verify=verify,
        before=lambda: f"Installed: {item.name} {item.version}",
        plan_steps=plan_steps,
        preview_text=f"{operation} {package}",
        warnings=[
            "The package may not be restorable without the Microsoft Store or an offline source.",
            "WinFirst does not run third-party debloating scripts.",
        ],
        verification_method="Queries Get-AppxPackage for the exact package full name",
        details={"package_full_name": package, "source": "appx"},
    )


def _provisioned_spec(item: InventoryItem) -> OperationSpec:
    package = validate_package_name(item.package_full_name)

    def plan_steps():
        return [
            action_step(
                "Remove the provisioned package",
                detail=f"Remove-AppxProvisionedPackage -Online -PackageName {package} -ErrorAction Stop",
                destructive=True,
            ),
            action_step(
                "Confirm the package is no longer provisioned",
                detail="Get-AppxProvisionedPackage must not report the package name.",
            ),
        ]

    def apply(progress, cancel) -> ApplyOutcome:
        runner = CommandRunner()
        progress(f"Removing provisioned package {item.name}")
        script = (
            f"Remove-AppxProvisionedPackage -Online -PackageName {ps_quote(package)} "
            f"-ErrorAction Stop | Out-Null"
        )
        result = runner.powershell(script, timeout=300, cancel_event=cancel)
        if not result.launched:
            raise ServiceError(result.error or "PowerShell could not be started.")
        if result.timed_out:
            raise ServiceError("Removal timed out after 300 seconds.")
        if result.exit_code != 0:
            raise ServiceError(
                "Remove-AppxProvisionedPackage failed: " + (result.stderr or result.stdout)[:1200]
            )
        return ApplyOutcome(
            success=True,
            previous_state=f"Provisioned ({item.version})",
            requested_state="Not provisioned",
            actual_state="Removal command completed; verification pending",
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.exit_code,
            warnings=[
                "This affects future user profiles on this computer.",
                "Existing user installations are not removed by this operation.",
            ],
            details={"package_name": package, "scope": "provisioned"},
        )

    def verify() -> VerifyOutcome:
        runner = CommandRunner()
        script = (
            "$found = @(Get-AppxProvisionedPackage -Online -ErrorAction SilentlyContinue | "
            f"Where-Object {{ $_.PackageName -eq {ps_quote(package)} }}); "
            "if ($found.Count -gt 0) { 'present' } else { 'absent' }"
        )
        result = runner.powershell(script, timeout=120)
        if not result.ok:
            raise ServiceError(f"Could not verify provisioned package removal: {result.tail(500)}")
        state = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else "unknown"
        return VerifyOutcome(
            ok=state == "absent",
            actual_state="Not provisioned" if state == "absent" else "Still provisioned",
            message=f"Get-AppxProvisionedPackage reports the package as {state}.",
        )

    return OperationSpec(
        operation_id="appx.remove_provisioned",
        name=f"Remove provisioned app: {item.name}",
        category="bloatware",
        description=f"Removes the provisioned package {package} for future user profiles.",
        risk=RiskLevel.HIGH,
        requires_admin=True,
        reversible=False,
        affects_other_users=True,
        apply=apply,
        verify=verify,
        before=lambda: f"Provisioned: {item.name} {item.version}",
        plan_steps=plan_steps,
        preview_text=f"Remove-AppxProvisionedPackage -Online -PackageName {package}",
        warnings=[
            "Affects future user profiles. Existing installs for current users are separate.",
            "Reinstalling a provisioned package requires an offline source or administrative effort.",
        ],
        verification_method="Queries Get-AppxProvisionedPackage for the exact package name",
        details={"package_name": package, "source": "provisioned"},
    )


def _desktop_spec(item: InventoryItem) -> OperationSpec:
    requires_admin = item.scope.startswith("Machine")
    registry_path = str(item.raw.get("registry_path", ""))
    hive_name = str(item.raw.get("hive", "HKLM"))
    key_path = str(item.raw.get("key_path", ""))

    def plan_steps():
        argv, quiet = _desktop_command(item)
        detail = "Uses the uninstall command registered by the vendor."
        if not quiet:
            detail += " The vendor uninstaller may display its own window or ask for input."
        return [
            command_step(argv, title="Run the registered uninstaller", detail=detail, destructive=True),
            action_step(
                "Re-read the uninstall registry entry",
                detail="The application counts as removed when its registry entry is gone.",
            ),
        ]

    def apply(progress, cancel) -> ApplyOutcome:
        argv, quiet = _desktop_command(item)
        runner = CommandRunner()
        progress(f"Running the uninstaller for {item.name}")
        result: CommandResult = runner.run(argv, timeout=900, cancel_event=cancel)
        if not result.launched:
            raise ServiceError(result.error or "The uninstaller could not be started.")
        if result.timed_out:
            raise ServiceError(
                "The uninstaller did not finish within 15 minutes. Check Windows Settings > Apps; "
                "the application may be partially removed."
            )
        if result.cancelled:
            raise ServiceError("The uninstall operation was cancelled.")
        code = result.exit_code or 0
        if code not in _MSI_SUCCESS_CODES and code not in _MSI_RESTART_CODES:
            detail = (result.stderr or result.stdout).strip()[:1200]
            raise ServiceError(f"The uninstaller exited with code {code}. {detail}")
        restart = code in _MSI_RESTART_CODES
        return ApplyOutcome(
            success=True,
            previous_state=f"Installed: {item.name} {item.version}",
            requested_state="Removed",
            actual_state="Uninstaller completed; verification pending",
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=code,
            restart_required=restart,
            warnings=[
                "WinFirst cannot restore uninstalled desktop applications. Reinstall from the vendor."
            ]
            + ([] if quiet else ["The vendor uninstaller may have displayed its own window or required input."]),
            details={
                "argv": argv[:4],
                "quiet": quiet,
                "registry_path": registry_path,
            },
        )

    def verify() -> VerifyOutcome:
        if winreg is None or not key_path:
            return VerifyOutcome(
                ok=False,
                actual_state="Unknown",
                message="The uninstall registry key could not be checked.",
            )
        deadline = time.monotonic() + 30
        last: RegValue | None = None
        while time.monotonic() < deadline:
            if hive_name == "HKCU":
                exists = key_exists("HKCU", key_path)
            else:
                exists = key_exists("HKLM", key_path)
            if not exists:
                return VerifyOutcome(
                    ok=True,
                    actual_state="Removed",
                    message="The uninstall registry entry is gone.",
                )
            last = read_value(
                "HKCU" if hive_name == "HKCU" else "HKLM", key_path, "DisplayName"
            )
            if not last.exists:
                return VerifyOutcome(
                    ok=True,
                    actual_state="Removed",
                    message="The uninstall registry entry no longer has a display name.",
                )
            time.sleep(1.0)
        return VerifyOutcome(
            ok=False,
            actual_state="Still registered",
            message="The application is still registered after the uninstaller finished.",
        )

    return OperationSpec(
        operation_id="desktop.uninstall",
        name=f"Uninstall application: {item.name}",
        category="bloatware",
        description=f"Runs the registered uninstaller for {item.name}.",
        risk=RiskLevel.HIGH,
        requires_admin=requires_admin,
        reversible=False,
        apply=apply,
        verify=verify,
        before=lambda: f"Installed: {item.name} {item.version}",
        plan_steps=plan_steps,
        preview_text=f"Run registered uninstaller for {item.name}",
        warnings=[
            "This uses the vendor uninstaller and may require interaction.",
            "Uninstallation cannot be rolled back by WinFirst.",
        ],
        verification_method="Re-reads the uninstall registry entry",
        details={"registry_path": registry_path},
    )


def build_removal_specs(items: list[InventoryItem]) -> tuple[list[OperationSpec], list[str]]:
    specs: list[OperationSpec] = []
    errors: list[str] = []
    for item in items:
        try:
            specs.append(build_removal_spec(item))
        except ValueError as exc:
            errors.append(f"{item.name}: {exc}")
    return specs, errors
