"""Policy tests for service changes.

The UI is not exercised here; these tests pin the rules that every caller must
follow before an operation is built.
"""
from __future__ import annotations

import pytest

from winfirst.services.service_audit_service import (
    PROTECTED_SERVICES,
    ServiceInfo,
    build_start_stop_spec,
    build_startup_mode_spec,
    validate_startup_mode_batch,
)


def _service(name: str = "example", *, protected: bool = False) -> ServiceInfo:
    return ServiceInfo(
        name=name,
        display_name=name.title(),
        state="Stopped",
        start_mode="demand",
        protected=protected,
        protection_reason="protected" if protected else "",
    )


def test_single_service_may_be_disabled() -> None:
    validate_startup_mode_batch([_service()], "disabled")


def test_batch_disable_is_refused() -> None:
    with pytest.raises(ValueError, match="mass-disable"):
        validate_startup_mode_batch([_service("one"), _service("two")], "disabled")


def test_batch_automatic_or_manual_is_allowed() -> None:
    services = [_service("one"), _service("two"), _service("three")]
    validate_startup_mode_batch(services, "auto")
    validate_startup_mode_batch(services, "demand")


def test_protected_service_cannot_be_changed() -> None:
    with pytest.raises(ValueError, match="protected"):
        build_startup_mode_spec(_service("windefend", protected=True), "disabled")
    with pytest.raises(ValueError, match="protected"):
        build_start_stop_spec(_service("windefend", protected=True), False)


def test_unknown_startup_mode_is_rejected() -> None:
    with pytest.raises(ValueError, match="Unsupported"):
        build_startup_mode_spec(_service(), "sometimes")


def test_security_and_platform_services_are_protected() -> None:
    required = {
        "windefend",
        "wscsvc",
        "securityhealthservice",
        "wdnissvc",
        "sense",
        "mpssvc",
        "bfe",
        "wuauserv",
        "eventlog",
        "rpcss",
        "schedule",
        "winmgmt",
        "spooler",
    }
    assert required <= PROTECTED_SERVICES
    assert len(PROTECTED_SERVICES) == len({name.casefold() for name in PROTECTED_SERVICES})

