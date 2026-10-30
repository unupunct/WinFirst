"""Software installation through winget or a catalog-approved local installer.

No command, script, URL or installer path from a profile is ever executed.
Winget package identifiers and local installer paths come only from a validated
catalog, are passed as argument lists, and every install is independently
verified before it is reported as successful.
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from winfirst.core.command_runner import CommandRunner, ps_quote
from winfirst.core.operation import ApplyOutcome, OperationSpec, VerifyOutcome
from winfirst.core.risk_engine import RiskLevel
from winfirst.services.common import ServiceError, require_windows
from winfirst.services.software_catalog_service import CatalogPackage

ProgressFn = Callable[[str], None]
_KNOWN_WINGET = (
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WindowsApps", "winget.exe"),
    os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "WindowsApps", "winget.exe"),
)


_WINGET_NOT_FOUND_CODES = {0x8A150014, 0x8A150014 - (1 << 32)}


@dataclass
class WingetStatus:
    available: bool = False
    path: str = ""
    version: str = ""
    error: str = ""

    @property
    def summary(self) -> str:
        if self.available:
            return f"winget {self.version}" if self.version else "winget available"
        return self.error or "winget is not available"


@dataclass
class InstallCheck:
    installed: bool
    detail: str = ""
    error: str = ""


def find_winget(runner: CommandRunner | None = None) -> WingetStatus:
    require_windows("Software installation")
    runner = runner or CommandRunner()
    for name in ("winget.exe", "winget"):
        try:
            path = runner.resolve_executable(name)
        except Exception:
            path = ""
        if path:
            return _probe_winget(runner, path)
    for candidate in _KNOWN_WINGET:
        if candidate and os.path.isfile(candidate):
            return _probe_winget(runner, candidate)
    return WingetStatus(
        False,
        error=(
            "winget was not found. Install or update 'App Installer' from the Microsoft Store, "
            "then reopen WinFirst."
        ),
    )


def _probe_winget(runner: CommandRunner, path: str) -> WingetStatus:
    result = runner.run([path, "--version"], timeout=30)
    if not result.launched:
        return WingetStatus(False, path=path, error=result.error or "winget could not be started.")
    if result.exit_code != 0:
        text = (result.stderr or result.stdout).strip()[:500]
        return WingetStatus(False, path=path, error=f"winget returned exit {result.exit_code}: {text}")
    lines = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
    version = lines[-1].lstrip("vV") if lines else ""
    return WingetStatus(True, path=path, version=version)


def _winget_list(runner: CommandRunner, status: WingetStatus, package_id: str) -> tuple[bool, str]:
    result = runner.run(
        [
            status.path,
            "list",
            "--id",
            package_id,
            "--exact",
            "--accept-source-agreements",
            "--disable-interactivity",
        ],
        timeout=180,
    )
    if not result.launched:
        raise ServiceError(result.error or "winget list could not be started.")
    text = result.combined
    # APPINSTALLER_CLI_ERROR_NO_APPLICATIONS_FOUND; the exit code is locale
    # independent, unlike the message text.
    if (
        result.exit_code in _WINGET_NOT_FOUND_CODES
        or "no installed package found" in text.lower()
    ):
        return False, "winget reports no installed package with this identifier."
    if result.exit_code != 0:
        raise ServiceError(
            f"winget list returned exit {result.exit_code}: {text.strip()[:700] or 'no output'}"
        )
    # Avoid matching only a header or an unrelated name: the identifier must
    # appear as a token in the output.
    found = bool(re.search(rf"(?<![\w.\-]){re.escape(package_id)}(?![\w.\-])", text, re.IGNORECASE))
    return found, ("winget lists the identifier as installed." if found else "The identifier was not listed.")


def _uninstall_entry_exists(runner: CommandRunner, display_name: str) -> tuple[bool, str]:
    script = (
        "$roots = @("
        "'HKLM:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*',"
        "'HKLM:\\Software\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*',"
        "'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*'); "
        "$found = @(Get-ItemProperty -Path $roots -ErrorAction SilentlyContinue | "
        "Where-Object { $_.DisplayName -eq " + ps_quote(display_name) + " }); "
        "if ($found.Count -gt 0) { 'FOUND' } else { 'MISSING' }"
    )
    result = runner.powershell(script, timeout=60)
    if not result.launched or result.exit_code != 0:
        return False, (result.stderr or result.stdout).strip()[:500] or "Could not read uninstall registry entries."
    text = (result.stdout or "").strip().upper()
    return "FOUND" in text, "Checked uninstall registry entries by exact DisplayName."


def check_installed(package: CatalogPackage, status: WingetStatus | None = None) -> InstallCheck:
    """Independent verification used before and after installation."""
    verify_type = package.verify.get("type", "")
    try:
        if verify_type == "winget_id":
            status = status or find_winget()
            if not status.available:
                return InstallCheck(False, error=status.error)
            installed, detail = _winget_list(CommandRunner(), status, package.verify.get("value", package.id))
            return InstallCheck(installed, detail)
        if verify_type == "uninstall_name":
            installed, detail = _uninstall_entry_exists(
                CommandRunner(), package.verify.get("value", "")
            )
            return InstallCheck(installed, detail)
        if verify_type == "file":
            path = os.path.expandvars(os.path.expanduser(package.verify.get("path", "")))
            exists = bool(path) and os.path.isfile(path)
            return InstallCheck(exists, f"Checked file: {path}")
        return InstallCheck(False, error="No independent verification method is configured for this package.")
    except Exception as exc:
        return InstallCheck(False, error=str(exc))


def _installer_path(package: CatalogPackage) -> Path:
    raw = os.path.expandvars(os.path.expanduser(package.installer))
    path = Path(raw)
    if not path.is_absolute():
        raise ServiceError("The local installer path must be absolute.")
    if not path.is_file():
        raise ServiceError(f"The local installer does not exist: {path}")
    if path.suffix.casefold() not in {".exe", ".msi"}:
        raise ServiceError("Only .exe and .msi installers are supported.")
    return path


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _verify_installer_hash(package: CatalogPackage, path: Path) -> None:
    expected = (package.sha256 or "").lower()
    if not expected:
        raise ServiceError("The catalog entry has no sha256 for this local installer; it will not be run.")
    actual = _sha256_of(path)
    if actual != expected:
        raise ServiceError(
            "The local installer does not match the sha256 in the catalog "
            f"(expected {expected[:12]}..., found {actual[:12]}...). It was not run."
        )


def build_uninstall_spec(package_id: str, name: str = "") -> OperationSpec:
    """Uninstall a winget package that WinFirst installed (used by rollback)."""
    require_windows("Software removal")
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._+\-]{0,199}$", package_id):
        raise ValueError("The winget package identifier is invalid.")
    status = find_winget()
    if not status.available:
        raise ValueError(status.error or "winget is not available.")
    label = name or package_id
    runner = CommandRunner()

    def apply(progress: ProgressFn, cancel) -> ApplyOutcome:
        installed, _ = _winget_list(runner, status, package_id)
        if not installed:
            return ApplyOutcome(
                success=True,
                previous_state="Not installed",
                requested_state="Uninstall",
                actual_state="Not installed",
                message="The package is already absent.",
            )
        progress(f"Uninstalling {label} with winget")
        result = runner.run(
            [
                status.path, "uninstall", "--id", package_id, "--exact", "--silent",
                "--accept-source-agreements", "--disable-interactivity",
            ],
            timeout=1800,
            on_output=progress,
            cancel_event=cancel,
        )
        if not result.launched:
            raise ServiceError(result.error or "winget could not be started.")
        if result.cancelled:
            raise ServiceError("The uninstall was cancelled; verify the package state before retrying.")
        still, _ = _winget_list(runner, status, package_id)
        if result.exit_code != 0 and still:
            raise ServiceError(
                f"winget uninstall failed (exit {result.exit_code}): {result.tail(1200) or 'no output'}"
            )
        return ApplyOutcome(
            success=True,
            previous_state="Installed",
            requested_state=f"Uninstall {label}",
            actual_state="Uninstall command completed; verification pending",
            stdout=result.tail(4000),
            exit_code=result.exit_code,
            details={"package": package_id, "installer": "winget"},
        )

    def verify() -> VerifyOutcome:
        installed, detail = _winget_list(runner, status, package_id)
        return VerifyOutcome(ok=not installed, actual_state="Installed" if installed else "Not installed", message=detail)

    return OperationSpec(
        operation_id=f"software.uninstall.{package_id}",
        name=f"Uninstall {label}",
        category="software",
        description=f"Removes {label}, which WinFirst installed through winget.",
        risk=RiskLevel.HIGH,
        requires_admin=False,
        reversible=False,
        apply=apply,
        verify=verify,
        before=lambda: "Installed",
        preview_text=f"Uninstall {label} ({package_id}) with winget",
        warnings=["Application data left by the program may remain after removal."],
        verification_method="winget list no longer shows the identifier",
        details={"package": package_id, "source": "winget"},
    )


def build_install_spec(
    package: CatalogPackage,
    *,
    accept_agreements: bool,
    winget_status: WingetStatus | None = None,
) -> OperationSpec:
    require_windows("Software installation")
    if not package.verify.get("type"):
        raise ValueError("This package has no independent verification method and cannot be installed.")
    supported_status = winget_status
    if package.source == "winget":
        if not accept_agreements:
            raise ValueError(
                "Winget requires accepting the package and source agreements. "
                "Review the package information and select the agreement checkbox first."
            )
        supported_status = winget_status or find_winget()
        if not supported_status.available:
            raise ValueError(supported_status.error or "winget is not available.")
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._+\-]{0,199}$", package.id):
            raise ValueError("The winget package identifier is invalid.")
    else:
        _installer_path(package)

    def apply(progress: ProgressFn, cancel) -> ApplyOutcome:
        pre = check_installed(package, winget_status)
        if pre.installed:
            return ApplyOutcome(
                success=False,
                previous_state="Installed",
                requested_state="Install",
                actual_state="Already installed",
                error="The package is already installed. Use the application removal page if you need to uninstall it.",
                warnings=[],
            )
        if package.source == "winget":
            status = winget_status or find_winget()
            if not status.available:
                raise ServiceError(status.error or "winget is not available.")
            argv = [
                status.path,
                "install",
                "--id",
                package.id,
                "--exact",
                "--source",
                "winget",
                "--silent",
                "--accept-package-agreements",
                "--accept-source-agreements",
                "--disable-interactivity",
            ]
            progress(f"Installing {package.name} with winget")
            result = CommandRunner().run(
                argv,
                timeout=1800,
                on_output=progress,
                cancel_event=cancel,
            )
            if not result.launched:
                raise ServiceError(result.error or "winget could not be started.")
            if result.cancelled:
                raise ServiceError("The installation was cancelled. The package may be partially installed; verify it before retrying.")
            if result.exit_code != 0:
                post = check_installed(package, status)
                if post.installed:
                    return ApplyOutcome(
                        success=True,
                        previous_state="Not installed",
                        requested_state=f"Install {package.name}",
                        actual_state="Installed; winget returned a non-zero exit code",
                        warnings=[
                            f"winget returned exit {result.exit_code}, but independent verification found the package installed.",
                            "Review the winget output for warnings.",
                        ],
                        stdout=result.tail(4000),
                        stderr=result.tail(2000),
                        exit_code=result.exit_code,
                        details={"package": package.id, "installer": "winget"},
                        rollback={
                            "strategy": "software_install",
                            "package": package.id,
                            "name": package.name,
                            "summary": f"Uninstall {package.name}",
                        },
                    )
                raise ServiceError(
                    f"winget install failed (exit {result.exit_code}): "
                    f"{result.tail(1200) or 'no output'}"
                )
            return ApplyOutcome(
                success=True,
                previous_state="Not installed",
                requested_state=f"Install {package.name}",
                actual_state="Install command completed; verification pending",
                warnings=[
                    "The package and source agreements were accepted for this operation.",
                    "A restart may be required by the installer.",
                    "Review the installed application separately; WinFirst did not download or inspect its files.",
                ],
                stdout=result.tail(4000),
                exit_code=result.exit_code,
                details={"package": package.id, "installer": "winget"},
                rollback={
                    "strategy": "software_install",
                    "package": package.id,
                    "name": package.name,
                    "summary": f"Uninstall {package.name}",
                },
            )
        installer = _installer_path(package)
        progress("Checking the installer sha256")
        _verify_installer_hash(package, installer)
        if installer.suffix.casefold() == ".msi":
            argv = ["msiexec.exe", "/i", str(installer), "/qn", "/norestart"] + list(package.silent_args)
        else:
            argv = [str(installer)] + list(package.silent_args)
        progress(f"Running the local installer for {package.name}")
        result = CommandRunner().run(argv, timeout=1800, on_output=progress, cancel_event=cancel)
        if not result.launched:
            raise ServiceError(result.error or "The installer could not be started.")
        if result.cancelled:
            raise ServiceError("The installation was cancelled. The installer may have partially completed.")
        allowed = {0, 1641, 3010}
        if result.exit_code not in allowed:
            post = check_installed(package)
            if post.installed:
                return ApplyOutcome(
                    success=True,
                    previous_state="Not installed",
                    requested_state=f"Install {package.name}",
                    actual_state="Installed; the installer returned a non-zero exit code",
                    warnings=[
                        f"The installer returned exit {result.exit_code}, but independent verification found it installed."
                    ],
                    stdout=result.tail(4000),
                    stderr=result.tail(2000),
                    exit_code=result.exit_code,
                    restart_required=result.exit_code in {1641, 3010},
                    details={"package": package.id, "installer": "local"},
                )
            raise ServiceError(
                f"The local installer failed (exit {result.exit_code}): {result.tail(1200) or 'no output'}"
            )
        return ApplyOutcome(
            success=True,
            previous_state="Not installed",
            requested_state=f"Install {package.name}",
            actual_state="Installer completed; verification pending",
            warnings=[
                "Local installers are run with the arguments defined in the catalog. Review the catalog entry first.",
                "A restart may be required.",
            ],
            stdout=result.tail(4000),
            exit_code=result.exit_code,
            restart_required=result.exit_code in {1641, 3010},
            details={"package": package.id, "installer": "local"},
        )

    def verify() -> VerifyOutcome:
        check = check_installed(package, winget_status)
        if check.error:
            return VerifyOutcome(False, "Unknown", f"Independent verification failed: {check.error}")
        return VerifyOutcome(
            ok=check.installed,
            actual_state="Installed" if check.installed else "Not installed",
            message=check.detail,
        )

    def before() -> str:
        return "Not installed" if package.source == "winget" else "Installation state checked by verification method"

    return OperationSpec(
        operation_id=f"software.install.{package.id}",
        name=f"Install {package.name}",
        category="software",
        description=package.description or f"Installs {package.name} from the verified software catalog.",
        risk=RiskLevel.HIGH,
        requires_admin=False,
        reversible=package.source == "winget",
        restart_required=True,
        support_check=(
            (
                lambda: (
                    bool(supported_status and supported_status.available),
                    (supported_status.error if supported_status else "") or "winget is not available.",
                )
            )
            if package.source == "winget"
            else None
        ),
        apply=apply,
        verify=verify,
        before=before,
        preview_text=f"{package.name} ({package.id}) from {package.source_label}",
        warnings=[
            "Software is installed from a catalog entry; the profile cannot supply commands or scripts.",
            "Accepting package agreements is a separate, explicit action.",
            "Windows may show a UAC prompt for machine-wide installers.",
            "Uninstalling later requires the vendor uninstaller or Apps settings; WinFirst cannot guarantee restoration.",
            "A restart may be required.",
        ],
        verification_method=package.verify_summary,
        details={
            "package": package.id,
            "source": package.source,
            "publisher": package.publisher,
            "catalog_version": package.version,
        },
    )
