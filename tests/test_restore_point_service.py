from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from winfirst.core.command_runner import CommandResult
from winfirst.services import restore_point_service as rp
from winfirst.services.common import ServiceError
from winfirst.services.restore_point_service import RestorePointInfo, RestorePointService
from winfirst.services.win_registry import RegValue

NOW = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)


class FakePowerShell:
    """Records scripts and returns queued results.

    A callable queue item receives the script text so tests can answer list
    queries and checkpoint commands differently.
    """

    def __init__(self, *answers):
        self.answers = list(answers)
        self.scripts: list[str] = []

    def powershell(self, script, **_kwargs):
        self.scripts.append(script)
        if not self.answers:
            return CommandResult(argv=["powershell.exe"], launched=True, exit_code=0, stdout="")
        answer = self.answers.pop(0)
        if callable(answer):
            return answer(script)
        return answer

    @property
    def checkpoints(self) -> list[str]:
        return [script for script in self.scripts if "Checkpoint-Computer" in script]


def _points(rows):
    return CommandResult(
        argv=["powershell.exe"],
        launched=True,
        exit_code=0,
        stdout=json.dumps(rows),
    )


def _point(sequence: int, description: str, created_at: str) -> dict:
    return {
        "SequenceNumber": sequence,
        "Description": description,
        "RestorePointType": "MODIFY_SETTINGS",
        "CreatedAt": created_at,
    }


@pytest.fixture
def registry(monkeypatch):
    values: dict[tuple[str, str], RegValue] = {}

    def fake_read_value(hive, path, name):
        return values.get((path, name), RegValue(exists=False))

    monkeypatch.setattr(rp, "read_value", fake_read_value)
    return values


def _service(runner, *, elevated=True, clock=None) -> RestorePointService:
    return RestorePointService(
        runner,
        elevation_check=lambda: elevated,
        clock=clock or (lambda: NOW),
    )


def test_support_defaults_to_enabled_with_windows_frequency(registry) -> None:
    support = _service(FakePowerShell()).support()
    assert support.available is True
    assert support.enabled is True
    assert support.frequency_minutes == 1440
    assert support.can_create is True


def test_support_requires_elevation(registry) -> None:
    support = _service(FakePowerShell(), elevated=False).support()
    assert support.enabled is True
    assert support.elevated is False
    assert support.can_create is False
    assert "administrator rights" in support.summary()


def test_support_reports_disabled_system_protection(registry) -> None:
    registry[(rp.RESTORE_KEY, rp.DISABLE_VALUE)] = RegValue(exists=True, value=1)
    support = _service(FakePowerShell()).support()
    assert support.enabled is False
    assert support.can_create is False
    assert "System Protection is disabled" in support.summary()


def test_support_reads_custom_frequency(registry) -> None:
    registry[(rp.RESTORE_KEY, rp.FREQUENCY_VALUE)] = RegValue(exists=True, value=0)
    support = _service(FakePowerShell()).support()
    assert support.frequency_minutes == 0
    assert "does not throttle" in support.summary()


def test_list_points_parses_single_object_and_sorts(registry) -> None:
    runner = FakePowerShell(
        _points(
            [
                _point(7, "Old", "2026-04-01T10:00:00+00:00"),
                _point(9, "Newer", "2026-04-30T10:00:00+00:00"),
            ]
        )
    )
    points = _service(runner).list_points()
    assert [point.sequence for point in points] == [9, 7]
    assert points[0].description == "Newer"
    assert points[0].created_datetime() == datetime(2026, 4, 30, 10, 0, tzinfo=timezone.utc)


def test_list_points_handles_empty_output(registry) -> None:
    runner = FakePowerShell(CommandResult(argv=["ps"], launched=True, exit_code=0, stdout=""))
    assert _service(runner).list_points() == []


def test_wmi_datetime_is_parsed(registry) -> None:
    point = RestorePointInfo(1, "x", "20260430101010.000000-000")
    assert point.created_datetime() == datetime(2026, 4, 30, 10, 10, 10, tzinfo=timezone.utc)


def test_create_requires_elevation(registry) -> None:
    with pytest.raises(ServiceError, match="administrator rights"):
        _service(FakePowerShell(), elevated=False).create("WinFirst test")


def test_create_refuses_when_protection_is_disabled(registry) -> None:
    registry[(rp.RESTORE_KEY, rp.DISABLE_VALUE)] = RegValue(exists=True, value=1)
    with pytest.raises(ServiceError, match="System Protection is disabled"):
        _service(FakePowerShell()).create("WinFirst test")


def test_create_verifies_the_new_point(registry) -> None:
    old = _point(3, "Old", "2026-04-01T10:00:00+00:00")
    new = _point(4, "WinFirst test", "2026-05-01T11:59:00+00:00")

    def answer(script: str):
        if "Checkpoint-Computer" in script:
            return CommandResult(argv=["ps"], launched=True, exit_code=0)
        if "SystemRestore" in script:
            return _points([new, old])
        return CommandResult(argv=["ps"], launched=True, exit_code=0)

    runner = FakePowerShell(  # newest before, checkpoint, list after
        lambda script: _points([old]),
        answer,
        lambda script: _points([new, old]),
    )
    point = _service(runner).create("WinFirst test", wait_seconds=1, poll_interval=0.01)
    assert point.sequence == 4
    assert len(runner.checkpoints) == 1
    assert "WinFirst test" in runner.checkpoints[0]


def test_create_reports_the_windows_frequency_limit(registry) -> None:
    old = _point(3, "Old", "2026-05-01T10:00:00+00:00")
    failure = CommandResult(
        argv=["ps"],
        launched=True,
        exit_code=1,
        stderr=(
            "A new system restore point cannot be created because one has already "
            "been created within the past 24 hours."
        ),
    )
    runner = FakePowerShell(
        lambda script: _points([old]),
        lambda script: failure,
        lambda script: _points([old]),
    )
    with pytest.raises(ServiceError, match="created recently"):
        _service(runner).create("WinFirst test", wait_seconds=0.1, poll_interval=0.01)


def test_create_raises_when_the_point_never_appears(registry) -> None:
    old = _point(3, "Old", "2026-03-01T10:00:00+00:00")
    runner = FakePowerShell(
        lambda script: _points([old]),
        lambda script: CommandResult(argv=["ps"], launched=True, exit_code=0),
        lambda script: _points([old]),
    )
    with pytest.raises(ServiceError, match="no new restore point appeared"):
        _service(runner).create("WinFirst test", wait_seconds=0.05, poll_interval=0.01)


def test_ensure_reuses_a_recent_point_without_a_checkpoint(registry) -> None:
    recent = _point(8, "Recent", "2026-05-01T11:00:00+00:00")
    runner = FakePowerShell(lambda script: _points([recent]))
    point, created = _service(runner).ensure_point("WinFirst batch")
    assert created is False
    assert point.sequence == 8
    assert runner.checkpoints == []


def test_ensure_creates_when_the_last_point_is_old(registry) -> None:
    old = _point(2, "Old", "2026-04-01T10:00:00+00:00")
    new = _point(3, "WinFirst batch", "2026-05-01T11:59:30+00:00")
    runner = FakePowerShell(
        lambda script: _points([old]),  # newest check
        lambda script: _points([old]),  # create() reads newest again
        lambda script: CommandResult(argv=["ps"], launched=True, exit_code=0),
        lambda script: _points([new, old]),
    )
    point, created = _service(runner).ensure_point("WinFirst batch")
    assert created is True
    assert point.sequence == 3


def test_throttle_remaining_uses_frequency_and_clock(registry) -> None:
    recent = _point(8, "Recent", "2026-05-01T11:00:00+00:00")
    runner = FakePowerShell(lambda script: _points([recent]))
    remaining = _service(runner).throttle_remaining()
    assert remaining == timedelta(minutes=1380)


def test_build_spec_has_read_only_plan_steps(registry) -> None:
    runner = FakePowerShell(lambda script: _points([]))
    service = _service(runner)
    spec = service.build_spec("Before baseline")
    steps = spec.plan_steps()
    assert any("Checkpoint-Computer" in (step.detail or "") for step in steps)
    assert spec.requires_admin is True
    assert runner.checkpoints == []
    assert "Before baseline" in spec.preview_text


def test_clixml_error_is_readable() -> None:
    text = (
        '#< CLIXML\n<Objs Version="1.1.0.1"><S S="Error">Get-CimInstance : '
        "Access denied_x000D__x000A_</S></Objs>"
    )
    cleaned = rp._clean_powershell_error(text)
    assert "Access denied" in cleaned
    assert "<Objs" not in cleaned
    assert "_x000D_" not in cleaned


def test_description_is_sanitised(registry) -> None:
    cleaned = rp.sanitize_description("  Win\u00e9irst\tbatch  ")
    assert cleaned == "Win irst batch"
    with pytest.raises(ValueError):
        rp.sanitize_description("   ")
