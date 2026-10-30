"""Models for configurable settings."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from winfirst.core.operation import ApplyOutcome
from winfirst.core.risk_engine import RiskLevel

Detector = Callable[[], "DetectedSetting"]
Applier = Callable[[Any], ApplyOutcome]


@dataclass
class DetectedSetting:
    value: Any
    exists: bool = True
    supported: bool = True
    raw: str = ""
    note: str = ""
    policy_managed: bool = False
    error: str = ""

    @property
    def status(self) -> str:
        if not self.supported:
            return "Unsupported"
        if not self.exists:
            return "Not configured"
        return str(self.value)


@dataclass
class SettingDefinition:
    key: str
    label: str
    group: str
    description: str
    value_type: str  # bool | int | enum | string
    default: Any
    detect: Detector
    apply: Applier | None = None
    choices: list[tuple[Any, str]] = field(default_factory=list)
    minimum: int | None = None
    maximum: int | None = None
    unit: str = ""
    risk: RiskLevel = RiskLevel.LOW
    requires_admin: bool = False
    reversible: bool = True
    requires_restart: bool = False
    consequences: str = ""
    verification: str = "Reads back the setting"
    profile_allowed: bool = True
    remediation_available: bool = True
    severity: str = "medium"
    sensitivity: str = ""  # e.g. "privacy"
    tags: list[str] = field(default_factory=list)

    @property
    def read_only(self) -> bool:
        return self.apply is None

    def format_value(self, value: Any) -> str:
        if value is None:
            return "Not available"
        if self.value_type == "bool":
            return "Enabled" if bool(value) else "Disabled"
        if self.value_type == "enum":
            for choice_value, choice_label in self.choices:
                if choice_value == value:
                    return choice_label
            return str(value)
        if self.unit:
            if self.value_type == "int" and value == 0 and self.unit == "minutes":
                return "Never"
            return f"{value} {self.unit}"
        return str(value)

    def validate(self, value: Any) -> Any:
        """Validate and normalise a profile-supplied value."""
        if self.value_type == "bool":
            if isinstance(value, bool):
                return value
            if isinstance(value, int) and value in (0, 1):
                return bool(value)
            raise ValueError(f"{self.label}: expected true or false")
        if self.value_type == "int":
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{self.label}: expected an integer")
            if self.minimum is not None and value < self.minimum:
                raise ValueError(f"{self.label}: minimum is {self.minimum}")
            if self.maximum is not None and value > self.maximum:
                raise ValueError(f"{self.label}: maximum is {self.maximum}")
            return value
        if self.value_type == "enum":
            allowed = [choice[0] for choice in self.choices]
            if value not in allowed:
                raise ValueError(f"{self.label}: expected one of {allowed}")
            return value
        if self.value_type == "string":
            if not isinstance(value, str):
                raise ValueError(f"{self.label}: expected a string")
            if len(value) > 1024:
                raise ValueError(f"{self.label}: value is too long")
            return value
        raise ValueError(f"{self.label}: unsupported value type")
