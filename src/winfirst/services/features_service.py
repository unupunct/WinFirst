"""Windows optional features and a curated set of optional capabilities.

Feature changes use the documented DISM PowerShell cmdlets with ``-NoRestart``.
A pending state is reported as pending rather than as an immediate success.
WinFirst never enables or disables multiple features as a bulk action.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from winfirst.core.command_runner import CommandRunner, as_list, ps_quote
from winfirst.core.operation import ApplyOutcome, OperationSpec, VerifyOutcome
from winfirst.core.plan import action_step
from winfirst.core.risk_engine import RiskLevel
from winfirst.services.common import ServiceError, ps_json, require_windows

ProgressFn = Callable[[str], None]

_FEATURE_LABELS = {
    "NetFx3": ".NET Framework 3.5 (includes .NET 2.0 and 3.0)",
    "Microsoft-Hyper-V-All": "Hyper-V",
    "Microsoft-Hyper-V": "Hyper-V",
    "VirtualMachinePlatform": "Virtual Machine Platform",
    "Microsoft-Windows-Subsystem-Linux": "Windows Subsystem for Linux",
    "Containers": "Containers",
    "Containers-DisposableClientVM": "Windows Sandbox",
    "TelnetClient": "Telnet Client",
    "TFTP": "TFTP Client",
    "SMB1Protocol": "SMB 1.0/CIFS File Sharing Support",
    "SMB1Protocol-Client": "SMB 1.0/CIFS Client",
    "SMB1Protocol-Server": "SMB 1.0/CIFS Server",
    "MicrosoftWindowsPowerShellV2": "Windows PowerShell 2.0 Engine",
    "MicrosoftWindowsPowerShellV2Root": "Windows PowerShell 2.0 Engine root",
    "Printing-Foundation-Features": "Printing Foundation Features",
    "Printing-Foundation-InternetPrinting-Client": "Internet Printing Client",
    "WindowsSandbox": "Windows Sandbox",
    "MicrosoftWindowsBackup": "Windows Backup",
    "SmbDirect": "SMB Direct",
    "WorkFolders-Client": "Work Folders Client",
    "Client-DeviceLockdown": "Assigned Access",
    "Internet-Explorer-Optional-amd64": "Internet Explorer 11",
    "MediaPlayback": "Media Features",
    "Microsoft-Windows-Media-Format": "Media Foundation",
    "Windows-Defender-ApplicationGuard": "Microsoft Defender Application Guard",
    "Microsoft-Hyper-V-Management-PowerShell": "Hyper-V PowerShell management tools",
    "Microsoft-Hyper-V-Management-Clients": "Hyper-V management clients",
    "ServicesForNFS-ClientOnly": "Client for NFS",
    "NFS-Client": "Client for NFS",
}

_CAPABILITY_LABELS = {
    "OpenSSH.Client": "OpenSSH Client",
    "OpenSSH.Server": "OpenSSH Server",
    "NetFX3": ".NET Framework 3.5",
    "MathRecognizer": "Math Recognizer",
    "Tools.DevelopmentMode": "Development Mode",
    "Language.Handwriting": "Handwriting recognition",
    "Language.OCR": "Optical character recognition",
    "Language.Speech": "Speech recognition",
    "Language.TextToSpeech": "Text to speech",
}

_FEATURE_SCRIPT = r"""
try {
    @(Get-WindowsOptionalFeature -Online -ErrorAction Stop | ForEach-Object {
        [pscustomobject]@{ Name = [string]$_.FeatureName; State = [string]$_.State }
    }) | ConvertTo-Json -Depth 3 -Compress
} catch { '' }
"""

_CAPABILITY_SCRIPT = r"""
try {
    $patterns = @('OpenSSH.Client*','OpenSSH.Server*','NetFX3*','MathRecognizer*',
        'Tools.DevelopmentMode*','Language.Handwriting*','Language.OCR*',
        'Language.Speech*','Language.TextToSpeech*')
    $all = @(Get-WindowsCapability -Online -ErrorAction Stop)
    $items = @()
    foreach ($pattern in $patterns) {
        $items += @($all | Where-Object { $_.Name -like $pattern } | ForEach-Object {
            [pscustomobject]@{ Name = [string]$_.Name; State = [string]$_.State }
        })
    }
    $items | ConvertTo-Json -Depth 3 -Compress
} catch { '' }
"""

_CACHE_LOCK = threading.Lock()
_CACHE: tuple[float, "FeatureSnapshot | None"] = (0.0, None)
_CACHE_TTL = 180.0


@dataclass
class FeatureInfo:
    name: str
    display_name: str
    state: str
    kind: str = "Feature"
    error: str = ""

    @property
    def installed(self) -> bool:
        return self.state.lower() in {"enabled", "enablepending"}

    @property
    def pending(self) -> bool:
        return self.state.lower() in {"enablepending", "disablepending"}

    @property
    def available(self) -> bool:
        return self.state.lower() not in {"notpresent", "unknown", ""}

    @property
    def state_label(self) -> str:
        return {
            "enabled": "Enabled",
            "disabled": "Disabled",
            "enablepending": "Enabled (restart pending)",
            "disablepending": "Disabled (restart pending)",
            "notpresent": "Not present",
            "notsupported": "Not supported",
        }.get(self.state.lower(), self.state or "Unknown")

    @property
    def restart_required(self) -> bool:
        return self.pending


@dataclass
class FeatureSnapshot:
    features: list[FeatureInfo] = field(default_factory=list)
    capabilities: list[FeatureInfo] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    collected_at: float = field(default_factory=time.time)

    @property
    def requires_admin_error(self) -> bool:
        text = " ".join(self.errors).lower()
        return "access" in text or "elevation" in text or "administrator" in text

    def all_items(self) -> list[FeatureInfo]:
        return self.features + self.capabilities

    def find(self, name: str) -> FeatureInfo | None:
        for item in self.all_items():
            if item.name.casefold() == name.casefold():
                return item
        return None


def _display_name(name: str, kind: str) -> str:
    base = name.split("~", 1)[0]
    labels = _FEATURE_LABELS if kind == "Feature" else _CAPABILITY_LABELS
    if base in labels:
        return labels[base]
    if base.startswith("Language."):
        package = name.split("~~~", 1)[-1] if "~~~" in name else ""
        suffix = f" ({package})" if package else ""
        return f"{_CAPABILITY_LABELS.get(base, base)} language{suffix}"
    return base


def _run_snapshot(force: bool = False) -> FeatureSnapshot:
    require_windows("Windows feature inspection")
    global _CACHE
    with _CACHE_LOCK:
        if not force and _CACHE[1] is not None and time.time() - _CACHE[0] < _CACHE_TTL:
            return _CACHE[1]
        runner = CommandRunner()
        snapshot = FeatureSnapshot()
        try:
            data = ps_json(runner, _FEATURE_SCRIPT, timeout=180, default=[])
            for row in as_list(data):
                if not isinstance(row, dict) or not row.get("Name"):
                    continue
                name = str(row.get("Name"))
                state = str(row.get("State") or "Unknown")
                snapshot.features.append(FeatureInfo(name, _display_name(name, "Feature"), state, "Feature"))
            if not snapshot.features:
                snapshot.errors.append(
                    "No optional feature data was returned. This usually means administrator rights are required."
                )
        except Exception as exc:
            snapshot.errors.append(f"Optional features: {exc}")
        try:
            data = ps_json(runner, _CAPABILITY_SCRIPT, timeout=180, default=[])
            seen: set[str] = set()
            for row in as_list(data):
                if not isinstance(row, dict) or not row.get("Name"):
                    continue
                name = str(row.get("Name"))
                if name in seen:
                    continue
                seen.add(name)
                state = str(row.get("State") or "Unknown")
                snapshot.capabilities.append(
                    FeatureInfo(name, _display_name(name, "Capability"), state, "Capability")
                )
            if not snapshot.capabilities and not snapshot.errors:
                snapshot.errors.append("No capability data was returned; administrator rights may be required.")
        except Exception as exc:
            snapshot.errors.append(f"Optional capabilities: {exc}")
        snapshot.features.sort(key=lambda item: (not item.available, item.display_name.casefold()))
        snapshot.capabilities.sort(key=lambda item: item.display_name.casefold())
        _CACHE = (time.time(), snapshot)
        return snapshot


def get_feature_snapshot(force: bool = False) -> FeatureSnapshot:
    return _run_snapshot(force=force)


def invalidate_feature_cache() -> None:
    global _CACHE
    with _CACHE_LOCK:
        _CACHE = (0.0, None)


def _read_state(feature: FeatureInfo) -> str | None:
    runner = CommandRunner()
    if feature.kind == "Capability":
        script = (
            f"$c = Get-WindowsCapability -Online -Name {ps_quote(feature.name)} -ErrorAction Stop; "
            "$c.State.ToString()"
        )
    else:
        script = (
            f"$f = Get-WindowsOptionalFeature -Online -FeatureName {ps_quote(feature.name)} -ErrorAction Stop; "
            "$f.State.ToString()"
        )
    result = runner.powershell(script, timeout=90)
    if not result.launched or result.exit_code != 0:
        return None
    lines = (result.stdout or "").strip().splitlines()
    return lines[-1].strip() if lines else None


def _refuse_unsafe_feature_action(feature: FeatureInfo, enable: bool) -> None:
    """Block feature changes that weaken the security posture.

    This is a policy check, not a compatibility check: it applies even when the
    caller is elevated and has confirmed the change.
    """
    name = feature.name.casefold()
    if not enable and name.startswith("windows-defender"):
        raise ValueError(
            "Defender features cannot be disabled by WinFirst. "
            "Windows Security settings are read-only in this application."
        )
    if enable and name.startswith("smb1protocol"):
        raise ValueError(
            "SMB 1.0 is an obsolete, insecure protocol and cannot be enabled by WinFirst."
        )
    if enable and name.startswith("microsoftwindowspowershellv2"):
        raise ValueError(
            "Windows PowerShell 2.0 is an obsolete engine and cannot be enabled by WinFirst."
        )


def build_change_spec(feature: FeatureInfo, enable: bool) -> OperationSpec:
    if not feature.available:
        raise ValueError(f"{feature.display_name} is not present on this system.")
    if feature.pending:
        raise ValueError("This feature has a restart pending. Restart Windows and refresh before changing it again.")
    if feature.installed == enable:
        raise ValueError(f"{feature.display_name} is already {'enabled' if enable else 'disabled'}.")
    _refuse_unsafe_feature_action(feature, enable)
    action = "enable" if enable else "disable"
    if feature.kind == "Capability":
        tool = "Add-WindowsCapability" if enable else "Remove-WindowsCapability"
        script = (
            f"{tool} -Online -Name {ps_quote(feature.name)} -ErrorAction Stop -NoRestart | Out-Null"
        )
        verification = "Get-WindowsCapability -Online -Name"
    else:
        tool = "Enable-WindowsOptionalFeature" if enable else "Disable-WindowsOptionalFeature"
        all_flag = " -All" if enable else ""
        script = (
            f"{tool} -Online -FeatureName {ps_quote(feature.name)}{all_flag} -NoRestart "
            "-ErrorAction Stop | Out-Null"
        )
        verification = "Get-WindowsOptionalFeature -Online -FeatureName"

    def plan_steps():
        return [
            action_step(
                f"{tool} for {feature.name}",
                detail=script,
            ),
            action_step(
                "Read the feature state back",
                detail=f"{verification} -Online -Name {feature.name} (a pending restart is reported, not hidden).",
            ),
        ]

    def apply(progress: ProgressFn, cancel) -> ApplyOutcome:
        runner = CommandRunner()
        progress(f"{action.capitalize()}ing {feature.display_name}")
        result = runner.powershell(script, timeout=1800, cancel_event=cancel)
        if not result.launched:
            raise ServiceError(result.error or "PowerShell could not be started.")
        if result.exit_code != 0:
            detail = (result.stderr or result.stdout).strip()[:1200]
            raise ServiceError(
                f"{tool} failed (exit {result.exit_code}): {detail or 'no error text was returned.'}"
            )
        invalidate_feature_cache()
        return ApplyOutcome(
            success=True,
            previous_state=feature.state_label,
            requested_state="Enabled" if enable else "Disabled",
            actual_state="Command completed; verification pending",
            warnings=[
                "A restart may be required to finish this change.",
                "Windows may need internet access or installation media to enable a feature.",
                "Disabling a feature can break applications that depend on it.",
            ],
            restart_required=True,
            details={"feature": feature.name, "kind": feature.kind, "action": action},
            exit_code=result.exit_code,
        )

    def verify() -> VerifyOutcome:
        state = _read_state(feature)
        if state is None:
            return VerifyOutcome(False, "Unknown", "The feature state could not be read back.")
        lowered = state.lower()
        if enable:
            ok = lowered in {"enabled", "enablepending"}
        else:
            ok = lowered in {"disabled", "disablepending"}
        pending = lowered in {"enablepending", "disablepending"}
        return VerifyOutcome(
            ok=ok,
            actual_state=state,
            message=(
                "The feature reports a pending state; restart Windows to complete the change."
                if pending
                else f"Read the feature state back with {verification}."
            ),
            details={"pending_restart": pending},
        )

    return OperationSpec(
        operation_id=f"feature.{action}.{feature.name}",
        name=f"{action.capitalize()} Windows feature: {feature.display_name}",
        category="features",
        description=f"{action.capitalize()}s a Windows optional {feature.kind.lower()}.",
        risk=RiskLevel.HIGH,
        requires_admin=True,
        reversible=False,
        restart_required=True,
        apply=apply,
        verify=verify,
        before=lambda: feature.state_label,
        plan_steps=plan_steps,
        preview_text=f"{feature.display_name}: {feature.state_label} -> {'Enabled' if enable else 'Disabled'}",
        warnings=[
            "This is a single, explicit feature change; WinFirst does not change features in bulk.",
            "A restart may be required. Pending restarts are reported, not hidden.",
            "Feature changes can require internet access, installation media, or a Windows source.",
        ],
        verification_method=f"Reads the state back with {verification}.",
        details={"feature": feature.name, "kind": feature.kind},
    )
