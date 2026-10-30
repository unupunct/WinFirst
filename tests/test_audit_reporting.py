from __future__ import annotations

import json
import zipfile
from pathlib import Path

from winfirst.core.audit_logger import AuditLogger
from winfirst.core.database import Database
from winfirst.core.operation_result import OperationResult, OperationStatus, utc_now
from winfirst.core.report_manager import (
    ReportDocument,
    ReportManager,
    ReportSection,
    ReportTable,
    build_history_report,
)


def _result(**overrides) -> OperationResult:
    result = OperationResult(
        operation_id="test.operation",
        name="Test operation",
        category="settings",
        status=OperationStatus.SUCCESS,
        started_at=utc_now(),
        finished_at=utc_now(),
        actual_state="Enabled",
        risk="low",
    )
    for key, value in overrides.items():
        setattr(result, key, value)
    return result


def test_record_and_read_operation(audit: AuditLogger) -> None:
    result = _result(
        details={"setting": "show_file_extensions"},
        verification="Read back: Enabled",
        stdout="line one\npassword=topsecret",
        warnings=["a warning"],
    )
    row_id = audit.record(result, profile_name="Test profile")
    assert row_id > 0
    assert audit.count() == 1

    row = audit.get_operation(row_id)
    assert row is not None
    assert row["operation_id"] == "test.operation"
    assert row["profile_name"] == "Test profile"
    assert row["details"]["setting"] == "show_file_extensions"
    assert row["details"]["verification"] == "Read back: Enabled"
    assert "topsecret" not in row["details"]["stdout_tail"]
    assert json.loads(row["details"]["warnings"]) == ["a warning"]


def test_list_operations_filters(audit: AuditLogger) -> None:
    audit.record(_result(operation_id="one", name="One", category="power"))
    audit.record(_result(operation_id="two", name="Two", category="network", status=OperationStatus.FAILED))
    assert len(audit.list_operations()) == 2
    assert len(audit.list_operations(category="power")) == 1
    assert len(audit.list_operations(status="failed")) == 1
    assert len(audit.list_operations(search="Two")) == 1
    assert audit.categories() == ["network", "power"]


def test_rollback_entry_lifecycle(audit: AuditLogger) -> None:
    result = _result(rollback_available=True)
    rollback = {
        "strategy": "setting",
        "key": "show_file_extensions",
        "previous": False,
        "summary": "Restore file extensions",
    }
    audit.record(result, rollback=rollback)
    entries = audit.rollback_entries(available_only=True)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["summary"] == "Restore file extensions"
    assert entry["status"] == "available"

    audit.mark_restored(int(entry["id"]), success=True, note="test")
    assert audit.rollback_entries(available_only=True) == []
    all_entries = audit.rollback_entries(available_only=False)
    assert all_entries[0]["status"] == "restored"
    assert all_entries[0]["restored_at"]


def test_rollback_payload_secrets_are_redacted(audit: AuditLogger) -> None:
    result = _result(rollback_available=True)
    audit.record(
        result,
        rollback={
            "strategy": "env_set",
            "previous_value": "token=super-secret-value",
            "summary": "Restore token=super-secret-value",
        },
    )
    entry = audit.rollback_entries(available_only=True)[0]
    assert "super-secret-value" not in entry["payload_json"]
    assert "super-secret-value" not in entry["summary"]
    assert "***REDACTED***" in entry["payload_json"]


def test_export_csv_and_json(audit: AuditLogger, tmp_path: Path) -> None:
    audit.record(_result())
    csv_path = audit.export(tmp_path / "history.csv")
    assert csv_path.is_file()
    assert "operation_id" in csv_path.read_text(encoding="utf-8-sig")
    json_path = audit.export(tmp_path / "history.json", fmt="json")
    rows = json.loads(json_path.read_text(encoding="utf-8"))
    assert rows[0]["operation_id"] == "test.operation"


def test_clear_and_stats(audit: AuditLogger) -> None:
    audit.record(_result())
    assert audit.stats()["total"] == 1
    assert audit.stats()["by_status"]["success"] == 1
    assert audit.clear() == 1
    assert audit.count() == 0


def test_history_report_escapes_html() -> None:
    document = build_history_report(
        [
            {
                "started_at": utc_now(),
                "category": "settings",
                "name": "<script>alert(1)</script>",
                "status": "success",
                "profile_name": "Test",
            }
        ]
    )
    assert "history" in document.kind
    assert document.sections


def test_html_report_is_self_contained_and_escaped(db: Database, tmp_path: Path) -> None:
    manager = ReportManager(db)
    document = ReportDocument(title="Test <report>", kind="test")
    document.sections.append(
        ReportSection(
            title="Summary",
            key_values=[("Host", "TEST-HOST")],
            notes=["<script>bad()</script>"],
        )
    )
    document.sections.append(
        ReportSection(
            title="Table",
            tables=[ReportTable(columns=["A", "B"], rows=[["one", "two"]])],
        )
    )
    path = manager.save(document, "html")
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert "<script>bad()</script>" not in text
    assert "&lt;script&gt;bad()&lt;/script&gt;" in text
    assert "TEST-HOST" in text
    records = manager.list_reports()
    assert records[0]["path"] == str(path)
    assert records[0]["exists"] is True


def test_pdf_report_is_real_pdf(db: Database) -> None:
    manager = ReportManager(db)
    document = ReportDocument(title="PDF test", kind="test")
    document.sections.append(ReportSection(title="Summary", key_values=[("A", "B")]))
    path = manager.save(document, "pdf")
    assert path.is_file()
    assert path.read_bytes().startswith(b"%PDF")


def test_diagnostic_bundle_redacts_secrets_and_identifiers(db: Database) -> None:
    manager = ReportManager(db)
    path = manager.create_diagnostic_bundle(
        data={
            "windows": {
                "hostname": "TEST-HOST",
                "username": "tester",
                "ip": "192.168.1.50",
            },
            "settings": {"api_token": "do-not-share", "theme": "dark"},
            "operations": [
                {
                    "started_at": utc_now(),
                    "category": "settings",
                    "name": "Test",
                    "status": "success",
                    "message": "password=abc123",
                }
            ],
        },
        log_text="token=abc123 host TEST-HOST",
        include_identifiers=False,
        hostname="TEST-HOST",
        username="tester",
    )
    assert path.suffix == ".zip"
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        assert {"diagnostics.json", "operations.csv", "winfirst.log", "README.txt", "manifest.json"} <= names
        diagnostics = archive.read("diagnostics.json").decode("utf-8")
        log = archive.read("winfirst.log").decode("utf-8")
    assert "do-not-share" not in diagnostics
    assert "TEST-HOST" not in diagnostics
    assert "TEST-HOST" not in log
    assert "abc123" not in log
    assert "dark" in diagnostics
