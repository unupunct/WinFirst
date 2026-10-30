"""Shared test fixtures.

Every test runs against an isolated data directory and an in-memory database.
Windows is never asked to change anything.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from winfirst.core.audit_logger import AuditLogger
from winfirst.core.database import Database
from winfirst.core.operation import OperationController


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data = tmp_path / "data"
    monkeypatch.setenv("WINFIRST_DATA_DIR", str(data))
    monkeypatch.delenv("WINFIRST_PORTABLE_DATA", raising=False)
    return data


@pytest.fixture
def db() -> Database:
    database = Database(":memory:")
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
def audit(db: Database) -> AuditLogger:
    return AuditLogger(
        db,
        machine=lambda: {
            "hostname": "TEST-HOST",
            "windows_version": "Windows Test",
            "username": "tester",
        },
    )


@pytest.fixture
def controller(audit: AuditLogger) -> OperationController:
    return OperationController(audit, privilege_check=lambda: True)
