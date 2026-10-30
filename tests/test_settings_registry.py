from __future__ import annotations

import json
from pathlib import Path

import pytest

from winfirst.core import settings_registry
from winfirst.core.paths import resources_dir
from winfirst.core.risk_engine import RiskLevel


def test_registry_has_unique_supported_keys() -> None:
    definitions = settings_registry.all_definitions()
    keys = [definition.key for definition in definitions]
    assert len(keys) == len(set(keys))
    assert len(keys) >= 20
    assert all(definition.label and definition.group for definition in definitions)
    assert all(settings_registry.is_supported_key(key) for key in keys)


def test_unknown_key_is_rejected() -> None:
    with pytest.raises(KeyError):
        settings_registry.get_definition("registry.anything")
    assert settings_registry.is_supported_key("registry.anything") is False


def test_bool_validation_and_formatting() -> None:
    definition = settings_registry.get_definition("show_file_extensions")
    assert definition.validate(True) is True
    assert definition.validate(0) is False
    assert definition.format_value(True) == "Enabled"
    with pytest.raises(ValueError):
        definition.validate("true")


def test_int_validation_bounds_and_never_display() -> None:
    definition = settings_registry.get_definition("ac_display_timeout_minutes")
    assert definition.validate(15) == 15
    assert definition.format_value(0) == "Never"
    assert definition.format_value(15) == "15 minutes"
    with pytest.raises(ValueError):
        definition.validate(-1)
    with pytest.raises(ValueError):
        definition.validate(True)


def test_enum_validation() -> None:
    definition = settings_registry.get_definition("lid_close_action")
    allowed = [choice[0] for choice in definition.choices]
    assert definition.validate(allowed[0]) == allowed[0]
    with pytest.raises(ValueError):
        definition.validate("eject")


def test_security_settings_are_read_only() -> None:
    definition = settings_registry.get_definition("defender_realtime_enabled")
    assert definition.read_only
    assert definition.remediation_available is False
    assert definition.risk is RiskLevel.INFORMATIONAL


def test_build_spec_for_supported_setting() -> None:
    spec = settings_registry.build_spec("show_file_extensions", False)
    assert spec.operation_id == "setting.show_file_extensions"
    assert spec.category == "explorer_desktop"
    assert callable(spec.apply)
    assert spec.verify is not None


def test_build_spec_rejects_unknown_and_invalid_values() -> None:
    with pytest.raises(KeyError):
        settings_registry.build_spec("unknown.setting", True)
    with pytest.raises(ValueError):
        settings_registry.build_spec("show_file_extensions", "yes")


def test_build_specs_returns_one_spec_per_setting() -> None:
    specs = settings_registry.build_specs(
        {"show_file_extensions": True, "open_explorer_to_this_pc": False}
    )
    assert len(specs) == 2
    assert {spec.operation_id for spec in specs} == {
        "setting.show_file_extensions",
        "setting.open_explorer_to_this_pc",
    }


def test_values_match() -> None:
    assert settings_registry.values_match(True, True) is True
    assert settings_registry.values_match(True, False) is False
    assert settings_registry.values_match("sleep", "sleep") is True


def test_detected_setting_status_labels() -> None:
    from winfirst.models.setting_definition import DetectedSetting

    assert DetectedSetting(True).status == "True"
    assert DetectedSetting(True, exists=False).status == "Not configured"
    assert DetectedSetting(True, supported=False).status == "Unsupported"


def test_builtin_profiles_never_change_read_only_security_controls() -> None:
    read_only = {
        definition.key for definition in settings_registry.all_definitions() if definition.read_only
    }
    profiles = sorted((resources_dir() / "profiles").glob("*.json"))
    assert profiles, "Built-in profiles are missing."

    for path in profiles:
        document = json.loads(path.read_text(encoding="utf-8"))
        settings = document.get("settings") or {}
        assert not (set(settings) & read_only), (
            f"{path.name} attempts to change read-only security settings: "
            f"{sorted(set(settings) & read_only)}"
        )
        # Every key must be validated by the same central registry used at apply time.
        settings_registry.validate_profile_settings(settings)

