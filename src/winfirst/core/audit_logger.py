"""Persistent local operation history.

The audit history is an application record, not a tamper-proof compliance
system.  Secrets are redacted before storage; full command output is truncated
and stored only as a redacted detail entry.
"""
from __future__ import annotations

import csv
import json
import logging
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from winfirst.core.command_runner import as_list
from winfirst.core.database import Database
from winfirst.core.operation_result import OperationResult, OperationStatus, utc_now
from winfirst.core.redaction import redact_secrets

_log = logging.getLogger("winfirst.audit")
MAX_STORED_OUTPUT = 60_000

MachineProvider = Callable[[], dict[str, str]]


def _default_machine() -> dict[str, str]:
    return {
        "hostname": socket.gethostname(),
        "windows_version": "",
        "username": "",
    }


class AuditLogger:
    def __init__(self, db: Database, machine: MachineProvider | None = None) -> None:
        self.db = db
        self.machine = machine or _default_machine

    # ------------------------------------------------------------------ write
    def record(
        self,
        result: OperationResult,
        *,
        rollback: dict[str, Any] | None = None,
        profile_name: str = "",
        extra_details: dict[str, Any] | None = None,
    ) -> int:
        machine = self.machine() or {}
        details: dict[str, Any] = {}
        for key, value in (result.details or {}).items():
            details[str(key)] = value
        if extra_details:
            details.update({str(k): v for k, v in extra_details.items()})
        details["dry_run"] = result.dry_run
        if result.verification:
            details["verification"] = result.verification
        if result.warnings:
            details["warnings"] = list(result.warnings)
        if result.stdout:
            details["stdout_tail"] = redact_secrets(result.stdout)[-MAX_STORED_OUTPUT:]
        if result.stderr:
            details["stderr_tail"] = redact_secrets(result.stderr)[-MAX_STORED_OUTPUT:]

        message = redact_secrets(result.error or result.actual_state or "")[:4000]
        row_id = self.db.execute(
            """
            INSERT INTO operations(
                operation_id, name, category, risk, status, started_at, finished_at,
                hostname, windows_version, username, requires_admin, elevated,
                restart_required, rollback_available, profile_name, message,
                exit_code, duration_ms, requested_state, previous_state, actual_state
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                result.operation_id,
                result.name,
                result.category,
                result.risk,
                result.status.value,
                result.started_at,
                result.finished_at or utc_now(),
                machine.get("hostname", ""),
                machine.get("windows_version", ""),
                machine.get("username", ""),
                1 if result.requires_admin else 0,
                1 if result.elevated else 0,
                1 if result.restart_required else 0,
                1 if result.rollback_available else 0,
                profile_name,
                message,
                result.exit_code,
                int(result.duration_ms),
                redact_secrets(result.requested_state)[:4000],
                redact_secrets(result.previous_state)[:4000],
                redact_secrets(result.actual_state)[:4000],
            ),
        )
        rows: list[tuple[int, str, str]] = []
        for key, value in details.items():
            if isinstance(value, (dict, list)):
                text = json.dumps(value, ensure_ascii=False, default=str)
            else:
                text = str(value)
            rows.append((row_id, key, redact_secrets(text)[:MAX_STORED_OUTPUT]))
        if rows:
            self.db.executemany(
                "INSERT INTO operation_details(operation_row_id, key, value) VALUES(?,?,?)",
                rows,
            )
        if rollback and result.rollback_available:
            try:
                payload = json.dumps(rollback, ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                payload = ""
            if payload:
                payload = redact_secrets(payload)
                summary = redact_secrets(str(rollback.get("summary", "")))[:1000]
                self.db.execute(
                    """
                    INSERT INTO rollback_entries(
                        operation_row_id, strategy, payload_json, summary, status, created_at
                    ) VALUES(?,?,?,?,?,?)
                    """,
                    (
                        row_id,
                        str(rollback.get("strategy", "unknown")),
                        payload,
                        summary,
                        "available",
                        utc_now(),
                    ),
                )
        _log.info(
            "%s [%s] %s (%.0f ms)",
            result.name,
            result.status.value,
            (result.error or result.actual_state)[:300],
            result.duration_ms,
        )
        return row_id

    # ------------------------------------------------------------------- read
    def list_operations(
        self,
        *,
        search: str = "",
        category: str = "",
        status: str = "",
        since: str = "",
        until: str = "",
        limit: int = 500,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if search:
            clauses.append(
                "(name LIKE ? OR operation_id LIKE ? OR message LIKE ? OR profile_name LIKE ?)"
            )
            like = f"%{search}%"
            params.extend([like, like, like, like])
        if category and category != "All":
            clauses.append("category = ?")
            params.append(category)
        if status and status != "All":
            clauses.append("status = ?")
            params.append(status)
        if since:
            clauses.append("started_at >= ?")
            params.append(since)
        if until:
            clauses.append("started_at <= ?")
            params.append(until)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.extend([max(1, min(limit, 5000)), max(0, offset)])
        return self.db.query(
            f"SELECT * FROM operations {where} ORDER BY id DESC LIMIT ? OFFSET ?", params
        )

    def recent(self, limit: int = 8) -> list[dict[str, Any]]:
        return self.list_operations(limit=limit)

    def count(self) -> int:
        return int(self.db.scalar("SELECT COUNT(*) FROM operations", default=0) or 0)

    def categories(self) -> list[str]:
        rows = self.db.query("SELECT DISTINCT category FROM operations ORDER BY category")
        return [str(row["category"]) for row in rows if row["category"]]

    def get_operation(self, row_id: int) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM operations WHERE id = ?", (row_id,))
        if not row:
            return None
        details = self.db.query(
            "SELECT key, value FROM operation_details WHERE operation_row_id = ? ORDER BY id",
            (row_id,),
        )
        row["details"] = {str(item["key"]): item["value"] for item in details}
        row["rollback"] = self.db.query(
            "SELECT * FROM rollback_entries WHERE operation_row_id = ? ORDER BY id", (row_id,)
        )
        return row

    def rollback_entries(self, *, available_only: bool = True) -> list[dict[str, Any]]:
        where = "WHERE r.status = 'available'" if available_only else ""
        return self.db.query(
            f"""
            SELECT r.*, o.name AS operation_name, o.category
            FROM rollback_entries r
            JOIN operations o ON o.id = r.operation_row_id
            {where}
            ORDER BY r.id DESC
            """,
        )

    def get_rollback_entry(self, entry_id: int) -> dict[str, Any] | None:
        return self.db.query_one("SELECT * FROM rollback_entries WHERE id = ?", (entry_id,))

    def mark_restored(self, entry_id: int, *, success: bool, note: str = "") -> None:
        self.db.execute(
            "UPDATE rollback_entries SET status = ?, restored_at = ? WHERE id = ?",
            ("restored" if success else "failed", utc_now(), entry_id),
        )
        if note:
            _log.info("Rollback entry %s: %s", entry_id, note)

    # ---------------------------------------------------------------- cleanup
    def clear(self) -> int:
        count = self.count()
        self.db.execute("DELETE FROM operations")
        return count

    def apply_retention(self, days: int) -> int:
        if days <= 0:
            return 0
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        count = int(
            self.db.scalar(
                "SELECT COUNT(*) FROM operations WHERE started_at < ?", (cutoff,), default=0
            )
            or 0
        )
        if count:
            self.db.execute("DELETE FROM operations WHERE started_at < ?", (cutoff,))
        return count

    # ---------------------------------------------------------------- exports
    def export(self, path: str | Path, *, fmt: str = "csv") -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        rows = self.list_operations(limit=5000)
        if fmt == "json":
            target.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
            return target
        fields = [
            "id",
            "started_at",
            "finished_at",
            "category",
            "name",
            "operation_id",
            "status",
            "risk",
            "profile_name",
            "requires_admin",
            "restart_required",
            "rollback_available",
            "exit_code",
            "duration_ms",
            "requested_state",
            "actual_state",
            "message",
        ]
        with target.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({key: row.get(key, "") for key in fields})
        return target

    def stats(self) -> dict[str, Any]:
        rows = self.db.query(
            "SELECT status, COUNT(*) AS count FROM operations GROUP BY status"
        )
        by_status = {str(row["status"]): int(row["count"]) for row in rows}
        return {"total": self.count(), "by_status": by_status}
