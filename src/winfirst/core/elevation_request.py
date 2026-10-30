"""Validated elevation request files.

The non-elevated instance writes a small JSON document describing declarative
settings only.  The elevated instance reads it once, validates it strictly and
deletes it.  The file never contains commands, scripts, credentials or
arbitrary paths.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from winfirst.core.paths import elevation_request_path

MAX_BYTES = 64 * 1024
MAX_SETTINGS = 250
MAX_AGE_SECONDS = 15 * 60
ALLOWED_TYPES = (bool, int, str, type(None))


class ElevationRequestError(ValueError):
    pass


def _validate_settings(settings: Any) -> dict[str, Any]:
    if not isinstance(settings, dict):
        raise ElevationRequestError("The request does not contain a settings object")
    if len(settings) > MAX_SETTINGS:
        raise ElevationRequestError("The request contains too many settings")
    cleaned: dict[str, Any] = {}
    for key, value in settings.items():
        if not isinstance(key, str) or not key or len(key) > 120:
            raise ElevationRequestError("Invalid setting key")
        if not isinstance(value, ALLOWED_TYPES):
            raise ElevationRequestError(f"Unsupported value type for setting {key}")
        if isinstance(value, str) and len(value) > 4096:
            raise ElevationRequestError(f"Value for setting {key} is too long")
        cleaned[key] = value
    return cleaned


def write_request(payload: dict[str, Any], path: Path | None = None) -> Path:
    if not isinstance(payload, dict) or payload.get("type") != "settings":
        raise ElevationRequestError("Only declarative settings requests are supported")
    document = {
        "version": 1,
        "type": "settings",
        "created_at": time.time(),
        "profile_name": str(payload.get("profile_name", ""))[:200],
        "settings": _validate_settings(payload.get("settings")),
    }
    target = path or elevation_request_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(document, indent=2)
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise ElevationRequestError("The request is too large")
    temp = target.with_suffix(".tmp")
    temp.write_text(text, encoding="utf-8")
    os.replace(temp, target)
    return target


def read_request(path: Path | None = None) -> dict[str, Any] | None:
    target = path or elevation_request_path()
    if not target.is_file():
        return None
    try:
        if target.stat().st_size > MAX_BYTES:
            discard(target)
            return None
        document = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        discard(target)
        return None
    finally:
        pass
    try:
        if not isinstance(document, dict) or document.get("version") != 1:
            raise ElevationRequestError("Unsupported request version")
        if document.get("type") != "settings":
            raise ElevationRequestError("Only declarative settings requests are supported")
        created = float(document.get("created_at", 0))
        if created <= 0 or time.time() - created > MAX_AGE_SECONDS:
            raise ElevationRequestError("The elevation request has expired")
        settings = _validate_settings(document.get("settings"))
    except (ElevationRequestError, TypeError, ValueError):
        discard(target)
        return None
    document["settings"] = settings
    return document


def consume_request(path: Path | None = None) -> dict[str, Any] | None:
    document = read_request(path)
    discard(path)
    return document


def discard(path: Path | None = None) -> None:
    target = path or elevation_request_path()
    try:
        target.unlink(missing_ok=True)
    except OSError:
        pass
