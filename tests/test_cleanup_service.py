from __future__ import annotations

from pathlib import Path

import pytest

from winfirst.services import cleanup_service


def _target(tmp_path: Path, key: str = "test_cache", *, available: bool = True):
    folder = tmp_path / key
    folder.mkdir()
    (folder / "old.tmp").write_text("x", encoding="utf-8")
    return cleanup_service.CleanupTarget(
        key=key,
        label="Test cache",
        description="A test target",
        kind="cache",
        paths=[str(folder)],
        estimated_bytes=1,
        estimated_files=1,
        available=available,
        reason="" if available else "Not present",
    )


def test_cleanup_target_definitions_are_curated_and_unique() -> None:
    targets = cleanup_service._target_definitions()
    keys = [target.key for target in targets]
    assert len(keys) == len(set(keys))
    assert "recycle_bin" in keys
    assert all(target.paths or target.kind == "recycle_bin" for target in targets)
    assert all(target.risk.rank >= 1 for target in targets)


def test_build_cleanup_spec_rejects_unavailable_target(tmp_path: Path) -> None:
    target = _target(tmp_path, available=False)
    with pytest.raises(ValueError, match="not available"):
        cleanup_service.build_cleanup_spec(target)


def test_build_cleanup_spec_rejects_missing_paths(tmp_path: Path) -> None:
    target = _target(tmp_path)
    target.paths = [str(tmp_path / "does-not-exist")]
    with pytest.raises(ValueError, match="does not exist"):
        cleanup_service.build_cleanup_spec(target)


def test_build_cleanup_specs_skips_unavailable_and_rejects_unknown(tmp_path: Path) -> None:
    available = _target(tmp_path, "one")
    unavailable = _target(tmp_path, "two", available=False)
    analysis = cleanup_service.CleanupAnalysis(targets=[available, unavailable])
    specs = cleanup_service.build_cleanup_specs(
        analysis, ["one", "two"], min_age_hours=12
    )
    assert len(specs) == 1
    assert specs[0].operation_id == "cleanup.one"
    assert specs[0].risk.rank >= 2
    assert specs[0].before is not None
    with pytest.raises(ValueError, match="Unknown cleanup target"):
        cleanup_service.build_cleanup_specs(analysis, ["unknown"])


def test_cleanup_analysis_totals_only_available_targets(tmp_path: Path) -> None:
    one = _target(tmp_path, "one")
    one.estimated_bytes = 100
    one.estimated_files = 3
    two = _target(tmp_path, "two", available=False)
    two.estimated_bytes = 999
    two.estimated_files = 9
    analysis = cleanup_service.CleanupAnalysis(targets=[one, two])
    assert analysis.known_bytes == 100
    assert analysis.known_files == 3
    assert analysis.find("one") is one
    assert analysis.find("missing") is None


def test_update_cache_restart_failure_is_reported_as_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "update-cache"
    folder.mkdir()
    (folder / "payload.bin").write_bytes(b"x" * 8)
    target = cleanup_service.CleanupTarget(
        key="update_cache",
        label="Windows Update cache",
        description="Test cache",
        kind="update_cache",
        paths=[str(folder)],
        estimated_bytes=8,
        estimated_files=1,
    )

    states = iter([True, False])  # running before cleanup, stopped afterwards
    monkeypatch.setattr(cleanup_service, "_service_is_running", lambda *_: next(states, False))
    monkeypatch.setattr(cleanup_service, "_stop_service", lambda *_: None)

    def fail_start(*_args, **_kwargs):
        raise RuntimeError("service refused to start")

    monkeypatch.setattr(cleanup_service, "_start_service", fail_start)

    spec = cleanup_service.build_cleanup_spec(target)
    outcome = spec.apply(lambda _message: None, None)
    assert outcome.success is True
    assert outcome.details["service_restart_failed"] is True
    assert any("could not be restarted" in warning for warning in outcome.warnings)

    assert spec.verify is not None
    verified = spec.verify()
    assert verified.ok is False
    assert "stopped" in verified.actual_state.lower()

