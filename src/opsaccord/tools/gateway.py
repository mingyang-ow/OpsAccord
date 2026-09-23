"""The tool gateway: the only component that touches the demo environment.

Design rules enforced here:

* Typed tools only. Arguments are validated against the registry before any
  executor sees them, so there is no arbitrary shell, URL, or container path.
* Read tools retry within an explicit attempt and deadline budget. Side-effect
  tools never blind-retry: they claim, dispatch once, and reconcile.
* The action row is claimed with a conditional UPDATE inside a transaction, and
  the approval is consumed in that same transaction, so a consumed approval can
  never authorise a second dispatch.
* Policy is revalidated immediately before the side effect, from persisted rows.

Exactly-once is *not* guaranteed by a unique row. The demo adapter is idempotent
on the action id; when the adapter cannot report an outcome, the action is left
UNCERTAIN and escalated rather than retried.
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
    ToolResult,
    VerificationResult,
)
from opsaccord.domain.models import (
    PolicyDecision as PolicyDecisionModel,
)
from opsaccord.ids import utcnow
from opsaccord.persistence import repositories as repo
from opsaccord.persistence.db import Database
from opsaccord.policy import engine as policy_engine
from opsaccord.tools.contracts import DispatchTimeout, Environment, ReadTimeout
from opsaccord.tools.registry import ToolRegistry

_EVIDENCE_KIND = {
    "read_metrics": EvidenceKind.METRIC,
    "read_traces": EvidenceKind.TRACE,
    "read_service_health": EvidenceKind.HEALTH,
    "read_recent_changes": EvidenceKind.CHANGE,
    "read_runbook": EvidenceKind.RUNBOOK,
}

# The single deterministic rule that may authorise a side effect without a human
# approval. It is named here so the gateway can refuse any other claimed
# pre-authorisation instead of trusting a bare ``requires_approval`` flag.
ALLOW_LISTED_AUTONOMOUS_RULE = "ALLOW_LISTED_REMEDIATION"


class UnknownTool(RuntimeError):
    pass


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
        arguments = spec.validate_arguments(proposal.arguments)
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
        spec = self.registry.require(proposal.tool_name)
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
        """Claim, revalidate, dispatch once, and record the outcome."""
        with self.db.session() as session:
            action = repo.get_action(session, action_id)
            if action is None:
                raise repo.RepositoryError(f"unknown action {action_id}")
            # Serialise against human controls: from here until the claim commits,
            # a concurrent takeover or constraint either lands before this
            # revalidation (and blocks the dispatch) or waits for it. There is no
            # window in between for a control to be ignored.
            repo.lock_incident(session, action.incident_id)
            blocked = self._revalidate(session, action)
            if blocked is not None:
                return ActionExecution(action, False, blocked)
            claim = repo.claim_action(session, action_id=action_id, actor=actor)
            if not claim.claimed:
                return ActionExecution(
                    claim.action or action, False, claim.reason or "not claimable"
                )
            claimed_action = claim.action
            assert claimed_action is not None
        # Transaction committed: we are the only worker holding this action.

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
                status, outcome = ActionStatus.FAILED, ActionOutcome.CONFIRMED_NOT_APPLIED
                error = tool_result.error
        except DispatchTimeout as exc:
            # Dispatched, outcome unknown. Never retry; reconcile instead.
            status, outcome, error = ActionStatus.UNCERTAIN, ActionOutcome.UNKNOWN, str(exc)
        except Exception as exc:
            status, outcome = ActionStatus.FAILED, ActionOutcome.CONFIRMED_NOT_APPLIED
            error = f"{type(exc).__name__}: {exc}"

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
            ActionStatus.UNCERTAIN: "dispatch did not return; outcome must be reconciled",
        }[status]
        if budget_note:
            detail = f"{detail}; budget: {budget_note}"
        return ActionExecution(updated, True, detail)

    def _revalidate(self, session: Any, action: Action) -> str | None:
        """Immediately-before-execution policy revalidation from persisted rows."""
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
        for constraint in repo.list_active_constraints(session, action.incident_id):
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
                constraints=tuple(repo.list_active_constraints(session, action.incident_id)),
                rejected_intents=frozenset(repo.rejected_intents(session, workflow_id=workflow.id)),
                budget=policy_engine.budget_state(workflow),
                settings=self.settings,
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
    ) -> tuple[list[str], bool, str | None, ReadOutcome | None, bool]:
        """One round of fresh, service-specific verification reads."""
        evidence_ids: list[str] = []
        collected_after_action = False
        blocked: str | None = None
        health: ReadOutcome | None = None
        metrics_ok = True

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
            blocked = f"the verification reads are no longer affordable ({exc})"
        except PolicyBlocked as exc:
            # A human control (a freeze or a manual-handling constraint) can land
            # between the action and its verification. That is a decision, not a
            # crash: report an unverified outcome and let the controller escalate.
            blocked = f"a human control blocked the verification reads ({exc})"

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
                        blocked = f"the verification reads are no longer affordable ({exc})"
                        break
                    except PolicyBlocked as exc:
                        blocked = f"a human control blocked the verification reads ({exc})"
                        break
                    evidence_ids.append(outcome.evidence_id)
                    metrics_ok = metrics_ok and outcome.result.succeeded
            else:
                metrics_ok = False

        return evidence_ids, collected_after_action, blocked, health, metrics_ok

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
        blocked: str | None = None
        health: ReadOutcome | None = None
        metrics_ok = True

        for attempt in range(attempts):
            if attempt and settle_seconds:
                time.sleep(settle_seconds)
            (
                evidence_ids,
                collected_after_action,
                blocked,
                health,
                metrics_ok,
            ) = self._collect_verification_evidence(
                incident_id=incident_id,
                workflow_id=workflow_id,
                target=target,
                action_completed_at=action_completed_at,
            )
            if blocked is not None:
                break
            if (
                health is not None
                and health.result.succeeded
                and metrics_ok
                and bool(health.result.value.get("healthy"))
            ):
                break

        if blocked is not None:
            result = VerificationResult(
                outcome=VerificationOutcome.VERIFICATION_INCONCLUSIVE,
                service=target,
                evidence_ids=tuple(evidence_ids),
                detail=f"verification is inconclusive: {blocked}",
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
