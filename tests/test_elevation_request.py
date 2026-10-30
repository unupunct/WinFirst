from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from winfirst.core import elevation_request


def test_write_and_read_request(tmp_path: Path) -> None:
    path = tmp_path / "request.json"
    written = elevation_request.write_request(
        {
            "type": "settings",
            "profile_name": "Test profile",
            "settings": {"show_file_extensions": True, "ac_display_timeout_minutes": 10},
        },
        path,
    )
    assert written == path
    document = elevation_request.read_request(path)
    assert document is not None
    assert document["type"] == "settings"
    assert document["profile_name"] == "Test profile"
    assert document["settings"]["show_file_extensions"] is True


def test_only_declarative_settings_are_accepted(tmp_path: Path) -> None:
    path = tmp_path / "request.json"
    with pytest.raises(elevation_request.ElevationRequestError):
        elevation_request.write_request({"type": "command", "settings": {}}, path)
    with pytest.raises(elevation_request.ElevationRequestError):
        elevation_request.write_request(
            {"type": "settings", "settings": {"nested_wrong": {"a": 1}}}, path
        )
    with pytest.raises(elevation_request.ElevationRequestError):
        elevation_request.write_request(
            {"type": "settings", "settings": {"list": ["nested"]}}, path
        )


def test_expired_request_is_discarded(tmp_path: Path) -> None:
    path = tmp_path / "request.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "type": "settings",
                "created_at": time.time() - elevation_request.MAX_AGE_SECONDS - 1,
                "profile_name": "",
                "settings": {},
            }
        ),
        encoding="utf-8",
    )
    assert elevation_request.read_request(path) is None
    assert not path.exists()


def test_invalid_json_is_discarded(tmp_path: Path) -> None:
    path = tmp_path / "request.json"
    path.write_text("{not json", encoding="utf-8")
    assert elevation_request.read_request(path) is None
    assert not path.exists()


def test_consume_removes_the_file(tmp_path: Path) -> None:
    path = tmp_path / "request.json"
    elevation_request.write_request(
        {"type": "settings", "settings": {"show_file_extensions": True}}, path
    )
    document = elevation_request.consume_request(path)
    assert document is not None
    assert not path.exists()


def test_missing_request_returns_none(tmp_path: Path) -> None:
    assert elevation_request.read_request(tmp_path / "missing.json") is None
