"""Security posture and Windows Update inspection.

Read-only by design for security controls: WinFirst reports Defender, Firewall,
UAC, Secure Boot, TPM and activation state but never disables them.  Windows
Update scanning uses the documented update agent COM interface and never
installs or restarts without an explicit user action.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from winfirst.core.command_runner import CommandRunner, as_list
from winfirst.core.os_detection import detect_pending_reboot
from winfirst.services.common import ServiceError
from winfirst.services.win_registry import read_value

_CACHE: dict[str, tuple[float, Any]] = {}
_CACHE_LOCK = threading.Lock()
DEFAULT_TTL = 60


def clear_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


def _cached(key: str, ttl: int, factory: Callable[[], Any]) -> Any:
    now = time.monotonic()
    with _CACHE_LOCK:
        item = _CACHE.get(key)
        if item and now - item[0] < ttl:
            return item[1]
    value = factory()
    with _CACHE_LOCK:
        _CACHE[key] = (now, value)
    return value


def _ps_json(script: str, *, timeout: float = 60) -> Any:
    runner = CommandRunner()
    if not runner.powershell_available:
        raise ServiceError("Windows PowerShell is not available.")
    return runner.powershell_json(script, timeout=timeout, default=None)


@dataclass
class DefenderSummary:
    available: bool
    realtime_enabled: bool | None = None
    antivirus_enabled: bool | None = None
    am_service_enabled: bool | None = None
    tamper_protected: bool | None = None
    signature_age_days: int | None = None
    signature_updated: str = ""
    last_quick_scan: str = ""
    third_party: list[str] = field(default_factory=list)
    error: str = ""


@dataclass
class FirewallProfile:
    name: str
    enabled: bool
    default_inbound: str = ""
    default_outbound: str = ""


@dataclass
class UacSettings:
    enabled: bool
    consent_prompt: str = ""
    consent_value: int | None = None
    secure_desktop: bool | None = None
    admin_approval_mode: bool | None = None
    note: str = ""


@dataclass
class SecureBootStatus:
    available: bool
    enabled: bool = False
    detail: str = ""
    reason: str = ""


@dataclass
class TpmStatus:
    available: bool
    present: bool = False
    ready: bool = False
    enabled: bool = False
    activated: bool = False
    version: str = ""
    detail: str = ""
    reason: str = ""


@dataclass
class ActivationStatus:
    available: bool
    activated: bool = False
    status_text: str = ""
    product_name: str = ""
    detail: str = ""
    reason: str = ""


@dataclass
class BitLockerVolume:
    mount_point: str
    volume_status: str
    protection_status: str
    encryption_percentage: int | None = None
    encryption_method: str = ""


@dataclass
class WindowsUpdateStatus:
    available: bool
    service_state: str = ""
    service_start_mode: str = ""
    hotfixes: list[dict[str, Any]] = field(default_factory=list)
    pending_reboot: bool = False
    pending_reasons: list[str] = field(default_factory=list)
    policy_managed: bool = False
    policy_detail: str = ""
    error: str = ""


@dataclass
class AvailableUpdate:
    title: str
    severity: str = ""
    downloaded: bool = False
    reboot_required: bool = False
    kb_ids: list[str] = field(default_factory=list)


_CONFIRM_SECURE_BOOT = r"""
try {
    $value = Confirm-SecureBootUEFI -ErrorAction Stop
    [pscustomobject]@{ Available = $true; Enabled = [bool]$value; Reason = ''; Detail = 'UEFI Secure Boot state read from firmware' } |
        ConvertTo-Json -Compress
} catch {
    [pscustomobject]@{ Available = $false; Enabled = $false; Reason = $_.Exception.Message; Detail = '' } |
        ConvertTo-Json -Compress
}
"""


def get_defender_summary(force: bool = False) -> DefenderSummary:
    if force:
        with _CACHE_LOCK:
            _CACHE.pop("defender", None)

    def factory() -> DefenderSummary:
        script = r"""
$result = [ordered]@{ Available = $false; Reason = ''; RealTime = $null; Antivirus = $null;
    AMService = $null; Tamper = $null; SignatureAge = $null; SignatureUpdated = ''; QuickScan = '' }
try {
    $s = Get-MpComputerStatus -ErrorAction Stop
    $result.Available = $true
    $result.RealTime = [bool]$s.RealTimeProtectionEnabled
    $result.Antivirus = [bool]$s.AntivirusEnabled
    $result.AMService = [bool]$s.AMServiceEnabled
    $result.Tamper = [bool]$s.IsTamperProtected
    $result.SignatureAge = [int]$s.AntivirusSignatureAge
    if ($s.AntivirusSignatureLastUpdated) { $result.SignatureUpdated = $s.AntivirusSignatureLastUpdated.ToString('yyyy-MM-dd HH:mm') }
    if ($s.QuickScanEndTime) { $result.QuickScan = $s.QuickScanEndTime.ToString('yyyy-MM-dd HH:mm') }
} catch {
    $result.Reason = $_.Exception.Message
}
$thirdParty = @()
try {
    $thirdParty = @(Get-CimInstance -Namespace 'root/SecurityCenter2' -ClassName AntiVirusProduct -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty displayName)
} catch { }
[pscustomobject]@{ Defender = $result; ThirdParty = $thirdParty } | ConvertTo-Json -Depth 4 -Compress
"""
        try:
            data = _ps_json(script, timeout=45) or {}
        except Exception as exc:
            return DefenderSummary(False, error=str(exc))
        defender = data.get("Defender") or {}
        third_party = [str(x) for x in (data.get("ThirdParty") or []) if x]
        if not defender.get("Available"):
            reason = str(defender.get("Reason") or "Defender status is unavailable")
            if third_party:
                reason += f". Registered antivirus products: {', '.join(third_party)}"
            return DefenderSummary(False, third_party=third_party, error=reason)
        return DefenderSummary(
            available=True,
            realtime_enabled=defender.get("RealTime"),
            antivirus_enabled=defender.get("Antivirus"),
            am_service_enabled=defender.get("AMService"),
            tamper_protected=defender.get("Tamper"),
            signature_age_days=defender.get("SignatureAge"),
            signature_updated=str(defender.get("SignatureUpdated") or ""),
            last_quick_scan=str(defender.get("QuickScan") or ""),
            third_party=third_party,
        )

    return _cached("defender", DEFAULT_TTL, factory)


def get_firewall_profiles(force: bool = False) -> list[FirewallProfile]:
    if force:
        with _CACHE_LOCK:
            _CACHE.pop("firewall", None)

    def factory() -> list[FirewallProfile]:
        script = r"""
$profiles = @(Get-NetFirewallProfile -ErrorAction Stop | ForEach-Object {
    [pscustomobject]@{ Name = $_.Name; Enabled = [bool]$_.Enabled;
        Inbound = [string]$_.DefaultInboundAction; Outbound = [string]$_.DefaultOutboundAction }
})
$profiles | ConvertTo-Json -Depth 3 -Compress
"""
        data = _ps_json(script, timeout=45)
        profiles = []
        for row in as_list(data):
            profiles.append(
                FirewallProfile(
                    name=str(row.get("Name") or "Unknown"),
                    enabled=bool(row.get("Enabled")),
                    default_inbound=str(row.get("Inbound") or ""),
                    default_outbound=str(row.get("Outbound") or ""),
                )
            )
        return profiles

    return _cached("firewall", DEFAULT_TTL, factory)


_CONSENT_LABELS = {
    0: "Elevate without prompting",
    1: "Prompt for credentials on the secure desktop",
    2: "Prompt for consent on the secure desktop",
    3: "Prompt for credentials",
    4: "Prompt for consent",
    5: "Prompt for consent for non-Windows binaries",
}


def get_uac_settings() -> UacSettings:
    path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System"
    enable_lua = read_value("HKLM", path, "EnableLUA")
    consent = read_value("HKLM", path, "ConsentPromptBehaviorAdmin")
    secure = read_value("HKLM", path, "PromptOnSecureDesktop")
    admin_approval = read_value("HKLM", path, "FilterAdministratorToken")
    if not enable_lua.exists:
        return UacSettings(False, note="EnableLUA is not present; UAC state could not be confirmed.")
    enabled = int(enable_lua.value or 0) == 1
    consent_value = int(consent.value) if consent.exists else None
    note = ""
    if enabled and consent_value == 0:
        note = "UAC is enabled but administrators are elevated without a prompt."
    return UacSettings(
        enabled=enabled,
        consent_prompt=_CONSENT_LABELS.get(consent_value, "Unknown") if consent_value is not None else "",
        consent_value=consent_value,
        secure_desktop=bool(int(secure.value or 0)) if secure.exists else None,
        admin_approval_mode=bool(int(admin_approval.value or 0)) if admin_approval.exists else None,
        note=note,
    )


def get_secure_boot_status(force: bool = False) -> SecureBootStatus:
    if force:
        with _CACHE_LOCK:
            _CACHE.pop("secureboot", None)

    def factory() -> SecureBootStatus:
        try:
            data = _ps_json(_CONFIRM_SECURE_BOOT, timeout=30) or {}
        except Exception as exc:
            return SecureBootStatus(False, reason=str(exc))
        available = bool(data.get("Available"))
        return SecureBootStatus(
            available=available,
            enabled=bool(data.get("Enabled")),
            detail=str(data.get("Detail") or ""),
            reason=str(data.get("Reason") or "") if not available else "",
        )

    return _cached("secureboot", 300, factory)


def get_tpm_status(force: bool = False) -> TpmStatus:
    if force:
        with _CACHE_LOCK:
            _CACHE.pop("tpm", None)

    def factory() -> TpmStatus:
        script = r"""
try {
    $t = Get-Tpm -ErrorAction Stop
    [pscustomobject]@{ Available = $true; Present = [bool]$t.TpmPresent; Ready = [bool]$t.TpmReady;
        Enabled = [bool]$t.TpmEnabled; Activated = [bool]$t.TpmActivated;
        Version = [string]$t.ManufacturerVersion; Reason = '' } | ConvertTo-Json -Compress
} catch {
    [pscustomobject]@{ Available = $false; Present = $false; Ready = $false; Enabled = $false;
        Activated = $false; Version = ''; Reason = $_.Exception.Message } | ConvertTo-Json -Compress
}
"""
        try:
            data = _ps_json(script, timeout=30) or {}
        except Exception as exc:
            return TpmStatus(False, reason=str(exc))
        available = bool(data.get("Available"))
        present = bool(data.get("Present"))
        detail = "TPM is present and ready." if (present and data.get("Ready")) else (
            "TPM is present but not ready." if present else "No TPM detected."
        )
        return TpmStatus(
            available=available,
            present=present,
            ready=bool(data.get("Ready")),
            enabled=bool(data.get("Enabled")),
            activated=bool(data.get("Activated")),
            version=str(data.get("Version") or ""),
            detail=detail,
            reason=str(data.get("Reason") or "") if not available else "",
        )

    return _cached("tpm", 300, factory)


_LICENSE_STATUS = {
    0: "Unlicensed",
    1: "Licensed",
    2: "Out-of-box grace period",
    3: "Out-of-tolerance grace period",
    4: "Non-genuine grace period",
    5: "Notification",
    6: "Extended grace period",
}


def get_activation_status(force: bool = False) -> ActivationStatus:
    if force:
        with _CACHE_LOCK:
            _CACHE.pop("activation", None)

    def factory() -> ActivationStatus:
        script = r"""
try {
    $appId = '55c92734-d682-4d71-983e-d6ec3f16059f'
    $products = @(Get-CimInstance -ClassName SoftwareLicensingProduct -Filter "PartialProductKey is not null" -ErrorAction Stop |
        Where-Object { $_.ApplicationId -eq $appId })
    if ($products.Count -eq 0) {
        [pscustomobject]@{ Available = $false; Reason = 'No active Windows license was found.' } | ConvertTo-Json -Compress
    } else {
        $p = $products | Select-Object -First 1
        [pscustomobject]@{ Available = $true; Status = [int]$p.LicenseStatus; Name = [string]$p.Name;
            Description = [string]$p.Description; Reason = '' } | ConvertTo-Json -Compress
    }
} catch {
    [pscustomobject]@{ Available = $false; Reason = $_.Exception.Message } | ConvertTo-Json -Compress
}
"""
        try:
            data = _ps_json(script, timeout=45) or {}
        except Exception as exc:
            return ActivationStatus(False, reason=str(exc))
        if not data.get("Available"):
            return ActivationStatus(False, reason=str(data.get("Reason") or "Activation status unavailable."))
        status = int(data.get("Status") or 0)
        return ActivationStatus(
            available=True,
            activated=status == 1,
            status_text=_LICENSE_STATUS.get(status, f"Unknown status ({status})"),
            product_name=str(data.get("Description") or data.get("Name") or ""),
            detail="No product key or license secret is read or displayed.",
        )

    return _cached("activation", 300, factory)


def get_bitlocker_volumes(force: bool = False) -> list[BitLockerVolume]:
    if force:
        with _CACHE_LOCK:
            _CACHE.pop("bitlocker", None)

    def factory() -> list[BitLockerVolume]:
        script = r"""
try {
    $items = @(Get-BitLockerVolume -ErrorAction Stop | ForEach-Object {
        [pscustomobject]@{ Mount = [string]$_.MountPoint; VolumeStatus = [string]$_.VolumeStatus;
            Protection = [string]$_.ProtectionStatus; Percentage = $_.EncryptionPercentage;
            Method = [string]$_.EncryptionMethod }
    })
    $items | ConvertTo-Json -Depth 3 -Compress
} catch { '' }
"""
        try:
            data = _ps_json(script, timeout=45)
        except Exception:
            return []
        return [
            BitLockerVolume(
                mount_point=str(row.get("Mount") or ""),
                volume_status=str(row.get("VolumeStatus") or ""),
                protection_status=str(row.get("Protection") or ""),
                encryption_percentage=row.get("Percentage"),
                encryption_method=str(row.get("Method") or ""),
            )
            for row in as_list(data)
        ]

    return _cached("bitlocker", 120, factory)


_UPDATE_POLICY_KEYS = (
    (r"SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate", "WUServer"),
    (r"SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate", "NoAutoUpdate"),
    (r"SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate", "TargetGroup"),
    (r"SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU", "NoAutoUpdate"),
    (r"SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU", "UseWUServer"),
)


def get_update_status(force: bool = False) -> WindowsUpdateStatus:
    if force:
        with _CACHE_LOCK:
            _CACHE.pop("update", None)

    def factory() -> WindowsUpdateStatus:
        pending, reasons = detect_pending_reboot()
        policy_detail = ""
        for path, name in _UPDATE_POLICY_KEYS:
            reg = read_value("HKLM", path, name)
            if reg.exists:
                policy_detail = f"{name} is set under HKLM\\{path}"
                break
        policy_managed = bool(policy_detail)
        script = r"""
$result = [ordered]@{ Available = $false; State = ''; StartMode = ''; Hotfixes = @(); Reason = '' }
try {
    $svc = Get-CimInstance -ClassName Win32_Service -Filter "Name='wuauserv'" -ErrorAction Stop
    $result.Available = $true
    $result.State = [string]$svc.State
    $result.StartMode = [string]$svc.StartMode
    $result.Hotfixes = @(Get-HotFix -ErrorAction SilentlyContinue | Sort-Object InstalledOn -Descending |
        Select-Object -First 8 HotFixID, Description, InstalledOn)
} catch { $result.Reason = $_.Exception.Message }
$result | ConvertTo-Json -Depth 4 -Compress
"""
        try:
            data = _ps_json(script, timeout=60) or {}
        except Exception as exc:
            return WindowsUpdateStatus(
                False, pending_reboot=pending, pending_reasons=reasons,
                policy_managed=policy_managed, policy_detail=policy_detail, error=str(exc)
            )
        hotfixes = []
        for row in as_list(data.get("Hotfixes")):
            installed = row.get("InstalledOn")
            hotfixes.append(
                {
                    "id": str(row.get("HotFixID") or ""),
                    "description": str(row.get("Description") or ""),
                    "installed_on": _format_ps_date(installed),
                }
            )
        return WindowsUpdateStatus(
            available=bool(data.get("Available")),
            service_state=str(data.get("State") or ""),
            service_start_mode=str(data.get("StartMode") or ""),
            hotfixes=hotfixes,
            pending_reboot=pending,
            pending_reasons=reasons,
            policy_managed=policy_managed,
            policy_detail=policy_detail,
            error=str(data.get("Reason") or "") if not data.get("Available") else "",
        )

    return _cached("update", 60, factory)


def _format_ps_date(value: Any) -> str:
    if not value:
        return ""
    if isinstance(value, str):
        return value[:10]
    if isinstance(value, dict):
        # .NET DateTime serialised by ConvertTo-Json
        year = value.get("year")
        month = value.get("month")
        day = value.get("day")
        if year and month and day:
            return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
    return str(value)[:10]


def scan_available_updates(*, timeout: float = 300, cancel_event=None) -> list[AvailableUpdate]:
    runner = CommandRunner()
    script = r"""
$session = New-Object -ComObject Microsoft.Update.Session
$searcher = $session.CreateUpdateSearcher()
$result = $searcher.Search("IsInstalled=0 and IsHidden=0")
$items = @($result.Updates | ForEach-Object {
    [pscustomobject]@{
        Title = [string]$_.Title
        Severity = [string]$_.MsrcSeverity
        Downloaded = [bool]$_.IsDownloaded
        RebootRequired = [bool]$_.RebootRequired
        KB = @($_.KBArticleIDs | ForEach-Object { [string]$_ })
    }
})
$items | ConvertTo-Json -Depth 4 -Compress
"""
    data = runner.powershell_json(script, timeout=timeout, default=[], cancel_event=cancel_event)
    updates: list[AvailableUpdate] = []
    for row in as_list(data):
        updates.append(
            AvailableUpdate(
                title=str(row.get("Title") or "Unnamed update"),
                severity=str(row.get("Severity") or ""),
                downloaded=bool(row.get("Downloaded")),
                reboot_required=bool(row.get("RebootRequired")),
                kb_ids=[str(k) for k in (row.get("KB") or [])],
            )
        )
    return updates


def security_findings() -> list[dict[str, Any]]:
    """Transparent findings for the dashboard and security page."""
    findings: list[dict[str, Any]] = []
    defender = get_defender_summary()
    if not defender.available:
        findings.append(
            {
                "key": "defender",
                "title": "Defender status unavailable",
                "status": "info",
                "detail": defender.error or "Microsoft Defender is not reporting status. A third-party antivirus may be in use.",
            }
        )
    else:
        findings.append(
            {
                "key": "defender",
                "title": "Real-time protection",
                "status": "good" if defender.realtime_enabled else "critical",
                "detail": "Enabled" if defender.realtime_enabled else "Real-time protection reports as disabled.",
            }
        )
        if defender.signature_age_days is not None and defender.signature_age_days > 7:
            findings.append(
                {
                    "key": "defender_signatures",
                    "title": "Defender signatures are old",
                    "status": "warning",
                    "detail": f"Signatures are {defender.signature_age_days} days old.",
                }
            )
    try:
        profiles = get_firewall_profiles()
        disabled = [profile.name for profile in profiles if not profile.enabled]
        findings.append(
            {
                "key": "firewall",
                "title": "Windows Firewall",
                "status": "critical" if disabled else ("good" if profiles else "info"),
                "detail": (
                    f"Disabled profiles: {', '.join(disabled)}"
                    if disabled
                    else ("All detected profiles are enabled." if profiles else "Profiles could not be read.")
                ),
            }
        )
    except Exception as exc:
        findings.append(
            {"key": "firewall", "title": "Windows Firewall", "status": "info", "detail": str(exc)}
        )
    uac = get_uac_settings()
    findings.append(
        {
            "key": "uac",
            "title": "User Account Control",
            "status": "good" if uac.enabled else "critical",
            "detail": uac.note if uac.note else ("Enabled" if uac.enabled else "UAC reports as disabled."),
        }
    )
    secure = get_secure_boot_status()
    if secure.available:
        findings.append(
            {
                "key": "secure_boot",
                "title": "Secure Boot",
                "status": "good" if secure.enabled else "warning",
                "detail": "Enabled" if secure.enabled else "Secure Boot is supported but disabled.",
            }
        )
    else:
        findings.append(
            {"key": "secure_boot", "title": "Secure Boot", "status": "info",
             "detail": secure.reason or "Secure Boot status is unavailable (legacy BIOS or unsupported platform)."}
        )
    tpm = get_tpm_status()
    if tpm.available:
        findings.append(
            {
                "key": "tpm",
                "title": "TPM",
                "status": "good" if tpm.present and tpm.ready else ("warning" if tpm.present else "info"),
                "detail": tpm.detail,
            }
        )
    activation = get_activation_status()
    if activation.available:
        findings.append(
            {
                "key": "activation",
                "title": "Windows activation",
                "status": "good" if activation.activated else "warning",
                "detail": activation.status_text,
            }
        )
    update = get_update_status()
    if update.pending_reboot:
        findings.append(
            {
                "key": "pending_reboot",
                "title": "Restart pending",
                "status": "warning",
                "detail": "; ".join(update.pending_reasons),
            }
        )
    if update.available and update.service_state.lower() not in {"running", ""}:
        findings.append(
            {
                "key": "update_service",
                "title": "Windows Update service",
                "status": "warning",
                "detail": f"wuauserv is {update.service_state or 'unknown'} ({update.service_start_mode}).",
            }
        )
    return findings
