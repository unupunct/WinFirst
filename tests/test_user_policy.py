"""Safety policy for local account enable/disable batches."""
from __future__ import annotations

import pytest

from winfirst.services.users_service import (
    LocalUser,
    UserSnapshot,
    build_toggle_user_spec,
    validate_user_toggle_batch,
)


def _user(name: str, *, admin: bool = False, enabled: bool = True) -> LocalUser:
    return LocalUser(name=name, enabled=enabled, is_admin=admin)


def test_batch_cannot_disable_every_administrator() -> None:
    one = _user("admin-one", admin=True)
    two = _user("admin-two", admin=True)
    snapshot = UserSnapshot(users=[one, two, _user("standard")])

    with pytest.raises(ValueError, match="every enabled administrator"):
        validate_user_toggle_batch([one, two], False, snapshot)


def test_batch_can_disable_an_admin_when_another_remains() -> None:
    one = _user("admin-one", admin=True)
    two = _user("admin-two", admin=True)
    snapshot = UserSnapshot(users=[one, two])
    validate_user_toggle_batch([one], False, snapshot)


def test_batch_can_disable_standard_users() -> None:
    one = _user("standard-one")
    two = _user("standard-two")
    snapshot = UserSnapshot(users=[_user("admin", admin=True), one, two])
    validate_user_toggle_batch([one, two], False, snapshot)


def test_enable_batch_is_always_allowed_by_the_last_admin_policy() -> None:
    one = _user("admin-one", admin=True, enabled=False)
    snapshot = UserSnapshot(users=[one, _user("admin-two", admin=True)])
    validate_user_toggle_batch([one], True, snapshot)


def test_last_enabled_admin_cannot_be_disabled_individually() -> None:
    one = _user("admin-one", admin=True)
    snapshot = UserSnapshot(users=[one])
    with pytest.raises(ValueError, match="no enabled administrator"):
        build_toggle_user_spec(one, False, snapshot)
