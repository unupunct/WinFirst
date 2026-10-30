"""Typed application settings stored in SQLite."""
from __future__ import annotations

import json
from typing import Any

from winfirst.core.database import Database

DEFAULTS: dict[str, str] = {
    "theme": "dark",
    "history_retention_days": "365",
    "dashboard_auto_refresh": "true",
    "repository_dir": "",
    "last_profile": "",
    "report_author": "",
    "confirm_before_close": "false",
    "checklist_template": "Standard Workstation",
    "show_informational_diffs": "true",
    "software_accept_agreements": "false",
    "restore_point_before_apply": "true",
}

_BOOL_TRUE = {"1", "true", "yes", "on"}


class AppSettings:
    def __init__(self, db: Database) -> None:
        self.db = db
        self._cache: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        try:
            rows = self.db.query("SELECT key, value FROM app_settings")
        except Exception:
            rows = []
        self._cache = {str(row["key"]): str(row["value"]) for row in rows}

    def get(self, key: str, default: str | None = None) -> str:
        if key in self._cache:
            return self._cache[key]
        if default is not None:
            return default
        return DEFAULTS.get(key, "")

    def get_bool(self, key: str, default: bool = False) -> bool:
        raw = self.get(key, "true" if default else "false")
        return raw.strip().lower() in _BOOL_TRUE

    def get_int(self, key: str, default: int = 0) -> int:
        try:
            return int(self.get(key, str(default)))
        except (TypeError, ValueError):
            return default

    def set(self, key: str, value: Any) -> None:
        text = json.dumps(value) if isinstance(value, (dict, list)) else str(value)
        from winfirst.core.operation_result import utc_now

        self.db.execute(
            """
            INSERT INTO app_settings(key, value, updated_at) VALUES(?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (key, text, utc_now()),
        )
        self._cache[key] = text

    def set_many(self, values: dict[str, Any]) -> None:
        for key, value in values.items():
            self.set(key, value)

    def all(self) -> dict[str, str]:
        merged = dict(DEFAULTS)
        merged.update(self._cache)
        return merged

    def is_managed_by_policy(self, key: str) -> tuple[bool, str]:
        """Placeholder for future policy-aware settings.

        WinFirst never claims a setting is policy-managed unless it can verify
        the relevant policy registry value.  Individual services perform that
        check for their own settings.
        """
        return False, ""
