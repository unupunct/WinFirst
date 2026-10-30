"""Strict profile and catalog validation.

Profiles are declarative.  They cannot contain commands, scripts, credentials or
arbitrary registry paths.  Unknown settings and unknown fields are rejected so a
profile cannot silently acquire new behaviour.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from winfirst.core import settings_registry

PROFILE_SCHEMA_VERSION = 1
MAX_PROFILE_BYTES = 2 * 1024 * 1024

_TOP_LEVEL_KEYS = {
    "schema_version",
    "profile_name",
    "description",
    "settings",
    "software",
    "checklist",
    "author",
    "notes",
}
_SOFTWARE_KEYS = {"id", "name", "optional", "version", "source", "notes"}
_CHECKLIST_KEYS = {"key", "title", "description", "required", "auto_check"}
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+\-]{0,199}$")
_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_]{0,79}$")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class ProfileValidationError(ValueError):
    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("; ".join(errors[:5]) if errors else "Invalid profile")


@dataclass
class ValidationResult:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    document: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return not self.errors


def clean_text(value: Any, *, field_name: str, max_length: int, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    text = _CONTROL_RE.sub("", value).strip()
    if not text and not allow_empty:
        raise ValueError(f"{field_name} cannot be empty")
    if len(text) > max_length:
        raise ValueError(f"{field_name} is longer than {max_length} characters")
    return text


def validate_profile_document(
    data: Any, *, source: str = "profile"
) -> ValidationResult:
    result = ValidationResult()
    if not isinstance(data, dict):
        result.errors.append(f"{source}: the document must be a JSON object")
        return result
    unknown = set(data) - _TOP_LEVEL_KEYS
    if unknown:
        result.errors.append(f"Unsupported profile fields: {', '.join(sorted(unknown))}")
    if data.get("schema_version") != PROFILE_SCHEMA_VERSION:
        result.errors.append(
            f"Unsupported schema_version: {data.get('schema_version')!r}. "
            f"This build supports version {PROFILE_SCHEMA_VERSION}."
        )
    try:
        name = clean_text(data.get("profile_name"), field_name="profile_name", max_length=120)
    except ValueError as exc:
        result.errors.append(str(exc))
        name = ""
    try:
        description = clean_text(
            data.get("description", ""),
            field_name="description",
            max_length=2000,
            allow_empty=True,
        )
    except ValueError as exc:
        result.errors.append(str(exc))
        description = ""

    settings = data.get("settings", {})
    cleaned_settings: dict[str, Any] = {}
    if not isinstance(settings, dict):
        result.errors.append("settings must be an object")
    else:
        for key, value in settings.items():
            if not isinstance(key, str) or not settings_registry.is_supported_key(key):
                result.errors.append(f"Unsupported setting: {key!r}")
                continue
            definition = settings_registry.get_definition(key)
            if not definition.profile_allowed:
                result.errors.append(f"Setting {key} is not allowed in a profile")
                continue
            try:
                cleaned_settings[key] = definition.validate(value)
            except ValueError as exc:
                result.errors.append(str(exc))
            if not definition.remediation_available:
                result.warnings.append(
                    f"{definition.label} is read-only; the profile can compare it but cannot remediate it."
                )

    software = data.get("software", [])
    cleaned_software: list[dict[str, Any]] = []
    if not isinstance(software, list):
        result.errors.append("software must be an array")
    else:
        seen_ids: set[str] = set()
        for index, entry in enumerate(software):
            if not isinstance(entry, dict):
                result.errors.append(f"software[{index}] must be an object")
                continue
            unknown_fields = set(entry) - _SOFTWARE_KEYS
            if unknown_fields:
                result.errors.append(
                    f"software[{index}] contains unsupported fields: {', '.join(sorted(unknown_fields))}"
                )
                continue
            package_id = entry.get("id")
            if not isinstance(package_id, str) or not _ID_RE.match(package_id):
                result.errors.append(f"software[{index}].id is not a valid package identifier")
                continue
            if package_id.lower() in seen_ids:
                result.warnings.append(f"Duplicate software entry: {package_id}")
                continue
            seen_ids.add(package_id.lower())
            item: dict[str, Any] = {"id": package_id}
            if "name" in entry:
                try:
                    item["name"] = clean_text(
                        entry["name"], field_name=f"software[{index}].name", max_length=200
                    )
                except ValueError as exc:
                    result.errors.append(str(exc))
            if "optional" in entry:
                if not isinstance(entry["optional"], bool):
                    result.errors.append(f"software[{index}].optional must be true or false")
                else:
                    item["optional"] = entry["optional"]
            for optional_field, limit in (("version", 80), ("source", 200), ("notes", 500)):
                if optional_field in entry:
                    try:
                        item[optional_field] = clean_text(
                            entry[optional_field],
                            field_name=f"software[{index}].{optional_field}",
                            max_length=limit,
                            allow_empty=True,
                        )
                    except ValueError as exc:
                        result.errors.append(str(exc))
            cleaned_software.append(item)

    checklist = data.get("checklist", [])
    cleaned_checklist: list[dict[str, Any]] = []
    if not isinstance(checklist, list):
        result.errors.append("checklist must be an array")
    else:
        seen_keys: set[str] = set()
        for index, entry in enumerate(checklist):
            if not isinstance(entry, dict):
                result.errors.append(f"checklist[{index}] must be an object")
                continue
            unknown_fields = set(entry) - _CHECKLIST_KEYS
            if unknown_fields:
                result.errors.append(
                    f"checklist[{index}] contains unsupported fields: {', '.join(sorted(unknown_fields))}"
                )
                continue
            key = entry.get("key")
            if not isinstance(key, str) or not _KEY_RE.match(key):
                result.errors.append(f"checklist[{index}].key is invalid")
                continue
            if key in seen_keys:
                result.warnings.append(f"Duplicate checklist key: {key}")
                continue
            seen_keys.add(key)
            try:
                title = clean_text(
                    entry.get("title"), field_name=f"checklist[{index}].title", max_length=200
                )
            except ValueError as exc:
                result.errors.append(str(exc))
                continue
            item = {"key": key, "title": title}
            if "description" in entry:
                try:
                    item["description"] = clean_text(
                        entry["description"],
                        field_name=f"checklist[{index}].description",
                        max_length=1000,
                        allow_empty=True,
                    )
                except ValueError as exc:
                    result.errors.append(str(exc))
            if "required" in entry:
                if not isinstance(entry["required"], bool):
                    result.errors.append(f"checklist[{index}].required must be true or false")
                else:
                    item["required"] = entry["required"]
            if "auto_check" in entry and entry["auto_check"] is not None:
                if not isinstance(entry["auto_check"], str):
                    result.errors.append(f"checklist[{index}].auto_check must be a string")
                else:
                    item["auto_check"] = entry["auto_check"]
            cleaned_checklist.append(item)

    if result.errors:
        return result
    result.document = {
        "schema_version": PROFILE_SCHEMA_VERSION,
        "profile_name": name,
        "description": description,
        "settings": cleaned_settings,
        "software": cleaned_software,
        "checklist": cleaned_checklist,
    }
    return result


def require_valid_profile(data: Any, *, source: str = "profile") -> dict[str, Any]:
    result = validate_profile_document(data, source=source)
    if not result.ok:
        raise ProfileValidationError(result.errors)
    assert result.document is not None
    return result.document


def validate_software_catalog(data: Any) -> ValidationResult:
    """Validate the editable software catalog document."""
    result = ValidationResult()
    if not isinstance(data, dict):
        result.errors.append("The software catalog must be a JSON object")
        return result
    if data.get("schema_version") != 1:
        result.errors.append("The software catalog schema_version must be 1")
    items = data.get("packages")
    if not isinstance(items, list):
        result.errors.append("packages must be an array")
        return result
    seen: set[str] = set()
    cleaned: list[dict[str, Any]] = []
    allowed = {
        "id",
        "name",
        "publisher",
        "category",
        "source",
        "description",
        "license",
        "homepage",
        "silent_args",
        "installer",
        "verify",
        "optional",
        "version",
    }
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            result.errors.append(f"packages[{index}] must be an object")
            continue
        unknown = set(item) - allowed
        if unknown:
            result.errors.append(
                f"packages[{index}] contains unsupported fields: {', '.join(sorted(unknown))}"
            )
            continue
        package_id = item.get("id")
        if not isinstance(package_id, str) or not _ID_RE.match(package_id):
            result.errors.append(f"packages[{index}].id is invalid")
            continue
        if package_id.lower() in seen:
            result.errors.append(f"Duplicate package id: {package_id}")
            continue
        seen.add(package_id.lower())
        if not isinstance(item.get("name"), str) or not item.get("name", "").strip():
            result.errors.append(f"packages[{index}].name is required")
            continue
        if item.get("source") not in {"winget", "local"}:
            result.errors.append(f"packages[{index}].source must be 'winget' or 'local'")
            continue
        if item.get("source") == "local" and not item.get("installer"):
            result.errors.append(f"packages[{index}] is local but has no installer path")
            continue
        silent_args = item.get("silent_args", [])
        if silent_args and (
            not isinstance(silent_args, list)
            or not all(isinstance(arg, str) and "\x00" not in arg for arg in silent_args)
        ):
            result.errors.append(f"packages[{index}].silent_args must be an array of strings")
            continue
        cleaned.append(dict(item))
    if not result.errors:
        result.document = {
            "schema_version": 1,
            "packages": cleaned,
        }
    return result
