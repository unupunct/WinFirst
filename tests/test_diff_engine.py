from __future__ import annotations

from winfirst.core import settings_registry
from winfirst.core.database import Database
from winfirst.core.diff_engine import DiffEngine
from winfirst.models.profile import Profile
from winfirst.models.setting_definition import DetectedSetting


def _comparison(monkeypatch, detected: DetectedSetting, expected: bool = False):
    definition = settings_registry.get_definition("show_file_extensions")
    monkeypatch.setattr(definition, "detect", lambda: detected)
    return DiffEngine().compare(
        Profile(name="Test", settings={"show_file_extensions": expected})
    )


def test_matching_setting(monkeypatch) -> None:
    result = _comparison(monkeypatch, DetectedSetting(False), expected=False)
    assert result.matches == 1
    assert result.differences == 0
    assert result.attention_items == [] or not result.attention_items[0].needs_attention


def test_different_setting_is_remediable(monkeypatch) -> None:
    result = _comparison(monkeypatch, DetectedSetting(True), expected=False)
    assert result.differences == 1
    item = result.items[0]
    assert item.needs_attention
    assert item.status == "different"
    assert item.remediation_available is True
    assert result.remediation_keys() == ["show_file_extensions"]


def test_unsupported_detection_has_no_remediation(monkeypatch) -> None:
    result = _comparison(
        monkeypatch,
        DetectedSetting(False, supported=False, note="Not available"),
        expected=False,
    )
    assert result.unsupported == 1
    item = result.items[0]
    assert item.supported is False
    assert item.remediation_available is False
    assert item.status == "unsupported"


def test_policy_managed_setting_has_no_remediation(monkeypatch) -> None:
    result = _comparison(
        monkeypatch,
        DetectedSetting(True, policy_managed=True),
        expected=False,
    )
    item = result.items[0]
    assert item.policy_managed is True
    assert item.remediation_available is False


def test_detection_error_becomes_unsupported(monkeypatch) -> None:
    definition = settings_registry.get_definition("show_file_extensions")

    def broken():
        raise RuntimeError("cannot read")

    monkeypatch.setattr(definition, "detect", broken)
    result = DiffEngine().compare(
        Profile(name="Test", settings={"show_file_extensions": True})
    )
    assert result.unsupported == 1
    assert "cannot read" in result.items[0].note


def test_unknown_profile_key_is_reported(monkeypatch) -> None:
    result = DiffEngine().compare(Profile(name="Test", settings={"older.setting": True}))
    assert result.unsupported == 1
    assert result.items[0].status == "unsupported"
    assert result.remediation_keys() == []


def test_snapshot_roundtrip(monkeypatch, db: Database) -> None:
    result = _comparison(monkeypatch, DetectedSetting(True), expected=False)
    engine = DiffEngine(db)
    engine.save_snapshot(result)
    snapshots = engine.recent_snapshots(limit=5)
    assert len(snapshots) == 1
    assert snapshots[0]["profile_name"] == "Test"
    assert snapshots[0]["differences"] == 1
    assert engine.latest_deviation_count() == 1


def test_diff_json_is_valid(monkeypatch) -> None:
    result = _comparison(monkeypatch, DetectedSetting(True), expected=False)
    payload = result.to_json()
    assert '"profile_name": "Test"' in payload
    assert '"differences": 1' in payload
