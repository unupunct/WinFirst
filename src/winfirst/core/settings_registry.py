"""Registry of supported, individually verifiable settings.

Profiles, the Configuration Drift Detector, Quick Setup and the settings
browser all use this single registry.  A setting is only offered for automatic
remediation when it has an explicit applier and a read-back detector.  Security
posture values are read-only: WinFirst inspects them but does not silently
weaken or "optimise" security controls.
"""
from __future__ import annotations

import threading
from typing import Any, Iterable

from winfirst.core.operation import ApplyOutcome, OperationSpec, VerifyOutcome
from winfirst.core.plan import action_step
from winfirst.core.risk_engine import RiskLevel
from winfirst.models.setting_definition import DetectedSetting, SettingDefinition
from winfirst.services import explorer_service, godmode_service
from winfirst.services.common import ServiceError
from winfirst.services.win_registry import read_value, write_value

GROUP_ORDER = [
    "Power management",
    "Explorer & Desktop",
    "Desktop utilities",
    "Privacy",
    "Security status",
]
GROUPS = GROUP_ORDER


def _power():
    from winfirst.services import power_service

    return power_service


def _security():
    from winfirst.services import security_service

    return security_service


def _display_bool(value: bool) -> str:
    return "Enabled" if value else "Disabled"


# --------------------------------------------------------------------------- power
def _detect_hibernate() -> DetectedSetting:
    status = _power().PowerService().hibernate_status()
    if status.enabled is None and not status.supported:
        return DetectedSetting(
            False, exists=False, supported=False, note=status.reason or "Hibernation is unavailable."
        )
    return DetectedSetting(
        bool(status.enabled),
        exists=status.enabled is not None,
        supported=status.supported or status.enabled is not None,
        note=status.detail,
        raw=_display_bool(bool(status.enabled)),
    )


def _apply_hibernate(value: Any) -> ApplyOutcome:
    service = _power().PowerService()
    before = service.hibernate_status()
    enabled = bool(value)
    service.set_hibernate(enabled)
    after = service.hibernate_status()
    warnings = []
    if not enabled:
        warnings.append(
            "Disabling hibernation removes the Hibernate option and can disable Fast Startup. "
            "It also removes hiberfil.sys, which may reduce disk usage."
        )
    return ApplyOutcome(
        success=after.enabled == enabled,
        previous_state=_display_bool(bool(before.enabled)),
        requested_state=_display_bool(enabled),
        actual_state=_display_bool(bool(after.enabled)),
        message=f"Hibernation is now {_display_bool(enabled).lower()}.",
        warnings=warnings,
        rollback={
            "strategy": "setting",
            "key": "hibernate_enabled",
            "previous": bool(before.enabled),
            "summary": f"Restore hibernation to {_display_bool(bool(before.enabled))}",
        },
        details={"fast_startup_enabled": after.fast_startup},
    )


def _timeout_detector(getter_name: str, ac: bool):
    def detect() -> DetectedSetting:
        service = _power().PowerService()
        try:
            value = getattr(service, getter_name)()
        except ServiceError as exc:
            return DetectedSetting(0, exists=False, supported=False, note=str(exc))
        seconds = value.ac_seconds if ac else value.dc_seconds
        return DetectedSetting(
            round(seconds / 60) if seconds else 0,
            exists=True,
            raw=f"{seconds} seconds",
        )

    return detect


def _timeout_applier(kind: str, ac: bool):
    def apply(value: Any) -> ApplyOutcome:
        service = _power().PowerService()
        minutes = int(value)
        before = _timeout_detector(
            "get_display_timeout" if kind == "display" else "get_sleep_timeout", ac
        )()
        kwargs: dict[str, int] = {}
        if kind == "display":
            kwargs["display_ac_minutes" if ac else "display_dc_minutes"] = minutes
        else:
            kwargs["sleep_ac_minutes" if ac else "sleep_dc_minutes"] = minutes
        service.set_timeout(**kwargs)
        after = _timeout_detector(
            "get_display_timeout" if kind == "display" else "get_sleep_timeout", ac
        )()
        requested = minutes * 60
        try:
            current = (
                service.get_display_timeout() if kind == "display" else service.get_sleep_timeout()
            )
            actual_seconds = current.ac_seconds if ac else current.dc_seconds
        except ServiceError:
            actual_seconds = -1
        return ApplyOutcome(
            success=actual_seconds == requested,
            previous_state=_format_minutes(before.value),
            requested_state=_format_minutes(minutes),
            actual_state=_format_minutes(after.value),
            message=f"Timeout set to {_format_minutes(minutes)}.",
            rollback={
                "strategy": "setting",
                "key": (
                    ("ac_" if ac else "battery_")
                    + ("display_timeout_minutes" if kind == "display" else "sleep_timeout_minutes")
                ),
                "previous": int(before.value),
                "summary": f"Restore timeout to {_format_minutes(int(before.value))}",
            },
        )

    return apply


def _format_minutes(minutes: int) -> str:
    return "Never" if int(minutes) == 0 else f"{int(minutes)} min"


def _button_detector(setting: str):
    def detect() -> DetectedSetting:
        service = _power().PowerService()
        try:
            value = service.get_button_action(setting)
        except ServiceError as exc:
            return DetectedSetting("do_nothing", exists=False, supported=False, note=str(exc))
        code = value.ac_seconds
        for key, mapped in _power().ACTION_VALUES.items():
            if mapped == code:
                return DetectedSetting(key, exists=True, raw=str(code))
        return DetectedSetting("do_nothing", exists=False, note="Unknown action value", supported=False)

    return detect


def _button_applier(setting: str):
    def apply(value: Any) -> ApplyOutcome:
        service = _power().PowerService()
        before = _button_detector(setting)()
        code = _power().ACTION_VALUES.get(str(value))
        if code is None:
            raise ServiceError("Invalid button action.")
        service.set_button_action(setting, code, code)
        after = service.get_button_action(setting)
        success = after.ac_seconds == code and after.dc_seconds == code
        return ApplyOutcome(
            success=success,
            previous_state=str(before.value),
            requested_state=str(value),
            actual_state=str(after.ac_seconds),
            message="The power button action was updated.",
            rollback={
                "strategy": "setting",
                "key": "lid_close_action" if setting == _power().LID_ACTION else "power_button_action",
                "previous": before.value,
                "summary": f"Restore action to {before.value}",
            },
        )

    return apply


# ------------------------------------------------------------------------ explorer
def _explorer_detector(reader_name: str):
    def detect() -> DetectedSetting:
        reading = getattr(explorer_service, reader_name)()
        return DetectedSetting(
            bool(reading.value),
            exists=reading.exists,
            raw=reading.raw,
            note=reading.note,
        )

    return detect


def _explorer_applier(writer_name: str, rollback_key: str, warning: str = ""):
    def apply(value: Any) -> ApplyOutcome:
        reader = reader_name_for(writer_name)
        before = getattr(explorer_service, reader)()
        getattr(explorer_service, writer_name)(bool(value))
        after = getattr(explorer_service, reader)()
        return ApplyOutcome(
            success=bool(after.value) == bool(value),
            previous_state=_display_bool(bool(before.value)),
            requested_state=_display_bool(bool(value)),
            actual_state=_display_bool(bool(after.value)),
            warnings=[warning] if warning else [],
            rollback={
                "strategy": "setting",
                "key": rollback_key,
                "previous": bool(before.value),
                "summary": f"Restore {rollback_key} to {_display_bool(bool(before.value))}",
            },
        )

    return apply


def reader_name_for(writer_name: str) -> str:
    return {
        "set_show_file_extensions": "read_show_file_extensions",
        "set_show_hidden_files": "read_show_hidden_files",
        "set_show_super_hidden": "read_show_super_hidden",
        "set_open_to_this_pc": "read_open_to_this_pc",
    }.get(writer_name, "read_show_file_extensions")


def _desktop_icon_detector(name: str, default: bool):
    def detect() -> DetectedSetting:
        reading = explorer_service.read_desktop_icon_value(name, default)
        return DetectedSetting(
            bool(reading.value), exists=reading.exists, raw=reading.raw, note=reading.note
        )

    return detect


def _desktop_icon_applier(name: str, default: bool, key: str):
    def apply(value: Any) -> ApplyOutcome:
        before = explorer_service.read_desktop_icon_value(name, default)
        explorer_service.set_desktop_icon(name, bool(value), default_visible=default)
        after = explorer_service.read_desktop_icon_value(name, default)
        return ApplyOutcome(
            success=bool(after.value) == bool(value),
            previous_state=_display_bool(bool(before.value)),
            requested_state=_display_bool(bool(value)),
            actual_state=_display_bool(bool(after.value)),
            rollback={
                "strategy": "setting",
                "key": key,
                "previous": bool(before.value),
                "summary": f"Restore desktop icon visibility to {_display_bool(bool(before.value))}",
            },
        )

    return apply


def _detect_godmode() -> DetectedSetting:
    state = godmode_service.get_state()
    return DetectedSetting(
        bool(state.exists and state.is_directory),
        exists=state.exists,
        supported=state.writable or state.exists,
        raw=state.status,
        note=state.error or ("Desktop: " + str(state.desktop)),
    )


def _apply_godmode(value: Any) -> ApplyOutcome:
    state = godmode_service.get_state()
    if bool(value):
        godmode_service.create()
        after = godmode_service.get_state()
        return ApplyOutcome(
            success=after.exists and after.is_directory,
            previous_state=state.status,
            requested_state="Present",
            actual_state=after.status,
            rollback={
                "strategy": "setting",
                "key": "create_god_mode",
                "previous": False,
                "summary": "Remove the WinFirst-created God Mode folder",
            },
        )
    godmode_service.remove(force=False)
    after = godmode_service.get_state()
    return ApplyOutcome(
        success=not after.exists,
        previous_state=state.status,
        requested_state="Not present",
        actual_state=after.status,
        rollback={
            "strategy": "setting",
            "key": "create_god_mode",
            "previous": True,
            "summary": "Recreate the God Mode folder",
        },
    )


# -------------------------------------------------------------------------- privacy
def _dword_bool_detector(path: str, name: str, *, inverted: bool = False):
    def detect() -> DetectedSetting:
        reg = read_value("HKCU", path, name)
        if not reg.exists:
            return DetectedSetting(False, exists=False, note="Not configured; the Windows default applies.")
        raw = int(reg.value or 0)
        value = (raw == 0) if inverted else (raw != 0)
        return DetectedSetting(value, exists=True, raw=str(raw))

    return detect


def _dword_bool_applier(path: str, name: str, key: str, *, inverted: bool = False):
    def apply(value: Any) -> ApplyOutcome:
        before = _dword_bool_detector(path, name, inverted=inverted)()
        raw = (0 if bool(value) else 1) if inverted else (1 if bool(value) else 0)
        write_value("HKCU", path, name, raw)
        after = _dword_bool_detector(path, name, inverted=inverted)()
        return ApplyOutcome(
            success=bool(after.value) == bool(value),
            previous_state=_display_bool(bool(before.value)),
            requested_state=_display_bool(bool(value)),
            actual_state=_display_bool(bool(after.value)),
            rollback={
                "strategy": "setting",
                "key": key,
                "previous": bool(before.value),
                "summary": f"Restore {key}",
            },
        )

    return apply


CONTENT_DELIVERY = r"Software\Microsoft\Windows\CurrentVersion\ContentDeliveryManager"
_SUGGESTION_VALUES = (
    "SubscribedContent-338393Enabled",
    "SubscribedContent-353694Enabled",
    "SubscribedContent-353696Enabled",
)


def _detect_settings_suggestions() -> DetectedSetting:
    readings = [read_value("HKCU", CONTENT_DELIVERY, name) for name in _SUGGESTION_VALUES]
    exists = any(item.exists for item in readings)
    disabled = exists and all(item.exists and int(item.value or 0) == 0 for item in readings)
    return DetectedSetting(
        disabled,
        exists=exists,
        note="" if exists else "Not configured; Windows may show suggestions.",
        raw=", ".join(f"{item.value}" for item in readings),
    )


def _apply_settings_suggestions(value: Any) -> ApplyOutcome:
    before = _detect_settings_suggestions()
    raw = 0 if bool(value) else 1
    for name in _SUGGESTION_VALUES:
        write_value("HKCU", CONTENT_DELIVERY, name, raw)
    after = _detect_settings_suggestions()
    return ApplyOutcome(
        success=bool(after.value) == bool(value),
        previous_state=_display_bool(bool(before.value)),
        requested_state=_display_bool(bool(value)),
        actual_state=_display_bool(bool(after.value)),
        rollback={
            "strategy": "setting",
            "key": "disable_settings_suggestions",
            "previous": bool(before.value),
            "summary": "Restore the previous suggestion preferences",
        },
    )


# ------------------------------------------------------------------------- security
def _security_bool_detector(fetcher, label: str):
    def detect() -> DetectedSetting:
        try:
            data = fetcher()
        except Exception as exc:
            return DetectedSetting(False, exists=False, supported=False, note=str(exc))
        if not data.get("available", True):
            return DetectedSetting(
                bool(data.get("value", False)),
                exists=False,
                supported=False,
                note=str(data.get("reason", "")),
            )
        value = data.get("value")
        if value is None:
            return DetectedSetting(False, exists=False, supported=False, note=label + " is unavailable.")
        return DetectedSetting(bool(value), exists=True, note=str(data.get("detail", "")))

    return detect


def _defender_realtime() -> dict:
    summary = _security().get_defender_summary()
    if not summary.available:
        return {"available": False, "reason": summary.error}
    return {
        "available": True,
        "value": summary.realtime_enabled,
        "detail": f"Antivirus enabled: {summary.antivirus_enabled}; signature age: {summary.signature_age_days} days"
        if summary.signature_age_days is not None
        else "",
    }


def _firewall(profile_name: str):
    def fetch() -> dict:
        try:
            profiles = _security().get_firewall_profiles()
        except Exception as exc:
            return {"available": False, "reason": str(exc)}
        for profile in profiles:
            if profile.name.lower() == profile_name:
                return {
                    "available": True,
                    "value": profile.enabled,
                    "detail": f"{profile.name} profile (default inbound: {profile.default_inbound})",
                }
        return {"available": False, "reason": f"{profile_name} firewall profile was not found"}

    return fetch


def _uac_enabled() -> dict:
    try:
        uac = _security().get_uac_settings()
    except Exception as exc:
        return {"available": False, "reason": str(exc)}
    return {"available": True, "value": uac.enabled, "detail": f"Consent prompt: {uac.consent_prompt}"}


def _secure_boot() -> dict:
    try:
        status = _security().get_secure_boot_status()
    except Exception as exc:
        return {"available": False, "reason": str(exc)}
    return {"available": status.available, "value": status.enabled, "detail": status.detail, "reason": status.reason}


def _tpm() -> dict:
    try:
        status = _security().get_tpm_status()
    except Exception as exc:
        return {"available": False, "reason": str(exc)}
    return {"available": status.available, "value": status.present, "detail": status.detail, "reason": status.reason}


def _activation() -> dict:
    try:
        status = _security().get_activation_status()
    except Exception as exc:
        return {"available": False, "reason": str(exc)}
    return {"available": status.available, "value": status.activated, "detail": status.detail, "reason": status.reason}


_DEFINITIONS: dict[str, SettingDefinition] = {}


def _register(definition: SettingDefinition) -> None:
    _DEFINITIONS[definition.key] = definition


def _build_definitions() -> None:
    if _DEFINITIONS:
        return
    lock = _REGISTRY_LOCK
    with lock:
        if _DEFINITIONS:
            return
        # Power -----------------------------------------------------------------
        _register(SettingDefinition(
            key="hibernate_enabled",
            label="Hibernation",
            group="Power management",
            description="Enable or disable hibernation. Disabling also removes hiberfil.sys and can affect Fast Startup.",
            value_type="bool",
            default=False,
            detect=_detect_hibernate,
            apply=_apply_hibernate,
            risk=RiskLevel.MEDIUM,
            requires_admin=True,
            reversible=True,
            consequences="Disabling hibernation removes the Hibernate option and may disable Fast Startup.",
            verification="Reads the HibernateEnabled registry value after powercfg completes",
            severity="medium",
        ))
        for key, label, getter, ac, kind in (
            ("ac_display_timeout_minutes", "Display timeout (plugged in)", "get_display_timeout", True, "display"),
            ("battery_display_timeout_minutes", "Display timeout (battery)", "get_display_timeout", False, "display"),
            ("ac_sleep_timeout_minutes", "Sleep timeout (plugged in)", "get_sleep_timeout", True, "sleep"),
            ("battery_sleep_timeout_minutes", "Sleep timeout (battery)", "get_sleep_timeout", False, "sleep"),
        ):
            _register(SettingDefinition(
                key=key,
                label=label,
                group="Power management",
                description="Timeout in minutes. 0 means never.",
                value_type="int",
                default=0,
                detect=_timeout_detector(getter, ac),
                apply=_timeout_applier(kind, ac),
                minimum=0,
                maximum=1440,
                unit="minutes",
                risk=RiskLevel.LOW,
                requires_admin=False,
                reversible=True,
                consequences="Changes when the display turns off or the system sleeps.",
                severity="low",
            ))
        for key, label, setting in (
            ("lid_close_action", "Lid close action", _power().LID_ACTION),
            ("power_button_action", "Power button action", _power().POWER_BUTTON_ACTION),
        ):
            _register(SettingDefinition(
                key=key,
                label=label,
                group="Power management",
                description="Action Windows takes for both AC and battery operation.",
                value_type="enum",
                default="sleep",
                detect=_button_detector(setting),
                apply=_button_applier(setting),
                choices=[(value, label_) for value, label_ in _power().ACTION_LABELS.items()],
                risk=RiskLevel.LOW,
                requires_admin=False,
                reversible=True,
                consequences="Changes hardware button or lid behaviour.",
                severity="low",
            ))

        # Explorer --------------------------------------------------------------
        _register(SettingDefinition(
            key="show_file_extensions",
            label="Show file name extensions",
            group="Explorer & Desktop",
            description="Shows known file extensions in File Explorer.",
            value_type="bool",
            default=True,
            detect=_explorer_detector("read_show_file_extensions"),
            apply=_explorer_applier("set_show_file_extensions", "show_file_extensions"),
            risk=RiskLevel.LOW,
            reversible=True,
            consequences="Helps identify file types; extensions become visible in Explorer.",
            severity="low",
        ))
        _register(SettingDefinition(
            key="show_hidden_files",
            label="Show hidden files and folders",
            group="Explorer & Desktop",
            description="Shows items marked with the hidden attribute.",
            value_type="bool",
            default=False,
            detect=_explorer_detector("read_show_hidden_files"),
            apply=_explorer_applier("set_show_hidden_files", "show_hidden_files"),
            risk=RiskLevel.LOW,
            reversible=True,
            consequences="Hidden configuration files become visible in Explorer.",
            severity="low",
        ))
        _register(SettingDefinition(
            key="show_super_hidden",
            label="Show protected operating system files",
            group="Explorer & Desktop",
            description="Shows files marked as protected operating system files.",
            value_type="bool",
            default=False,
            detect=_explorer_detector("read_show_super_hidden"),
            apply=_explorer_applier(
                "set_show_super_hidden",
                "show_super_hidden",
                warning="Protected operating system files include critical Windows components. Deleting them can make Windows unbootable.",
            ),
            risk=RiskLevel.MEDIUM,
            reversible=True,
            consequences="Critical system files become visible; accidental deletion can damage Windows.",
            severity="high",
        ))
        _register(SettingDefinition(
            key="open_explorer_to_this_pc",
            label="Open File Explorer to This PC",
            group="Explorer & Desktop",
            description="File Explorer opens at This PC instead of Quick Access/Home.",
            value_type="bool",
            default=True,
            detect=_explorer_detector("read_open_to_this_pc"),
            apply=_explorer_applier("set_open_to_this_pc", "open_explorer_to_this_pc"),
            risk=RiskLevel.LOW,
            reversible=True,
            severity="low",
        ))
        _register(SettingDefinition(
            key="show_desktop_icon_this_pc",
            label="Show This PC icon on the Desktop",
            group="Explorer & Desktop",
            description="Controls the This PC desktop icon.",
            value_type="bool",
            default=True,
            detect=_desktop_icon_detector(explorer_service.CLSID_THIS_PC, True),
            apply=_desktop_icon_applier(explorer_service.CLSID_THIS_PC, True, "show_desktop_icon_this_pc"),
            risk=RiskLevel.LOW,
            reversible=True,
            severity="low",
        ))
        _register(SettingDefinition(
            key="show_desktop_icon_user_files",
            label="Show user files icon on the Desktop",
            group="Explorer & Desktop",
            description="Controls the current user's files desktop icon.",
            value_type="bool",
            default=False,
            detect=_desktop_icon_detector(explorer_service.CLSID_USER_FILES, False),
            apply=_desktop_icon_applier(explorer_service.CLSID_USER_FILES, False, "show_desktop_icon_user_files"),
            risk=RiskLevel.LOW,
            reversible=True,
            severity="low",
        ))
        _register(SettingDefinition(
            key="create_god_mode",
            label="God Mode folder",
            group="Desktop utilities",
            description="Creates the documented God Mode folder on the Desktop.",
            value_type="bool",
            default=False,
            detect=_detect_godmode,
            apply=_apply_godmode,
            risk=RiskLevel.LOW,
            reversible=True,
            consequences="Adds a folder containing shortcuts to Windows settings; it is not a security bypass.",
            verification="Checks the managed folder on the resolved Desktop",
            severity="informational",
        ))

        # Privacy ---------------------------------------------------------------
        _register(SettingDefinition(
            key="disable_advertising_id",
            label="Disable advertising ID",
            group="Privacy",
            description="Disables the per-user advertising identifier used for personalized ads.",
            value_type="bool",
            default=True,
            detect=_dword_bool_detector(
                r"Software\Microsoft\Windows\CurrentVersion\AdvertisingInfo", "Enabled", inverted=True
            ),
            apply=_dword_bool_applier(
                r"Software\Microsoft\Windows\CurrentVersion\AdvertisingInfo",
                "Enabled",
                "disable_advertising_id",
                inverted=True,
            ),
            risk=RiskLevel.LOW,
            reversible=True,
            consequences="Some apps may show less personalized advertising.",
            severity="low",
            sensitivity="privacy",
        ))
        _register(SettingDefinition(
            key="disable_settings_suggestions",
            label="Disable Settings suggestions",
            group="Privacy",
            description="Turns off selected suggestion cards in Windows Settings.",
            value_type="bool",
            default=True,
            detect=_detect_settings_suggestions,
            apply=_apply_settings_suggestions,
            risk=RiskLevel.LOW,
            reversible=True,
            consequences="Windows will show fewer tips and suggested content in Settings.",
            severity="low",
            sensitivity="privacy",
        ))
        _register(SettingDefinition(
            key="disable_activity_history_upload",
            label="Disable activity history publishing",
            group="Privacy",
            description="Stops Windows from publishing local activity history for cross-device experiences.",
            value_type="bool",
            default=True,
            detect=_dword_bool_detector(
                r"Software\Microsoft\Windows\CurrentVersion\Privacy",
                "PublishUserActivities",
                inverted=True,
            ),
            apply=_dword_bool_applier(
                r"Software\Microsoft\Windows\CurrentVersion\Privacy",
                "PublishUserActivities",
                "disable_activity_history_upload",
                inverted=True,
            ),
            risk=RiskLevel.LOW,
            reversible=True,
            consequences="Timeline-style cross-device activity features are reduced.",
            severity="low",
            sensitivity="privacy",
        ))
        _register(SettingDefinition(
            key="disable_app_launch_tracking",
            label="Disable app launch tracking",
            group="Privacy",
            description="Prevents Windows from tracking which apps you launch to improve Start and search.",
            value_type="bool",
            default=True,
            detect=_dword_bool_detector(
                explorer_service.ADVANCED, "Start_TrackProgs", inverted=True
            ),
            apply=_dword_bool_applier(
                explorer_service.ADVANCED,
                "Start_TrackProgs",
                "disable_app_launch_tracking",
                inverted=True,
            ),
            risk=RiskLevel.LOW,
            reversible=True,
            consequences="Start and search may be less tailored to recent app usage.",
            severity="low",
            sensitivity="privacy",
        ))

        # Security status (read-only) ------------------------------------------
        _register(SettingDefinition(
            key="defender_realtime_enabled",
            label="Microsoft Defender real-time protection",
            group="Security status",
            description="Read-only inspection of Defender real-time protection.",
            value_type="bool",
            default=True,
            detect=_security_bool_detector(_defender_realtime, "Defender status"),
            risk=RiskLevel.INFORMATIONAL,
            reversible=False,
            profile_allowed=True,
            remediation_available=False,
            consequences="WinFirst never disables Defender. Manage security software through Windows or your organization.",
            severity="critical",
        ))
        for profile_name in ("Domain", "Private", "Public"):
            _register(SettingDefinition(
                key=f"firewall_enabled_{profile_name.lower()}",
                label=f"Firewall enabled ({profile_name})",
                group="Security status",
                description="Read-only inspection of the Windows Firewall profile.",
                value_type="bool",
                default=True,
                detect=_security_bool_detector(_firewall(profile_name), "Firewall status"),
                risk=RiskLevel.INFORMATIONAL,
                reversible=False,
                remediation_available=False,
                consequences="WinFirst never disables the firewall.",
                severity="critical",
            ))
        _register(SettingDefinition(
            key="uac_enabled",
            label="User Account Control enabled",
            group="Security status",
            description="Read-only inspection of UAC (EnableLUA).",
            value_type="bool",
            default=True,
            detect=_security_bool_detector(_uac_enabled, "UAC status"),
            risk=RiskLevel.INFORMATIONAL,
            reversible=False,
            remediation_available=False,
            severity="critical",
        ))
        _register(SettingDefinition(
            key="secure_boot_enabled",
            label="Secure Boot enabled",
            group="Security status",
            description="Read-only inspection of Secure Boot.",
            value_type="bool",
            default=True,
            detect=_security_bool_detector(_secure_boot, "Secure Boot status"),
            risk=RiskLevel.INFORMATIONAL,
            reversible=False,
            remediation_available=False,
            severity="high",
        ))
        _register(SettingDefinition(
            key="tpm_present",
            label="TPM present",
            group="Security status",
            description="Read-only inspection of the Trusted Platform Module.",
            value_type="bool",
            default=True,
            detect=_security_bool_detector(_tpm, "TPM status"),
            risk=RiskLevel.INFORMATIONAL,
            reversible=False,
            remediation_available=False,
            severity="medium",
        ))
        _register(SettingDefinition(
            key="windows_activated",
            label="Windows activation",
            group="Security status",
            description="Read-only inspection of Windows licensing status. Product keys are never displayed.",
            value_type="bool",
            default=True,
            detect=_security_bool_detector(_activation, "Activation status"),
            risk=RiskLevel.INFORMATIONAL,
            reversible=False,
            remediation_available=False,
            severity="high",
        ))


_REGISTRY_LOCK = threading.Lock()


def ensure_registered() -> None:
    _build_definitions()


def all_definitions() -> list[SettingDefinition]:
    ensure_registered()
    return list(_DEFINITIONS.values())


def get_definition(key: str) -> SettingDefinition:
    ensure_registered()
    try:
        return _DEFINITIONS[key]
    except KeyError as exc:
        raise KeyError(f"Unknown setting: {key}") from exc


def is_supported_key(key: str) -> bool:
    ensure_registered()
    return key in _DEFINITIONS


def definitions_by_group() -> dict[str, list[SettingDefinition]]:
    grouped: dict[str, list[SettingDefinition]] = {group: [] for group in GROUP_ORDER}
    for definition in all_definitions():
        grouped.setdefault(definition.group, []).append(definition)
    return grouped


def detect_all(keys: Iterable[str] | None = None) -> dict[str, DetectedSetting]:
    definitions = all_definitions() if keys is None else [get_definition(k) for k in keys]
    results: dict[str, DetectedSetting] = {}
    for definition in definitions:
        try:
            results[definition.key] = definition.detect()
        except Exception as exc:
            results[definition.key] = DetectedSetting(
                definition.default, exists=False, supported=False, error=str(exc), note=str(exc)
            )
    return results


def validate_profile_settings(settings: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(settings, dict):
        raise ValueError("Profile settings must be an object")
    cleaned: dict[str, Any] = {}
    for key, value in settings.items():
        definition = get_definition(key)
        if not definition.profile_allowed:
            raise ValueError(f"{key} cannot be stored in a profile")
        cleaned[key] = definition.validate(value)
    return cleaned


def values_match(expected: Any, actual: Any) -> bool:
    if isinstance(expected, bool) or isinstance(actual, bool):
        return bool(expected) == bool(actual)
    if isinstance(expected, int) or isinstance(actual, int):
        try:
            return int(expected) == int(actual)
        except (TypeError, ValueError):
            return False
    return str(expected).strip().casefold() == str(actual).strip().casefold()


def build_spec(key: str, value: Any) -> OperationSpec:
    definition = get_definition(key)
    if definition.apply is None:
        raise ValueError(f"{definition.label} is read-only and cannot be changed by WinFirst")
    cleaned = definition.validate(value)

    def apply(progress, _cancel) -> ApplyOutcome:
        progress(f"Changing {definition.label}")
        outcome = definition.apply(cleaned)
        if outcome.rollback is None and definition.reversible:
            outcome.rollback = {
                "strategy": "setting",
                "key": key,
                "previous": definition.detect().value,
                "summary": f"Restore {definition.label}",
            }
        if outcome.rollback and outcome.rollback.get("strategy") == "setting":
            # Recorded so rollback can refuse to overwrite a later manual change.
            outcome.rollback.setdefault("applied", cleaned)
        return outcome

    def verify() -> VerifyOutcome:
        detected = definition.detect()
        if not detected.supported:
            return VerifyOutcome(
                ok=False,
                actual_state=detected.status,
                message=detected.note or "The setting could not be read back.",
            )
        return VerifyOutcome(
            ok=values_match(cleaned, detected.value),
            actual_state=definition.format_value(detected.value),
            message=f"Read back: {definition.format_value(detected.value)}",
        )

    def before() -> str:
        detected = definition.detect()
        return definition.format_value(detected.value)

    def plan_steps():
        return [
            action_step(
                f"Set {definition.label} to {definition.format_value(cleaned)}",
                detail=definition.description,
            ),
            action_step(
                "Read the value back",
                detail=definition.verification,
            ),
        ]

    return OperationSpec(
        operation_id=f"setting.{key}",
        name=f"Set {definition.label}",
        category=definition.group.lower().replace(" & ", "_").replace(" ", "_"),
        description=definition.description,
        risk=definition.risk,
        requires_admin=definition.requires_admin,
        reversible=definition.reversible,
        restart_required=definition.requires_restart,
        can_interrupt_connectivity=False,
        affects_other_users=False,
        apply=apply,
        verify=verify,
        before=before,
        plan_steps=plan_steps,
        preview_text=f"{definition.label}: {before()} -> {definition.format_value(cleaned)}",
        warnings=[definition.consequences] if definition.consequences else [],
        verification_method=definition.verification,
        details={"setting_key": key, "requested_value": cleaned},
    )


def build_specs(settings: dict[str, Any]) -> list[OperationSpec]:
    cleaned = validate_profile_settings(settings)
    return [build_spec(key, value) for key, value in cleaned.items()]
