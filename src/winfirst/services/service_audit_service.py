"""Read-first Windows service audit with carefully controlled changes."""
from __future__ import annotations

from dataclasses import dataclass, field

from winfirst.core.command_runner import CommandRunner, as_list
from winfirst.core.operation import ApplyOutcome, OperationSpec, VerifyOutcome
from winfirst.core.plan import command_step
from winfirst.core.risk_engine import RiskLevel
from winfirst.services.common import ps_json, require_windows, run_ok

PROTECTED_SERVICES = {
    # Security and update
    "windefend",
    "wscsvc",
    "securityhealthservice",
    "wdnissvc",
    "sense",
    "webthreatdefsvc",
    "webthreatdefusersvc",
    "mpssvc",
    "bfe",
    "appidsvc",
    "applockerfltr",
    "wuauserv",
    "bits",
    "cryptsvc",
    "trustedinstaller",
    "msiserver",
    "sppsvc",
    "keyiso",
    "ncryptsvc",
    "vaultsvc",
    "certpropsvc",
    "wlidsvc",
    "dsregsvc",
    # Core Windows and management
    "winmgmt",
    "rpcss",
    "dcomlaunch",
    "eventlog",
    "schedule",
    "samss",
    "gpsvc",
    "profsvc",
    "usermanager",
    "systemeventsbroker",
    "brokerinfrastructure",
    "coremessagingregistrar",
    "lsm",
    "power",
    "seclogon",
    "appinfo",
    "tokenbroker",
    # Networking
    "dnscache",
    "dhcp",
    "nsi",
    "netprofm",
    "nlasvc",
    "lanmanworkstation",
    "lanmanserver",
    # Devices, audio, printing and time
    "audiosrv",
    "audioendpointbuilder",
    "themes",
    "spooler",
    "w32time",
}

COMMONLY_NEEDED = {
    "wuauserv": "Windows Update",
    "bits": "Background Intelligent Transfer Service",
    "windefend": "Microsoft Defender Antivirus",
    "mpssvc": "Windows Firewall",
    "dnscache": "DNS Client",
    "dhcp": "DHCP Client",
    "lanmanworkstation": "Workstation (network shares)",
    "spooler": "Print Spooler",
    "schedule": "Task Scheduler",
    "eventlog": "Windows Event Log",
    "cryptsvc": "Cryptographic Services",
    "trustedinstaller": "Windows Modules Installer",
    "winmgmt": "WMI",
    "themes": "Themes",
    "audiosrv": "Windows Audio",
}

_START_MODES = {"auto": "Automatic", "demand": "Manual", "disabled": "Disabled", "boot": "Boot", "system": "System"}
_SERVICE_NAME_RE = None
# WinFirst never mass-disables services. A batch that would disable more than
# this many at once is rejected before a single command is built.
MAX_BATCH_DISABLE = 1


@dataclass
class ServiceInfo:
    name: str
    display_name: str
    state: str
    start_mode: str
    executable: str = ""
    account: str = ""
    description: str = ""
    protected: bool = False
    protection_reason: str = ""
    review_note: str = ""

    @property
    def start_mode_label(self) -> str:
        return _START_MODES.get(self.start_mode.lower(), self.start_mode or "Unknown")

    @property
    def enabled(self) -> bool:
        return self.start_mode.lower() != "disabled"


@dataclass
class ServiceSnapshot:
    services: list[ServiceInfo] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def find(self, name: str) -> ServiceInfo | None:
        for service in self.services:
            if service.name.casefold() == name.casefold():
                return service
        return None


_SCRIPT = r"""
try {
    $items = @(Get-CimInstance -ClassName Win32_Service -ErrorAction Stop | ForEach-Object {
        [pscustomobject]@{
            Name = [string]$_.Name; DisplayName = [string]$_.DisplayName;
            State = [string]$_.State; StartMode = [string]$_.StartMode;
            PathName = [string]$_.PathName; StartName = [string]$_.StartName;
            Description = [string]$_.Description
        }
    })
    $items | ConvertTo-Json -Depth 4 -Compress
} catch { '' }
"""


def get_services(runner: CommandRunner | None = None) -> ServiceSnapshot:
    require_windows("Service inventory")
    runner = runner or CommandRunner()
    try:
        data = ps_json(runner, _SCRIPT, timeout=90, default=[])
    except Exception as exc:
        return ServiceSnapshot(errors=[str(exc)])
    services = []
    for row in as_list(data):
        name = str(row.get("Name") or "").strip()
        if not name:
            continue
        protected = name.casefold() in PROTECTED_SERVICES
        reason = ""
        if protected:
            reason = "Security, update, networking, audio or core Windows service"
        start_mode = str(row.get("StartMode") or "")
        review = ""
        if start_mode.lower() == "disabled" and name.casefold() in COMMONLY_NEEDED:
            review = f"Disabled, but normally needed for {COMMONLY_NEEDED[name.casefold()]}."
        services.append(
            ServiceInfo(
                name=name,
                display_name=str(row.get("DisplayName") or name),
                state=str(row.get("State") or ""),
                start_mode=start_mode,
                executable=str(row.get("PathName") or ""),
                account=str(row.get("StartName") or ""),
                description=str(row.get("Description") or ""),
                protected=protected,
                protection_reason=reason,
                review_note=review,
            )
        )
    services.sort(key=lambda item: item.display_name.casefold())
    return ServiceSnapshot(services=services)


def validate_startup_mode_batch(services: list[ServiceInfo], mode: str) -> None:
    """Reject a batch that would mass-disable services.

    The check is deliberately independent of the UI so every caller is bound by
    the same policy.  Changing several services to Automatic or Manual is
    allowed; setting more than one service to Disabled is not.
    """
    if str(mode).lower() != "disabled":
        return
    if len(services) > MAX_BATCH_DISABLE:
        raise ValueError(
            "WinFirst does not mass-disable services. Select one service at a time "
            "and review it before disabling it."
        )


def build_startup_mode_spec(service: ServiceInfo, mode: str) -> OperationSpec:
    if service.protected:
        raise ValueError(f"{service.display_name} is protected from generic service changes.")
    normalized = mode.lower()
    if normalized not in {"auto", "demand", "disabled"}:
        raise ValueError("Unsupported service startup mode.")
    def plan_steps():
        return [
            command_step(
                ["sc.exe", "config", service.name, "start=", normalized],
                title=f"Set {service.name} startup type to {_START_MODES[normalized]}",
                detail="The change is read back through Win32_Service after it is applied.",
            )
        ]

    def apply(progress, cancel) -> ApplyOutcome:
        runner = CommandRunner()
        progress(f"Setting {service.display_name} startup to {_START_MODES[normalized]}")
        run_ok(
            runner,
            ["sc.exe", "config", service.name, "start=", normalized],
            timeout=60,
            error_prefix=f"Could not change {service.name}",
            cancel_event=cancel,
        )
        return ApplyOutcome(
            success=True,
            previous_state=service.start_mode_label,
            requested_state=_START_MODES[normalized],
            actual_state="Change applied; verification pending",
            warnings=[
                "Changing service startup can break dependent features, including security or networking.",
                "WinFirst does not mass-disable services.",
            ],
            rollback={
                "strategy": "service_startup",
                "name": service.name,
                "previous": service.start_mode,
                "summary": f"Restore {service.name} startup to {service.start_mode_label}",
            },
            details={"service": service.name, "mode": normalized},
        )

    def verify() -> VerifyOutcome:
        snapshot = get_services()
        current = snapshot.find(service.name)
        if current is None:
            return VerifyOutcome(False, "Unknown", "The service could not be read back.")
        return VerifyOutcome(
            ok=current.start_mode.lower() == normalized,
            actual_state=current.start_mode_label,
            message=f"Read back startup mode: {current.start_mode_label}",
        )

    return OperationSpec(
        operation_id=f"service.startup.{service.name}",
        name=f"Set service startup: {service.display_name}",
        category="services",
        description=f"Changes the startup mode of {service.name}.",
        risk=RiskLevel.HIGH,
        requires_admin=True,
        reversible=True,
        apply=apply,
        verify=verify,
        before=lambda: service.start_mode_label,
        plan_steps=plan_steps,
        preview_text=f"{service.display_name}: {service.start_mode_label} -> {_START_MODES[normalized]}",
        warnings=[
            "Only change services you understand. Dependencies may fail.",
            "Security, update, networking and critical system services are blocked from generic changes.",
        ],
        verification_method="Get-CimInstance Win32_Service start mode read-back",
    )


def build_start_stop_spec(service: ServiceInfo, start: bool) -> OperationSpec:
    if service.protected:
        raise ValueError(f"{service.display_name} is protected from generic service changes.")
    action = "start" if start else "stop"

    def plan_steps():
        return [
            command_step(
                ["sc.exe", action, service.name],
                title=f"{action.capitalize()} {service.name}",
                detail="The service state is read back through Win32_Service afterwards.",
            )
        ]

    def apply(progress, cancel) -> ApplyOutcome:
        runner = CommandRunner()
        progress(f"{action.capitalize()}ing {service.display_name}")
        run_ok(
            runner,
            ["sc.exe", action, service.name],
            timeout=120,
            error_prefix=f"Could not {action} {service.name}",
            cancel_event=cancel,
        )
        return ApplyOutcome(
            success=True,
            previous_state=service.state,
            requested_state="Running" if start else "Stopped",
            actual_state="Command accepted; verification pending",
            warnings=["Stopping a service can interrupt dependent features."],
            rollback={
                "strategy": "service_start_stop",
                "name": service.name,
                "previous_state": service.state,
                "summary": f"Restore {service.name} to {service.state}",
            },
        )

    def verify() -> VerifyOutcome:
        snapshot = get_services()
        current = snapshot.find(service.name)
        if current is None:
            return VerifyOutcome(False, "Unknown", "The service could not be read back.")
        expected = "running" if start else "stopped"
        return VerifyOutcome(
            ok=current.state.lower() == expected,
            actual_state=current.state,
            message=f"Read back service state: {current.state}",
        )

    return OperationSpec(
        operation_id=f"service.{action}.{service.name}",
        name=f"{action.capitalize()} service: {service.display_name}",
        category="services",
        description=f"{action.capitalize()}s the Windows service {service.name}.",
        risk=RiskLevel.MEDIUM,
        requires_admin=True,
        reversible=True,
        apply=apply,
        verify=verify,
        before=lambda: service.state,
        plan_steps=plan_steps,
        preview_text=f"{service.display_name}: {service.state} -> {'Running' if start else 'Stopped'}",
        warnings=["Stopping a service can interrupt dependent features."],
        verification_method="Get-CimInstance Win32_Service state read-back",
    )
