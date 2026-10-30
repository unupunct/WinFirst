from __future__ import annotations

import pytest

from winfirst.core.schema_validation import (
    ProfileValidationError,
    require_valid_profile,
    validate_profile_document,
    validate_software_catalog,
)


def _profile(**overrides):
    document = {
        "schema_version": 1,
        "profile_name": "Test profile",
        "description": "A test",
        "settings": {"show_file_extensions": True},
        "software": [],
        "checklist": [],
    }
    document.update(overrides)
    return document


def test_valid_profile_is_cleaned() -> None:
    result = validate_profile_document(_profile())
    assert result.ok
    assert result.document is not None
    assert result.document["profile_name"] == "Test profile"
    assert result.document["settings"] == {"show_file_extensions": True}


def test_unknown_top_level_field_is_rejected() -> None:
    result = validate_profile_document(_profile(command="rm -rf /"))
    assert not result.ok
    assert any("Unsupported profile fields" in error for error in result.errors)


def test_unknown_setting_is_rejected() -> None:
    result = validate_profile_document(_profile(settings={"registry.free_for_all": 1}))
    assert not result.ok
    assert any("Unsupported setting" in error for error in result.errors)


def test_wrong_setting_type_is_rejected() -> None:
    result = validate_profile_document(_profile(settings={"show_file_extensions": "yes"}))
    assert not result.ok
    assert any("expected true or false" in error for error in result.errors)


def test_enum_outside_choices_is_rejected() -> None:
    result = validate_profile_document(
        _profile(settings={"lid_close_action": "explode"})
    )
    assert not result.ok
    assert any("expected one of" in error for error in result.errors)


def test_out_of_range_integer_is_rejected() -> None:
    result = validate_profile_document(
        _profile(settings={"ac_display_timeout_minutes": 999999})
    )
    assert not result.ok
    assert any("maximum" in error for error in result.errors)


def test_read_only_security_setting_produces_warning() -> None:
    result = validate_profile_document(
        _profile(settings={"defender_realtime_enabled": True})
    )
    assert result.ok
    assert any("read-only" in warning for warning in result.warnings)


def test_duplicate_software_produces_warning_and_single_entry() -> None:
    software = [{"id": "Mozilla.Firefox"}, {"id": "mozilla.firefox"}]
    result = validate_profile_document(_profile(software=software))
    assert result.ok
    assert result.document is not None
    assert len(result.document["software"]) == 1
    assert any("Duplicate software" in warning for warning in result.warnings)


def test_invalid_software_id_is_rejected() -> None:
    result = validate_profile_document(
        _profile(software=[{"id": "bad id with spaces"}])
    )
    assert not result.ok


def test_requesting_valid_profile_raises_for_invalid_document() -> None:
    with pytest.raises(ProfileValidationError) as excinfo:
        require_valid_profile({"schema_version": 99, "profile_name": "x"})
    assert excinfo.value.errors


def test_valid_catalog() -> None:
    result = validate_software_catalog(
        {
            "schema_version": 1,
            "packages": [
                {
                    "id": "Mozilla.Firefox",
                    "name": "Firefox",
                    "source": "winget",
                }
            ],
        }
    )
    assert result.ok
    assert result.document is not None
    assert len(result.document["packages"]) == 1


def test_catalog_rejects_unknown_source() -> None:
    result = validate_software_catalog(
        {
            "schema_version": 1,
            "packages": [{"id": "A.B", "name": "X", "source": "download"}],
        }
    )
    assert not result.ok
    assert any("source must be" in error for error in result.errors)


def test_catalog_rejects_local_package_without_installer() -> None:
    result = validate_software_catalog(
        {
            "schema_version": 1,
            "packages": [{"id": "A.B", "name": "X", "source": "local"}],
        }
    )
    assert not result.ok
    assert any("no installer" in error for error in result.errors)


def test_catalog_rejects_duplicate_packages() -> None:
    result = validate_software_catalog(
        {
            "schema_version": 1,
            "packages": [
                {"id": "A.B", "name": "X", "source": "winget"},
                {"id": "a.b", "name": "Y", "source": "winget"},
            ],
        }
    )
    assert not result.ok
    assert any("Duplicate" in error for error in result.errors)
