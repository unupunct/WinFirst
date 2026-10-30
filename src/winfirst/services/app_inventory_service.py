"""Application inventory across Appx, provisioned packages and desktop software.

Sources are merged without collapsing genuinely different products: Appx
packages are keyed by full package name, desktop entries by install location,
name, publisher and version.  Classification uses curated package and publisher
sets, never raw substring matching.
"""
from __future__ import annotations

import json
import logging
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

try:
    import winreg
except ImportError:  # pragma: no cover
    winreg = None  # type: ignore[assignment]

from winfirst.core.command_runner import CommandRunner, as_list
from winfirst.services.common import ServiceError, require_windows

_log = logging.getLogger("winfirst.inventory")

CATEGORY_LABELS = {
    "recommended_keep": "Recommended to keep",
    "optional_consumer": "Optional consumer app",
    "optional_third_party": "Optional third-party app",
    "advanced_candidate": "Advanced removal candidate",
    "protected_system": "Protected system component",
    "unknown": "Unknown / unclassified",
}

CATEGORY_ORDER = [
    "recommended_keep",
    "optional_consumer",
    "optional_third_party",
    "advanced_candidate",
    "unknown",
    "protected_system",
]

# Exact package-family matches (lower-case).  These are shared frameworks, shell
# components, store infrastructure and security components that must never be
# included in a broad removal operation.
PROTECTED_FAMILIES = {
    "microsoft.windowsstore",
    "microsoft.storepurchaseapp",
    "microsoft.services.store.engagement",
    "microsoft.windows.shellexperiencehost",
    "microsoft.windows.startmenuexperiencehost",
    "microsoft.windows.search",
    "microsoft.windows.contentdeliverymanager",
    "microsoft.windows.cortana",
    "microsoft.sechealthui",
    "microsoft.windows.defender",
    "microsoft.windows.securitycenter",
    "microsoft.aad.brokerplugin",
    "microsoft.accountcontrol",
    "microsoft.windows.apprep.chxapp",
    "microsoft.windows.assignedaccess",
    "microsoft.windows.deviceflow",
    "microsoft.windows.cloudexperiencehost",
    "microsoft.windows.notifications",
    "microsoft.windows.systemsettings",
    "microsoft.windows.immersivecontrolpanel",
}

PROTECTED_PREFIXES = (
    "microsoft.vclibs.",
    "microsoft.ui.xaml.",
    "microsoft.net.native.",
    "microsoft.windowsappruntime.",
    "microsoft.services.store",
    "microsoft.windowsstore",
    "microsoft.windows.defender",
    "microsoft.sechealthui",
    "microsoft.storepurchaseapp",
)

OPTIONAL_CONSUMER = {
    "microsoft.bingnews",
    "microsoft.bingweather",
    "microsoft.bingfinance",
    "microsoft.bingsports",
    "microsoft.zunemusic",
    "microsoft.zunevideo",
    "microsoft.skypeapp",
    "microsoft.gethelp",
    "microsoft.getstarted",
    "microsoft.windowsfeedbackhub",
    "microsoft.microsoftsolitairecollection",
    "microsoft.officehub",
    "microsoft.oneconnect",
    "microsoft.windowsmaps",
    "microsoft.mixedreality.portal",
    "microsoft.microsoft3dviewer",
    "microsoft.xboxapp",
    "microsoft.xboxgamingoverlay",
    "microsoft.xboxspeechtotextoverlay",
    "microsoft.yourphone",
    "microsoft.yourphoneapp",
    "microsoft.teams",
    "microsoft.family",
    "microsoft.powerautomatedesktop",
    "microsoft.microsoftjournal",
    "clipchamp.clipchamp",
    "microsoft.todos",
    "microsoft.outlookforwindows",
    "microsoft.copilot",
    "microsoft.windowssubsystemforandroid",
}

ADVANCED_CANDIDATES = {
    "microsoft.windows.devhome",
    "microsoft.powerautomatedesktop",
    "microsoft.microsoftjournal",
    "microsoft.windows.wingetdevelopment",
    "microsoft.windowssubsystemforandroid",
}

RECOMMENDED_KEEP = PROTECTED_FAMILIES | {
    "microsoft.windows.photos",
    "microsoft.windows.paint",
    "microsoft.windows.notepad",
    "microsoft.windows.calculator",
    "microsoft.windows.terminal",
    "microsoft.desktopappinstaller",
    "microsoft.windows.snippingtool",
    "microsoft.screensketch",
    "microsoft.windowscamera",
    "microsoft.windows.soundrecorder",
    "microsoft.windowsalarms",
    "microsoft.windowscommunicationsapps",
    "microsoft.people",
}

DESKTOP_PROTECTED = {
    "windows security",
    "microsoft defender antivirus",
    "microsoft defender",
    "windows update",
    "microsoft store",
    "microsoft edge",
    "microsoft edge update",
    "microsoft edge webview2 runtime",
    "microsoft visual c++ redistributable",
    "microsoft .net runtime",
    "windows driver package",
    "realtek audio driver",
}

DESKTOP_ADVANCED = {
    "onedrive",
    "microsoft onedrive",
    "skype",
    "cortana",
    "xbox",
}

_UNINSTALL_KEYS = (
    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", "Machine"),
    (
        winreg.HKEY_LOCAL_MACHINE,
        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
        "Machine (32-bit)",
    ),
    (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall", "Current user"),
) if winreg is not None else ()

_PACKAGE_RE = re.compile(r"^[A-Za-z0-9._~+\-]{3,240}$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class InventoryItem:
    id: str
    name: str
    source: str  # appx | provisioned | desktop
    scope: str
    publisher: str = ""
    version: str = ""
    install_date: str = ""
    install_location: str = ""
    package_full_name: str = ""
    package_family: str = ""
    uninstall_string: str = ""
    quiet_uninstall_string: str = ""
    estimated_size_mb: float | None = None
    category: str = "unknown"
    removable: bool = False
    safe_remove: bool = False
    removal_method: str = "none"
    protected_reason: str = ""
    consequences: str = ""
    reversible: bool = False
    restart_required: bool = False
    signature_kind: str = ""
    is_framework: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def category_label(self) -> str:
        return CATEGORY_LABELS.get(self.category, self.category)

    @property
    def display_scope(self) -> str:
        return self.scope

    @property
    def can_uninstall(self) -> bool:
        return self.removable and self.removal_method != "none"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "source": self.source,
            "scope": self.scope,
            "publisher": self.publisher,
            "version": self.version,
            "install_date": self.install_date,
            "install_location": self.install_location,
            "package_full_name": self.package_full_name,
            "package_family": self.package_family,
            "estimated_size_mb": self.estimated_size_mb,
            "category": self.category,
            "removable": self.removable,
            "safe_remove": self.safe_remove,
            "removal_method": self.removal_method,
            "protected_reason": self.protected_reason,
            "reversible": self.reversible,
            "restart_required": self.restart_required,
            "signature_kind": self.signature_kind,
        }


@dataclass
class InventorySnapshot:
    items: list[InventoryItem] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    sources: dict[str, int] = field(default_factory=dict)
    collected_at: str = field(default_factory=_now)

    def by_id(self, item_id: str) -> InventoryItem | None:
        for item in self.items:
            if item.id == item_id:
                return item
        return None

    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self.items:
            counts[item.category] = counts.get(item.category, 0) + 1
        return counts

    @property
    def safe_removal_candidates(self) -> list[InventoryItem]:
        return [item for item in self.items if item.safe_remove and item.can_uninstall]


def normalize_family(name: str) -> str:
    base = (name or "").strip().lower()
    if "_" in base:
        base = base.split("_", 1)[0]
    return base


def _is_protected_family(family: str) -> bool:
    if family in PROTECTED_FAMILIES:
        return True
    return any(family.startswith(prefix) for prefix in PROTECTED_PREFIXES)


def classify_appx(
    name: str,
    *,
    is_framework: bool = False,
    is_resource: bool = False,
    non_removable: bool = False,
    signature_kind: str = "",
) -> tuple[str, bool, str]:
    """Return (category, safe_remove, protected_reason)."""
    family = normalize_family(name)
    if is_framework:
        return "protected_system", False, "Shared framework package"
    if is_resource:
        return "protected_system", False, "Resource package"
    if non_removable:
        return "protected_system", False, "Windows marks this package as non-removable"
    if signature_kind.lower() == "system":
        return "protected_system", False, "System-signed Windows component"
    if _is_protected_family(family):
        return "protected_system", False, "Required Windows, Store or security component"
    if family in ADVANCED_CANDIDATES:
        return "advanced_candidate", True, ""
    if family in OPTIONAL_CONSUMER:
        return "optional_consumer", True, ""
    if family in RECOMMENDED_KEEP:
        return "recommended_keep", False, ""
    return "unknown", False, ""


def classify_desktop(name: str, publisher: str, *, system_component: bool = False) -> tuple[str, bool, str]:
    lowered = (name or "").strip().lower()
    pub = (publisher or "").strip().lower()
    if system_component:
        return "protected_system", False, "Registered as a system component"
    if lowered in DESKTOP_PROTECTED or any(lowered.startswith(item + " ") for item in DESKTOP_PROTECTED):
        return "protected_system", False, "Security, Store or Windows infrastructure"
    if lowered in DESKTOP_ADVANCED:
        return "advanced_candidate", False, ""
    if pub.startswith("microsoft"):
        return "unknown", False, ""
    return "optional_third_party", False, ""


def _clean_version(value: Any) -> str:
    text = str(value or "").strip()
    return text if _PACKAGE_RE.match(text) or re.match(r"^[0-9][0-9A-Za-z.\-+ ]{0,60}$", text) else text[:80]


def _package_id(source: str, key: str) -> str:
    return f"{source}:{key}"


class AppInventoryService:
    def __init__(self, runner: CommandRunner | None = None, admin_check=None) -> None:
        self.runner = runner or CommandRunner()
        self._admin_check = admin_check

    def _is_admin(self) -> bool:
        if self._admin_check is not None:
            return bool(self._admin_check())
        if sys.platform != "win32":
            return False
        try:
            import ctypes

            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False

    # ------------------------------------------------------------------ public
    def load(
        self,
        *,
        include_appx: bool = True,
        include_all_users: bool | None = None,
        include_provisioned: bool | None = None,
        include_desktop: bool = True,
    ) -> InventorySnapshot:
        require_windows("Application inventory")
        snapshot = InventorySnapshot()
        admin = self._is_admin()
        if include_all_users is None:
            include_all_users = admin
        if include_provisioned is None:
            include_provisioned = admin
        seen_appx: set[str] = set()
        if include_appx:
            self._load_appx(snapshot, seen_appx, all_users=False)
            if include_all_users:
                self._load_appx(snapshot, seen_appx, all_users=True, only_new=True)
        if include_provisioned:
            self._load_provisioned(snapshot)
        if include_desktop:
            self._load_desktop(snapshot)
        snapshot.items.sort(
            key=lambda item: (
                CATEGORY_ORDER.index(item.category) if item.category in CATEGORY_ORDER else 99,
                item.name.casefold(),
            )
        )
        return snapshot

    # ------------------------------------------------------------------- appx
    def _load_appx(
        self,
        snapshot: InventorySnapshot,
        seen: set[str],
        *,
        all_users: bool,
        only_new: bool = False,
    ) -> None:
        scope = "All users" if all_users else "Current user"
        flag = "-AllUsers" if all_users else ""
        script = f"""
$items = @(Get-AppxPackage {flag} -ErrorAction Stop | ForEach-Object {{
    [pscustomobject]@{{
        Name = [string]$_.Name
        PackageFullName = [string]$_.PackageFullName
        PackageFamilyName = [string]$_.PackageFamilyName
        Publisher = [string]$_.Publisher
        PublisherDisplayName = [string]$_.PublisherDisplayName
        Version = [string]$_.Version
        InstallLocation = [string]$_.InstallLocation
        SignatureKind = [string]$_.SignatureKind
        IsFramework = [bool]$_.IsFramework
        IsResourcePackage = [bool]$_.IsResourcePackage
        NonRemovable = [bool]$_.NonRemovable
        Architecture = [string]$_.Architecture
    }}
}})
$items | ConvertTo-Json -Depth 4 -Compress
"""
        try:
            data = self.runner.powershell_json(script, timeout=180, default=[])
        except Exception as exc:
            snapshot.errors.append(f"Appx inventory ({scope}) failed: {exc}")
            return
        added = 0
        for row in as_list(data):
            full_name = str(row.get("PackageFullName") or "").strip()
            if not full_name:
                continue
            if full_name in seen:
                if only_new:
                    continue
                continue
            seen.add(full_name)
            display_name = str(row.get("Name") or full_name)
            family = str(row.get("PackageFamilyName") or "").split("_", 1)[0] or normalize_family(display_name)
            category, safe_remove, reason = classify_appx(
                display_name,
                is_framework=bool(row.get("IsFramework")),
                is_resource=bool(row.get("IsResourcePackage")),
                non_removable=bool(row.get("NonRemovable")),
                signature_kind=str(row.get("SignatureKind") or ""),
            )
            protected = category == "protected_system"
            removable = not protected
            removal_method = "none"
            if removable:
                removal_method = "Remove-AppxPackage -AllUsers" if all_users else "Remove-AppxPackage"
            snapshot.items.append(
                InventoryItem(
                    id=_package_id("appx", full_name),
                    name=display_name,
                    source="appx",
                    scope=scope,
                    publisher=str(row.get("PublisherDisplayName") or row.get("Publisher") or ""),
                    version=_clean_version(row.get("Version")),
                    install_location=str(row.get("InstallLocation") or ""),
                    package_full_name=full_name,
                    package_family=family,
                    category=category,
                    removable=removable,
                    safe_remove=safe_remove,
                    removal_method=removal_method,
                    protected_reason=reason,
                    consequences=(
                        "The package may be reinstallable from the Microsoft Store, but restoration is not guaranteed."
                    ),
                    reversible=False,
                    restart_required=False,
                    signature_kind=str(row.get("SignatureKind") or ""),
                    is_framework=bool(row.get("IsFramework")),
                    raw={"architecture": row.get("Architecture")},
                )
            )
            added += 1
        snapshot.sources[f"appx_{'all' if all_users else 'user'}"] = added

    def _load_provisioned(self, snapshot: InventorySnapshot) -> None:
        script = r"""
$items = @(Get-AppxProvisionedPackage -Online -ErrorAction Stop | ForEach-Object {
    [pscustomobject]@{
        DisplayName = [string]$_.DisplayName
        PackageName = [string]$_.PackageName
        Version = [string]$_.Version
        Architecture = [string]$_.Architecture
        PublisherId = [string]$_.PublisherId
    }
})
$items | ConvertTo-Json -Depth 4 -Compress
"""
        try:
            data = self.runner.powershell_json(script, timeout=180, default=[])
        except Exception as exc:
            snapshot.errors.append(f"Provisioned package inventory failed: {exc}")
            return
        seen_names = {item.package_full_name for item in snapshot.items if item.package_full_name}
        added = 0
        for row in as_list(data):
            package_name = str(row.get("PackageName") or "").strip()
            if not package_name:
                continue
            display = str(row.get("DisplayName") or package_name)
            family = normalize_family(display)
            category, _safe, reason = classify_appx(display)
            if category == "unknown":
                category = "unknown"
            protected = category == "protected_system"
            if package_name in seen_names:
                continue
            snapshot.items.append(
                InventoryItem(
                    id=_package_id("provisioned", package_name),
                    name=display,
                    source="provisioned",
                    scope="Provisioned (new users)",
                    publisher="",
                    version=_clean_version(row.get("Version")),
                    package_full_name=package_name,
                    package_family=family,
                    category=category,
                    removable=not protected,
                    safe_remove=False,
                    removal_method="none" if protected else "Remove-AppxProvisionedPackage",
                    protected_reason=reason,
                    consequences="Affects future user profiles. Reinstallation requires the package source.",
                    reversible=False,
                    raw={"architecture": row.get("Architecture")},
                )
            )
            added += 1
        snapshot.sources["provisioned"] = added

    # ---------------------------------------------------------------- desktop
    def _load_desktop(self, snapshot: InventorySnapshot) -> None:
        if winreg is None:
            snapshot.errors.append("Desktop application inventory is only available on Windows.")
            return
        added = 0
        seen: set[tuple[str, str, str, str]] = set()
        for hive, path, scope in _UNINSTALL_KEYS:
            try:
                with winreg.OpenKey(hive, path, 0, winreg.KEY_READ) as root:
                    index = 0
                    while True:
                        try:
                            subkey_name = winreg.EnumKey(root, index)
                        except OSError:
                            break
                        index += 1
                        try:
                            with winreg.OpenKey(root, subkey_name) as key:
                                values = self._read_uninstall_values(key)
                        except OSError:
                            continue
                        name = values.get("DisplayName")
                        if not name:
                            continue
                        if values.get("ParentKeyName") or values.get("SystemComponent") == 1:
                            category, safe, reason = classify_desktop(
                                str(name), str(values.get("Publisher", "")), system_component=True
                            )
                        else:
                            category, safe, reason = classify_desktop(
                                str(name), str(values.get("Publisher", ""))
                            )
                        version = str(values.get("DisplayVersion", "") or "")
                        publisher = str(values.get("Publisher", "") or "")
                        dedupe = (str(name).casefold(), publisher.casefold(), version, scope)
                        if dedupe in seen:
                            continue
                        seen.add(dedupe)
                        uninstall = str(values.get("UninstallString", "") or "")
                        quiet = str(values.get("QuietUninstallString", "") or "")
                        removable = category != "protected_system" and bool(uninstall or quiet)
                        method = "none"
                        if removable:
                            method = "Uninstall (MSI)" if self._msi_product_code(uninstall or quiet) else "Uninstall"
                        size_kb = values.get("EstimatedSize")
                        try:
                            size_mb = round(int(size_kb) / 1024, 1) if size_kb else None
                        except (TypeError, ValueError):
                            size_mb = None
                        snapshot.items.append(
                            InventoryItem(
                                id=_package_id("desktop", f"{hive}\\{path}\\{subkey_name}"),
                                name=str(name),
                                source="desktop",
                                scope=scope,
                                publisher=publisher,
                                version=version,
                                install_date=str(values.get("InstallDate", "") or ""),
                                install_location=str(values.get("InstallLocation", "") or ""),
                                uninstall_string=uninstall,
                                quiet_uninstall_string=quiet,
                                estimated_size_mb=size_mb,
                                category=category,
                                removable=removable,
                                safe_remove=False,
                                removal_method=method,
                                protected_reason=reason,
                                consequences="The vendor uninstaller is used. Reinstalling is required to restore the application.",
                                reversible=False,
                                raw={
                                    "registry_path": f"{hive}\\{path}\\{subkey_name}",
                                    "hive": "HKLM" if hive == winreg.HKEY_LOCAL_MACHINE else "HKCU",
                                    "key_path": f"{path}\\{subkey_name}",
                                    "windows_installer": values.get("WindowsInstaller"),
                                },
                            )
                        )
                        added += 1
            except OSError as exc:
                snapshot.errors.append(f"Could not read uninstall key {path}: {exc}")
        snapshot.sources["desktop"] = added

    @staticmethod
    def _read_uninstall_values(key) -> dict[str, Any]:
        values: dict[str, Any] = {}
        index = 0
        wanted = {
            "DisplayName",
            "DisplayVersion",
            "Publisher",
            "InstallDate",
            "UninstallString",
            "QuietUninstallString",
            "InstallLocation",
            "EstimatedSize",
            "SystemComponent",
            "WindowsInstaller",
            "ParentKeyName",
            "ReleaseType",
        }
        while True:
            try:
                name, value, _kind = winreg.EnumValue(key, index)
            except OSError:
                break
            index += 1
            if name in wanted:
                values[name] = value
        return values

    @staticmethod
    def _msi_product_code(command: str) -> str:
        match = re.search(
            r"\{[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\}",
            command or "",
        )
        return match.group(0) if match else ""

    # ------------------------------------------------------------- aggregation
    @staticmethod
    def installed_desktop_names(snapshot: InventorySnapshot) -> set[str]:
        return {item.name.casefold() for item in snapshot.items if item.source == "desktop"}

    def search(self, snapshot: InventorySnapshot, query: str) -> list[InventoryItem]:
        if not query:
            return snapshot.items
        needle = query.casefold()
        return [
            item
            for item in snapshot.items
            if needle in item.name.casefold()
            or needle in item.publisher.casefold()
            or needle in item.package_family.casefold()
            or needle in item.version.casefold()
        ]

    def export(self, snapshot: InventorySnapshot, path: str, fmt: str = "csv") -> str:
        from pathlib import Path
        import csv

        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        rows = [item.to_dict() for item in snapshot.items]
        if fmt == "json":
            target.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
            return str(target)
        fields = list(rows[0].keys()) if rows else ["id", "name"]
        with target.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        return str(target)
