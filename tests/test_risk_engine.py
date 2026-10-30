from __future__ import annotations

import pytest

from winfirst.core.risk_engine import (
    KNOWN_ACTIONS,
    RiskLevel,
    assess,
    assess_action,
    requires_extra_confirmation,
)


def test_unknown_risk_level_parses_as_informational() -> None:
    assert RiskLevel.parse("not-a-level") is RiskLevel.INFORMATIONAL
    assert RiskLevel.parse(None) is RiskLevel.INFORMATIONAL


def test_risk_rank_and_labels() -> None:
    assert RiskLevel.INFORMATIONAL.rank == 0
    assert RiskLevel.CRITICAL.rank == 4
    assert RiskLevel.MEDIUM.label == "Medium"


def test_read_only_assessment_is_informational() -> None:
    result = assess(RiskLevel.CRITICAL, modifies_state=False)
    assert result.level is RiskLevel.INFORMATIONAL
    assert result.requires_confirmation is False
    assert result.requires_typed_confirmation is False
    assert result.reversible is False


def test_medium_requires_confirmation_but_not_typing() -> None:
    result = assess(RiskLevel.MEDIUM, reversible=True)
    assert result.requires_confirmation is True
    assert result.requires_typed_confirmation is False
    assert result.reversible is True


def test_critical_requires_typed_confirmation() -> None:
    result = assess(RiskLevel.CRITICAL)
    assert result.requires_confirmation is True
    assert result.requires_typed_confirmation is True


def test_known_actions_are_stable_and_safe() -> None:
    assert KNOWN_ACTIONS["appx.remove"].level is RiskLevel.HIGH
    assert KNOWN_ACTIONS["cleanup.delete"].reversible is False
    assert KNOWN_ACTIONS["power.set_active_plan"].level is RiskLevel.LOW
    assert KNOWN_ACTIONS["driver.export"].modifies_state is False


def test_known_action_override_keeps_base_risk() -> None:
    result = assess_action("power.set_active_plan", consequences="Custom text")
    assert result.level is RiskLevel.LOW
    assert result.consequences == "Custom text"


def test_unknown_action_defaults_to_medium() -> None:
    result = assess_action("something.new")
    assert result.level is RiskLevel.MEDIUM


def test_extra_confirmation_threshold() -> None:
    assert requires_extra_confirmation(RiskLevel.HIGH) is True
    assert requires_extra_confirmation(RiskLevel.CRITICAL) is True
    assert requires_extra_confirmation(RiskLevel.MEDIUM) is False
    assert requires_extra_confirmation("unknown") is False
