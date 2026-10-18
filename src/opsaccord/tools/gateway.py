"""The tool gateway: the only component that touches the demo environment.

Design rules enforced here:

* Typed tools only. Arguments are validated against the registry before any
  executor sees them, so there is no arbitrary shell, URL, or container path.
* Read tools retry within an explicit attempt and deadline budget. Side-effect
  tools never blind-retry: they claim, dispatch once, and reconcile.
* The action row is claimed with a conditional UPDATE inside a transaction, and
  the approval is consumed in that same transaction, so a consumed approval can
  never authorise a second dispatch.
* Policy is revalidated immediately before the side effect, from persisted rows,
  inside the same transaction that claims the action, under the control gate.

The dispatch transaction in ``execute_action`` is the **execution commitment
point**. It takes the control gate first, the incident row lock second, then reads
the control set, revalidates the whole policy, claims the action and consumes its
approval. Once that transaction commits, the action is committed to execution: a
human control that commits later cannot cancel the request (which may already be
in flight), and a control that commits earlier cannot be missed, because every
control write takes the same gate. No lock is held across the external call.

Exactly-once is *not* guaranteed by a unique row. The demo adapter is idempotent
on the action id; when the adapter cannot report an outcome, the action is left
UNCERTAIN and escalated rather than retried. An error the gateway cannot classify
is one of those cases: see ``classify_dispatch_failure``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from opsaccord.config import Settings
from opsaccord.domain.enums import (
    ActionOutcome,
    ActionStatus,
    ConstraintKind,
    EvidenceKind,
    IncidentStatus,
    SideEffect,
    VerificationOutcome,
)
from opsaccord.domain.models import (
    Action,
    ActionProposal,
    Constraint,
    ToolResult,
    ToolSpec,
    VerificationResult,
)
from opsaccord.domain.models import (
    PolicyDecision as PolicyDecisionModel,
)
from opsaccord.ids import utcnow
from opsaccord.persistence import repositories as repo
from opsaccord.persistence.db import Database
from opsaccord.policy import engine as policy_engine
from opsaccord.policy.engine import RefusalCause
from opsaccord.tools.contracts import (
    DependencyVerdict,
    Environment,
    ReadTimeout,
    classify_dependency_edge,
    classify_dispatch_failure,
    dependency_callers,
)
from opsaccord.tools.registry import ToolRegistry

_EVIDENCE_KIND = {
    "read_metrics": EvidenceKind.METRIC,
    "read_traces": EvidenceKind.TRACE,
    "read_service_health": EvidenceKind.HEALTH,
    # The dependency read is span-derived in live mode, so it is trace evidence:
    # what it adds is the caller's own observation of the edge, not a new kind.
    "read_dependency": EvidenceKind.TRACE,
    "read_recent_changes": EvidenceKind.CHANGE,
    "read_runbook": EvidenceKind.RUNBOOK,
}

# The single deterministic rule that may authorise a side effect without a human
# approval. It is named here so the gateway can refuse any other claimed
# pre-authorisation instead of trusting a bare ``requires_approval`` flag.
ALLOW_LISTED_AUTONOMOUS_RULE = "ALLOW_LISTED_REMEDIATION"


class UnknownTool(RuntimeError):
    pass


class TargetMismatch(ValueError):
    """A write proposal named one target and carried another in its arguments."""


class NotAnActionTool(UnknownTool):
    """A registered tool that is not a write tool was proposed as an action."""


def _require_write_tool(spec: ToolSpec) -> None:
    """Only write tools may travel the action path.

    Investigation reads belong to the controller's own read path. A read proposed as an
    action used to be claimed and handed to the write dispatcher (deepseek-flash, P1-02,
    2026-10-17), which called ``apply_remediation`` with a read's arguments.
    """
    if spec.side_effect is not SideEffect.WRITE:
        raise NotAnActionTool(
            f"{spec.name!r} is not a write tool, so it cannot be proposed as an action"
        )


def _bind_target(proposal: ActionProposal, arguments: dict[str, Any]) -> None:
    """The target policy evaluates must be the target the executor acts on.

    Policy, approval binding and the audit trail all use ``proposal.target``, while the
    adapter is called with the validated arguments. A proposal whose arguments name a
    different target would be authorised for one service and executed against another;
    in the Ad incident that was an unapproved change to Payment.
    """
    bound = arguments.get("target")
    if bound is not None and bound != proposal.target:
        raise TargetMismatch(
            f"{proposal.tool_name} target {proposal.target!r} does not match the "
            f"argument target {bound!r}"
        )


class PolicyBlocked(RuntimeError):
    def __init__(self, decision: PolicyDecisionModel) -> None:
        super().__init__("; ".join(decision.reasons) or decision.matched_rule)
        self.decision = decision


@dataclass(frozen=True)
class ReadOutcome:
    result: ToolResult
    evidence_id: str


@dataclass(frozen=True)
class ActionExecution:
    action: Action
    executed: bool
    detail: str


@dataclass(frozen=True)
class NeedCheck:
    """Whether an approved action is still needed, decided just before dispatch."""

    recovered: bool
    detail: str
    evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class _VerificationRound:
    """One round of fresh verification reads, and what it established."""

    evidence_ids: list[str]
    collected_after_action: bool
    blocked: VerificationBlock | None
    health: ReadOutcome | None
    metrics_ok: bool
    dependency: DependencyVerdict | None = None


@dataclass(frozen=True)
class VerificationBlock:
    """A verification read that could not be taken, with its cause kept apart.

    ``detail`` is the sentence a human reads; ``cause`` is the same fact as
    structure. They are built together so the readable text cannot drift into
    claiming the wrong actor: a hard cap is not a human control, and a human
    control is not a budget wall.
    """

    cause: RefusalCause
    detail: str


def _verification_block(
    *,
    cause: RefusalCause,
    subject: str,
    plural: bool,
    reason: BaseException,
) -> VerificationBlock:
    """Describe a refused verification read without misattributing its cause."""
    if cause is RefusalCause.BUDGET:
        detail = f"the {subject} {'are' if plural else 'is'} no longer affordable ({reason})"
    elif cause is RefusalCause.HUMAN_CONTROL:
        detail = f"a human control blocked the {subject} ({reason})"
    else:
        detail = f"policy refused the {subject} ({reason})"
    return VerificationBlock(cause=cause, detail=detail)


class ToolGateway:
    def __init__(
        self,
        *,
        db: Database,
        environment: Environment,
        registry: ToolRegistry | None = None,
        settings: Settings,
    ) -> None:
        self.db = db
        self.environment = environment
        self.registry = registry or ToolRegistry.default()
        self.settings = settings
        # Provenance is stamped from the adapter that produced the result, so a live
        # run can never be persisted as simulated evidence, or the reverse.
        self.is_simulated = bool(environment.is_simulated)

    # --- read path --------------------------------------------------------

    def read(
        self,
        *,
        incident_id: str,
        workflow_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        target: str,
        budget_class: str = "investigation",
    ) -> ReadOutcome:
        """Execute a read-only tool with bounded retries, then persist evidence."""
        spec = self.registry.get(tool_name)
        if spec is None:
            raise UnknownTool(f"{tool_name!r} is not a registered typed tool")
        if spec.side_effect is not SideEffect.READ:
            raise UnknownTool(f"{tool_name!r} is not a read-only tool")
        # The typed registry is the single gate for arguments, reads included.
        # Binding the executed target to the validated argument keeps the value
        # policy approves and the value the adapter reads the same value.
        arguments = spec.validate_arguments(arguments)
        if arguments["target"] != target:
            raise ValueError(
                f"read target {target!r} does not match the validated argument target "
                f"{arguments['target']!r}"
            )

        with self.db.session() as session:
            repo.consume_step(
                session,
                incident_id=incident_id,
                kind="tool",
                actor="tool-gateway",
                allow_over_soft=budget_class == "verification",
            )
            workflow = repo.get_workflow(session, incident_id)
            incident = repo.get_incident(session, incident_id)
            service = repo.get_service(session, target)
            constraints = tuple(repo.list_active_constraints(session, incident_id))
            assert workflow is not None and incident is not None
            decision = policy_engine.evaluate(
                policy_engine.PolicyRequest(
                    proposal=ActionProposal(
                        tool_name=tool_name, target=target, arguments=arguments, rationale="read"
                    ),
                    tool=spec,
                    service=service,
                    workflow=workflow,
                    incident_service=incident.service,
                    constraints=constraints,
                    rejected_intents=frozenset(),
                    budget=policy_engine.budget_state(workflow),
                    settings=self.settings,
                )
            )
            if not decision.allowed:
                raise PolicyBlocked(decision)

        started = utcnow()
        attempts = 0
        deadline = time.monotonic() + max(self.settings.read_deadline_seconds, 0.05)
        last_error: str | None = None
        result: ToolResult | None = None
        max_attempts = min(spec.max_attempts, self.settings.read_attempts)

        while attempts < max_attempts and time.monotonic() <= deadline:
            attempts += 1
            try:
                result = self._invoke_read(spec.name, arguments, spec.timeout_seconds)
                break
            except ReadTimeout as exc:
                last_error = str(exc)
            except Exception as exc:  # adapter faults are evidence, not crashes
                last_error = f"{type(exc).__name__}: {exc}"
                break

        finished = utcnow()
        if result is None:
            result = ToolResult(
                tool_name=spec.name,
                target=target,
                succeeded=False,
                error=last_error or "read did not complete inside its budget",
                attempts=attempts,
                is_simulated=self.is_simulated,
                collected_at=finished,
            )
        else:
            result = result.model_copy(update={"attempts": attempts})

        with self.db.session() as session:
            tool_call = repo.record_tool_call(
                session,
                incident_id=incident_id,
                workflow_id=workflow_id,
                tool_name=spec.name,
                target=target,
                arguments=arguments,
                side_effect=SideEffect.READ,
                attempt=attempts,
                succeeded=result.succeeded,
                result=result.value,
                error=result.error,
                started_at=started,
                finished_at=finished,
                is_simulated=self.is_simulated,
            )
            kind = (
                _EVIDENCE_KIND.get(spec.name, EvidenceKind.LOG)
                if result.succeeded
                else EvidenceKind.COLLECTION_ERROR
            )
            summary = (
                f"{spec.name} on {target}: "
                + (", ".join(f"{k}={v}" for k, v in list(result.value.items())[:3]) or "ok")
                if result.succeeded
                else f"{spec.name} on {target} failed after {attempts} attempt(s): {result.error}"
            )
            evidence = repo.add_evidence(
                session,
                incident_id=incident_id,
                workflow_id=workflow_id,
                kind=kind,
                source=spec.name,
                service=target,
                summary=summary[:600],
                value=result.value if result.succeeded else {"error": result.error},
                observation_window_start=result.observation_window_start,
                observation_window_end=result.observation_window_end,
                tool_call_id=tool_call.id,
                is_simulated=self.is_simulated,
                collected_at=result.collected_at,
            )
        return ReadOutcome(result=result, evidence_id=evidence.id)

    def _invoke_read(self, tool_name: str, arguments: dict[str, Any], timeout: float) -> ToolResult:
        env = self.environment
        if tool_name == "read_metrics":
            return env.read_metrics(
                target=arguments["target"],
                metric=arguments["metric"],
                window_seconds=arguments["window_seconds"],
                timeout_seconds=timeout,
            )
        if tool_name == "read_traces":
            return env.read_traces(
                target=arguments["target"],
                operation=arguments["operation"],
                window_seconds=arguments["window_seconds"],
                timeout_seconds=timeout,
            )
        if tool_name == "read_service_health":
            return env.read_service_health(target=arguments["target"], timeout_seconds=timeout)
        if tool_name == "read_dependency":
            return env.read_dependency(
                target=arguments["target"],
                dependency=arguments["dependency"],
                window_seconds=arguments["window_seconds"],
                timeout_seconds=timeout,
            )
        if tool_name == "read_recent_changes":
            return env.read_recent_changes(target=arguments["target"], timeout_seconds=timeout)
        if tool_name == "read_runbook":
            return env.read_runbook(target=arguments["target"], timeout_seconds=timeout)
        raise UnknownTool(tool_name)

    # --- action path ------------------------------------------------------

    def evaluate_action(
        self, *, incident_id: str, proposal: ActionProposal
    ) -> tuple[PolicyDecisionModel, dict[str, Any]]:
        """Validate the typed arguments, then apply deterministic policy."""
        spec = self.registry.get(proposal.tool_name)
        if spec is None:
            raise UnknownTool(f"{proposal.tool_name!r} is not a registered typed tool")
        _require_write_tool(spec)
        raw = dict(proposal.arguments)
        if "target" in spec.arguments_schema:
            # The proposal already names its target; a model that does not repeat it
            # inside the arguments is not refused for that. A *different* value is.
            raw.setdefault("target", proposal.target)
        arguments = spec.validate_arguments(raw)
        _bind_target(proposal, arguments)
        with self.db.session() as session:
            workflow = repo.get_workflow(session, incident_id)
            incident = repo.get_incident(session, incident_id)
            if workflow is None or incident is None:
                raise repo.RepositoryError(f"no workflow for incident {incident_id}")
            decision = policy_engine.evaluate(
                policy_engine.PolicyRequest(
                    proposal=proposal,
                    tool=spec,
                    service=repo.get_service(session, proposal.target),
                    workflow=workflow,
                    incident_service=incident.service,
                    constraints=tuple(repo.list_active_constraints(session, incident_id)),
                    rejected_intents=frozenset(
                        repo.rejected_intents(session, workflow_id=workflow.id)
                    ),
                    budget=policy_engine.budget_state(workflow),
                    settings=self.settings,
                    target_telemetry=repo.count_target_telemetry(
                        session, incident_id=incident_id, target=proposal.target
                    ),
                )
            )
        return decision, arguments

    def request_action(
        self,
        *,
        incident_id: str,
        workflow_id: str,
        proposal: ActionProposal,
        decision: PolicyDecisionModel,
        arguments: dict[str, Any],
        decision_id: str | None,
    ) -> Action:
        """Persist an approved-or-awaiting-approval action. Never dispatches."""
        if not decision.allowed:
            raise PolicyBlocked(decision)
        # Checked again where the action is written, so no caller can persist an action
        # whose recorded target differs from the one it would dispatch to.
        _bind_target(proposal, arguments)
        spec = self.registry.require(proposal.tool_name)
        _require_write_tool(spec)
        # The policy engine is the authority on whether a human must approve this
        # exact action. The catalog attribute is a static floor for tools that
        # always need a human, and it is deliberately narrowed for the one
        # deterministic rule that pre-authorises allow-listed low-risk targets:
        # without that narrowing the allow-listed autonomous path could never run.
        requires_approval = decision.requires_approval or (
            spec.requires_approval and decision.matched_rule != ALLOW_LISTED_AUTONOMOUS_RULE
        )
        with self.db.session() as session:
            existing = repo.find_action_by_intent(
                session,
                workflow_id=workflow_id,
                intent_digest=Action.compute_intent_digest(
                    proposal.tool_name, proposal.target, arguments
                ),
            )
            if existing and existing.status not in {
                ActionStatus.FAILED,
                ActionStatus.CANCELLED,
                ActionStatus.REJECTED,
            }:
                # Retrying the same intent reuses its existing action id.
                return existing
            action = repo.create_action(
                session,
                incident_id=incident_id,
                workflow_id=workflow_id,
                tool_name=proposal.tool_name,
                target=proposal.target,
                arguments=arguments,
                rationale=proposal.rationale,
                decision_id=decision_id,
                requires_approval=requires_approval,
                is_simulated=self.is_simulated,
            )
        return action

    def execute_action(self, *, action_id: str, actor: str = "tool-gateway") -> ActionExecution:
        """Claim, revalidate, dispatch once, and record the outcome.

        The transaction below is the execution commitment point described in the
        module docstring. The order of the two locks is fixed: control gate, then
        incident row. The gate is what makes a *global* control (one with no
        incident row to lock) ordered against this authorization; the incident row
        is what orders an incident-scoped control or a takeover against it.
        """
        controls: tuple[Constraint, ...] = ()
        with self.db.session() as session:
            # The gate is taken before anything else in this transaction, and
            # before the incident row.
            repo.lock_control_gate(session)
            action = repo.get_action(session, action_id)
            if action is None:
                raise repo.RepositoryError(f"unknown action {action_id}")
            action_spec = self.registry.get(action.tool_name)
            if action_spec is None or action_spec.side_effect is not SideEffect.WRITE:
                # Defence in depth, before anything is claimed: an action row written
                # by any path other than prepare_action still cannot reach the write
                # dispatcher unless its tool is a registered write tool.
                return ActionExecution(
                    action, False, f"{action.tool_name!r} is not a registered write tool"
                )
            # Serialise against human controls: from here until the claim commits,
            # a concurrent takeover or constraint either lands before this
            # revalidation (and blocks the dispatch) or waits for it. There is no
            # window in between for a control to be ignored.
            repo.lock_incident(session, action.incident_id)
            # Read the control set once, under both locks, and evaluate exactly
            # what was read. The same snapshot is recorded on the claim, so the
            # audit says which controls the commitment was made against.
            controls = tuple(repo.list_active_constraints(session, action.incident_id))
            blocked = self._revalidate(session, action, constraints=controls)
            if blocked is not None:
                return ActionExecution(action, False, blocked)
            claim = repo.claim_action(
                session,
                action_id=action_id,
                actor=actor,
                commit_context={
                    "evaluated_constraints": [
                        {
                            "id": constraint.id,
                            "kind": str(constraint.kind),
                            "scope": "global" if constraint.incident_id is None else "incident",
                        }
                        for constraint in controls
                    ]
                },
            )
            if not claim.claimed:
                return ActionExecution(
                    claim.action or action, False, claim.reason or "not claimable"
                )
            claimed_action = claim.action
            assert claimed_action is not None
        # Transaction committed: we are the only worker holding this action, and
        # no control that commits from here on can cancel it.

        spec = self.registry.require(claimed_action.tool_name)
        started = utcnow()
        status: ActionStatus
        outcome: ActionOutcome | None
        result_value: dict[str, Any] = {}
        error: str | None = None
        try:
            tool_result = self.environment.apply_remediation(
                idempotency_key=claimed_action.idempotency_key,
                timeout_seconds=spec.timeout_seconds,
                **claimed_action.arguments,
            )
            result_value = dict(tool_result.value)
            if tool_result.succeeded:
                status, outcome = ActionStatus.SUCCEEDED, ActionOutcome.CONFIRMED_APPLIED
            else:
                # The adapter's contract: succeeded=False means the change provably
                # did not happen. Both shipped adapters refuse before they write.
                status, outcome = ActionStatus.FAILED, ActionOutcome.CONFIRMED_NOT_APPLIED
                error = tool_result.error
        except Exception as exc:
            # Every raised error is classified in one place, so this handler cannot
            # drift from the outcome contract: a proven pre-dispatch refusal is a
            # definite non-execution, and anything the gateway cannot classify -
            # including an unexpected error after the adapter already applied the
            # change - stays UNCERTAIN. Never retry an uncertain action; reconcile.
            failure = classify_dispatch_failure(exc)
            status, outcome, error = failure.status, failure.outcome, failure.error

        finished = utcnow()
        with self.db.session() as session:
            repo.record_tool_call(
                session,
                incident_id=claimed_action.incident_id,
                workflow_id=claimed_action.workflow_id,
                tool_name=claimed_action.tool_name,
                target=claimed_action.target,
                arguments=claimed_action.arguments,
                side_effect=SideEffect.WRITE,
                attempt=claimed_action.attempt,
                succeeded=status is ActionStatus.SUCCEEDED,
                result=result_value,
                error=error,
                started_at=started,
                finished_at=finished,
                is_simulated=self.is_simulated,
            )
            updated = repo.finish_action(
                session,
                action_id=claimed_action.id,
                status=status,
                result=result_value,
                error=error,
                external_outcome=outcome,
            )
            # Dispatching and recording an already-approved action is safety
            # critical: it may exceed the soft investigation budget, never the cap.
            # The side effect has already happened by now, so a budget refusal must
            # not roll back the tool-call ledger row or the action's final status.
            # Losing those would leave an applied change with no record and an
            # action stuck in DISPATCHING; an uncounted step is the lesser evil and
            # it is reported instead of hidden.
            budget_note: str | None = None
            try:
                repo.consume_step(
                    session,
                    incident_id=claimed_action.incident_id,
                    kind="tool",
                    actor="tool-gateway",
                    allow_over_soft=True,
                )
            except repo.BudgetExhausted as exc:
                budget_note = str(exc)
        detail = {
            ActionStatus.SUCCEEDED: "remediation dispatched and acknowledged",
            ActionStatus.FAILED: f"remediation reported failure: {error}",
            ActionStatus.UNCERTAIN: (
                "dispatch outcome is unknown; reconcile it before resolving or retrying"
            ),
        }[status]
        if budget_note:
            detail = f"{detail}; budget: {budget_note}"
        return ActionExecution(updated, True, detail)

    def _revalidate(
        self, session: Any, action: Action, *, constraints: tuple[Constraint, ...]
    ) -> str | None:
        """Immediately-before-execution policy revalidation from persisted rows.

        ``constraints`` is the control set read by the caller under the control
        gate and the incident row lock, so the cheap checks below and the policy
        engine both evaluate one consistent snapshot rather than two reads that a
        concurrent control could land between.
        """
        workflow = repo.get_workflow(session, action.incident_id)
        if workflow is None:
            return "workflow disappeared"
        if workflow.takeover_actor:
            return f"human takeover active ({workflow.takeover_actor})"
        if workflow.status in {
            IncidentStatus.RESOLVED,
            IncidentStatus.READY_FOR_HUMAN,
            IncidentStatus.ESCALATED_INCOMPLETE,
        }:
            return f"workflow is already terminal ({workflow.status})"
        for constraint in constraints:
            if constraint.kind is ConstraintKind.FREEZE_ACTIONS:
                return "freeze-actions constraint is active"
            if constraint.kind is ConstraintKind.REQUIRE_MANUAL:
                return "manual-handling constraint is active"
            if constraint.kind is ConstraintKind.DENY_TARGET and constraint.target == action.target:
                return f"constraint {constraint.id} forbids target {action.target}"
            if (
                constraint.kind is ConstraintKind.DENY_ACTION
                and constraint.tool_name == action.tool_name
            ):
                return f"constraint {constraint.id} forbids tool {action.tool_name}"
        if action.intent_digest in repo.rejected_intents(session, workflow_id=action.workflow_id):
            return "this exact action intent was already rejected by a human"
        if action.status is ActionStatus.AWAITING_APPROVAL:
            return "action is still waiting for approval"
        # The cheap checks above are a subset of policy. Re-run the whole
        # deterministic engine against the rows as they are now: service posture,
        # the target and argument allow-lists, the service/target binding, the
        # rejected-intent set and the hard budget wall all have to be true at the
        # instant of dispatch, not only when the action was requested.
        incident = repo.get_incident(session, action.incident_id)
        spec = self.registry.get(action.tool_name)
        if spec is None:
            return f"{action.tool_name!r} is no longer a registered typed tool"
        decision = policy_engine.evaluate(
            policy_engine.PolicyRequest(
                proposal=ActionProposal(
                    tool_name=action.tool_name,
                    target=action.target,
                    arguments=action.arguments,
                    rationale=action.rationale,
                ),
                tool=spec,
                service=repo.get_service(session, action.target),
                workflow=workflow,
                incident_service=incident.service if incident else action.target,
                constraints=constraints,
                rejected_intents=frozenset(repo.rejected_intents(session, workflow_id=workflow.id)),
                budget=policy_engine.budget_state(workflow),
                settings=self.settings,
                target_telemetry=repo.count_target_telemetry(
                    session, incident_id=action.incident_id, target=action.target
                ),
            )
        )
        if not decision.allowed:
            return "policy revalidation blocked the dispatch: " + "; ".join(decision.reasons)
        # An approval-backed action carries the approval row that claim_action
        # consumes; an autonomous one carries none. If policy requires approval
        # now and none is bound to this action, the dispatch is not authorised.
        if decision.requires_approval and action.approval_id is None:
            return (
                "policy revalidation now requires a human approval for this action; "
                "dispatch is blocked"
            )
        return None

    def reconcile_action(self, *, action_id: str) -> Action:
        """Resolve an uncertain side effect by asking the external system."""
        with self.db.session() as session:
            action = repo.get_action(session, action_id)
            if action is None:
                raise repo.RepositoryError(f"unknown action {action_id}")
            if action.status is not ActionStatus.UNCERTAIN:
                return action
            outcome = self.environment.action_status(action.idempotency_key)
            if outcome is ActionOutcome.CONFIRMED_APPLIED:
                return repo.finish_action(
                    session,
                    action_id=action_id,
                    status=ActionStatus.SUCCEEDED,
                    result=action.result,
                    external_outcome=outcome,
                    actor="reconciler",
                )
            if outcome is ActionOutcome.CONFIRMED_NOT_APPLIED:
                return repo.finish_action(
                    session,
                    action_id=action_id,
                    status=ActionStatus.FAILED,
                    error="reconciled: the external system confirms the change was not applied",
                    external_outcome=outcome,
                    actor="reconciler",
                )
            return action

    # --- verification -----------------------------------------------------

    def _collect_verification_evidence(
        self,
        *,
        incident_id: str,
        workflow_id: str,
        target: str,
        action_completed_at: datetime | None,
    ) -> _VerificationRound:
        """One round of fresh, service-specific verification reads.

        Recovery is verified on the same path the incident was diagnosed on. When
        the target sits behind an allow-listed dependency edge, a healthy target
        probe is not enough: the caller's own calls to it have to succeed too,
        because a target can read healthy while the caller still cannot reach it.
        """
        evidence_ids: list[str] = []
        collected_after_action = False
        blocked: VerificationBlock | None = None
        health: ReadOutcome | None = None
        metrics_ok = True
        dependency: DependencyVerdict | None = None

        try:
            health = self.read(
                incident_id=incident_id,
                workflow_id=workflow_id,
                tool_name="read_service_health",
                arguments={"target": target},
                target=target,
                budget_class="verification",
            )
        except repo.BudgetExhausted as exc:
            # The hard cap can land between the action and its verification. The
            # service state is then unmeasured, which is not a recovery: leave the
            # health probe unset so the verdict is inconclusive and the workflow
            # hands off, rather than failing the run with an exception.
            blocked = _verification_block(
                cause=RefusalCause.BUDGET,
                subject="verification reads",
                plural=True,
                reason=exc,
            )
        except PolicyBlocked as exc:
            # A human control (a freeze or a manual-handling constraint) can land
            # between the action and its verification. That is a decision, not a
            # crash: report an unverified outcome and let the controller escalate.
            # The cause comes from the verdict's structured rule id, not from this
            # sentence: a budget wall is refused here too, and must not be
            # described as a human control.
            blocked = _verification_block(
                cause=policy_engine.refusal_cause(exc.decision),
                subject="verification reads",
                plural=True,
                reason=exc,
            )

        if health is not None:
            evidence_ids.append(health.evidence_id)
            # "Fresh" only means something relative to a completed action. With no
            # completed action there is nothing for this evidence to be fresh with
            # respect to, so it cannot be a verified recovery of anything.
            if (
                action_completed_at is not None
                and health.result.collected_at >= action_completed_at
            ):
                collected_after_action = True

            if health.result.succeeded:
                for metric in ("cpu_utilization", "error_rate"):
                    try:
                        outcome = self.read(
                            incident_id=incident_id,
                            workflow_id=workflow_id,
                            tool_name="read_metrics",
                            arguments={
                                "target": target,
                                "metric": metric,
                                "window_seconds": self.settings.verification_window_seconds,
                            },
                            target=target,
                            budget_class="verification",
                        )
                    except repo.BudgetExhausted as exc:
                        blocked = _verification_block(
                            cause=RefusalCause.BUDGET,
                            subject="verification reads",
                            plural=True,
                            reason=exc,
                        )
                        break
                    except PolicyBlocked as exc:
                        blocked = _verification_block(
                            cause=policy_engine.refusal_cause(exc.decision),
                            subject="verification reads",
                            plural=True,
                            reason=exc,
                        )
                        break
                    evidence_ids.append(outcome.evidence_id)
                    metrics_ok = metrics_ok and outcome.result.succeeded
            else:
                metrics_ok = False

        # A round that has already been refused has its verdict. Reading on would
        # spend budget on a round that cannot settle, and a later refusal would
        # overwrite the cause with its own -- so the stated cause stays the one
        # that actually stopped the round.
        callers = dependency_callers(target)
        if callers and blocked is None:
            caller = callers[0]
            try:
                edge = self.read(
                    incident_id=incident_id,
                    workflow_id=workflow_id,
                    tool_name="read_dependency",
                    arguments={
                        "target": caller,
                        "dependency": target,
                        "window_seconds": self.settings.verification_window_seconds,
                    },
                    target=caller,
                    budget_class="verification",
                )
            except repo.BudgetExhausted as exc:
                # The round is already unaffordable, so stop settling instead of
                # spending the remaining attempts on reads that cannot happen.
                blocked = _verification_block(
                    cause=RefusalCause.BUDGET,
                    subject="dependency-path verification read",
                    plural=False,
                    reason=exc,
                )
                dependency = DependencyVerdict(
                    state="indeterminate",
                    detail=(
                        f"{blocked.detail}, so recovery was not established on the affected path"
                    ),
                )
            except PolicyBlocked as exc:
                blocked = _verification_block(
                    cause=policy_engine.refusal_cause(exc.decision),
                    subject="dependency-path verification read",
                    plural=False,
                    reason=exc,
                )
                dependency = DependencyVerdict(
                    state="indeterminate",
                    detail=(
                        f"{blocked.detail}, so recovery was not established on the affected path"
                    ),
                )
            else:
                evidence_ids.append(edge.evidence_id)
                if not (
                    action_completed_at is not None
                    and edge.result.collected_at >= action_completed_at
                ):
                    collected_after_action = False
                if not edge.result.succeeded:
                    dependency = DependencyVerdict(
                        state="indeterminate",
                        detail=(
                            "the dependency-path verification read failed "
                            f"({edge.result.error}), so recovery was not established"
                        ),
                    )
                else:
                    dependency = classify_dependency_edge(edge.result.value)

        return _VerificationRound(
            evidence_ids=evidence_ids,
            collected_after_action=collected_after_action,
            blocked=blocked,
            health=health,
            metrics_ok=metrics_ok,
            dependency=dependency,
        )

    def assess_need(self, *, incident_id: str, workflow_id: str, target: str) -> NeedCheck:
        """Has the target recovered on its own while its fix waited for approval?

        Two reads, charged as verification: the target's health probe and, where the
        target sits behind an allow-listed edge, the caller's own calls to it, which is
        the path the incident was diagnosed on. Only a *proven* recovery returns
        ``recovered=True``. Anything else, a failing or unproven path, a failed read or a
        refused one, returns False, and the human's approval stands. A recovered verdict
        here never resolves anything by itself: the full verification still has to
        confirm it.
        """
        evidence: list[str] = []
        try:
            health = self.read(
                incident_id=incident_id,
                workflow_id=workflow_id,
                tool_name="read_service_health",
                arguments={"target": target},
                target=target,
                budget_class="verification",
            )
        except (repo.BudgetExhausted, PolicyBlocked) as exc:
            return NeedCheck(False, f"need check could not read {target}: {exc}")
        evidence.append(health.evidence_id)
        value = health.result.value
        if not (health.result.succeeded and bool(value.get("healthy"))):
            return NeedCheck(
                False,
                f"{target} still needs the fix: health={value.get('health')} "
                f"error_rate={value.get('error_rate')}",
                tuple(evidence),
            )
        callers = dependency_callers(target)
        if callers:
            caller = callers[0]
            try:
                edge = self.read(
                    incident_id=incident_id,
                    workflow_id=workflow_id,
                    tool_name="read_dependency",
                    arguments={
                        "target": caller,
                        "dependency": target,
                        "window_seconds": self.settings.verification_window_seconds,
                    },
                    target=caller,
                    budget_class="verification",
                )
            except (repo.BudgetExhausted, PolicyBlocked) as exc:
                return NeedCheck(
                    False, f"need check could not read {caller} -> {target}: {exc}", tuple(evidence)
                )
            evidence.append(edge.evidence_id)
            if not edge.result.succeeded:
                return NeedCheck(
                    False,
                    f"{caller} -> {target} could not be read ({edge.result.error})",
                    tuple(evidence),
                )
            verdict = classify_dependency_edge(edge.result.value)
            if not verdict.healthy:
                return NeedCheck(
                    False,
                    f"{caller} -> {target} is not proven recovered: {verdict.detail}",
                    tuple(evidence),
                )
        return NeedCheck(
            True,
            f"{target} recovered while the fix waited for approval: health="
            f"{value.get('health')} error_rate={value.get('error_rate')}"
            + (f"; {callers[0]} -> {target} calls are succeeding" if callers else ""),
            tuple(evidence),
        )

    def verify_service(
        self,
        *,
        incident_id: str,
        workflow_id: str,
        target: str,
        action_id: str | None,
        action_completed_at: datetime | None,
    ) -> VerificationResult:
        """Require fresh, service-specific evidence before RESOLVED.

        Real telemetry needs a convergence window. A PromQL rate() over a fixed
        lookback still contains the fault for as long as the lookback spans it, so
        a probe taken the instant after a fix can legitimately still report the
        service as unhealthy -- and treating that as a failed recovery would be
        just as wrong as calling it resolved. ``verification_settle_attempts`` and
        ``verification_settle_seconds`` re-collect fresh evidence a bounded number
        of times before the verdict is recorded; the default is one attempt and no
        delay, which is exactly the previous behaviour.

        This refreshes *evidence* only. It never dispatches, claims, or retries a
        side effect, so a bounded settle cannot become a repeat of the action.
        """
        attempts = max(1, self.settings.verification_settle_attempts)
        settle_seconds = max(0.0, self.settings.verification_settle_seconds)
        evidence_ids: list[str] = []
        collected_after_action = False
        blocked: VerificationBlock | None = None
        health: ReadOutcome | None = None
        metrics_ok = True

        for attempt in range(attempts):
            if attempt and settle_seconds:
                time.sleep(settle_seconds)
            fresh = self._collect_verification_evidence(
                incident_id=incident_id,
                workflow_id=workflow_id,
                target=target,
                action_completed_at=action_completed_at,
            )
            evidence_ids = fresh.evidence_ids
            collected_after_action = fresh.collected_after_action
            blocked = fresh.blocked
            health = fresh.health
            metrics_ok = fresh.metrics_ok
            dependency = fresh.dependency
            if blocked is not None:
                break
            if (
                health is not None
                and health.result.succeeded
                and metrics_ok
                and bool(health.result.value.get("healthy"))
                and (dependency is None or dependency.healthy)
            ):
                break

        if blocked is not None:
            result = VerificationResult(
                outcome=VerificationOutcome.VERIFICATION_INCONCLUSIVE,
                service=target,
                evidence_ids=tuple(evidence_ids),
                detail=f"verification is inconclusive: {blocked.detail}",
                checked_at=utcnow(),
            )
        elif health is None or not health.result.succeeded or not metrics_ok:
            result = VerificationResult(
                outcome=VerificationOutcome.VERIFICATION_INCONCLUSIVE,
                service=target,
                evidence_ids=tuple(evidence_ids),
                detail=(
                    "fresh health/metrics evidence could not be collected after the action; "
                    "an unresolved verification is not a recovery"
                ),
                checked_at=utcnow(),
            )
        elif not collected_after_action:
            result = VerificationResult(
                outcome=VerificationOutcome.VERIFICATION_INCONCLUSIVE,
                service=target,
                evidence_ids=tuple(evidence_ids),
                detail="no evidence collected at or after the action completed",
                checked_at=utcnow(),
            )
        elif dependency is not None and dependency.failing:
            # The flag is off and the target's own probe recovered, but the caller
            # still cannot complete the operation. A path that is still failing is
            # not a recovery, so this can never be RESOLVED.
            result = VerificationResult(
                outcome=VerificationOutcome.VERIFICATION_FAILURE,
                service=target,
                evidence_ids=tuple(evidence_ids),
                detail=(
                    "the affected dependency path is still failing after the action: "
                    f"{dependency.detail}; target probe health="
                    f"{health.result.value.get('health')} error_rate="
                    f"{health.result.value.get('error_rate')}"
                ),
                checked_at=utcnow(),
            )
        elif dependency is not None and dependency.indeterminate:
            # Unproven is not recovered: no traffic, no attribution, or a missing
            # share leaves the verdict inconclusive and hands off.
            result = VerificationResult(
                outcome=VerificationOutcome.VERIFICATION_INCONCLUSIVE,
                service=target,
                evidence_ids=tuple(evidence_ids),
                detail=f"recovery is unproven on the affected dependency path: {dependency.detail}",
                checked_at=utcnow(),
            )
        elif bool(health.result.value.get("healthy")):
            result = VerificationResult(
                outcome=VerificationOutcome.VERIFICATION_SUCCESS,
                service=target,
                evidence_ids=tuple(evidence_ids),
                detail=(
                    "fresh health probe reports SERVING with cpu_utilization "
                    f"{health.result.value.get('cpu_utilization')} and error_rate "
                    f"{health.result.value.get('error_rate')}"
                ),
                checked_at=utcnow(),
            )
        else:
            result = VerificationResult(
                outcome=VerificationOutcome.VERIFICATION_FAILURE,
                service=target,
                evidence_ids=tuple(evidence_ids),
                detail=(
                    "the service did not recover: health="
                    f"{health.result.value.get('health')} reachable="
                    f"{health.result.value.get('reachable')} cpu_utilization="
                    f"{health.result.value.get('cpu_utilization')} error_rate="
                    f"{health.result.value.get('error_rate')}"
                ),
                checked_at=utcnow(),
            )

        with self.db.session() as session:
            repo.add_verification(
                session,
                incident_id=incident_id,
                workflow_id=workflow_id,
                service=target,
                result=result,
                action_id=action_id,
            )
        return result
