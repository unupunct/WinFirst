"""Configuration drift comparison engine.

Every item reports expected and actual values, how the value was detected,
whether remediation is available and whether administrator rights are needed.
Missing values are reported as "not configured" rather than as a known wrong
value, and unsupported settings are never counted as failures.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from winfirst.core import settings_registry
from winfirst.core.database import Database
from winfirst.core.risk_engine import RiskLevel
from winfirst.models.profile import Profile

SEVERITY_ORDER = {
    RiskLevel.CRITICAL: 0,
    RiskLevel.HIGH: 1,
    RiskLevel.MEDIUM: 2,
    RiskLevel.LOW: 3,
    RiskLevel.INFORMATIONAL: 4,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class DiffItem:
    key: str
    label: str
    group: str
    expected: Any
    actual: Any
    expected_display: str
    actual_display: str
    severity: RiskLevel
    status: str  # match | different | not_configured | unsupported | missing_software
    detection_method: str = ""
    supported: bool = True
    remediation_available: bool = False
    requires_admin: bool = False
    reversible: bool = True
    consequences: str = ""
    note: str = ""
    policy_managed: bool = False

    @property
    def needs_attention(self) -> bool:
        return self.status in {"different", "not_configured", "missing_software"}

    @property
    def status_label(self) -> str:
        return {
            "match": "Match",
            "different": "Different",
            "not_configured": "Not configured",
            "unsupported": "Unsupported",
            "missing_software": "Not installed",
        }.get(self.status, self.status)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "group": self.group,
            "expected": self.expected,
            "actual": self.actual,
            "expected_display": self.expected_display,
            "actual_display": self.actual_display,
            "severity": self.severity.value,
            "status": self.status,
            "detection_method": self.detection_method,
            "supported": self.supported,
            "remediation_available": self.remediation_available,
            "requires_admin": self.requires_admin,
            "reversible": self.reversible,
            "consequences": self.consequences,
            "note": self.note,
            "policy_managed": self.policy_managed,
        }


@dataclass
class DiffResult:
    profile_name: str
    items: list[DiffItem] = field(default_factory=list)
    created_at: str = field(default_factory=_now)
    missing_software: list[str] = field(default_factory=list)

    @property
    def matches(self) -> int:
        return sum(1 for item in self.items if item.status == "match")

    @property
    def differences(self) -> int:
        return sum(1 for item in self.items if item.status == "different")

    @property
    def unconfigured(self) -> int:
        return sum(1 for item in self.items if item.status == "not_configured")

    @property
    def unsupported(self) -> int:
        return sum(1 for item in self.items if item.status == "unsupported")

    @property
    def deviations(self) -> int:
        """Differences that are confirmed (not merely unset or unsupported)."""
        return self.differences + len(self.missing_software)

    @property
    def attention_items(self) -> list[DiffItem]:
        return [item for item in self.items if item.needs_attention]

    def sorted_items(self) -> list[DiffItem]:
        return sorted(
            self.items,
            key=lambda item: (
                0 if item.needs_attention else 1,
                SEVERITY_ORDER.get(item.severity, 9),
                item.group,
                item.label,
            ),
        )

    def remediation_keys(self) -> list[str]:
        return [
            item.key
            for item in self.items
            if item.needs_attention
            and item.remediation_available
            and item.supported
            and not item.key.startswith("software.")
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_name": self.profile_name,
            "created_at": self.created_at,
            "matches": self.matches,
            "differences": self.differences,
            "unconfigured": self.unconfigured,
            "unsupported": self.unsupported,
            "missing_software": list(self.missing_software),
            "deviations": self.deviations,
            "items": [item.to_dict() for item in self.items],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, ensure_ascii=False)


class DiffEngine:
    def __init__(self, db: Database | None = None) -> None:
        self.db = db

    def compare(
        self,
        profile: Profile,
        *,
        keys: Iterable[str] | None = None,
        include_software: bool = False,
        installed_software_ids: set[str] | None = None,
    ) -> DiffResult:
        selected = set(keys) if keys is not None else None
        result = DiffResult(profile_name=profile.name)
        for key, expected in profile.settings.items():
            if selected is not None and key not in selected:
                continue
            try:
                definition = settings_registry.get_definition(key)
            except KeyError:
                result.items.append(
                    DiffItem(
                        key=key,
                        label=key,
                        group="Unknown",
                        expected=expected,
                        actual=None,
                        expected_display=str(expected),
                        actual_display="Unknown setting",
                        severity=RiskLevel.MEDIUM,
                        status="unsupported",
                        supported=False,
                        note="This profile references a setting this build does not support.",
                    )
                )
                continue
            try:
                detected = definition.detect()
            except Exception as exc:
                result.items.append(
                    DiffItem(
                        key=key,
                        label=definition.label,
                        group=definition.group,
                        expected=expected,
                        actual=None,
                        expected_display=definition.format_value(expected),
                        actual_display="Unavailable",
                        severity=RiskLevel.INFORMATIONAL,
                        status="unsupported",
                        supported=False,
                        detection_method=definition.verification,
                        note=f"Detection failed: {exc}",
                    )
                )
                continue
            expected_display = definition.format_value(expected)
            actual_display = definition.format_value(detected.value)
            remediation = bool(
                definition.apply is not None
                and definition.remediation_available
                and detected.supported
                and not detected.policy_managed
            )
            base = dict(
                key=key,
                label=definition.label,
                group=definition.group,
                expected=expected,
                actual=detected.value,
                expected_display=expected_display,
                actual_display=actual_display,
                detection_method=definition.verification,
                supported=detected.supported,
                remediation_available=remediation,
                requires_admin=definition.requires_admin,
                reversible=definition.reversible,
                consequences=definition.consequences,
                note=detected.note,
                policy_managed=detected.policy_managed,
            )
            if not detected.supported:
                result.items.append(
                    DiffItem(
                        severity=RiskLevel.INFORMATIONAL,
                        status="unsupported",
                        **base,
                    )
                )
                continue
            if settings_registry.values_match(expected, detected.value):
                result.items.append(
                    DiffItem(severity=RiskLevel.INFORMATIONAL, status="match", **base)
                )
                continue
            if not detected.exists:
                matches_default = settings_registry.values_match(
                    expected, definition.default
                )
                result.items.append(
                    DiffItem(
                        severity=RiskLevel.INFORMATIONAL if matches_default else RiskLevel.parse(definition.severity),
                        status="not_configured",
                        **base,
                    )
                )
                continue
            result.items.append(
                DiffItem(
                    severity=RiskLevel.parse(definition.severity),
                    status="different",
                    **base,
                )
            )

        if include_software and profile.software:
            installed = {value.casefold() for value in (installed_software_ids or set())}
            for entry in profile.software:
                package_id = str(entry.get("id", ""))
                if not package_id:
                    continue
                result.items.append(
                    DiffItem(
                        key=f"software.{package_id}",
                        label=str(entry.get("name") or package_id),
                        group="Software",
                        expected=package_id,
                        actual="installed" if package_id.casefold() in installed else "not installed",
                        expected_display="Installed",
                        actual_display="Installed" if package_id.casefold() in installed else "Not installed",
                        severity=RiskLevel.MEDIUM,
                        status="match"
                        if package_id.casefold() in installed
                        else "missing_software",
                        detection_method="winget package identifier / inventory",
                        supported=True,
                        remediation_available=False,
                        note="Install this package from the Software Installation page.",
                    )
                )
                if package_id.casefold() not in installed:
                    result.missing_software.append(package_id)
        return result

    # --------------------------------------------------------------- snapshots
    def save_snapshot(self, result: DiffResult) -> int:
        if self.db is None:
            return 0
        return self.db.execute(
            """
            INSERT INTO drift_snapshots(profile_name, created_at, total, differences, data_json)
            VALUES(?,?,?,?,?)
            """,
            (
                result.profile_name,
                result.created_at,
                len(result.items),
                result.deviations,
                result.to_json(),
            ),
        )

    def recent_snapshots(self, limit: int = 20) -> list[dict[str, Any]]:
        if self.db is None:
            return []
        return self.db.query(
            "SELECT id, profile_name, created_at, total, differences FROM drift_snapshots ORDER BY id DESC LIMIT ?",
            (max(1, limit),),
        )

    def latest_deviation_count(self) -> int | None:
        if self.db is None:
            return None
        row = self.db.query_one(
            "SELECT differences FROM drift_snapshots ORDER BY id DESC LIMIT 1"
        )
        return int(row["differences"]) if row else None
