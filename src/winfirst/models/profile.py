"""Validated configuration profile model."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from winfirst.core.schema_validation import PROFILE_SCHEMA_VERSION


@dataclass
class Profile:
    name: str
    description: str = ""
    settings: dict[str, Any] = field(default_factory=dict)
    software: list[dict[str, Any]] = field(default_factory=list)
    checklist: list[dict[str, Any]] = field(default_factory=list)
    schema_version: int = PROFILE_SCHEMA_VERSION
    is_builtin: bool = False
    db_id: int | None = None
    updated_at: str = ""
    version: int = 0

    def to_document(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "profile_name": self.name,
            "description": self.description,
            "settings": dict(self.settings),
            "software": [dict(item) for item in self.software],
            "checklist": [dict(item) for item in self.checklist],
        }

    @classmethod
    def from_document(cls, document: dict[str, Any], **kwargs: Any) -> "Profile":
        return cls(
            name=str(document.get("profile_name", "")),
            description=str(document.get("description", "")),
            settings=dict(document.get("settings") or {}),
            software=[dict(item) for item in document.get("software") or []],
            checklist=[dict(item) for item in document.get("checklist") or []],
            schema_version=int(document.get("schema_version", PROFILE_SCHEMA_VERSION)),
            **kwargs,
        )

    @property
    def setting_count(self) -> int:
        return len(self.settings)

    @property
    def software_count(self) -> int:
        return len(self.software)

    @property
    def checklist_count(self) -> int:
        return len(self.checklist)

    def summary(self) -> str:
        bits = [f"{self.setting_count} settings"]
        if self.software_count:
            bits.append(f"{self.software_count} software items")
        if self.checklist_count:
            bits.append(f"{self.checklist_count} checklist tasks")
        return ", ".join(bits)
