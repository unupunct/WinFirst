"""Change plans: what an operation will do, without doing it.

A plan is built from the same :class:`~winfirst.core.operation.OperationSpec`
that the controller executes, so the review and the execution cannot drift
apart.  Services may declare the exact commands they will run through
``OperationSpec.plan_steps``; operations that only change a value describe the
change and the read-back instead of inventing command lines.

Plans are safe to generate on a live system: building one only runs read-only
detection (``OperationSpec.before`` and the support check) and never applies
anything.  Plans are redacted before they are exported.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Sequence

from winfirst.core.operation_result import utc_now
from winfirst.core.redaction import redact_secrets
from winfirst.core.risk_engine import RiskLevel

STEP_KINDS = {"command", "action", "verify", "note"}


def _clean(text: str) -> str:
    return redact_secrets(str(text or ""))


@dataclass(frozen=True)
class PlanStep:
    """One reviewable step of a planned operation."""

    title: str
    kind: str = "action"
    argv: tuple[str, ...] = ()
    detail: str = ""
    destructive: bool = False

    def __post_init__(self) -> None:
        if self.kind not in STEP_KINDS:
            raise ValueError(f"Unsupported plan step kind: {self.kind}")

    @property
    def command(self) -> str:
        return " ".join(self.argv)

    def to_dict(self) -> dict[str, Any]:
        raw = self.command
        safe = _clean(raw)
        # Redaction works on the whole command line; when a secret spans
        # arguments, export the masked token stream instead of the raw argv.
        argv = self.argv if safe == raw else tuple(safe.split(" "))
        return {
            "title": _clean(self.title),
            "kind": self.kind,
            "command": safe,
            "argv": [_clean(part) for part in argv],
            "detail": _clean(self.detail),
            "destructive": bool(self.destructive),
        }

    def to_markdown(self) -> str:
        bits: list[str] = []
        label = _clean(self.title) or self.kind.capitalize()
        if self.kind == "command" and self.argv:
            bits.append(f"- **{label}**: `{_clean(self.command)}`")
        else:
            bits.append(f"- **{label}**")
        if self.detail:
            bits.append(f"  - {_clean(self.detail)}")
        if self.destructive:
            bits.append("  - Permanently removes or overwrites data")
        return "\n".join(bits)


def command_step(
    argv: Sequence[str],
    *,
    title: str = "",
    detail: str = "",
    destructive: bool = False,
) -> PlanStep:
    """Build a command step from an argument list (never a shell string)."""
    parts = tuple(str(part) for part in argv)
    if not parts:
        raise ValueError("A command step needs at least one argument.")
    return PlanStep(
        title=title or " ".join(parts[:2]),
        kind="command",
        argv=parts,
        detail=detail,
        destructive=destructive,
    )


def action_step(title: str, *, detail: str = "", destructive: bool = False) -> PlanStep:
    return PlanStep(title=title, kind="action", detail=detail, destructive=destructive)


def note_step(text: str) -> PlanStep:
    return PlanStep(title=text, kind="note")


def coerce_steps(values: Sequence[PlanStep | str]) -> list[PlanStep]:
    steps: list[PlanStep] = []
    for value in values:
        if isinstance(value, PlanStep):
            steps.append(value)
        elif isinstance(value, str) and value.strip():
            steps.append(note_step(value))
        else:
            raise TypeError("Plan steps must be PlanStep objects or strings.")
    return steps


@dataclass
class OperationPlan:
    """Everything a reviewer needs to decide about one operation."""

    operation_id: str
    name: str
    category: str = "general"
    risk: RiskLevel = RiskLevel.LOW
    requires_admin: bool = False
    reversible: bool = False
    restart_required: bool = False
    supported: bool = True
    support_reason: str = ""
    destructive: bool = False
    preview: str = ""
    previous_state: str = ""
    verification: str = ""
    steps: list[PlanStep] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "name": _clean(self.name),
            "category": self.category,
            "risk": self.risk.value,
            "requires_admin": self.requires_admin,
            "reversible": self.reversible,
            "restart_required": self.restart_required,
            "supported": self.supported,
            "support_reason": _clean(self.support_reason),
            "destructive": self.destructive,
            "preview": _clean(self.preview),
            "previous_state": _clean(self.previous_state),
            "verification": _clean(self.verification),
            "steps": [step.to_dict() for step in self.steps],
            "warnings": [_clean(str(item)) for item in self.warnings],
            "notes": [_clean(str(item)) for item in self.notes],
        }

    def to_markdown(self, index: int | None = None) -> str:
        heading = f"{index}. " if index is not None else ""
        flags = [f"{self.risk.label} risk"]
        if self.requires_admin:
            flags.append("administrator")
        if self.reversible:
            flags.append("reversible")
        else:
            flags.append("not reversible")
        if self.restart_required:
            flags.append("restart may be required")
        lines = [
            f"### {heading}{_clean(self.name)}",
            "",
            f"`{_clean(self.operation_id)}` · _{_clean(self.category)} · " + " · ".join(flags) + "_",
        ]
        if self.preview:
            lines.append(f"\n**Planned change:** {_clean(self.preview)}")
        if self.previous_state:
            lines.append(f"\n**Current state:** {_clean(self.previous_state)}")
        if not self.supported:
            lines.append(f"\n**Not supported here:** {_clean(self.support_reason) or 'not available'}")
        if self.steps:
            lines.append("\n**Steps:**")
            lines.extend(step.to_markdown() for step in self.steps)
        else:
            lines.append(
                "\n**Steps:** this operation does not expose command-level steps; "
                "review the planned change and the verification method."
            )
        if self.verification:
            lines.append(f"\n**Verification:** {_clean(self.verification)}")
        if self.warnings:
            lines.append("\n**Warnings:**")
            for warning in self.warnings:
                lines.append(f"- {_clean(str(warning))}")
        if self.notes:
            lines.append("\n**Notes:**")
            for note in self.notes:
                lines.append(f"- {_clean(str(note))}")
        return "\n".join(lines)


@dataclass
class PlanDocument:
    """A reviewable, exportable set of operation plans."""

    title: str = "WinFirst change plan"
    host: str = ""
    profile_name: str = ""
    created_at: str = field(default_factory=utc_now)
    plans: list[OperationPlan] = field(default_factory=list)

    @property
    def requires_admin(self) -> bool:
        return any(plan.requires_admin for plan in self.plans)

    @property
    def destructive(self) -> bool:
        return any(plan.destructive for plan in self.plans)

    @property
    def highest_risk(self) -> RiskLevel:
        if not self.plans:
            return RiskLevel.INFORMATIONAL
        return max((plan.risk for plan in self.plans), key=lambda item: item.rank)

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": _clean(self.title),
            "host": _clean(self.host),
            "profile": _clean(self.profile_name),
            "created_at": self.created_at,
            "operation_count": len(self.plans),
            "requires_admin": self.requires_admin,
            "destructive": self.destructive,
            "highest_risk": self.highest_risk.value,
            "plans": [plan.to_dict() for plan in self.plans],
        }

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    def to_markdown(self) -> str:
        lines = [f"# {_clean(self.title)}", ""]
        meta = [f"Created: {self.created_at}"]
        if self.host:
            meta.append(f"Computer: {_clean(self.host)}")
        if self.profile_name:
            meta.append(f"Profile: {_clean(self.profile_name)}")
        meta.append(f"Operations: {len(self.plans)}")
        meta.append(f"Highest risk: {self.highest_risk.label}")
        if self.requires_admin:
            meta.append("Administrator rights required")
        if self.destructive:
            meta.append("Includes changes that cannot be undone")
        lines.append("  \n".join(meta))
        lines.append("")
        if not self.plans:
            lines.append("No applicable operations were selected.")
            return "\n".join(lines)
        for index, plan in enumerate(self.plans, start=1):
            lines.append(plan.to_markdown(index))
            lines.append("")
        lines.append("---")
        lines.append(
            "Generated by WinFirst. No changes were applied while creating this plan. "
            "Commands are shown as argument lists exactly as they would be executed."
        )
        return "\n".join(lines)


def build_plan(
    spec: Any,
    *,
    previous_state: str = "",
    steps: Sequence[PlanStep | str] | None = None,
    supported: bool = True,
    support_reason: str = "",
    notes: Sequence[str] | None = None,
) -> OperationPlan:
    """Turn an ``OperationSpec`` into an :class:`OperationPlan`.

    ``steps`` overrides ``spec.plan_steps`` when supplied (used by the
    controller so that a failing plan hook can degrade to a preview instead of
    aborting the whole review).
    """
    if steps is None:
        steps = spec.plan_steps() if getattr(spec, "plan_steps", None) else []
    plan_steps = coerce_steps(list(steps))
    risk = RiskLevel.parse(getattr(spec, "risk", RiskLevel.LOW))
    destructive = any(step.destructive for step in plan_steps) or (
        risk.rank >= RiskLevel.HIGH.rank and not getattr(spec, "reversible", False)
    )
    plan = OperationPlan(
        operation_id=str(getattr(spec, "operation_id", "")),
        name=str(getattr(spec, "name", "")),
        category=str(getattr(spec, "category", "general")),
        risk=risk,
        requires_admin=bool(getattr(spec, "requires_admin", False)),
        reversible=bool(getattr(spec, "reversible", False)),
        restart_required=bool(getattr(spec, "restart_required", False)),
        supported=supported,
        support_reason=support_reason,
        destructive=destructive,
        preview=str(getattr(spec, "preview_text", "") or getattr(spec, "description", "")),
        previous_state=str(previous_state or ""),
        verification=str(getattr(spec, "verification_method", "")),
        steps=plan_steps,
        warnings=list(getattr(spec, "warnings", []) or []),
        notes=list(notes or []),
    )
    if not plan_steps:
        plan.notes.append(
            "No command-level steps are declared for this operation; the planned change "
            "above is the authoritative description."
        )
    return plan


def build_document(
    plans: Sequence[OperationPlan],
    *,
    title: str = "WinFirst change plan",
    host: str = "",
    profile_name: str = "",
) -> PlanDocument:
    return PlanDocument(
        title=title,
        host=host,
        profile_name=profile_name,
        plans=list(plans),
    )
