"""Structured operation results.

Every system-changing action produces an :class:`OperationResult`.  Success is
never inferred from an exit code alone: services provide an independent
verification step and the controller records the verified state.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class OperationStatus(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    PARTIAL = "partial"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"
    DENIED = "denied"
    UNSUPPORTED = "unsupported"
    PENDING = "pending"

    @property
    def label(self) -> str:
        return {
            "success": "Success",
            "failed": "Failed",
            "partial": "Partial success",
            "skipped": "Skipped",
            "cancelled": "Cancelled",
            "denied": "Denied",
            "unsupported": "Unsupported",
            "pending": "Pending",
        }[self.value]

    @property
    def is_success(self) -> bool:
        return self is OperationStatus.SUCCESS

    @property
    def is_failure(self) -> bool:
        return self in {
            OperationStatus.FAILED,
            OperationStatus.PARTIAL,
            OperationStatus.DENIED,
            OperationStatus.CANCELLED,
            OperationStatus.UNSUPPORTED,
        }


@dataclass
class OperationResult:
    operation_id: str
    name: str
    category: str = "general"
    status: OperationStatus = OperationStatus.PENDING
    started_at: str = field(default_factory=utc_now)
    finished_at: str = ""
    previous_state: str = ""
    requested_state: str = ""
    actual_state: str = ""
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    requires_admin: bool = False
    elevated: bool = False
    restart_required: bool = False
    rollback_available: bool = False
    warnings: list[str] = field(default_factory=list)
    error: str = ""
    risk: str = "informational"
    details: dict[str, Any] = field(default_factory=dict)
    verification: str = ""
    dry_run: bool = False
    duration_ms: int = 0

    @property
    def ok(self) -> bool:
        return self.status is OperationStatus.SUCCESS

    def summary(self) -> str:
        parts = [f"{self.name}: {self.status.label}"]
        if self.actual_state:
            parts.append(f"state: {self.actual_state}")
        if self.error:
            parts.append(self.error)
        return " | ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        data = {
            "operation_id": self.operation_id,
            "name": self.name,
            "category": self.category,
            "status": self.status.value,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "previous_state": self.previous_state,
            "requested_state": self.requested_state,
            "actual_state": self.actual_state,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "exit_code": self.exit_code,
            "requires_admin": self.requires_admin,
            "elevated": self.elevated,
            "restart_required": self.restart_required,
            "rollback_available": self.rollback_available,
            "warnings": list(self.warnings),
            "error": self.error,
            "risk": self.risk,
            "details": self.details,
            "verification": self.verification,
            "dry_run": self.dry_run,
            "duration_ms": self.duration_ms,
        }
        return data

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, ensure_ascii=False)


def skipped_result(operation_id: str, name: str, reason: str, **kwargs: Any) -> OperationResult:
    return OperationResult(
        operation_id=operation_id,
        name=name,
        status=OperationStatus.SKIPPED,
        error=reason,
        finished_at=utc_now(),
        **kwargs,
    )


def cancelled_result(operation_id: str, name: str, reason: str, **kwargs: Any) -> OperationResult:
    return OperationResult(
        operation_id=operation_id,
        name=name,
        status=OperationStatus.CANCELLED,
        error=reason,
        finished_at=utc_now(),
        **kwargs,
    )


def denied_result(operation_id: str, name: str, reason: str, **kwargs: Any) -> OperationResult:
    return OperationResult(
        operation_id=operation_id,
        name=name,
        status=OperationStatus.DENIED,
        error=reason,
        finished_at=utc_now(),
        **kwargs,
    )


def unsupported_result(operation_id: str, name: str, reason: str, **kwargs: Any) -> OperationResult:
    return OperationResult(
        operation_id=operation_id,
        name=name,
        status=OperationStatus.UNSUPPORTED,
        error=reason,
        finished_at=utc_now(),
        **kwargs,
    )
