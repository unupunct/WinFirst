from __future__ import annotations

import json
from pathlib import Path

import pytest

from winfirst.core.database import Database
from winfirst.core.profile_manager import ProfileManager
from winfirst.core.schema_validation import ProfileValidationError


@pytest.fixture
def manager(db: Database) -> ProfileManager:
    return ProfileManager(db)


def _document(name: str = "Test profile") -> dict:
    return {
        "schema_version": 1,
        "profile_name": name,
        "description": "Created by tests",
        "settings": {"show_file_extensions": True},
        "software": [{"id": "Mozilla.Firefox", "name": "Firefox"}],
        "checklist": [{"key": "check_one", "title": "Check one", "required": True}],
    }


def test_builtin_profiles_load_once(manager: ProfileManager) -> None:
    loaded = manager.ensure_builtin_profiles()
    assert loaded >= 5
    second = manager.ensure_builtin_profiles()
    assert second == 0
    names = manager.profile_names()
    assert "Custom Configuration" in names
    assert len(names) >= 5
    assert all(manager.get_profile(name).is_builtin for name in names)


def test_create_and_update_profile_versions(manager: ProfileManager) -> None:
    profile = manager.save_profile(_document(), note="first")
    assert profile.name == "Test profile"
    assert profile.version == 1
    history = manager.version_history(profile.name)
    assert len(history) == 1
    assert history[0]["version"] == 1

    updated = _document()
    updated["settings"]["show_file_extensions"] = False
    saved = manager.save_profile(updated, note="second")
    assert saved.settings["show_file_extensions"] is False
    history = manager.version_history(saved.name)
    assert [row["version"] for row in history] == [2, 1]


def test_invalid_profile_is_rejected(manager: ProfileManager) -> None:
    document = _document()
    document["settings"]["unknown_setting"] = True
    with pytest.raises(ProfileValidationError):
        manager.save_profile(document)
    assert "Test profile" not in manager.profile_names()


def test_builtin_profile_cannot_be_edited_or_deleted(manager: ProfileManager) -> None:
    manager.ensure_builtin_profiles()
    profile = manager.get_profile("Laptop Workstation")
    with pytest.raises(PermissionError):
        manager.save_profile(profile.to_document())
    with pytest.raises(PermissionError):
        manager.delete_profile(profile.name)


def test_duplicate_then_delete(manager: ProfileManager) -> None:
    manager.save_profile(_document())
    copy = manager.duplicate_profile("Test profile", "Test profile copy")
    assert copy.name == "Test profile copy"
    assert copy.description.endswith("(copy)")
    with pytest.raises(ValueError):
        manager.duplicate_profile("Test profile", "Test profile")
    with pytest.raises(ValueError):
        manager.duplicate_profile("Test profile", "Test profile copy")
    manager.delete_profile(copy.name)
    assert copy.name not in manager.profile_names()


def test_export_and_import_roundtrip(manager: ProfileManager, tmp_path: Path) -> None:
    manager.save_profile(_document())
    exported = manager.export_profile("Test profile", tmp_path / "profile.json")
    assert exported.is_file()
    raw = json.loads(exported.read_text(encoding="utf-8"))
    assert raw["profile_name"] == "Test profile"

    raw["profile_name"] = "Imported profile"
    imported_path = tmp_path / "import.json"
    imported_path.write_text(json.dumps(raw), encoding="utf-8")
    imported = manager.import_profile(imported_path)
    assert imported.name == "Imported profile"
    assert imported.settings == {"show_file_extensions": True}

    with pytest.raises(ProfileValidationError):
        manager.import_profile(imported_path)


def test_import_can_overwrite_a_user_profile(manager: ProfileManager, tmp_path: Path) -> None:
    manager.save_profile(_document())
    changed = _document()
    changed["settings"]["show_file_extensions"] = False
    path = tmp_path / "changed.json"
    path.write_text(json.dumps(changed), encoding="utf-8")
    imported = manager.import_profile(path, overwrite=True)
    assert imported.settings["show_file_extensions"] is False


def test_missing_profile_raises(manager: ProfileManager) -> None:
    with pytest.raises(KeyError):
        manager.get_profile("does not exist")


def test_profile_summary_mentions_all_parts(manager: ProfileManager) -> None:
    profile = manager.save_profile(_document())
    assert "1 settings" in profile.summary()
    assert "1 software" in profile.summary()
    assert "1 checklist" in profile.summary()
