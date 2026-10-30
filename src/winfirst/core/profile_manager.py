"""Profile library: create, validate, version, import, export and compare."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from winfirst.core import settings_registry
from winfirst.core.database import Database
from winfirst.core.operation import OperationSpec
from winfirst.core.paths import resources_dir
from winfirst.core.schema_validation import (
    MAX_PROFILE_BYTES,
    ProfileValidationError,
    clean_text,
    require_valid_profile,
    validate_profile_document,
)
from winfirst.models.profile import Profile

_log = logging.getLogger("winfirst.profiles")
BUILTIN_NAMES = [
    "Personal Computer",
    "Desktop Workstation",
    "Laptop Workstation",
    "IT Technician Workstation",
    "Custom Configuration",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ProfileManager:
    def __init__(self, db: Database) -> None:
        self.db = db
        self._ensure_tables_ready()

    def _ensure_tables_ready(self) -> None:
        self.db.query("SELECT 1 FROM profiles LIMIT 1")

    # ---------------------------------------------------------------- builtins
    def builtin_directory(self) -> Path:
        return resources_dir() / "profiles"

    def ensure_builtin_profiles(self) -> int:
        added = 0
        directory = self.builtin_directory()
        if not directory.is_dir():
            _log.warning("Built-in profile directory is missing: %s", directory)
            return 0
        for path in sorted(directory.glob("*.json")):
            try:
                result = validate_profile_document(
                    json.loads(path.read_text(encoding="utf-8")), source=path.name
                )
            except (OSError, json.JSONDecodeError) as exc:
                _log.error("Could not read built-in profile %s: %s", path.name, exc)
                continue
            if not result.ok or result.document is None:
                _log.error("Built-in profile %s is invalid: %s", path.name, result.errors)
                continue
            name = result.document["profile_name"]
            existing = self.db.query_one("SELECT id FROM profiles WHERE name = ?", (name,))
            if existing:
                continue
            self._insert_profile(result.document, is_builtin=True, version=1, note="Built-in profile")
            added += 1
        return added

    # ------------------------------------------------------------------- reads
    def list_profiles(self) -> list[Profile]:
        rows = self.db.query("SELECT * FROM profiles ORDER BY is_builtin DESC, name COLLATE NOCASE")
        versions = {
            int(row["profile_id"]): int(row["version"])
            for row in self.db.query(
                "SELECT profile_id, MAX(version) AS version FROM profile_versions GROUP BY profile_id"
            )
        }
        profiles: list[Profile] = []
        for row in rows:
            try:
                document = json.loads(row["data_json"])
            except (TypeError, json.JSONDecodeError):
                _log.error("Profile %s contains invalid JSON", row.get("name"))
                continue
            profiles.append(
                Profile.from_document(
                    document,
                    is_builtin=bool(row["is_builtin"]),
                    db_id=int(row["id"]),
                    updated_at=str(row["updated_at"]),
                    version=versions.get(int(row["id"]), 0),
                )
            )
        return profiles

    def get_profile(self, name: str) -> Profile:
        row = self.db.query_one("SELECT * FROM profiles WHERE name = ?", (name,))
        if not row:
            raise KeyError(f"Profile not found: {name}")
        document = json.loads(row["data_json"])
        version = int(
            self.db.scalar(
                "SELECT COALESCE(MAX(version), 0) FROM profile_versions WHERE profile_id = ?",
                (int(row["id"]),),
                default=0,
            )
            or 0
        )
        return Profile.from_document(
            document,
            is_builtin=bool(row["is_builtin"]),
            db_id=int(row["id"]),
            updated_at=str(row["updated_at"]),
            version=version,
        )

    def profile_names(self) -> list[str]:
        return [profile.name for profile in self.list_profiles()]

    def version_history(self, name: str) -> list[dict[str, Any]]:
        row = self.db.query_one("SELECT id FROM profiles WHERE name = ?", (name,))
        if not row:
            return []
        return self.db.query(
            "SELECT version, note, created_at FROM profile_versions WHERE profile_id = ? ORDER BY version DESC",
            (int(row["id"]),),
        )

    # ------------------------------------------------------------------ writes
    def _insert_profile(
        self,
        document: dict[str, Any],
        *,
        is_builtin: bool = False,
        version: int = 1,
        note: str = "",
        existing_id: int | None = None,
    ) -> int:
        timestamp = _now()
        payload = json.dumps(document, indent=2, ensure_ascii=False)
        if existing_id is None:
            profile_id = self.db.execute(
                """
                INSERT INTO profiles(name, description, schema_version, is_builtin, data_json, created_at, updated_at)
                VALUES(?,?,?,?,?,?,?)
                """,
                (
                    document["profile_name"],
                    document.get("description", ""),
                    document.get("schema_version", 1),
                    1 if is_builtin else 0,
                    payload,
                    timestamp,
                    timestamp,
                ),
            )
        else:
            profile_id = existing_id
            self.db.execute(
                """
                UPDATE profiles SET description = ?, schema_version = ?, data_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    document.get("description", ""),
                    document.get("schema_version", 1),
                    payload,
                    timestamp,
                    profile_id,
                ),
            )
        self.db.execute(
            "INSERT INTO profile_versions(profile_id, version, data_json, note, created_at) VALUES(?,?,?,?,?)",
            (profile_id, version, payload, note[:500], timestamp),
        )
        return profile_id

    def save_profile(
        self,
        document: dict[str, Any],
        *,
        note: str = "Edited in WinFirst",
        allow_builtin: bool = False,
    ) -> Profile:
        validated = require_valid_profile(document)
        name = validated["profile_name"]
        row = self.db.query_one("SELECT * FROM profiles WHERE name = ?", (name,))
        if row and row["is_builtin"] and not allow_builtin:
            raise PermissionError(
                "Built-in profiles are read-only. Duplicate the profile and edit the copy."
            )
        if row:
            current_version = int(
                self.db.scalar(
                    "SELECT COALESCE(MAX(version), 0) FROM profile_versions WHERE profile_id = ?",
                    (int(row["id"]),),
                    default=0,
                )
                or 0
            )
            self._insert_profile(
                validated,
                is_builtin=bool(row["is_builtin"]),
                version=current_version + 1,
                note=note,
                existing_id=int(row["id"]),
            )
        else:
            self._insert_profile(validated, version=1, note=note)
        return self.get_profile(name)

    def duplicate_profile(self, name: str, new_name: str) -> Profile:
        source = self.get_profile(name)
        cleaned = clean_text(new_name, field_name="Profile name", max_length=120)
        if cleaned == name:
            raise ValueError("The new profile name must be different.")
        if self.db.query_one("SELECT id FROM profiles WHERE name = ?", (cleaned,)):
            raise ValueError("A profile with that name already exists.")
        document = source.to_document()
        document["profile_name"] = cleaned
        document["description"] = (source.description + " (copy)").strip()
        return self.save_profile(document, note=f"Duplicated from {name}")

    def delete_profile(self, name: str) -> None:
        row = self.db.query_one("SELECT id, is_builtin FROM profiles WHERE name = ?", (name,))
        if not row:
            raise KeyError(name)
        if row["is_builtin"]:
            raise PermissionError("Built-in profiles cannot be deleted. Duplicate and edit instead.")
        self.db.execute("DELETE FROM profiles WHERE id = ?", (int(row["id"]),))

    # ------------------------------------------------------------- import/export
    def export_profile(self, name: str, target: str | Path) -> Path:
        profile = self.get_profile(name)
        path = Path(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(profile.to_document(), indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return path

    def import_profile(self, source: str | Path, *, overwrite: bool = False) -> Profile:
        path = Path(source)
        if not path.is_file():
            raise FileNotFoundError(str(path))
        if path.stat().st_size > MAX_PROFILE_BYTES:
            raise ProfileValidationError(["The profile file is too large."])
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProfileValidationError([f"Could not read the profile: {exc}"]) from exc
        result = validate_profile_document(data, source=path.name)
        if not result.ok or result.document is None:
            raise ProfileValidationError(result.errors)
        name = result.document["profile_name"]
        existing = self.db.query_one("SELECT is_builtin FROM profiles WHERE name = ?", (name,))
        if existing and not overwrite:
            raise ProfileValidationError(
                [f"A profile named '{name}' already exists. Choose a different name or confirm overwrite."]
            )
        if existing and existing["is_builtin"] and overwrite:
            raise ProfileValidationError(
                [f"'{name}' is a built-in profile and cannot be overwritten. Rename the imported profile."]
            )
        return self.save_profile(result.document, note=f"Imported from {path.name}", allow_builtin=False)

    # ------------------------------------------------------------ capture/apply
    def capture_current(self, keys: list[str] | None = None) -> tuple[dict[str, Any], list[str]]:
        """Capture supported settings from this computer for profile creation."""
        definitions = settings_registry.all_definitions()
        if keys:
            wanted = set(keys)
            definitions = [d for d in definitions if d.key in wanted]
        captured: dict[str, Any] = {}
        notes: list[str] = []
        for definition in definitions:
            if not definition.profile_allowed:
                continue
            try:
                detected = definition.detect()
            except Exception as exc:
                notes.append(f"{definition.label}: could not be read ({exc})")
                continue
            if not detected.supported:
                notes.append(f"{definition.label}: unsupported on this computer")
                continue
            if not detected.exists:
                notes.append(f"{definition.label}: not configured; not captured")
                continue
            try:
                captured[definition.key] = definition.validate(detected.value)
            except ValueError as exc:
                notes.append(f"{definition.label}: {exc}")
        return captured, notes

    def build_apply_specs(
        self, profile: Profile, selected_keys: list[str] | None = None
    ) -> tuple[list[OperationSpec], list[str]]:
        keys = selected_keys if selected_keys is not None else list(profile.settings)
        specs: list[OperationSpec] = []
        skipped: list[str] = []
        for key in keys:
            if key not in profile.settings:
                continue
            definition = settings_registry.get_definition(key)
            if definition.apply is None or not definition.remediation_available:
                skipped.append(f"{definition.label}: read-only inspection only")
                continue
            try:
                specs.append(settings_registry.build_spec(key, profile.settings[key]))
            except ValueError as exc:
                skipped.append(f"{definition.label}: {exc}")
        return specs, skipped

    def setting_summary(self, profile: Profile) -> list[tuple[str, str, str]]:
        rows: list[tuple[str, str, str]] = []
        for key, value in profile.settings.items():
            try:
                definition = settings_registry.get_definition(key)
                rows.append((definition.label, definition.format_value(value), definition.group))
            except KeyError:
                rows.append((key, str(value), "Unknown"))
        return rows
