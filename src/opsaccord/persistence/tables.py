"""SQLAlchemy table definitions.

PostgreSQL is authoritative for incidents, evidence, actions, approvals, and
audit history. LangGraph checkpoints are execution continuity only.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from opsaccord.domain.enums import (
    ActionOutcome,
    ActionStatus,
    ActorType,
    ApprovalDecision,
    ApprovalStatus,
    ConstraintKind,
    DecisionType,
    EvidenceKind,
    IncidentStatus,
    SideEffect,
    VerificationOutcome,
    WorkflowOutcome,
)

JSONB = postgresql.JSONB
TZ = sa.DateTime(timezone=True)


class Base(DeclarativeBase):
    pass


def _enum(enum_cls: type, name: str) -> sa.Enum:
    return sa.Enum(enum_cls, name=name, native_enum=False, length=48, validate_strings=True)


class IncidentRow(Base):
    __tablename__ = "incidents"

    id: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    title: Mapped[str] = mapped_column(sa.String(200), nullable=False)
    service: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    severity: Mapped[str] = mapped_column(sa.String(8), nullable=False)
    status: Mapped[IncidentStatus] = mapped_column(
        _enum(IncidentStatus, "incident_status"), nullable=False
    )
    summary: Mapped[str | None] = mapped_column(sa.Text)
    scenario_id: Mapped[str | None] = mapped_column(sa.String(64))
    created_at: Mapped[datetime] = mapped_column(TZ, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(TZ, nullable=False)


class WorkflowRow(Base):
    __tablename__ = "workflows"

    id: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    incident_id: Mapped[str] = mapped_column(
        sa.ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    status: Mapped[IncidentStatus] = mapped_column(
        _enum(IncidentStatus, "workflow_status"), nullable=False
    )
    outcome: Mapped[WorkflowOutcome] = mapped_column(
        _enum(WorkflowOutcome, "workflow_outcome"), nullable=False
    )
    thread_id: Mapped[str] = mapped_column(sa.String(96), nullable=False)
    steps_used: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    model_calls: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    tool_calls: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    extensions_granted: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    soft_step_budget: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    hard_step_cap: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    max_extensions: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    takeover_actor: Mapped[str | None] = mapped_column(sa.String(96))
    takeover_at: Mapped[datetime | None] = mapped_column(TZ)
    terminal_reason: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = mapped_column(TZ, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(TZ, nullable=False)


class WorkflowEventRow(Base):
    __tablename__ = "workflow_events"
    __table_args__ = (sa.UniqueConstraint("workflow_id", "seq", name="uq_workflow_events_seq"),)

    id: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    incident_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    workflow_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    seq: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    from_status: Mapped[str | None] = mapped_column(sa.String(48))
    to_status: Mapped[str | None] = mapped_column(sa.String(48))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(TZ, nullable=False)


class EvidenceRow(Base):
    __tablename__ = "evidence"

    id: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    incident_id: Mapped[str] = mapped_column(sa.String(64), nullable=False, index=True)
    workflow_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    kind: Mapped[EvidenceKind] = mapped_column(_enum(EvidenceKind, "evidence_kind"), nullable=False)
    source: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    service: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    summary: Mapped[str] = mapped_column(sa.Text, nullable=False)
    value: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    observation_window_start: Mapped[datetime | None] = mapped_column(TZ)
    observation_window_end: Mapped[datetime | None] = mapped_column(TZ)
    collected_at: Mapped[datetime] = mapped_column(TZ, nullable=False, index=True)
    tool_call_id: Mapped[str | None] = mapped_column(sa.String(64))
    is_simulated: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)


class ModelDecisionRow(Base):
    __tablename__ = "model_decisions"

    id: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    incident_id: Mapped[str] = mapped_column(sa.String(64), nullable=False, index=True)
    workflow_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    decision_type: Mapped[DecisionType] = mapped_column(
        _enum(DecisionType, "decision_type"), nullable=False
    )
    provider: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    model_id: Mapped[str] = mapped_column(sa.String(96), nullable=False)
    rationale: Mapped[str] = mapped_column(sa.Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    evidence_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    input_tokens: Mapped[int | None] = mapped_column(sa.Integer)
    output_tokens: Mapped[int | None] = mapped_column(sa.Integer)
    cost_usd: Mapped[float | None] = mapped_column(sa.Float)
    latency_ms: Mapped[int | None] = mapped_column(sa.Integer)
    created_at: Mapped[datetime] = mapped_column(TZ, nullable=False)


class ToolCallRow(Base):
    __tablename__ = "tool_calls"

    id: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    incident_id: Mapped[str] = mapped_column(sa.String(64), nullable=False, index=True)
    workflow_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    tool_name: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    target: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    arguments: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    arguments_digest: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    side_effect: Mapped[SideEffect] = mapped_column(
        _enum(SideEffect, "side_effect"), nullable=False
    )
    attempt: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)
    succeeded: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)
    result: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    error: Mapped[str | None] = mapped_column(sa.Text)
    started_at: Mapped[datetime] = mapped_column(TZ, nullable=False)
    finished_at: Mapped[datetime] = mapped_column(TZ, nullable=False)
    is_simulated: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)


class ActionRow(Base):
    __tablename__ = "actions"
    __table_args__ = (
        sa.Index("ix_actions_workflow_status", "workflow_id", "status"),
        sa.Index("ix_actions_intent", "workflow_id", "intent_digest"),
    )

    id: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    incident_id: Mapped[str] = mapped_column(sa.String(64), nullable=False, index=True)
    workflow_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    tool_name: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    target: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    arguments: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    arguments_digest: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    intent_digest: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    rationale: Mapped[str] = mapped_column(sa.Text, nullable=False)
    status: Mapped[ActionStatus] = mapped_column(
        _enum(ActionStatus, "action_status"), nullable=False
    )
    decision_id: Mapped[str | None] = mapped_column(sa.String(64))
    approval_id: Mapped[str | None] = mapped_column(sa.String(64))
    attempt: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    idempotency_key: Mapped[str] = mapped_column(sa.String(96), nullable=False, unique=True)
    dispatch_started_at: Mapped[datetime | None] = mapped_column(TZ)
    completed_at: Mapped[datetime | None] = mapped_column(TZ)
    result: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    error: Mapped[str | None] = mapped_column(sa.Text)
    external_outcome: Mapped[ActionOutcome | None] = mapped_column(
        _enum(ActionOutcome, "action_external_outcome")
    )
    is_simulated: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(TZ, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(TZ, nullable=False)


class ApprovalRow(Base):
    __tablename__ = "approvals"
    __table_args__ = (
        # A given action can hold at most one live decision. Reuse requires a new
        # action identity and therefore a new approval.
        sa.Index(
            "uq_approvals_live_action",
            "action_id",
            unique=True,
            postgresql_where=sa.text("status IN ('PENDING', 'APPROVED')"),
        ),
    )

    id: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    incident_id: Mapped[str] = mapped_column(sa.String(64), nullable=False, index=True)
    workflow_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    action_id: Mapped[str] = mapped_column(sa.String(64), nullable=False, index=True)
    intent_digest: Mapped[str] = mapped_column(sa.String(64), nullable=False, index=True)
    target: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    arguments_digest: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    requested_by: Mapped[str] = mapped_column(sa.String(96), nullable=False)
    approver: Mapped[str | None] = mapped_column(sa.String(96))
    decision: Mapped[ApprovalDecision | None] = mapped_column(
        _enum(ApprovalDecision, "approval_decision")
    )
    status: Mapped[ApprovalStatus] = mapped_column(
        _enum(ApprovalStatus, "approval_status"), nullable=False
    )
    reason: Mapped[str | None] = mapped_column(sa.Text)
    expires_at: Mapped[datetime] = mapped_column(TZ, nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(TZ)
    consumed_at: Mapped[datetime | None] = mapped_column(TZ)
    created_at: Mapped[datetime] = mapped_column(TZ, nullable=False)


class ConstraintRow(Base):
    __tablename__ = "constraints"

    id: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    incident_id: Mapped[str | None] = mapped_column(sa.String(64), index=True)
    kind: Mapped[ConstraintKind] = mapped_column(
        _enum(ConstraintKind, "constraint_kind"), nullable=False
    )
    tool_name: Mapped[str | None] = mapped_column(sa.String(64))
    target: Mapped[str | None] = mapped_column(sa.String(64))
    note: Mapped[str | None] = mapped_column(sa.Text)
    created_by: Mapped[str] = mapped_column(sa.String(96), nullable=False)
    active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(TZ, nullable=False)


class ServiceRow(Base):
    __tablename__ = "services"

    name: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    display_name: Mapped[str] = mapped_column(sa.String(96), nullable=False)
    tier: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    owner: Mapped[str] = mapped_column(sa.String(96), nullable=False)
    auto_remediation: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    description: Mapped[str] = mapped_column(sa.Text, nullable=False)


class VerificationRow(Base):
    __tablename__ = "verifications"

    id: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    incident_id: Mapped[str] = mapped_column(sa.String(64), nullable=False, index=True)
    workflow_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    action_id: Mapped[str | None] = mapped_column(sa.String(64))
    service: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    outcome: Mapped[VerificationOutcome] = mapped_column(
        _enum(VerificationOutcome, "verification_outcome"), nullable=False
    )
    detail: Mapped[str] = mapped_column(sa.Text, nullable=False)
    evidence_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    checked_at: Mapped[datetime] = mapped_column(TZ, nullable=False)


class AuditEventRow(Base):
    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    incident_id: Mapped[str | None] = mapped_column(sa.String(64), index=True)
    workflow_id: Mapped[str | None] = mapped_column(sa.String(64))
    actor: Mapped[str] = mapped_column(sa.String(96), nullable=False)
    actor_type: Mapped[ActorType] = mapped_column(_enum(ActorType, "actor_type"), nullable=False)
    event_type: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    subject_type: Mapped[str | None] = mapped_column(sa.String(48))
    subject_id: Mapped[str | None] = mapped_column(sa.String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(TZ, nullable=False, index=True)


# Append-only enforcement for ``audit_events`` and ``workflow_events`` lives in
# the initial migration, so that a direct SQL session cannot rewrite history.
APPEND_ONLY_TABLES = ("audit_events", "workflow_events")
