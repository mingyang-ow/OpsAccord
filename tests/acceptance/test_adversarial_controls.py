"""Adversarial checks against the non-negotiable controls.

Approval mismatch, expiry and reuse, constraint enforcement, competing workers,
uncertain dispatch, crash recovery, exhausted budgets, malicious runbooks, and
false recovery. Each check asserts on persisted rows and on what the demo
environment actually observed.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import timedelta

import pytest
import sqlalchemy as sa

from opsaccord.config import Settings
from opsaccord.domain.enums import (
    ActionOutcome,
    ActionStatus,
    ApprovalStatus,
    ConstraintKind,
    IncidentStatus,
    SideEffect,
    VerificationOutcome,
)
from opsaccord.domain.models import Action, ActionProposal, Approval, ToolResult
from opsaccord.ids import utcnow
from opsaccord.persistence import repositories as repo
from opsaccord.persistence.db import Database
from opsaccord.persistence.tables import ApprovalRow, ToolCallRow, VerificationRow, WorkflowRow
from opsaccord.reasoning.deterministic import DeterministicAdapter
from opsaccord.scenarios.fixtures import SCENARIOS, ScenarioSpec, build_environment_for_scenario
from opsaccord.tools import demo_environment as demo
from opsaccord.tools.gateway import ToolGateway
from opsaccord.workflow.controller import WorkflowController

pytestmark = pytest.mark.integration


@dataclass
class Rig:
    environment: demo.DemoEnvironment
    gateway: ToolGateway
    controller: WorkflowController
    incident_id: str
    spec: ScenarioSpec


@pytest.fixture
def rig(db: Database, settings: Settings):
    def build(
        scenario_id: str,
        *,
        environment: demo.DemoEnvironment | None = None,
        **fault_overrides: object,
    ) -> Rig:
        spec = SCENARIOS[scenario_id]
        environment = environment or build_environment_for_scenario(spec)
        if fault_overrides:
            environment.faults = demo.FaultInjection(
                **{**vars(environment.faults), **fault_overrides}
            )
        gateway = ToolGateway(db=db, environment=environment, settings=settings)
        controller = WorkflowController(
            db=db, gateway=gateway, provider=DeterministicAdapter(), settings=settings
        )
        incident_id = controller.create_incident(
            title=spec.title, service=spec.service, severity=spec.severity, scenario_id=spec.id
        )
        return Rig(environment, gateway, controller, incident_id, spec)

    return build


def pending(db: Database, incident_id: str) -> Approval:
    with db.session() as session:
        approval = repo.pending_approval(session, incident_id)
    assert approval is not None
    return approval


def approve(db: Database, approval: Approval, approver: str = "on-call") -> None:
    with db.session() as session:
        repo.decide_approval(
            session,
            approval_id=approval.id,
            approver=approver,
            decision=repo.ApprovalDecision.APPROVED,
            reason="adversarial test approval",
        )


def action_of(db: Database, incident_id: str) -> Action:
    with db.read_session() as session:
        actions = repo.list_actions(session, incident_id)
    assert len(actions) == 1
    return actions[0]


def test_approval_is_bound_to_the_action_and_intent_it_was_granted_for(db: Database, rig) -> None:
    case = rig("P1-01")
    case.controller.run(case.incident_id)
    granted = pending(db, case.incident_id)
    approve(db, granted)

    original = action_of(db, case.incident_id)
    with db.session() as session:
        impersonated = repo.create_action(
            session,
            incident_id=case.incident_id,
            workflow_id=original.workflow_id,
            tool_name="apply_remediation",
            target="ad-service",
            arguments={
                "target": "ad-service",
                "remediation_id": "disable_feature_flag",
                "flag_id": "adHighCpu",
            },
            rationale="a second, unapproved intent",
            decision_id=None,
            requires_approval=True,
        )
        assert repo.create_approval(
            session, action=impersonated, requested_by="controller", ttl_seconds=900
        )
    with db.session() as session:
        # Tamper: point the second action at the approval granted for the first.
        session.execute(
            sa.update(repo.ActionRow)
            .where(repo.ActionRow.id == impersonated.id)
            .values(
                approval_id=granted.id,
                status=ActionStatus.APPROVED,
                intent_digest=impersonated.intent_digest,
            )
        )

    execution = case.gateway.execute_action(action_id=impersonated.id)

    assert execution.executed is False
    assert execution.action.status is not ActionStatus.DISPATCHING
    assert case.environment.applied == {}


def test_a_consumed_approval_cannot_authorise_a_second_dispatch(db: Database, rig) -> None:
    case = rig("P1-01")
    case.controller.run(case.incident_id)
    approval = pending(db, case.incident_id)
    approve(db, approval)
    action = action_of(db, case.incident_id)

    first = case.gateway.execute_action(action_id=action.id)
    second = case.gateway.execute_action(action_id=action.id)

    assert first.executed is True
    assert second.executed is False
    assert list(case.environment.applied) == [action.idempotency_key]
    with db.read_session() as session:
        stored = repo.get_approval(session, approval.id)
    assert stored is not None and stored.status is ApprovalStatus.CONSUMED


def test_expired_approval_is_never_re_issued_and_never_executes(db: Database, rig) -> None:
    case = rig("P1-01")
    case.controller.run(case.incident_id)
    approval = pending(db, case.incident_id)
    with db.session() as session:
        session.execute(
            sa.update(ApprovalRow)
            .where(ApprovalRow.id == approval.id)
            .values(expires_at=utcnow() - timedelta(seconds=1))
        )
        expired = repo.expire_stale_approvals(session)
    assert expired == 1

    with pytest.raises(repo.RepositoryError, match="not PENDING"), db.session() as session:
        repo.decide_approval(
            session,
            approval_id=approval.id,
            approver="on-call",
            decision=repo.ApprovalDecision.APPROVED,
            reason="too late",
        )

    outcome = case.controller.resume(case.incident_id)
    assert outcome.status is IncidentStatus.ESCALATED_INCOMPLETE
    assert case.environment.applied == {}
    with db.read_session() as session:
        approvals = repo.list_approvals(session, case.incident_id)
    assert [item.status for item in approvals] == [ApprovalStatus.EXPIRED]


def test_only_one_worker_can_claim_an_approved_action(
    db: Database, settings: Settings, rig
) -> None:
    case = rig("P1-01")
    case.controller.run(case.incident_id)
    approval = pending(db, case.incident_id)
    approve(db, approval)
    action = action_of(db, case.incident_id)

    claims: list[bool] = []
    barrier = threading.Barrier(2)

    def worker() -> None:
        database = Database(settings.test_database_url)
        try:
            with database.session() as session:
                barrier.wait(timeout=10)
                claim = repo.claim_action(session, action_id=action.id, actor="worker")
                claims.append(claim.claimed)
        finally:
            database.dispose()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert sorted(claims) == [False, True]
    with db.read_session() as session:
        stored = repo.get_action(session, action.id)
    assert stored is not None and stored.attempt == 1


def test_constraint_added_after_approval_blocks_the_side_effect(db: Database, rig) -> None:
    case = rig("P1-01")
    case.controller.run(case.incident_id)
    approval = pending(db, case.incident_id)
    approve(db, approval)
    with db.session() as session:
        repo.add_constraint(
            session,
            kind=ConstraintKind.FREEZE_ACTIONS,
            actor="on-call",
            incident_id=case.incident_id,
            note="freeze everything while I look",
        )

    case.controller.resume(case.incident_id)

    assert case.environment.applied == {}
    action = action_of(db, case.incident_id)
    assert action.status is ActionStatus.APPROVED
    with db.read_session() as session:
        incident = repo.get_incident(session, case.incident_id)
    assert incident is not None and incident.status is not IncidentStatus.RESOLVED


def test_uncertain_dispatch_is_reconciled_instead_of_retried(
    db: Database, settings: Settings, rig
) -> None:
    case = rig("P1-06", status_query_available=True)
    case.controller.run(case.incident_id)
    approval = pending(db, case.incident_id)
    approve(db, approval)
    action = action_of(db, case.incident_id)

    execution = case.gateway.execute_action(action_id=action.id)
    assert execution.executed is True
    assert execution.action.status is ActionStatus.UNCERTAIN
    # The external change landed even though the acknowledgement was lost.
    assert action.idempotency_key in case.environment.applied

    reconciled = case.gateway.reconcile_action(action_id=action.id)
    assert reconciled.status is ActionStatus.SUCCEEDED
    assert reconciled.external_outcome is ActionOutcome.CONFIRMED_APPLIED
    with db.read_session() as session:
        attempts = repo.list_actions(session, case.incident_id)
    assert len(attempts) == 1
    assert attempts[0].attempt == 1


def test_crash_after_claim_before_dispatch_is_not_blindly_retried(
    db: Database, settings: Settings, rig
) -> None:
    """A claim with no recorded outcome stays unknown; it never becomes a guess.

    The worker died holding the claim and nothing recorded what happened to the
    external call. The adapter's applied/attempted maps are in-memory, so after a
    restart it cannot tell "never asked" from "asked in a process that is gone".
    Reporting CONFIRMED_NOT_APPLIED here would convert an unknown outcome into a
    confident failure, which is the opposite of what this case is for: the run
    must not retry the side effect, and it must not resolve either.
    """
    case = rig("P1-01")
    case.controller.run(case.incident_id)
    approval = pending(db, case.incident_id)
    approve(db, approval)
    action = action_of(db, case.incident_id)

    with db.session() as session:
        claim = repo.claim_action(session, action_id=action.id, actor="worker-that-died")
    assert claim.claimed is True
    assert case.environment.applied == {}, "the worker died before the external call"

    notes = case.controller.reconcile(case.incident_id)

    assert any("UNCERTAIN" in note for note in notes)
    with db.read_session() as session:
        stored = repo.get_action(session, action.id)
        incident = repo.get_incident(session, case.incident_id)
    assert stored is not None
    assert stored.status is ActionStatus.UNCERTAIN
    assert stored.external_outcome is ActionOutcome.UNKNOWN
    assert incident is not None and incident.status is not IncidentStatus.RESOLVED
    assert stored.attempt == 1


def test_unknown_reconcile_outcome_hands_off_instead_of_resolving(
    db: Database, settings: Settings, rig
) -> None:
    """The other half of the same rule: an unresolved side effect blocks RESOLVED.

    Even when the service has genuinely recovered, a dispatch whose outcome is
    still unknown must not be reported as a resolved incident -- an action in
    DISPATCHING is exactly as open as one in UNCERTAIN.
    """
    case = rig("P1-01")
    case.controller.run(case.incident_id)
    approval = pending(db, case.incident_id)
    approve(db, approval)
    action = action_of(db, case.incident_id)

    # The worker both dispatched *and* applied the change, then died before the
    # ledger row was written: the flag is off, but the action row says DISPATCHING.
    case.environment.apply_remediation(
        idempotency_key=action.idempotency_key,
        target=action.target,
        remediation_id="disable_feature_flag",
        flag_id="paymentUnreachable",
        timeout_seconds=1.0,
    )
    with db.session() as session:
        claim = repo.claim_action(session, action_id=action.id, actor="worker-that-died")
    assert claim.claimed is True
    assert case.environment.applied, "the external change really happened"

    case.controller.advance(case.incident_id)

    with db.read_session() as session:
        stored = repo.get_action(session, action.id)
        incident = repo.get_incident(session, case.incident_id)
    assert stored is not None
    assert incident is not None
    assert stored.status is not ActionStatus.SUCCEEDED
    assert incident.status is not IncidentStatus.RESOLVED


def test_an_open_side_effect_blocks_resolution_even_after_real_recovery(
    db: Database, settings: Settings, rig
) -> None:
    """A DISPATCHING action is unresolved, so recovery alone cannot resolve.

    The first action really succeeded and really recovered the service, but a
    second intent is still mid-flight with no recorded outcome. The routing gate
    must refuse RESOLVED for as long as that side effect is open.
    """
    case = rig("P1-01")
    case.controller.run(case.incident_id)
    approve(db, pending(db, case.incident_id))
    settled = case.gateway.execute_action(action_id=action_of(db, case.incident_id).id).action
    assert settled.status is ActionStatus.SUCCEEDED
    assert settled.completed_at is not None

    with db.session() as session:
        second = repo.create_action(
            session,
            incident_id=case.incident_id,
            workflow_id=settled.workflow_id,
            tool_name="apply_remediation",
            target="payment-service",
            arguments={
                "target": "payment-service",
                "remediation_id": "disable_feature_flag",
                "flag_id": "paymentFailure",
            },
            rationale="a second intent that never recorded its outcome",
            decision_id=None,
            # Created APPROVED so the claim below really happens; this action is
            # not the point of the test, only the open DISPATCHING row it leaves.
            requires_approval=False,
        )
        claimed = repo.claim_action(session, action_id=second.id, actor="worker-that-died")
    assert claimed.claimed is True, claimed.reason

    with db.read_session() as session:
        stored = repo.get_action(session, second.id)
    assert stored is not None and stored.status is ActionStatus.DISPATCHING

    route = case.controller._route_after_verify(
        {
            "incident_id": case.incident_id,
            "verification": str(VerificationOutcome.VERIFICATION_SUCCESS),
        }
    )
    assert route == "escalate"


def test_restart_while_waiting_for_approval_resumes_from_postgres(
    db: Database, settings: Settings, rig
) -> None:
    case = rig("P1-01")
    case.controller.run(case.incident_id)
    approval = pending(db, case.incident_id)

    # Simulate a restart: brand-new engine, gateway, controller, and environment.
    restarted_environment = build_environment_for_scenario(case.spec)
    restarted_db = Database(settings.test_database_url)
    try:
        restarted_gateway = ToolGateway(
            db=restarted_db, environment=restarted_environment, settings=settings
        )
        restarted = WorkflowController(
            db=restarted_db,
            gateway=restarted_gateway,
            provider=DeterministicAdapter(),
            settings=settings,
        )
        with restarted_db.session() as session:
            repo.decide_approval(
                session,
                approval_id=approval.id,
                approver="on-call",
                decision=repo.ApprovalDecision.APPROVED,
                reason="approved after the restart",
            )
        outcome = restarted.resume(case.incident_id)
        assert outcome.status is IncidentStatus.RESOLVED
        assert len(restarted_environment.applied) == 1

        resumed_again = restarted.resume(case.incident_id)
        assert resumed_again.status is IncidentStatus.RESOLVED
        assert len(restarted_environment.applied) == 1
    finally:
        restarted_db.dispose()


def test_budget_walls_cannot_be_exceeded_by_any_actor(db: Database, rig) -> None:
    case = rig("P1-01")
    with db.session() as session:
        workflow = repo.get_workflow(session, case.incident_id)
        assert workflow is not None
        workflow_id = workflow.id
        for _ in range(8):
            repo.consume_step(session, incident_id=case.incident_id, kind="tool")
    with pytest.raises(repo.BudgetExhausted, match="soft step budget"), db.session() as session:
        repo.consume_step(session, incident_id=case.incident_id, kind="tool")
    with db.session() as session:
        repo.consume_step(session, incident_id=case.incident_id, kind="tool", allow_over_soft=True)
    for _ in range(7):
        with db.session() as session:
            repo.consume_step(
                session, incident_id=case.incident_id, kind="tool", allow_over_soft=True
            )
    with pytest.raises(repo.BudgetExhausted, match="hard step cap"), db.session() as session:
        repo.consume_step(session, incident_id=case.incident_id, kind="tool", allow_over_soft=True)

    # A soft-exhausted workflow cannot be given an unbounded number of extensions.
    with db.session() as session:
        for index in range(2):
            repo.grant_extension(
                session,
                incident_id=case.incident_id,
                actor="controller",
                justification=f"extension {index}",
            )
    with pytest.raises(repo.BudgetExhausted, match="extension"), db.session() as session:
        repo.grant_extension(
            session,
            incident_id=case.incident_id,
            actor="controller",
            justification="one too many",
        )
    with db.read_session() as session:
        stored = repo.get_workflow(session, case.incident_id)
    assert stored is not None and stored.id == workflow_id
    assert stored.steps_used == stored.hard_step_cap


def test_malicious_runbook_never_reaches_the_tool_layer(
    db: Database, settings: Settings, rig
) -> None:
    case = rig("P1-08")
    case.controller.run(case.incident_id)
    with db.read_session() as session:
        actions = repo.list_actions(session, case.incident_id)
        tool_calls = session.scalars(
            sa.select(ToolCallRow).where(ToolCallRow.incident_id == case.incident_id)
        ).all()
    assert actions == []
    assert {call.side_effect for call in tool_calls} <= {SideEffect.READ}
    assert not [call for call in tool_calls if call.tool_name == "shell_exec"]


def test_verification_requires_evidence_collected_after_the_action(db: Database, rig) -> None:
    case = rig("P1-01")
    with db.read_session() as session:
        workflow = repo.get_workflow(session, case.incident_id)
    assert workflow is not None
    result = case.gateway.verify_service(
        incident_id=case.incident_id,
        workflow_id=workflow.id,
        target="payment-service",
        action_id=None,
        action_completed_at=utcnow() + timedelta(hours=1),
    )
    assert result.outcome is VerificationOutcome.VERIFICATION_INCONCLUSIVE
    assert "at or after the action" in result.detail


# --- dependency-path recovery ---------------------------------------------------
#
# The fault class behind these tests: the caller cannot reach the callee while the
# callee's own telemetry looks perfectly healthy, because the calls never arrive.
# A recovery verdict taken from the callee's probe alone would be a false recovery.


class PathStillBroken(demo.DemoEnvironment):
    """The flag landed and the callee's probe recovered; the path did not.

    The caller keeps failing to reach the callee -- a broken path rather than a
    broken process -- so the caller-side read is the only signal that can see it.
    """

    def read_dependency(
        self, *, target: str, dependency: str, window_seconds: int, timeout_seconds: float
    ) -> ToolResult:
        result = super().read_dependency(
            target=target,
            dependency=dependency,
            window_seconds=window_seconds,
            timeout_seconds=timeout_seconds,
        )
        return result.model_copy(update={"value": {**result.value, "error_rate": 1.0}})


class StaleEdgeRead(demo.DemoEnvironment):
    """The edge read answers, but from a window that predates the action."""

    def read_dependency(
        self, *, target: str, dependency: str, window_seconds: int, timeout_seconds: float
    ) -> ToolResult:
        result = super().read_dependency(
            target=target,
            dependency=dependency,
            window_seconds=window_seconds,
            timeout_seconds=timeout_seconds,
        )
        return result.model_copy(update={"collected_at": result.collected_at - timedelta(hours=1)})


def verifications(db: Database, incident_id: str) -> list[VerificationRow]:
    with db.read_session() as session:
        return list(
            session.scalars(
                sa.select(VerificationRow).where(VerificationRow.incident_id == incident_id)
            ).all()
        )


def workflow_id_of(db: Database, incident_id: str) -> str:
    with db.session() as session:
        workflow = repo.get_workflow(session, incident_id)
    assert workflow is not None
    return workflow.id


def tool_calls(db: Database, incident_id: str) -> list[ToolCallRow]:
    with db.read_session() as session:
        return list(
            session.scalars(
                sa.select(ToolCallRow)
                .where(ToolCallRow.incident_id == incident_id)
                .order_by(ToolCallRow.started_at)
            ).all()
        )


def assert_no_resolution_and_one_action(db: Database, incident_id: str) -> None:
    """The refusal may not resolve the incident, nor spend a second side effect."""
    with db.read_session() as session:
        incident = repo.get_incident(session, incident_id)
        actions = repo.list_actions(session, incident_id)
    assert incident is not None and incident.status is not IncidentStatus.RESOLVED
    assert len(actions) == 1


def test_a_recovered_target_probe_is_not_a_recovered_dependency_path(db: Database, rig) -> None:
    """One successful action, no error from the adapter, and still no recovery.

    This is the false-recovery case the caller-side read exists to stop: the
    payment process is healthy again and its own probe says so, but the caller
    still cannot complete the Charge. Nothing here may resolve, and no second
    side effect may be attempted to "fix" the path.
    """
    base = build_environment_for_scenario(SCENARIOS["P1-01"])
    case = rig("P1-01", environment=PathStillBroken(services=base.services, faults=base.faults))
    case.controller.run(case.incident_id)
    approval = pending(db, case.incident_id)
    approve(db, approval)
    case.controller.advance(case.incident_id)

    assert case.environment.services["payment-service"].healthy, (
        "the remediation really did recover the payment process"
    )
    assert len(case.environment.applied) == 1, "exactly one side effect was attempted"

    recorded = verifications(db, case.incident_id)
    assert [item.outcome for item in recorded] == [VerificationOutcome.VERIFICATION_FAILURE.value]
    assert "dependency path is still failing" in recorded[0].detail
    with db.read_session() as session:
        incident = repo.get_incident(session, case.incident_id)
        actions = repo.list_actions(session, case.incident_id)
    assert incident is not None and incident.status is not IncidentStatus.RESOLVED
    assert len(actions) == 1, "a failing verification must not trigger a second action"


def test_a_stale_edge_read_cannot_verify_recovery(db: Database, rig) -> None:
    """Freshness is per read, on the path that named the fault.

    A window that closed before the action says nothing about what the action
    changed, so the round is not fresh even though the health probe in the same
    round was collected afterwards.
    """
    base = build_environment_for_scenario(SCENARIOS["P1-01"])
    case = rig("P1-01", environment=StaleEdgeRead(services=base.services, faults=base.faults))
    case.controller.run(case.incident_id)
    approve(db, pending(db, case.incident_id))
    case.controller.advance(case.incident_id)

    recorded = verifications(db, case.incident_id)
    assert [item.outcome for item in recorded] == [
        VerificationOutcome.VERIFICATION_INCONCLUSIVE.value
    ]
    assert "at or after the action" in recorded[0].detail
    with db.read_session() as session:
        incident = repo.get_incident(session, case.incident_id)
    assert incident is not None and incident.status is not IncidentStatus.RESOLVED


def test_an_unaffordable_edge_read_is_inconclusive_rather_than_fatal(
    db: Database, settings: Settings, rig
) -> None:
    """The budget is a wall on the path reads too, and the verdict says so.

    The dependency read is the fourth read of a verification round, behind the
    health probe and the two metrics reads, so a cap that leaves those three
    affordable and lands on the fourth must produce an unproven recovery -- never
    a success, and never an exception that loses the incident.
    """
    case = rig("P1-01")
    case.controller.run(case.incident_id)
    approve(db, pending(db, case.incident_id))
    action = action_of(db, case.incident_id)
    execution = case.gateway.execute_action(action_id=action.id)
    assert execution.action.completed_at is not None

    with db.session() as session:
        workflow = repo.get_workflow(session, case.incident_id)
        assert workflow is not None
        # A read is charged before its verdict is taken, and a refusal rolls that
        # charge back, so the read refused is the one that would *land on* the cap.
        # Four steps above the current count leaves the health probe and the two
        # metrics reads affordable and puts the refusal exactly on the dependency
        # read that follows them -- which is the case this test is about.
        session.execute(
            sa.update(WorkflowRow)
            .where(WorkflowRow.id == workflow.id)
            .values(hard_step_cap=workflow.steps_used + 4)
        )
        workflow_id = workflow.id

    result = case.gateway.verify_service(
        incident_id=case.incident_id,
        workflow_id=workflow_id,
        target="payment-service",
        action_id=action.id,
        action_completed_at=execution.action.completed_at,
    )

    assert result.outcome is VerificationOutcome.VERIFICATION_INCONCLUSIVE
    assert "dependency-path verification read is no longer affordable" in result.detail
    # A budget wall, described as a budget wall: the cause is the cap, not a human.
    assert "human" not in result.detail
    with db.read_session() as session:
        incident = repo.get_incident(session, case.incident_id)
    assert incident is not None and incident.status is not IncidentStatus.RESOLVED


def test_a_cap_spent_before_the_round_is_a_budget_cause_from_the_repository(
    db: Database, rig
) -> None:
    """The other budget producer: the repository's wall, reached before any read.

    The engine refuses a read at the cap with a policy verdict; ``consume_step``
    refuses one once the cap is reached. Both are budget refusals and both must
    reach the verdict as such -- this test drives the second producer end to end,
    where the first read of the round is refused and no read is taken at all.
    """
    case = rig("P1-01")
    case.controller.run(case.incident_id)
    approve(db, pending(db, case.incident_id))
    action = action_of(db, case.incident_id)
    execution = case.gateway.execute_action(action_id=action.id)
    assert execution.action.completed_at is not None

    with db.session() as session:
        workflow = repo.get_workflow(session, case.incident_id)
        assert workflow is not None
        session.execute(
            sa.update(WorkflowRow)
            .where(WorkflowRow.id == workflow.id)
            .values(hard_step_cap=workflow.steps_used)
        )
    before = len(tool_calls(db, case.incident_id))

    result = case.gateway.verify_service(
        incident_id=case.incident_id,
        workflow_id=workflow_id_of(db, case.incident_id),
        target="payment-service",
        action_id=action.id,
        action_completed_at=execution.action.completed_at,
    )

    assert result.outcome is VerificationOutcome.VERIFICATION_INCONCLUSIVE
    assert "the verification reads are no longer affordable" in result.detail
    assert "hard step cap reached" in result.detail
    assert "human" not in result.detail
    assert len(tool_calls(db, case.incident_id)) == before, "a refused round takes no read"
    recorded = verifications(db, case.incident_id)
    assert [item.outcome for item in recorded] == [
        VerificationOutcome.VERIFICATION_INCONCLUSIVE.value
    ]
    assert_no_resolution_and_one_action(db, case.incident_id)


def test_a_human_denial_of_the_path_read_is_inconclusive_with_the_human_cause(
    db: Database, rig
) -> None:
    """A tool-scoped human denial stops the path read, and is reported as one.

    The constraint names ``read_dependency``, so the probes still answer and the
    refusal lands on the dependency-path read -- the same read the hard cap hit in
    the measured failure, with the opposite cause. A human decision must never be
    described as a budget wall.
    """
    case = rig("P1-01")
    case.controller.run(case.incident_id)
    approve(db, pending(db, case.incident_id))
    action = action_of(db, case.incident_id)
    execution = case.gateway.execute_action(action_id=action.id)
    assert execution.action.completed_at is not None

    with db.session() as session:
        repo.add_constraint(
            session,
            kind=ConstraintKind.DENY_ACTION,
            actor="on-call",
            incident_id=case.incident_id,
            tool_name="read_dependency",
            note="do not probe the payment path while I check the instrumentation",
        )
    before = [call.tool_name for call in tool_calls(db, case.incident_id)]

    result = case.gateway.verify_service(
        incident_id=case.incident_id,
        workflow_id=workflow_id_of(db, case.incident_id),
        target="payment-service",
        action_id=action.id,
        action_completed_at=execution.action.completed_at,
    )

    assert result.outcome is VerificationOutcome.VERIFICATION_INCONCLUSIVE
    assert "a human control blocked the dependency-path verification read" in result.detail
    assert "no longer affordable" not in result.detail
    recorded = verifications(db, case.incident_id)
    assert [item.outcome for item in recorded] == [
        VerificationOutcome.VERIFICATION_INCONCLUSIVE.value
    ]
    assert "a human control blocked" in recorded[0].detail
    # The probes answered, so the refusal was the path read rather than them; and a
    # refused read records no tool call at all, so the round ended there.
    where = [call.tool_name for call in tool_calls(db, case.incident_id)]
    assert where[len(before) :] == ["read_service_health", "read_metrics", "read_metrics"], where
    assert_no_resolution_and_one_action(db, case.incident_id)


def test_takeover_stops_verification_with_the_human_cause(db: Database, rig) -> None:
    """A human takeover suspends the reads themselves, and the verdict says so."""
    case = rig("P1-01")
    case.controller.run(case.incident_id)
    approve(db, pending(db, case.incident_id))
    action = action_of(db, case.incident_id)
    execution = case.gateway.execute_action(action_id=action.id)
    assert execution.action.completed_at is not None

    with db.session() as session:
        repo.set_takeover(session, incident_id=case.incident_id, actor="on-call")
    before = len(tool_calls(db, case.incident_id))

    result = case.gateway.verify_service(
        incident_id=case.incident_id,
        workflow_id=workflow_id_of(db, case.incident_id),
        target="payment-service",
        action_id=action.id,
        action_completed_at=execution.action.completed_at,
    )

    assert result.outcome is VerificationOutcome.VERIFICATION_INCONCLUSIVE
    assert "a human control blocked the verification reads" in result.detail
    assert "human takeover is active" in result.detail
    assert "no longer affordable" not in result.detail
    assert len(tool_calls(db, case.incident_id)) == before, "takeover takes no read"
    assert_no_resolution_and_one_action(db, case.incident_id)


def test_the_requesting_actor_cannot_approve_its_own_action(db: Database, rig) -> None:
    case = rig("P1-01")
    case.controller.run(case.incident_id)
    approval = pending(db, case.incident_id)
    with (
        pytest.raises(repo.RepositoryError, match="cannot approve its own action"),
        db.session() as session,
    ):
        repo.decide_approval(
            session,
            approval_id=approval.id,
            approver=approval.requested_by,
            decision=repo.ApprovalDecision.APPROVED,
            reason="self approval attempt",
        )
    case.gateway.execute_action(action_id=action_of(db, case.incident_id).id)
    assert case.environment.applied == {}


def test_a_successful_command_is_not_evidence_of_recovery(
    db: Database, settings: Settings, rig
) -> None:
    case = rig("P1-07")
    case.controller.run(case.incident_id)
    approval = pending(db, case.incident_id)
    approve(db, approval)
    action = action_of(db, case.incident_id)
    execution = case.gateway.execute_action(action_id=action.id)
    assert execution.action.status is ActionStatus.SUCCEEDED
    assert execution.action.external_outcome is ActionOutcome.CONFIRMED_APPLIED

    with db.session() as session:
        workflow = repo.get_workflow(session, case.incident_id)
        completed = repo.get_action(session, action.id).completed_at
    assert workflow is not None
    verification = case.gateway.verify_service(
        incident_id=case.incident_id,
        workflow_id=workflow.id,
        target="payment-service",
        action_id=action.id,
        action_completed_at=completed,
    )
    assert verification.outcome is VerificationOutcome.VERIFICATION_FAILURE
    with db.read_session() as session:
        incident = repo.get_incident(session, case.incident_id)
    assert incident is not None and incident.status is not IncidentStatus.RESOLVED


def test_proposals_cannot_widen_the_tool_surface(db: Database, settings: Settings, rig) -> None:
    """Even an approved-looking proposal naming an unknown tool is refused."""
    case = rig("P1-01")
    with db.session() as session:
        workflow = repo.get_workflow(session, case.incident_id)
    assert workflow is not None
    from opsaccord.tools.gateway import UnknownTool

    with pytest.raises(UnknownTool):
        case.gateway.evaluate_action(
            incident_id=case.incident_id,
            proposal=ActionProposal(
                tool_name="shell_exec",
                target="payment-service",
                arguments={"cmd": "rm -rf /"},
                rationale="an injected proposal outside the typed catalog",
            ),
        )
