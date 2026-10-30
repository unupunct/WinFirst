"""Central safety classification used by previews and confirmations."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class RiskLevel(str, Enum):
    INFORMATIONAL = "informational"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def label(self) -> str:
        return self.value.capitalize()

    @property
    def rank(self) -> int:
        return {
            "informational": 0,
            "low": 1,
            "medium": 2,
            "high": 3,
            "critical": 4,
        }[self.value]

    @property
    def description(self) -> str:
        return {
            "informational": "Read-only inspection. Changes nothing.",
            "low": "Reversible user preference with limited scope.",
            "medium": "System configuration change with known consequences.",
            "high": "Can disrupt a device, application or access. Review before applying.",
            "critical": "Potentially destructive or security-sensitive. Requires explicit typed confirmation.",
        }[self.value]

    @classmethod
    def parse(cls, value: str | "RiskLevel" | None) -> "RiskLevel":
        if isinstance(value, RiskLevel):
            return value
        try:
            return cls(str(value).strip().lower())
        except ValueError:
            return cls.INFORMATIONAL


@dataclass
class RiskAssessment:
    level: RiskLevel
    modifies_state: bool = True
    reversible: bool = False
    requires_confirmation: bool = True
    requires_typed_confirmation: bool = False
    restart_may_be_required: bool = False
    can_interrupt_connectivity: bool = False
    affects_other_users: bool = False
    supported_versions: str = "Windows 10 and Windows 11"
    verification: str = "Reads back the affected state"
    consequences: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def badge(self) -> str:
        return self.level.label


def assess(
    level: RiskLevel | str,
    *,
    modifies_state: bool = True,
    reversible: bool = False,
    restart_may_be_required: bool = False,
    can_interrupt_connectivity: bool = False,
    affects_other_users: bool = False,
    consequences: str = "",
    verification: str = "Reads back the affected state",
    supported_versions: str = "Windows 10 and Windows 11",
) -> RiskAssessment:
    risk = RiskLevel.parse(level)
    if not modifies_state:
        risk = RiskLevel.INFORMATIONAL
    requires = modifies_state and risk.rank >= RiskLevel.MEDIUM.rank
    typed = risk is RiskLevel.CRITICAL
    return RiskAssessment(
        level=risk,
        modifies_state=modifies_state,
        reversible=reversible and modifies_state,
        requires_confirmation=requires,
        requires_typed_confirmation=typed,
        restart_may_be_required=restart_may_be_required,
        can_interrupt_connectivity=can_interrupt_connectivity,
        affects_other_users=affects_other_users,
        consequences=consequences,
        verification=verification,
        supported_versions=supported_versions,
    )


# Well-known actions whose classification must not drift between pages.
KNOWN_ACTIONS: dict[str, RiskAssessment] = {
    "power.set_hibernate": assess(
        RiskLevel.MEDIUM,
        reversible=True,
        restart_may_be_required=False,
        consequences="Disabling hibernation removes the Hibernate option and can affect Fast Startup.",
    ),
    "power.set_active_plan": assess(
        RiskLevel.LOW,
        reversible=True,
        consequences="Changes power consumption, heat and battery life.",
    ),
    "power.set_timeouts": assess(
        RiskLevel.LOW,
        reversible=True,
        consequences="Changes when the display turns off or the computer sleeps.",
    ),
    "explorer.registry_setting": assess(
        RiskLevel.LOW,
        reversible=True,
        consequences="Changes a per-user File Explorer preference.",
    ),
    "explorer.show_super_hidden": assess(
        RiskLevel.MEDIUM,
        reversible=True,
        consequences="Protected operating system files become visible in Explorer. Accidental deletion can damage Windows.",
    ),
    "godmode.create": assess(
        RiskLevel.LOW,
        reversible=True,
        consequences="Creates a God Mode folder on the current user's Desktop.",
    ),
    "godmode.remove": assess(
        RiskLevel.LOW,
        reversible=True,
        consequences="Removes only the WinFirst-managed God Mode folder.",
    ),
    "appx.remove": assess(
        RiskLevel.HIGH,
        reversible=False,
        consequences="The package may have to be reinstalled from the Microsoft Store. Some packages cannot be restored.",
    ),
    "appx.remove_provisioned": assess(
        RiskLevel.HIGH,
        reversible=False,
        affects_other_users=True,
        consequences="Affects future user profiles. Reinstalling provisioned packages requires administrative effort.",
    ),
    "desktop.uninstall": assess(
        RiskLevel.HIGH,
        reversible=False,
        consequences="Runs the vendor uninstaller. The application must be reinstalled to restore it.",
    ),
    "driver.import": assess(
        RiskLevel.HIGH,
        reversible=False,
        restart_may_be_required=True,
        consequences="Installing an unsuitable driver can make a device unusable.",
    ),
    "driver.export": assess(
        RiskLevel.LOW,
        modifies_state=False,
        consequences="Writes driver files to a selected folder. Does not change drivers.",
    ),
    "service.startup_change": assess(
        RiskLevel.HIGH,
        reversible=True,
        restart_may_be_required=True,
        consequences="Changing a service startup mode can break security, networking or update functionality.",
    ),
    "service.start_stop": assess(
        RiskLevel.MEDIUM,
        reversible=True,
        consequences="Stopping a service can interrupt dependent features.",
    ),
    "task.disable": assess(
        RiskLevel.MEDIUM,
        reversible=True,
        consequences="The scheduled task will not run until re-enabled.",
    ),
    "startup.disable": assess(
        RiskLevel.LOW,
        reversible=True,
        consequences="The application will not start automatically; it can be enabled again.",
    ),
    "network.flush_dns": assess(
        RiskLevel.LOW,
        reversible=False,
        consequences="Clears the DNS resolver cache. Name resolution may briefly be slower.",
    ),
    "network.renew_dhcp": assess(
        RiskLevel.MEDIUM,
        reversible=True,
        can_interrupt_connectivity=True,
        consequences="The network connection may drop while the lease is renewed.",
    ),
    "network.reset_winsock": assess(
        RiskLevel.HIGH,
        reversible=False,
        restart_may_be_required=True,
        can_interrupt_connectivity=True,
        consequences="Resets the Winsock catalog. A restart is usually required.",
    ),
    "network.reset_tcpip": assess(
        RiskLevel.HIGH,
        reversible=False,
        restart_may_be_required=True,
        can_interrupt_connectivity=True,
        consequences="Resets TCP/IP configuration. Static settings may be lost.",
    ),
    "repair.sfc": assess(
        RiskLevel.MEDIUM,
        reversible=False,
        restart_may_be_required=True,
        consequences="Scans and repairs protected Windows files. Can take a long time.",
    ),
    "repair.dism": assess(
        RiskLevel.MEDIUM,
        reversible=False,
        restart_may_be_required=True,
        consequences="Repairs the Windows component store. Requires internet access or installation media. Can take a long time.",
    ),
    "cleanup.delete": assess(
        RiskLevel.HIGH,
        reversible=False,
        consequences="Deleted files are not sent to the Recycle Bin and cannot be restored by WinFirst.",
    ),
    "printer.clear_jobs": assess(
        RiskLevel.MEDIUM,
        reversible=False,
        consequences="Queued print jobs are deleted and must be printed again.",
    ),
    "printer.restart_spooler": assess(
        RiskLevel.MEDIUM,
        reversible=True,
        consequences="Printing is briefly unavailable. Stuck jobs may remain.",
    ),
    "printer.restart_spooler_clear": assess(
        RiskLevel.HIGH,
        reversible=False,
        consequences="Stopping the spooler and deleting queued jobs cancels all current print jobs.",
    ),
    "env.set": assess(
        RiskLevel.MEDIUM,
        reversible=True,
        consequences="Running applications may need to be restarted to see the new value.",
    ),
    "env.delete": assess(
        RiskLevel.HIGH,
        reversible=True,
        consequences="Deleting an environment variable can break applications that depend on it.",
    ),
    "feature.change": assess(
        RiskLevel.HIGH,
        reversible=True,
        restart_may_be_required=True,
        consequences="Changing Windows features can require a restart and may need installation media.",
    ),
    "update.scan": assess(
        RiskLevel.INFORMATIONAL,
        modifies_state=False,
        consequences="Contacts Windows Update and reads available updates.",
    ),
    "software.install": assess(
        RiskLevel.HIGH,
        reversible=False,
        consequences="Installs software from an approved catalog entry. Uninstalling requires the vendor uninstaller.",
    ),
    "software.agreements": assess(
        RiskLevel.MEDIUM,
        reversible=False,
        consequences="Accepts the package and source agreements required by winget.",
    ),
    "user.account_change": assess(
        RiskLevel.HIGH,
        reversible=True,
        consequences="Changes whether a local account can sign in.",
    ),
    "rollback.setting": assess(
        RiskLevel.MEDIUM,
        reversible=False,
        consequences="Restores a previously recorded setting value.",
    ),
}


def assess_action(action_id: str, **overrides) -> RiskAssessment:
    base = KNOWN_ACTIONS.get(action_id)
    if base is None:
        return assess(overrides.pop("level", RiskLevel.MEDIUM), **overrides)
    if not overrides:
        return base
    data = {
        "modifies_state": base.modifies_state,
        "reversible": base.reversible,
        "restart_may_be_required": base.restart_may_be_required,
        "can_interrupt_connectivity": base.can_interrupt_connectivity,
        "affects_other_users": base.affects_other_users,
        "consequences": base.consequences,
        "verification": base.verification,
        "supported_versions": base.supported_versions,
    }
    data.update(overrides)
    return assess(base.level, **data)


def requires_extra_confirmation(level: RiskLevel | str) -> bool:
    return RiskLevel.parse(level).rank >= RiskLevel.HIGH.rank
