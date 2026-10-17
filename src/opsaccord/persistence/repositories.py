"""Transactional data access.

Every mutation that has a safety meaning is expressed here so that it can be
reused by the API, the UI, and the tests, and so that concurrency behaviour is
defined in exactly one place.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

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
    WorkflowOutcome,
    transition_allowed,
)
from opsaccord.domain.models import (
    EXTENSION_STEP_ALLOWANCE,
    Action,
    Approval,
    Constraint,
    Evidence,
    Incident,
    IncidentSnapshot,
    ModelDecision,
    ServiceRecord,
    ToolCallRecord,
    VerificationResult,
    Workflow,
)
from opsaccord.ids import prefixed, utcnow
from opsaccord.persistence.locks import lock_control_gate
from opsaccord.persistence.tables import (
    ActionRow,
    ApprovalRow,
    AuditEventRow,
    ConstraintRow,
    EvidenceRow,
    IncidentRow,
    ModelDecisionRow,
    ServiceRow,
    ToolCallRow,
    VerificationRow,
    WorkflowEventRow,
    WorkflowRow,
)


class RepositoryError(RuntimeError):
    pass


class IllegalTransition(RepositoryError):
    pass


class BudgetExhausted(RepositoryError):
    pass


# --- mappers ---------------------------------------------------------------


def _incident(row: IncidentRow) -> Incident:
    return Incident(
        id=row.id,
        title=row.title,
        service=row.service,
        severity=row.severity,  # type: ignore[arg-type]
        status=IncidentStatus(row.status),
        summary=row.summary,
        scenario_id=row.scenario_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _workflow(row: WorkflowRow) -> Workflow:
    return Workflow(
        id=row.id,
        incident_id=row.incident_id,
        status=IncidentStatus(row.status),
        outcome=WorkflowOutcome(row.outcome),
        thread_id=row.thread_id,
        steps_used=row.steps_used,
        model_calls=row.model_calls,
        tool_calls=row.tool_calls,
        extensions_granted=row.extensions_granted,
        soft_step_budget=row.soft_step_budget,
        hard_step_cap=row.hard_step_cap,
        max_extensions=row.max_extensions,
        takeover_actor=row.takeover_actor,
        takeover_at=row.takeover_at,
        terminal_reason=row.terminal_reason,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _action(row: ActionRow) -> Action:
    return Action(
        id=row.id,
        incident_id=row.incident_id,
        workflow_id=row.workflow_id,
        tool_name=row.tool_name,
        target=row.target,
        arguments=dict(row.arguments or {}),
        arguments_digest=row.arguments_digest,
        intent_digest=row.intent_digest,
        rationale=row.rationale,
        status=ActionStatus(row.status),
        decision_id=row.decision_id,
        approval_id=row.approval_id,
        attempt=row.attempt,
        idempotency_key=row.idempotency_key,
        dispatch_started_at=row.dispatch_started_at,
        completed_at=row.completed_at,
        result=dict(row.result or {}),
        error=row.error,
        external_outcome=ActionOutcome(row.external_outcome) if row.external_outcome else None,
        is_simulated=row.is_simulated,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _approval(row: ApprovalRow) -> Approval:
    return Approval(
        id=row.id,
        incident_id=row.incident_id,
        workflow_id=row.workflow_id,
        action_id=row.action_id,
        intent_digest=row.intent_digest,
        target=row.target,
        arguments_digest=row.arguments_digest,
        requested_by=row.requested_by,
        approver=row.approver,
        decision=ApprovalDecision(row.decision) if row.decision else None,
        status=ApprovalStatus(row.status),
        reason=row.reason,
        expires_at=row.expires_at,
        decided_at=row.decided_at,
        consumed_at=row.consumed_at,
        created_at=row.created_at,
    )


def _evidence(row: EvidenceRow) -> Evidence:
    return Evidence(
        id=row.id,
        incident_id=row.incident_id,
        workflow_id=row.workflow_id,
        kind=EvidenceKind(row.kind),
        source=row.source,
        service=row.service,
        summary=row.summary,
        value=dict(row.value or {}),
        observation_window_start=row.observation_window_start,
        observation_window_end=row.observation_window_end,
        collected_at=row.collected_at,
        tool_call_id=row.tool_call_id,
        is_simulated=row.is_simulated,
    )


def _decision(row: ModelDecisionRow) -> ModelDecision:
    return ModelDecision(
        id=row.id,
        incident_id=row.incident_id,
        workflow_id=row.workflow_id,
        decision_type=DecisionType(row.decision_type),
        provider=row.provider,
        model_id=row.model_id,
        rationale=row.rationale,
        payload=dict(row.payload or {}),
        evidence_ids=tuple(row.evidence_ids or ()),
        input_tokens=row.input_tokens,
        output_tokens=row.output_tokens,
        cost_usd=row.cost_usd,
        latency_ms=row.latency_ms,
        created_at=row.created_at,
    )


def _constraint(row: ConstraintRow) -> Constraint:
    return Constraint(
        id=row.id,
        incident_id=row.incident_id,
        kind=ConstraintKind(row.kind),
        tool_name=row.tool_name,
        target=row.target,
        note=row.note,
        created_by=row.created_by,
        active=row.active,
        created_at=row.created_at,
    )


def _service(row: ServiceRow) -> ServiceRecord:
    return ServiceRecord(
        name=row.name,
        display_name=row.display_name,
        tier=row.tier,  # type: ignore[arg-type]
        owner=row.owner,
        auto_remediation=row.auto_remediation,  # type: ignore[arg-type]
        description=row.description,
    )


def _tool_call(row: ToolCallRow) -> ToolCallRecord:
    return ToolCallRecord(
        id=row.id,
        incident_id=row.incident_id,
        workflow_id=row.workflow_id,
        tool_name=row.tool_name,
        target=row.target,
        arguments=dict(row.arguments or {}),
        arguments_digest=row.arguments_digest,
        side_effect=SideEffect(row.side_effect),
        attempt=row.attempt,
        succeeded=row.succeeded,
        result=dict(row.result or {}),
        error=row.error,
        started_at=row.started_at,
        finished_at=row.finished_at,
        is_simulated=row.is_simulated,
    )


# --- incidents and workflows ----------------------------------------------


def create_incident(
    session: Session,
    *,
    title: str,
    service: str,
    severity: str,
    scenario_id: str | None = None,
    summary: str | None = None,
) -> Incident:
    now = utcnow()
    row = IncidentRow(
        id=prefixed("inc"),
        title=title,
        service=service,
        severity=severity,
        status=IncidentStatus.NEW,
        summary=summary,
        scenario_id=scenario_id,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    session.flush()
    audit(
        session,
        actor="system",
        actor_type=ActorType.SYSTEM,
        event_type="incident.created",
        incident_id=row.id,
        subject_type="incident",
        subject_id=row.id,
        payload={"service": service, "severity": severity, "scenario_id": scenario_id},
    )
    return _incident(row)


def get_incident(session: Session, incident_id: str) -> Incident | None:
    row = session.get(IncidentRow, incident_id)
    return _incident(row) if row else None


def find_incident_by_scenario(session: Session, scenario_id: str) -> Incident | None:
    row = session.scalars(
        sa.select(IncidentRow)
        .where(IncidentRow.scenario_id == scenario_id)
        .order_by(IncidentRow.created_at.desc())
        .limit(1)
    ).first()
    return _incident(row) if row else None


def list_incidents(session: Session, limit: int = 50) -> list[Incident]:
    rows = session.scalars(
        sa.select(IncidentRow).order_by(IncidentRow.created_at.desc()).limit(limit)
    ).all()
    return [_incident(r) for r in rows]


def ensure_workflow(
    session: Session,
    *,
    incident_id: str,
    soft_step_budget: int,
    hard_step_cap: int,
    max_extensions: int,
) -> Workflow:
    existing = session.scalars(
        sa.select(WorkflowRow).where(WorkflowRow.incident_id == incident_id)
    ).first()
    if existing:
        return _workflow(existing)
    now = utcnow()
    workflow_id = prefixed("wf")
    row = WorkflowRow(
        id=workflow_id,
        incident_id=incident_id,
        status=IncidentStatus.NEW,
        outcome=WorkflowOutcome.IN_PROGRESS,
        thread_id=f"incident:{incident_id}",
        steps_used=0,
        model_calls=0,
        tool_calls=0,
        extensions_granted=0,
        soft_step_budget=soft_step_budget,
        hard_step_cap=hard_step_cap,
        max_extensions=max_extensions,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    session.flush()
    audit(
        session,
        actor="system",
        actor_type=ActorType.SYSTEM,
        event_type="workflow.created",
        incident_id=incident_id,
        workflow_id=workflow_id,
        subject_type="workflow",
        subject_id=workflow_id,
    )
    return _workflow(row)


def get_workflow(session: Session, incident_id: str) -> Workflow | None:
    row = session.scalars(
        sa.select(WorkflowRow).where(WorkflowRow.incident_id == incident_id)
    ).first()
    return _workflow(row) if row else None


def lock_incident(session: Session, incident_id: str) -> IncidentRow:
    """Take the row lock that serialises human controls against dispatch.

    ``set_takeover`` and an incident-scoped ``add_constraint`` (and therefore a
    human's freeze, takeover, or deny rule) commit under this lock, and the
    gateway holds it while it revalidates policy and claims an action. Without a
    shared lock a control committed between the revalidation read and the claim
    would be invisible under READ COMMITTED, and the side effect would go out
    anyway.

    This is the *per-incident* half of the ordering guarantee. The global half is
    :func:`~opsaccord.persistence.locks.lock_control_gate`, which covers controls
    with no incident to lock, and **this helper takes it first**. The documented
    order - control gate -> incident row -> workflow row - is therefore a property
    of this function rather than a convention its callers have to remember: taking
    the row lock first and only then asking for the gate is precisely the cycle
    PostgreSQL resolves as a deadlock, and a test that composed the two by hand
    did exactly that. Re-acquiring the gate inside the transaction that already
    holds it is free, so the callers that take it themselves are unaffected.
    """
    lock_control_gate(session)
    row = session.get(IncidentRow, incident_id, with_for_update=True)
    if row is None:
        raise RepositoryError(f"unknown incident {incident_id}")
    return row


def _lock_workflow_row(session: Session, workflow_id: str) -> WorkflowRow:
    row = session.scalars(
        sa.select(WorkflowRow).where(WorkflowRow.id == workflow_id).with_for_update()
    ).one_or_none()
    if row is None:
        raise RepositoryError(f"unknown workflow {workflow_id}")
    return row


def _append_event(
    session: Session,
    *,
    workflow_id: str,
    incident_id: str,
    event_type: str,
    from_status: IncidentStatus | None,
    to_status: IncidentStatus | None,
    payload: dict[str, Any] | None = None,
) -> None:
    seq = session.scalar(
        sa.select(sa.func.coalesce(sa.func.max(WorkflowEventRow.seq), 0) + 1).where(
            WorkflowEventRow.workflow_id == workflow_id
        )
    )
    session.add(
        WorkflowEventRow(
            id=prefixed("evt"),
            incident_id=incident_id,
            workflow_id=workflow_id,
            seq=int(seq or 1),
            event_type=event_type,
            from_status=str(from_status) if from_status else None,
            to_status=str(to_status) if to_status else None,
            payload=payload or {},
            created_at=utcnow(),
        )
    )


def audit(
    session: Session,
    *,
    actor: str,
    actor_type: ActorType,
    event_type: str,
    incident_id: str | None = None,
    workflow_id: str | None = None,
    subject_type: str | None = None,
    subject_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    session.add(
        AuditEventRow(
            id=prefixed("aud"),
            incident_id=incident_id,
            workflow_id=workflow_id,
            actor=actor,
            actor_type=actor_type,
            event_type=event_type,
            subject_type=subject_type,
            subject_id=subject_id,
            payload=payload or {},
            created_at=utcnow(),
        )
    )


def transition(
    session: Session,
    *,
    incident_id: str,
    target: IncidentStatus,
    actor: str,
    actor_type: ActorType,
    event_type: str,
    payload: dict[str, Any] | None = None,
    outcome: WorkflowOutcome | None = None,
) -> Workflow:
    """Apply a validated state transition. Illegal edges raise, not silently pass."""
    workflow_row = _lock_workflow_row(
        session,
        session.scalar(sa.select(WorkflowRow.id).where(WorkflowRow.incident_id == incident_id))
        or "",
    )
    current = IncidentStatus(workflow_row.status)
    if current is target:
        if outcome is not None:
            workflow_row.outcome = outcome
        return _workflow(workflow_row)
    if not transition_allowed(current, target):
        raise IllegalTransition(f"{current} -> {target} is not an allowed transition")
    now = utcnow()
    workflow_row.status = target
    workflow_row.updated_at = now
    if outcome is not None:
        workflow_row.outcome = outcome
    incident_row = session.get(IncidentRow, incident_id)
    if incident_row is not None:
        incident_row.status = target
        incident_row.updated_at = now
    _append_event(
        session,
        workflow_id=workflow_row.id,
        incident_id=incident_id,
        event_type=event_type,
        from_status=current,
        to_status=target,
        payload=payload,
    )
    audit(
        session,
        actor=actor,
        actor_type=actor_type,
        event_type=event_type,
        incident_id=incident_id,
        workflow_id=workflow_row.id,
        subject_type="workflow",
        subject_id=workflow_row.id,
        payload={"from": str(current), "to": str(target), **(payload or {})},
    )
    session.flush()
    return _workflow(workflow_row)


def consume_step(
    session: Session,
    *,
    incident_id: str,
    kind: str,
    actor: str = "controller",
    allow_over_soft: bool = False,
) -> Workflow:
    """Consume one unit of the deterministic budget.

    The soft budget is a checkpoint; the hard cap is a wall. Neither can be
    waived by a model, and both are persisted on the workflow row.
    """
    workflow_row = _lock_workflow_row(
        session,
        session.scalar(sa.select(WorkflowRow.id).where(WorkflowRow.incident_id == incident_id))
        or "",
    )
    if workflow_row.steps_used >= workflow_row.hard_step_cap:
        raise BudgetExhausted(
            f"hard step cap reached ({workflow_row.hard_step_cap}); refusing further work"
        )
    effective_soft = workflow_row.soft_step_budget + (
        workflow_row.extensions_granted * EXTENSION_STEP_ALLOWANCE
    )
    if workflow_row.steps_used >= effective_soft and not allow_over_soft:
        raise BudgetExhausted(f"soft step budget reached ({effective_soft}); extension required")
    workflow_row.steps_used += 1
    workflow_row.updated_at = utcnow()
    if kind == "model":
        workflow_row.model_calls += 1
    elif kind == "tool":
        workflow_row.tool_calls += 1
    _append_event(
        session,
        workflow_id=workflow_row.id,
        incident_id=incident_id,
        event_type="budget.consumed",
        from_status=None,
        to_status=None,
        payload={
            "kind": kind,
            "steps_used": workflow_row.steps_used,
            "soft": workflow_row.soft_step_budget,
            "hard": workflow_row.hard_step_cap,
        },
    )
    audit(
        session,
        actor=actor,
        actor_type=ActorType.SYSTEM,
        event_type="budget.consumed",
        incident_id=incident_id,
        workflow_id=workflow_row.id,
        payload={"kind": kind, "steps_used": workflow_row.steps_used},
    )
    session.flush()
    return _workflow(workflow_row)


def grant_extension(
    session: Session, *, incident_id: str, actor: str, justification: str
) -> Workflow:
    """Grant one bounded, justified extension of the soft budget."""
    workflow_row = _lock_workflow_row(
        session,
        session.scalar(sa.select(WorkflowRow.id).where(WorkflowRow.incident_id == incident_id))
        or "",
    )
    if workflow_row.extensions_granted >= workflow_row.max_extensions:
        raise BudgetExhausted("no budget extensions remain")
    if workflow_row.steps_used < workflow_row.soft_step_budget:
        raise RepositoryError("extension requested before the soft budget was consumed")
    workflow_row.extensions_granted += 1
    workflow_row.updated_at = utcnow()
    audit(
        session,
        actor=actor,
        actor_type=ActorType.SYSTEM,
        event_type="budget.extension.granted",
        incident_id=incident_id,
        workflow_id=workflow_row.id,
        payload={"justification": justification, "granted": workflow_row.extensions_granted},
    )
    session.flush()
    return _workflow(workflow_row)


def set_takeover(session: Session, *, incident_id: str, actor: str) -> Workflow:
    # Every human control write takes the gate first, so that dispatch
    # authorization cannot read the control set while a control is mid-commit.
    # ``lock_incident`` takes the same transaction-scoped lock again for its own
    # callers; within one transaction that is not a conflict.
    lock_control_gate(session)
    lock_incident(session, incident_id)
    workflow_row = _lock_workflow_row(
        session,
        session.scalar(sa.select(WorkflowRow.id).where(WorkflowRow.incident_id == incident_id))
        or "",
    )
    now = utcnow()
    workflow_row.takeover_actor = actor
    workflow_row.takeover_at = now
    workflow_row.updated_at = now
    _append_event(
        session,
        workflow_id=workflow_row.id,
        incident_id=incident_id,
        event_type="human.takeover",
        from_status=IncidentStatus(workflow_row.status),
        to_status=IncidentStatus(workflow_row.status),
        payload={"actor": actor},
    )
    audit(
        session,
        actor=actor,
        actor_type=ActorType.HUMAN,
        event_type="human.takeover",
        incident_id=incident_id,
        workflow_id=workflow_row.id,
        payload={"note": "backend policy blocks automated actions after takeover"},
    )
    session.flush()
    return _workflow(workflow_row)


#: Evidence kinds that are telemetry: a measurement of the service, not a probe, a
#: runbook, a change record or a failed read.
TELEMETRY_EVIDENCE_KINDS = (EvidenceKind.METRIC, EvidenceKind.TRACE)


def count_target_telemetry(session: Session, *, incident_id: str, target: str) -> int:
    """Successful telemetry reads this incident collected about ``target``.

    A metric or trace read of the target itself counts. So does a dependency read that
    a caller made of the target (``value.destination``), because that is how a fault
    on the call path is attributed when the target's own probe looks healthy. Failed
    reads are recorded as ``COLLECTION_ERROR`` and never count, and neither do health
    probes, runbooks or change records.
    """
    rows = session.execute(
        sa.select(EvidenceRow.service, EvidenceRow.value).where(
            EvidenceRow.incident_id == incident_id,
            EvidenceRow.kind.in_(TELEMETRY_EVIDENCE_KINDS),
        )
    ).all()
    return sum(
        1
        for service, value in rows
        if service == target or (value or {}).get("destination") == target
    )


def set_terminal_reason(session: Session, *, incident_id: str, reason: str) -> None:
    workflow_row = session.scalars(
        sa.select(WorkflowRow).where(WorkflowRow.incident_id == incident_id)
    ).one()
    workflow_row.terminal_reason = reason
    workflow_row.updated_at = utcnow()
    session.flush()


# --- evidence, decisions, tool calls --------------------------------------


def add_evidence(
    session: Session,
    *,
    incident_id: str,
    workflow_id: str,
    kind: EvidenceKind,
    source: str,
    service: str,
    summary: str,
    value: dict[str, Any] | None = None,
    observation_window_start: datetime | None = None,
    observation_window_end: datetime | None = None,
    tool_call_id: str | None = None,
    is_simulated: bool = True,
    collected_at: datetime | None = None,
) -> Evidence:
    row = EvidenceRow(
        id=prefixed("ev"),
        incident_id=incident_id,
        workflow_id=workflow_id,
        kind=kind,
        source=source,
        service=service,
        summary=summary,
        value=value or {},
        observation_window_start=observation_window_start,
        observation_window_end=observation_window_end,
        collected_at=collected_at or utcnow(),
        tool_call_id=tool_call_id,
        is_simulated=is_simulated,
    )
    session.add(row)
    session.flush()
    return _evidence(row)


def add_decision(
    session: Session,
    *,
    incident_id: str,
    workflow_id: str,
    decision_type: DecisionType,
    provider: str,
    model_id: str,
    rationale: str,
    payload: dict[str, Any] | None = None,
    evidence_ids: tuple[str, ...] = (),
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cost_usd: float | None = None,
    latency_ms: int | None = None,
) -> ModelDecision:
    row = ModelDecisionRow(
        id=prefixed("dec"),
        incident_id=incident_id,
        workflow_id=workflow_id,
        decision_type=decision_type,
        provider=provider,
        model_id=model_id,
        rationale=rationale,
        payload=payload or {},
        evidence_ids=list(evidence_ids),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
        latency_ms=latency_ms,
        created_at=utcnow(),
    )
    session.add(row)
    session.flush()
    audit(
        session,
        actor=f"model:{provider}/{model_id}",
        actor_type=ActorType.AGENT,
        event_type="model.decision",
        incident_id=incident_id,
        workflow_id=workflow_id,
        subject_type="model_decision",
        subject_id=row.id,
        payload={
            "decision_type": str(decision_type),
            "rationale": rationale,
            "evidence_ids": list(evidence_ids),
        },
    )
    return _decision(row)


def record_tool_call(
    session: Session,
    *,
    incident_id: str,
    workflow_id: str,
    tool_name: str,
    target: str,
    arguments: dict[str, Any],
    side_effect: SideEffect,
    attempt: int,
    succeeded: bool,
    result: dict[str, Any] | None = None,
    error: str | None = None,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
    is_simulated: bool = True,
) -> ToolCallRecord:
    from opsaccord.config import digest as _digest

    started = started_at or utcnow()
    row = ToolCallRow(
        id=prefixed("tc"),
        incident_id=incident_id,
        workflow_id=workflow_id,
        tool_name=tool_name,
        target=target,
        arguments=arguments,
        arguments_digest=_digest(arguments),
        side_effect=side_effect,
        attempt=attempt,
        succeeded=succeeded,
        result=result or {},
        error=error,
        started_at=started,
        finished_at=finished_at or started,
        is_simulated=is_simulated,
    )
    session.add(row)
    session.flush()
    return _tool_call(row)


def add_verification(
    session: Session,
    *,
    incident_id: str,
    workflow_id: str,
    service: str,
    result: VerificationResult,
    action_id: str | None = None,
) -> str:
    row = VerificationRow(
        id=prefixed("ver"),
        incident_id=incident_id,
        workflow_id=workflow_id,
        action_id=action_id,
        service=service,
        outcome=result.outcome,
        detail=result.detail,
        evidence_ids=list(result.evidence_ids),
        checked_at=result.checked_at,
    )
    session.add(row)
    session.flush()
    audit(
        session,
        actor="controller",
        actor_type=ActorType.SYSTEM,
        event_type="verification.recorded",
        incident_id=incident_id,
        workflow_id=workflow_id,
        subject_type="verification",
        subject_id=row.id,
        payload={
            "outcome": str(result.outcome),
            "detail": result.detail,
            "evidence_ids": list(result.evidence_ids),
        },
    )
    return row.id


# --- actions and approvals -------------------------------------------------


def find_action_by_intent(
    session: Session, *, workflow_id: str, intent_digest: str
) -> Action | None:
    row = session.scalars(
        sa.select(ActionRow)
        .where(ActionRow.workflow_id == workflow_id, ActionRow.intent_digest == intent_digest)
        .order_by(ActionRow.created_at.desc())
    ).first()
    return _action(row) if row else None


def create_action(
    session: Session,
    *,
    incident_id: str,
    workflow_id: str,
    tool_name: str,
    target: str,
    arguments: dict[str, Any],
    rationale: str,
    decision_id: str | None,
    requires_approval: bool,
    is_simulated: bool = True,
) -> Action:
    from opsaccord.config import digest as _digest

    now = utcnow()
    action_id = prefixed("act")
    intent_digest = Action.compute_intent_digest(tool_name, target, arguments)
    row = ActionRow(
        id=action_id,
        incident_id=incident_id,
        workflow_id=workflow_id,
        tool_name=tool_name,
        target=target,
        arguments=arguments,
        arguments_digest=_digest(arguments),
        intent_digest=intent_digest,
        rationale=rationale,
        status=ActionStatus.AWAITING_APPROVAL if requires_approval else ActionStatus.APPROVED,
        decision_id=decision_id,
        attempt=0,
        idempotency_key=action_id,
        is_simulated=is_simulated,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    session.flush()
    _append_event(
        session,
        workflow_id=workflow_id,
        incident_id=incident_id,
        event_type="action.proposed",
        from_status=None,
        to_status=None,
        payload={
            "action_id": action_id,
            "tool": tool_name,
            "target": target,
            "intent_digest": intent_digest,
            "requires_approval": requires_approval,
        },
    )
    audit(
        session,
        actor="controller",
        actor_type=ActorType.SYSTEM,
        event_type="action.proposed",
        incident_id=incident_id,
        workflow_id=workflow_id,
        subject_type="action",
        subject_id=action_id,
        payload={
            "tool": tool_name,
            "target": target,
            "arguments": arguments,
            "intent_digest": intent_digest,
        },
    )
    return _action(row)


def get_action(session: Session, action_id: str) -> Action | None:
    row = session.get(ActionRow, action_id)
    return _action(row) if row else None


def list_actions(session: Session, incident_id: str) -> list[Action]:
    rows = session.scalars(
        sa.select(ActionRow)
        .where(ActionRow.incident_id == incident_id)
        .order_by(ActionRow.created_at)
    ).all()
    return [_action(r) for r in rows]


def list_in_flight_actions(session: Session, *, workflow_id: str | None = None) -> list[Action]:
    stmt = sa.select(ActionRow).where(
        ActionRow.status.in_([ActionStatus.DISPATCHING, ActionStatus.UNCERTAIN])
    )
    if workflow_id:
        stmt = stmt.where(ActionRow.workflow_id == workflow_id)
    return [_action(r) for r in session.scalars(stmt.order_by(ActionRow.created_at)).all()]


@dataclass(frozen=True)
class ClaimResult:
    claimed: bool
    action: Action | None
    reason: str | None = None


def claim_action(
    session: Session,
    *,
    action_id: str,
    actor: str,
    now: datetime | None = None,
    commit_context: dict[str, Any] | None = None,
) -> ClaimResult:
    """Atomically claim an approved action and consume its approval.

    Both updates happen in the caller's transaction. Exactly one competing
    worker can win the conditional UPDATE, and consuming the approval in the
    same transaction means a consumed approval can never authorise a second
    dispatch.

    Winning this claim *is* the execution commitment point: once the caller's
    transaction commits, the action is committed to execution and no control that
    commits later can cancel it (see ``docs/architecture.md``). ``commit_context``
    is written into the ``action.claimed`` audit event so the record of that
    commitment states which controls the authorization actually evaluated.
    """
    moment = now or utcnow()
    claimed = session.execute(
        sa.update(ActionRow)
        .where(ActionRow.id == action_id, ActionRow.status == ActionStatus.APPROVED)
        .values(
            status=ActionStatus.DISPATCHING,
            attempt=ActionRow.attempt + 1,
            dispatch_started_at=moment,
            updated_at=moment,
        )
        .returning(ActionRow.id)
    ).scalar_one_or_none()
    if claimed is None:
        return ClaimResult(False, get_action(session, action_id), "action is not claimable")

    approval_id = session.scalar(sa.select(ActionRow.approval_id).where(ActionRow.id == action_id))
    if approval_id:
        claimed_intent = session.scalar(
            sa.select(ActionRow.intent_digest).where(ActionRow.id == action_id)
        )
        # The approval must belong to *this* action and *this* intent. Without
        # the identity predicates a single granted approval could be pointed at
        # a different action row and authorise it.
        consumed = session.execute(
            sa.update(ApprovalRow)
            .where(
                ApprovalRow.id == approval_id,
                ApprovalRow.action_id == action_id,
                ApprovalRow.intent_digest == claimed_intent,
                ApprovalRow.status == ApprovalStatus.APPROVED,
                ApprovalRow.expires_at > moment,
            )
            .values(status=ApprovalStatus.CONSUMED, consumed_at=moment)
            .returning(ApprovalRow.id)
        ).scalar_one_or_none()
        if consumed is None:
            session.rollback()
            return ClaimResult(
                False, get_action(session, action_id), "approval expired, consumed, or not granted"
            )
    session.flush()
    audit(
        session,
        actor=actor,
        actor_type=ActorType.SYSTEM,
        event_type="action.claimed",
        incident_id=session.scalar(
            sa.select(ActionRow.incident_id).where(ActionRow.id == action_id)
        ),
        workflow_id=session.scalar(
            sa.select(ActionRow.workflow_id).where(ActionRow.id == action_id)
        ),
        subject_type="action",
        subject_id=action_id,
        payload=dict(commit_context or {}),
    )
    return ClaimResult(True, get_action(session, action_id))


def finish_action(
    session: Session,
    *,
    action_id: str,
    status: ActionStatus,
    result: dict[str, Any] | None = None,
    error: str | None = None,
    external_outcome: ActionOutcome | None = None,
    actor: str = "tool-gateway",
) -> Action:
    now = utcnow()
    session.execute(
        sa.update(ActionRow)
        .where(ActionRow.id == action_id)
        .values(
            status=status,
            result=result or {},
            error=error,
            external_outcome=external_outcome,
            completed_at=now,
            updated_at=now,
        )
    )
    session.flush()
    action = get_action(session, action_id)
    assert action is not None
    audit(
        session,
        actor=actor,
        actor_type=ActorType.SYSTEM,
        event_type=f"action.{str(status).lower()}",
        incident_id=action.incident_id,
        workflow_id=action.workflow_id,
        subject_type="action",
        subject_id=action_id,
        payload={
            "error": error,
            "external_outcome": str(external_outcome) if external_outcome else None,
        },
    )
    return action


def mark_action_uncertain(session: Session, *, action_id: str, reason: str) -> Action:
    return finish_action(
        session,
        action_id=action_id,
        status=ActionStatus.UNCERTAIN,
        error=reason,
        external_outcome=ActionOutcome.UNKNOWN,
    )


def create_approval(
    session: Session,
    *,
    action: Action,
    requested_by: str,
    ttl_seconds: int,
    actor: str = "controller",
) -> Approval:
    now = utcnow()
    row = ApprovalRow(
        id=prefixed("apr"),
        incident_id=action.incident_id,
        workflow_id=action.workflow_id,
        action_id=action.id,
        intent_digest=action.intent_digest,
        target=action.target,
        arguments_digest=action.arguments_digest,
        requested_by=requested_by,
        status=ApprovalStatus.PENDING,
        expires_at=now + timedelta(seconds=ttl_seconds),
        created_at=now,
    )
    session.add(row)
    session.flush()
    session.execute(
        sa.update(ActionRow)
        .where(ActionRow.id == action.id)
        .values(approval_id=row.id, status=ActionStatus.AWAITING_APPROVAL, updated_at=now)
    )
    _append_event(
        session,
        workflow_id=action.workflow_id,
        incident_id=action.incident_id,
        event_type="approval.requested",
        from_status=None,
        to_status=None,
        payload={
            "approval_id": row.id,
            "action_id": action.id,
            "target": action.target,
            "arguments_digest": action.arguments_digest,
            "expires_at": row.expires_at.isoformat(),
        },
    )
    audit(
        session,
        actor=actor,
        actor_type=ActorType.SYSTEM,
        event_type="approval.requested",
        incident_id=action.incident_id,
        workflow_id=action.workflow_id,
        subject_type="approval",
        subject_id=row.id,
        payload={
            "action_id": action.id,
            "tool": action.tool_name,
            "target": action.target,
            "arguments": action.arguments,
            "arguments_digest": action.arguments_digest,
            "intent_digest": action.intent_digest,
            "expires_at": row.expires_at.isoformat(),
        },
    )
    return _approval(row)


def get_approval(session: Session, approval_id: str) -> Approval | None:
    row = session.get(ApprovalRow, approval_id)
    return _approval(row) if row else None


def list_approvals(session: Session, incident_id: str) -> list[Approval]:
    rows = session.scalars(
        sa.select(ApprovalRow)
        .where(ApprovalRow.incident_id == incident_id)
        .order_by(ApprovalRow.created_at)
    ).all()
    return [_approval(r) for r in rows]


def pending_approval(session: Session, incident_id: str) -> Approval | None:
    row = session.scalars(
        sa.select(ApprovalRow)
        .where(
            ApprovalRow.incident_id == incident_id,
            ApprovalRow.status == ApprovalStatus.PENDING,
        )
        .order_by(ApprovalRow.created_at.desc())
    ).first()
    return _approval(row) if row else None


def decide_approval(
    session: Session,
    *,
    approval_id: str,
    approver: str,
    decision: ApprovalDecision,
    reason: str | None,
    now: datetime | None = None,
) -> Approval:
    """Record a human decision on a pending approval.

    The agent cannot approve itself: ``requested_by`` is compared against the
    approver, and the API additionally requires a human-authenticated role.
    """
    moment = now or utcnow()
    row = session.scalars(
        sa.select(ApprovalRow).where(ApprovalRow.id == approval_id).with_for_update()
    ).one_or_none()
    if row is None:
        raise RepositoryError(f"unknown approval {approval_id}")
    if ApprovalStatus(row.status) is not ApprovalStatus.PENDING:
        raise RepositoryError(f"approval {approval_id} is {row.status}, not PENDING")
    if row.expires_at <= moment:
        row.status = ApprovalStatus.EXPIRED
        session.flush()
        raise RepositoryError(f"approval {approval_id} expired at {row.expires_at.isoformat()}")
    if approver == row.requested_by:
        raise RepositoryError("the requesting actor cannot approve its own action")
    row.approver = approver
    row.decision = decision
    row.status = (
        ApprovalStatus.APPROVED
        if decision is ApprovalDecision.APPROVED
        else ApprovalStatus.REJECTED
    )
    row.reason = reason
    row.decided_at = moment
    action_status = (
        ActionStatus.APPROVED if decision is ApprovalDecision.APPROVED else ActionStatus.REJECTED
    )
    session.execute(
        sa.update(ActionRow)
        .where(ActionRow.id == row.action_id, ActionRow.status == ActionStatus.AWAITING_APPROVAL)
        .values(status=action_status, updated_at=moment)
    )
    session.flush()
    _append_event(
        session,
        workflow_id=row.workflow_id,
        incident_id=row.incident_id,
        event_type=f"approval.{str(decision).lower()}",
        from_status=None,
        to_status=None,
        payload={"approval_id": row.id, "action_id": row.action_id, "approver": approver},
    )
    audit(
        session,
        actor=approver,
        actor_type=ActorType.HUMAN,
        event_type=f"approval.{str(decision).lower()}",
        incident_id=row.incident_id,
        workflow_id=row.workflow_id,
        subject_type="approval",
        subject_id=row.id,
        payload={
            "action_id": row.action_id,
            "reason": reason,
            "intent_digest": row.intent_digest,
            "target": row.target,
        },
    )
    return _approval(row)


def expire_stale_approvals(session: Session, *, now: datetime | None = None) -> int:
    moment = now or utcnow()
    rows = session.scalars(
        sa.select(ApprovalRow).where(
            ApprovalRow.status == ApprovalStatus.PENDING, ApprovalRow.expires_at <= moment
        )
    ).all()
    for row in rows:
        row.status = ApprovalStatus.EXPIRED
        audit(
            session,
            actor="system",
            actor_type=ActorType.SYSTEM,
            event_type="approval.expired",
            incident_id=row.incident_id,
            workflow_id=row.workflow_id,
            subject_type="approval",
            subject_id=row.id,
            payload={"action_id": row.action_id},
        )
    session.flush()
    return len(rows)


def invalidate_pending_approvals(session: Session, *, incident_id: str, actor: str) -> int:
    rows = session.scalars(
        sa.select(ApprovalRow).where(
            ApprovalRow.incident_id == incident_id, ApprovalRow.status == ApprovalStatus.PENDING
        )
    ).all()
    for row in rows:
        row.status = ApprovalStatus.INVALIDATED
        audit(
            session,
            actor=actor,
            actor_type=ActorType.SYSTEM,
            event_type="approval.invalidated",
            incident_id=row.incident_id,
            workflow_id=row.workflow_id,
            subject_type="approval",
            subject_id=row.id,
            payload={"action_id": row.action_id},
        )
    session.flush()
    return len(rows)


def rejected_intents(session: Session, *, workflow_id: str) -> set[str]:
    """Intent digests a human has rejected in this workflow.

    Intent identity excludes the action id, so proposing the same tool, target,
    and arguments under a fresh action id cannot get past a rejection.
    """
    rows = session.scalars(
        sa.select(ApprovalRow.intent_digest).where(
            ApprovalRow.workflow_id == workflow_id,
            ApprovalRow.status == ApprovalStatus.REJECTED,
        )
    ).all()
    return set(rows)


# --- constraints and services ---------------------------------------------


def add_constraint(
    session: Session,
    *,
    kind: ConstraintKind,
    actor: str,
    incident_id: str | None = None,
    tool_name: str | None = None,
    target: str | None = None,
    note: str | None = None,
) -> Constraint:
    """Record a human control.

    The scope decides which lock makes it ordered against an in-flight dispatch,
    and both are taken here so a caller cannot forget one:

    * an incident-scoped constraint is serialised by the incident row lock, which
      the dispatcher also holds across revalidate-and-claim;
    * a *global* constraint (``incident_id IS NULL``) is read by every incident's
      policy evaluation but has no incident row to lock, so it is serialised by
      the global control gate instead. Locking the existing constraint rows would
      not do it: an ``INSERT`` is not blocked by any lock on rows that already
      exist, and with no constraints recorded there is nothing to lock at all.
    """
    lock_control_gate(session)
    if incident_id is not None:
        lock_incident(session, incident_id)
    row = ConstraintRow(
        id=prefixed("con"),
        incident_id=incident_id,
        kind=kind,
        tool_name=tool_name,
        target=target,
        note=note,
        created_by=actor,
        active=True,
        created_at=utcnow(),
    )
    session.add(row)
    session.flush()
    audit(
        session,
        actor=actor,
        actor_type=ActorType.HUMAN,
        event_type="constraint.created",
        incident_id=incident_id,
        subject_type="constraint",
        subject_id=row.id,
        payload={"kind": str(kind), "tool_name": tool_name, "target": target, "note": note},
    )
    return _constraint(row)


def list_active_constraints(session: Session, incident_id: str) -> list[Constraint]:
    rows = session.scalars(
        sa.select(ConstraintRow)
        .where(
            ConstraintRow.active.is_(True),
            sa.or_(ConstraintRow.incident_id.is_(None), ConstraintRow.incident_id == incident_id),
        )
        .order_by(ConstraintRow.created_at)
    ).all()
    return [_constraint(r) for r in rows]


def list_services(session: Session) -> list[ServiceRecord]:
    rows = session.scalars(sa.select(ServiceRow).order_by(ServiceRow.name)).all()
    return [_service(r) for r in rows]


def get_service(session: Session, name: str) -> ServiceRecord | None:
    row = session.get(ServiceRow, name)
    return _service(row) if row else None


# --- read model ------------------------------------------------------------


def snapshot(session: Session, incident_id: str) -> IncidentSnapshot | None:
    incident = get_incident(session, incident_id)
    if incident is None:
        return None
    workflow = get_workflow(session, incident_id)
    evidence = [
        _evidence(r)
        for r in session.scalars(
            sa.select(EvidenceRow)
            .where(EvidenceRow.incident_id == incident_id)
            .order_by(EvidenceRow.collected_at)
        ).all()
    ]
    decisions = [
        _decision(r)
        for r in session.scalars(
            sa.select(ModelDecisionRow)
            .where(ModelDecisionRow.incident_id == incident_id)
            .order_by(ModelDecisionRow.created_at)
        ).all()
    ]
    actions = list_actions(session, incident_id)
    approvals = list_approvals(session, incident_id)
    constraints = list_active_constraints(session, incident_id)
    events: list[dict[str, Any]] = []
    if workflow:
        events = [
            {
                "seq": r.seq,
                "event_type": r.event_type,
                "from_status": r.from_status,
                "to_status": r.to_status,
                "payload": r.payload,
                "created_at": r.created_at.isoformat(),
            }
            for r in session.scalars(
                sa.select(WorkflowEventRow)
                .where(WorkflowEventRow.workflow_id == workflow.id)
                .order_by(WorkflowEventRow.seq)
            ).all()
        ]
    pending = pending_approval(session, incident_id)
    return IncidentSnapshot(
        incident=incident,
        workflow=workflow,
        evidence=tuple(evidence),
        decisions=tuple(decisions),
        actions=tuple(actions),
        approvals=tuple(approvals),
        constraints=tuple(constraints),
        events=tuple(events),
        pending_approval_id=pending.id if pending else None,
        awaiting_resume=workflow is not None
        and workflow.status is IncidentStatus.WAITING_FOR_APPROVAL,
    )
