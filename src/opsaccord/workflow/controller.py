"""The workflow controller.

The controller owns state transitions, budget consumption, approval gating,
action dispatch ordering, verification, and termination. LangGraph supplies the
execution plan and durable continuity; every authoritative fact is read from and
written to PostgreSQL.

Authority rules implemented here:

* A model proposal is input, never a decision.
* The controller re-reads the approval from PostgreSQL after a resume, so a
  crafted resume payload cannot approve anything.
* Actions are claimed and their approval consumed atomically before dispatch.
* RESOLVED requires a fresh verification success.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from opsaccord.config import Settings
from opsaccord.domain.enums import (
    ActionStatus,
    ActorType,
    ApprovalDecision,
    ApprovalStatus,
    DecisionType,
    IncidentStatus,
    VerificationOutcome,
    WorkflowOutcome,
)
from opsaccord.domain.models import ActionProposal
from opsaccord.persistence import repositories as repo
from opsaccord.persistence.db import Database
from opsaccord.persistence.tables import ModelDecisionRow
from opsaccord.reasoning.base import EvidenceDigest, ProposalRequest, ReasoningProvider
from opsaccord.tools.gateway import PolicyBlocked, ToolGateway
from opsaccord.workflow.checkpoints import checkpoint_saver, ensure_checkpoint_schema

# An action whose external outcome is not settled. The workflow may not report
# RESOLVED while one of these exists: the change might still be in flight.
_UNRESOLVED_SIDE_EFFECT_STATUSES = frozenset({ActionStatus.DISPATCHING, ActionStatus.UNCERTAIN})

READ_PLAN: dict[str, tuple[dict[str, Any], ...]] = {
    "payment-service": (
        {"tool_name": "read_service_health", "arguments": {"target": "payment-service"}},
        {
            "tool_name": "read_metrics",
            "arguments": {
                "target": "payment-service",
                "metric": "error_rate",
                "window_seconds": 300,
            },
        },
        {
            "tool_name": "read_traces",
            "arguments": {
                "target": "payment-service",
                "operation": "Charge",
                "window_seconds": 300,
            },
        },
        {
            # The caller's own view of the edge. When checkout cannot reach
            # payment, payment emits nothing at all, so every target-scoped probe
            # above stays quiet and reads as healthy; the failing client spans are
            # the only signal that can attribute this incident to the dependency.
            "tool_name": "read_dependency",
            "arguments": {
                "target": "checkout-service",
                "dependency": "payment-service",
                "window_seconds": 300,
            },
        },
        {"tool_name": "read_recent_changes", "arguments": {"target": "payment-service"}},
        {"tool_name": "read_runbook", "arguments": {"target": "payment-service"}},
    ),
    "ad-service": (
        {"tool_name": "read_service_health", "arguments": {"target": "ad-service"}},
        {
            # Container CPU metrics are exported on a fast cadence, so a short
            # window answers "is it burning now". A five-minute average would
            # dilute a burn that started a minute ago and read as healthy.
            "tool_name": "read_metrics",
            "arguments": {
                "target": "ad-service",
                "metric": "cpu_utilization",
                "window_seconds": 60,
            },
        },
        {
            "tool_name": "read_traces",
            "arguments": {"target": "ad-service", "operation": "GetAds", "window_seconds": 300},
        },
        {"tool_name": "read_runbook", "arguments": {"target": "ad-service"}},
    ),
    "cart-service": (
        {"tool_name": "read_service_health", "arguments": {"target": "cart-service"}},
        {
            "tool_name": "read_metrics",
            "arguments": {"target": "cart-service", "metric": "error_rate", "window_seconds": 300},
        },
        {
            # cartFailure in the pinned revision breaks EmptyCart, not GetCart:
            # the cart service routes EmptyCart to the bad store. Asking for
            # GetCart would collect a healthy span and miss the fault.
            "tool_name": "read_traces",
            "arguments": {
                "target": "cart-service",
                "operation": "EmptyCart",
                "window_seconds": 300,
            },
        },
        {"tool_name": "read_runbook", "arguments": {"target": "cart-service"}},
    ),
}


class GraphState(TypedDict, total=False):
    incident_id: str
    workflow_id: str
    target: str
    status: str
    evidence_ids: list[str]
    step_errors: list[str]
    decision_id: str
    action_id: str
    approval_id: str
    verification: str
    terminal_reason: str
    notes: list[str]
    verify_attempts: int
    escalate: bool
    blocked_reason: str


@dataclass
class RunOutcome:
    incident_id: str
    status: IncidentStatus
    terminal_reason: str | None
    interrupted: bool = False
    approval_id: str | None = None
    action_id: str | None = None
    notes: list[str] = field(default_factory=list)


class WorkflowController:
    def __init__(
        self,
        *,
        db: Database,
        gateway: ToolGateway,
        provider: ReasoningProvider,
        settings: Settings,
    ) -> None:
        self.db = db
        self.gateway = gateway
        self.provider = provider
        self.settings = settings
        ensure_checkpoint_schema(settings.checkpoint_url)

    # --- public API -------------------------------------------------------

    def create_incident(
        self,
        *,
        title: str,
        service: str,
        severity: str = "SEV2",
        scenario_id: str | None = None,
    ) -> str:
        with self.db.session() as session:
            incident = repo.create_incident(
                session,
                title=title,
                service=service,
                severity=severity,
                scenario_id=scenario_id,
            )
            if repo.get_service(session, service) is None:
                raise repo.RepositoryError(
                    f"{service!r} is not a registered demo service; refusing to create the incident"
                )
            repo.ensure_workflow(
                session,
                incident_id=incident.id,
                soft_step_budget=self.settings.soft_step_budget,
                hard_step_cap=self.settings.hard_step_cap,
                max_extensions=self.settings.max_budget_extensions,
            )
        return incident.id

    def run(self, incident_id: str) -> RunOutcome:
        return self._invoke(incident_id, payload=None)

    def resume(self, incident_id: str) -> RunOutcome:
        """Resume a workflow paused at an approval interrupt.

        The resume value is deliberately inert: the approval decision is read
        from PostgreSQL inside the graph.
        """
        return self._invoke(incident_id, payload={"ack": True})

    def advance(self, incident_id: str) -> RunOutcome:
        """Continue an incident from the state it is actually in.

        ``run`` starts the graph from the initial state and ``resume`` feeds a
        command into an interrupt. Picking the wrong one is not a cosmetic
        error: resuming a graph that was never started drops the initial state
        (the nodes then fail with a missing ``incident_id``), and starting a
        graph that is interrupted re-enters the interrupted node. The persisted
        status is the discriminator, because ``WAITING_FOR_APPROVAL`` is
        exactly the approval interrupt.
        """
        with self.db.session() as session:
            workflow = repo.get_workflow(session, incident_id)
            if workflow is None:
                raise repo.RepositoryError(f"unknown incident {incident_id}")
            paused_at_interrupt = workflow.status is IncidentStatus.WAITING_FOR_APPROVAL
        return self.resume(incident_id) if paused_at_interrupt else self.run(incident_id)

    def reconcile(self, incident_id: str) -> list[str]:
        """Reconcile uncertain side effects after a crash or a timeout."""
        with self.db.session() as session:
            workflow = repo.get_workflow(session, incident_id)
            if workflow is None:
                return []
            in_flight = repo.list_in_flight_actions(session, workflow_id=workflow.id)
        notes: list[str] = []
        for action in in_flight:
            if action.status is ActionStatus.DISPATCHING:
                # The worker died between claim and dispatch bookkeeping. We cannot
                # know whether the external call started, so never blind-retry.
                with self.db.session() as session:
                    repo.mark_action_uncertain(
                        session,
                        action_id=action.id,
                        reason="worker stopped after claiming the action; outcome unknown",
                    )
                notes.append(f"{action.id}: marked UNCERTAIN after an interrupted dispatch")
            reconciled = action
            if action.status in {ActionStatus.DISPATCHING, ActionStatus.UNCERTAIN}:
                reconciled = self.gateway.reconcile_action(action_id=action.id)
                notes.append(f"{action.id}: reconciled to {reconciled.status}")
        return notes

    # --- graph ------------------------------------------------------------

    def _thread_config(self, workflow_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": workflow_id}}

    def _invoke(self, incident_id: str, *, payload: dict[str, Any] | None) -> RunOutcome:
        with self.db.session() as session:
            workflow = repo.get_workflow(session, incident_id)
            incident = repo.get_incident(session, incident_id)
            if workflow is None or incident is None:
                raise repo.RepositoryError(f"unknown incident {incident_id}")
            if workflow.status in {
                IncidentStatus.RESOLVED,
                IncidentStatus.READY_FOR_HUMAN,
                IncidentStatus.ESCALATED_INCOMPLETE,
            }:
                return RunOutcome(
                    incident_id=incident_id,
                    status=workflow.status,
                    terminal_reason=workflow.terminal_reason,
                )
            thread_id = workflow.thread_id
            state: GraphState = {
                "incident_id": incident_id,
                "workflow_id": workflow.id,
                "target": incident.service,
                "evidence_ids": [],
                "step_errors": [],
                "notes": [],
                "verify_attempts": 0,
            }
            if repo.get_service(session, incident.service) is None:
                raise repo.RepositoryError("incident service is not in the registry")

        with checkpoint_saver(self.settings.checkpoint_url) as saver:
            graph = self._build(saver)
            config = self._thread_config(thread_id)
            result = (
                graph.invoke(Command(resume=payload), config)
                if payload is not None
                else graph.invoke(state, config)
            )

        with self.db.session() as session:
            workflow = repo.get_workflow(session, incident_id)
            assert workflow is not None
            pending = repo.pending_approval(session, incident_id)
            status = workflow.status
            interrupted = bool(isinstance(result, dict) and result.get("__interrupt__"))
            return RunOutcome(
                incident_id=incident_id,
                status=status,
                terminal_reason=workflow.terminal_reason,
                interrupted=interrupted or pending is not None,
                approval_id=pending.id if pending else None,
                action_id=result.get("action_id") if isinstance(result, dict) else None,
                notes=list(result.get("notes", [])) if isinstance(result, dict) else [],
            )

    def _build(self, saver: Any) -> Any:
        builder = StateGraph(GraphState)
        builder.add_node("investigate", self._investigate)
        builder.add_node("diagnose", self._diagnose)
        builder.add_node("plan", self._plan)
        builder.add_node("request_approval", self._request_approval)
        builder.add_node("execute", self._execute)
        builder.add_node("verify", self._verify)
        builder.add_node("resolved", self._resolved)
        builder.add_node("handoff", self._handoff)
        builder.add_node("escalate", self._escalate)

        builder.add_edge(START, "investigate")
        builder.add_edge("investigate", "diagnose")
        builder.add_conditional_edges(
            "diagnose",
            self._route_after_diagnose,
            {"plan": "plan", "handoff": "handoff", "escalate": "escalate"},
        )
        builder.add_conditional_edges(
            "plan",
            self._route_after_plan,
            {
                "request_approval": "request_approval",
                "execute": "execute",
                "handoff": "handoff",
                "escalate": "escalate",
            },
        )
        builder.add_conditional_edges(
            "request_approval",
            self._route_after_approval,
            {"execute": "execute", "handoff": "handoff", "escalate": "escalate"},
        )
        builder.add_edge("execute", "verify")
        builder.add_conditional_edges(
            "verify",
            self._route_after_verify,
            {
                "resolved": "resolved",
                "investigate": "investigate",
                "escalate": "escalate",
                "handoff": "handoff",
            },
        )
        for terminal in ("resolved", "handoff", "escalate"):
            builder.add_edge(terminal, END)
        return builder.compile(checkpointer=saver)

    # --- nodes ------------------------------------------------------------

    def _try_extension(self, incident_id: str, justification: str) -> bool:
        """Grant one bounded soft-budget extension, or report that none remains."""
        with self.db.session() as session:
            try:
                repo.grant_extension(
                    session,
                    incident_id=incident_id,
                    actor="controller",
                    justification=justification[:300],
                )
            except (repo.BudgetExhausted, repo.RepositoryError):
                return False
        return True

    def _investigate(self, state: GraphState) -> dict[str, Any]:
        incident_id = state["incident_id"]
        target = state["target"]
        notes = list(state.get("notes", []))
        errors = list(state.get("step_errors", []))
        evidence_ids = list(state.get("evidence_ids", []))
        with self.db.session() as session:
            workflow = repo.get_workflow(session, incident_id)
            assert workflow is not None
            workflow_id = workflow.id
            if workflow.status is IncidentStatus.NEW:
                repo.transition(
                    session,
                    incident_id=incident_id,
                    target=IncidentStatus.INVESTIGATING,
                    actor="controller",
                    actor_type=ActorType.SYSTEM,
                    event_type="workflow.investigating",
                )
        for step in READ_PLAN.get(target, ()):
            try:
                outcome = self._read_with_extension(
                    incident_id=incident_id,
                    workflow_id=workflow_id,
                    step=step,
                    # The step carries its own target. For every single-service
                    # step that is the incident's service; a dependency step names
                    # the caller, because the observation belongs to the caller.
                    target=step["arguments"]["target"],
                    notes=notes,
                )
                if outcome is None:
                    errors.append("budget: no extension available; investigation truncated")
                    break
                evidence_ids.append(outcome.evidence_id)
                if not outcome.result.succeeded:
                    errors.append(f"{step['tool_name']}: {outcome.result.error}")
            except PolicyBlocked as exc:
                errors.append(f"policy blocked read: {exc}")
        return {"evidence_ids": evidence_ids, "step_errors": errors, "notes": notes}

    def _read_with_extension(
        self,
        *,
        incident_id: str,
        workflow_id: str,
        step: dict[str, Any],
        target: str,
        notes: list[str],
    ) -> Any:
        """Read one evidence item, extending the soft budget at most once."""
        for attempt in (1, 2):
            try:
                return self.gateway.read(
                    incident_id=incident_id,
                    workflow_id=workflow_id,
                    tool_name=step["tool_name"],
                    arguments=step["arguments"],
                    target=target,
                )
            except repo.BudgetExhausted as exc:
                if attempt == 2:
                    return None
                if not self._try_extension(
                    incident_id, f"investigation needs more evidence: {exc}"
                ):
                    return None
                notes.append("soft investigation budget extended once, with justification")
        return None

    def _diagnose(self, state: GraphState) -> dict[str, Any]:
        incident_id = state["incident_id"]
        with self.db.session() as session:
            incident = repo.get_incident(session, incident_id)
            workflow = repo.get_workflow(session, incident_id)
            assert incident and workflow
            evidence = repo.snapshot(session, incident_id)
            assert evidence is not None
            digests = tuple(
                EvidenceDigest(
                    id=item.id,
                    kind=str(item.kind),
                    source=item.source,
                    service=item.service,
                    summary=item.summary,
                    value=item.value,
                )
                for item in evidence.evidence
            )
            runbooks = tuple(
                item.value.get("text", "")
                for item in evidence.evidence
                if item.source == "read_runbook"
            )
            try:
                repo.consume_step(
                    session, incident_id=incident_id, kind="model", actor="controller"
                )
            except repo.BudgetExhausted as exc:
                repo.set_terminal_reason(
                    session, incident_id=incident_id, reason=f"budget exhausted: {exc}"
                )
                return {"escalate": True, "terminal_reason": f"budget exhausted: {exc}"}

            request = ProposalRequest(
                incident_id=incident_id,
                incident_title=incident.title,
                service=incident.service,
                severity=incident.severity,
                status=str(workflow.status),
                evidence=digests,
                runbook_texts=runbooks,
                allowed_tools=self.gateway.registry.names(),
                steps_remaining=max(workflow.hard_step_cap - workflow.steps_used, 0),
                allowed_targets=tuple(self.settings.allowed_targets),
                tool_contracts=self.gateway.registry.contracts(),
            )
            workflow_id = workflow.id

        try:
            proposal = self.provider.propose(request)
        except Exception as exc:
            # A hosted model can time out, refuse, exceed its budget or return
            # something that is not a proposal. None of that may crash the workflow
            # or be read as a decision: the failure is recorded as a handoff decision
            # with unknown usage (unknown is not zero), and a human owns the
            # incident. The model step was already charged to the budget above.
            reason = f"model provider failed: {type(exc).__name__}: {str(exc)[:300]}"
            usage = getattr(exc, "usage", None)
            with self.db.session() as session:
                repo.add_decision(
                    session,
                    incident_id=incident_id,
                    workflow_id=workflow_id,
                    decision_type=DecisionType.HANDOFF,
                    provider=getattr(self.provider, "provider", "unknown"),
                    model_id=getattr(self.provider, "model_id", "unknown"),
                    rationale=reason,
                    payload={"provider_error": type(exc).__name__},
                    evidence_ids=tuple(state.get("evidence_ids", ())),
                    input_tokens=getattr(usage, "input_tokens", None),
                    output_tokens=getattr(usage, "output_tokens", None),
                    cost_usd=getattr(usage, "cost_usd", None),
                    latency_ms=getattr(usage, "latency_ms", None),
                )
                repo.set_terminal_reason(session, incident_id=incident_id, reason=reason)
            return {
                "escalate": True,
                "terminal_reason": reason,
                "notes": [*state.get("notes", []), reason],
            }
        with self.db.session() as session:
            decision = repo.add_decision(
                session,
                incident_id=incident_id,
                workflow_id=workflow.id,
                decision_type=DecisionType.DIAGNOSIS,
                provider=proposal.provider,
                model_id=proposal.model_id,
                rationale=proposal.diagnosis.summary,
                payload={
                    "confidence": proposal.diagnosis.confidence,
                    "escalate": proposal.diagnosis.escalate,
                    "escalate_reason": proposal.diagnosis.escalate_reason,
                    "proposed_action": (
                        proposal.diagnosis.proposed_action.model_dump()
                        if proposal.diagnosis.proposed_action
                        else None
                    ),
                    "runbook_texts_considered": len(request.runbook_texts),
                },
                evidence_ids=tuple(state.get("evidence_ids", ())),
                input_tokens=proposal.usage.input_tokens,
                output_tokens=proposal.usage.output_tokens,
                cost_usd=proposal.usage.cost_usd,
                latency_ms=proposal.usage.latency_ms,
            )
            repo.transition(
                session,
                incident_id=incident_id,
                target=IncidentStatus.DIAGNOSED,
                actor="controller",
                actor_type=ActorType.SYSTEM,
                event_type="workflow.diagnosed",
                payload={"summary": proposal.diagnosis.summary[:400]},
            )
        return {
            "decision_id": decision.id,
            "escalate": bool(
                proposal.diagnosis.escalate and not proposal.diagnosis.proposed_action
            ),
            "notes": [*state.get("notes", []), f"diagnosis: {proposal.diagnosis.summary[:200]}"],
        }

    def _plan(self, state: GraphState) -> dict[str, Any]:
        incident_id = state["incident_id"]
        with self.db.session() as session:
            workflow = repo.get_workflow(session, incident_id)
            decision_row = session.get(ModelDecisionRow, state["decision_id"])
            assert workflow and decision_row
            payload = decision_row.payload or {}
            proposal_payload = payload.get("proposed_action")
            workflow_id = workflow.id
        if not proposal_payload:
            return {"blocked_reason": "no action was proposed", "escalate": True}
        proposal = ActionProposal(**proposal_payload)
        try:
            decision, arguments = self.gateway.evaluate_action(
                incident_id=incident_id, proposal=proposal
            )
        except Exception as exc:  # invalid arguments cannot reach an executor
            # A proposal the typed catalog or the registry refuses is a settled
            # "no": the system will not act, and a human owns the next move. That
            # is a handoff, not an inconclusive escalation.
            return {
                "blocked_reason": f"action rejected by typed validation: {exc}",
                "notes": [*state.get("notes", []), f"blocked: {exc}"],
            }
        if not decision.allowed:
            return {
                "blocked_reason": "; ".join(decision.reasons) or decision.matched_rule,
                "notes": [
                    *state.get("notes", []),
                    f"policy blocked {proposal.tool_name} on {proposal.target}: "
                    f"{decision.matched_rule}",
                ],
            }
        action = self.gateway.request_action(
            incident_id=incident_id,
            workflow_id=workflow_id,
            proposal=proposal,
            decision=decision,
            arguments=arguments,
            decision_id=state["decision_id"],
        )
        return {
            "action_id": action.id,
            "notes": [
                *state.get("notes", []),
                f"action {action.id} prepared ({decision.matched_rule})",
            ],
        }

    def _request_approval(self, state: GraphState) -> dict[str, Any]:
        incident_id = state["incident_id"]
        action_id = state["action_id"]
        with self.db.session() as session:
            action = repo.get_action(session, action_id)
            assert action is not None
            approval = None
            if action.approval_id:
                approval = repo.get_approval(session, action.approval_id)
            if approval is None:
                # Exactly one approval request is created per action. Re-entering
                # this node after a resume must never mint a fresh request: doing
                # so would silently discard a human rejection, an expiry, or a
                # consumed decision and re-open a gate the human already closed.
                approval = repo.create_approval(
                    session,
                    action=action,
                    requested_by="controller",
                    ttl_seconds=self.settings.approval_ttl_seconds,
                )
            approval_id = approval.id
            approval_status = ApprovalStatus(approval.status)
            repo.transition(
                session,
                incident_id=incident_id,
                target=IncidentStatus.WAITING_FOR_APPROVAL,
                actor="controller",
                actor_type=ActorType.SYSTEM,
                event_type="workflow.awaiting_approval",
                payload={"approval_id": approval_id, "action_id": action_id},
            )
        if approval_status is ApprovalStatus.PENDING:
            # Pause the graph. The decision itself is read from PostgreSQL on resume.
            interrupt({"approval_id": approval_id, "action_id": action_id})
        return {"approval_id": approval_id}

    def _execute(self, state: GraphState) -> dict[str, Any]:
        incident_id = state["incident_id"]
        action_id = state["action_id"]
        with self.db.session() as session:
            repo.transition(
                session,
                incident_id=incident_id,
                target=IncidentStatus.ACTIONING,
                actor="controller",
                actor_type=ActorType.SYSTEM,
                event_type="workflow.actioning",
                payload={"action_id": action_id},
            )
        execution = self.gateway.execute_action(action_id=action_id)
        notes = list(state.get("notes", []))
        notes.append(f"execute: {execution.detail}")
        if execution.action.status is ActionStatus.UNCERTAIN:
            reconciled = self.gateway.reconcile_action(action_id=action_id)
            notes.append(f"reconcile: {action_id} is {reconciled.status}")
        return {"notes": notes}

    def _verify(self, state: GraphState) -> dict[str, Any]:
        incident_id = state["incident_id"]
        target = state["target"]
        action_id = state.get("action_id") or None
        with self.db.session() as session:
            workflow = repo.get_workflow(session, incident_id)
            action = repo.get_action(session, action_id) if action_id else None
            assert workflow is not None
            repo.transition(
                session,
                incident_id=incident_id,
                target=IncidentStatus.VERIFYING,
                actor="controller",
                actor_type=ActorType.SYSTEM,
                event_type="workflow.verifying",
                payload={"action_id": action_id},
            )
            completed_at = action.completed_at if action else None
        result = self.gateway.verify_service(
            incident_id=incident_id,
            workflow_id=workflow.id,
            target=target,
            action_id=action_id,
            action_completed_at=completed_at,
        )
        return {
            "verification": str(result.outcome),
            "notes": [*state.get("notes", []), f"verification: {result.detail}"],
            "verify_attempts": state.get("verify_attempts", 0) + 1,
        }

    def _resolved(self, state: GraphState) -> dict[str, Any]:
        incident_id = state["incident_id"]
        with self.db.session() as session:
            repo.transition(
                session,
                incident_id=incident_id,
                target=IncidentStatus.RESOLVED,
                actor="controller",
                actor_type=ActorType.SYSTEM,
                event_type="workflow.resolved",
                payload={"verification": state.get("verification")},
                outcome=WorkflowOutcome.RESOLVED,
            )
            repo.set_terminal_reason(
                session,
                incident_id=incident_id,
                reason="recovery verified with fresh service-specific evidence",
            )
        return {"terminal_reason": "recovery verified with fresh service-specific evidence"}

    def _handoff(self, state: GraphState) -> dict[str, Any]:
        incident_id = state["incident_id"]
        reason = (
            state.get("blocked_reason")
            or state.get("terminal_reason")
            or ("no permitted automated remediation; handing off to a human operator")
        )
        with self.db.session() as session:
            repo.invalidate_pending_approvals(session, incident_id=incident_id, actor="controller")
            repo.transition(
                session,
                incident_id=incident_id,
                target=IncidentStatus.READY_FOR_HUMAN,
                actor="controller",
                actor_type=ActorType.SYSTEM,
                event_type="workflow.handoff",
                payload={"reason": reason},
                outcome=WorkflowOutcome.HANDOFF,
            )
            repo.set_terminal_reason(session, incident_id=incident_id, reason=reason)
        return {"terminal_reason": reason}

    def _escalate(self, state: GraphState) -> dict[str, Any]:
        incident_id = state["incident_id"]
        reason = (
            state.get("terminal_reason")
            or state.get("blocked_reason")
            or "evidence remained insufficient for a safe decision"
        )
        with self.db.session() as session:
            repo.transition(
                session,
                incident_id=incident_id,
                target=IncidentStatus.ESCALATED_INCOMPLETE,
                actor="controller",
                actor_type=ActorType.SYSTEM,
                event_type="workflow.escalated_incomplete",
                payload={"reason": reason},
                outcome=WorkflowOutcome.ESCALATED_INCOMPLETE,
            )
            repo.set_terminal_reason(session, incident_id=incident_id, reason=reason)
        return {"terminal_reason": reason}

    # --- routing ----------------------------------------------------------

    def _route_after_diagnose(self, state: GraphState) -> str:
        if state.get("escalate"):
            return "escalate"
        if state.get("decision_id") and not state.get("blocked_reason"):
            with self.db.session() as session:
                decision_row = session.get(ModelDecisionRow, state["decision_id"])
                payload = (decision_row.payload or {}) if decision_row else {}
            if payload.get("proposed_action"):
                return "plan"
        return "handoff"

    def _route_after_plan(self, state: GraphState) -> str:
        if state.get("escalate"):
            return "escalate"
        if state.get("blocked_reason"):
            return "handoff"
        action_id = state.get("action_id")
        if not action_id:
            return "escalate"
        with self.db.session() as session:
            action = repo.get_action(session, action_id)
        if action is None:
            return "escalate"
        return "request_approval" if action.status is ActionStatus.AWAITING_APPROVAL else "execute"

    def _route_after_approval(self, state: GraphState) -> str:
        """Routing after resume reads PostgreSQL, never the resume payload."""
        incident_id = state["incident_id"]
        action_id = state["action_id"]
        with self.db.session() as session:
            action = repo.get_action(session, action_id)
            if action is None:
                return "escalate"
            approval = (
                repo.get_approval(session, action.approval_id) if action.approval_id else None
            )
            workflow = repo.get_workflow(session, incident_id)
        if workflow and workflow.takeover_actor:
            return "handoff"
        if approval is None:
            return "escalate"
        if approval.status is ApprovalStatus.CONSUMED and action.status is ActionStatus.DISPATCHING:
            return "execute"
        if (
            approval.decision is ApprovalDecision.APPROVED
            and approval.status is ApprovalStatus.APPROVED
        ):
            return "execute"
        if approval.decision is ApprovalDecision.REJECTED:
            return "handoff"
        if approval.status in {ApprovalStatus.EXPIRED, ApprovalStatus.INVALIDATED}:
            return "escalate"
        return "escalate"

    def _route_after_verify(self, state: GraphState) -> str:
        """RESOLVED requires verified recovery *and* no unresolved side effect.

        A service can look healthy while the outcome of the action that was
        supposed to fix it is still unknown. Treating that as resolved would
        hide a possible duplicate-dispatch risk, so any action whose outcome is
        still open escalates instead. A failed or inconclusive verification also
        escalates: Phase 1 does not automate a second side effect after a failed
        verification.
        """
        outcome = state.get("verification")
        with self.db.session() as session:
            workflow = repo.get_workflow(session, state["incident_id"])
            if workflow is None:
                return "escalate"
            # DISPATCHING is exactly as unresolved as UNCERTAIN: the side effect
            # may already have happened and its outcome was never recorded. Only
            # UNCERTAIN used to be checked here, which let a crash between the
            # external call and the ledger write resolve as if nothing were open.
            unresolved = [
                action
                for action in repo.list_actions(session, state["incident_id"])
                if action.status in _UNRESOLVED_SIDE_EFFECT_STATUSES
            ]
        if unresolved:
            return "escalate"
        if outcome == str(VerificationOutcome.VERIFICATION_SUCCESS):
            return "resolved"
        return "escalate"
