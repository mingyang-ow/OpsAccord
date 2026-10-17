"""Regression evidence for the control gaps an independent review found.

Every test here pins one defect that was fixed after review, so the fix cannot be
quietly reverted. They are deliberately written against persisted rows and real
gateway calls rather than against the private helpers, except where the defect
*is* the routing predicate.
"""

from __future__ import annotations

import threading
import time
from datetime import timedelta
from typing import Any

import pytest
import sqlalchemy as sa

from opsaccord.config import Settings
from opsaccord.domain.enums import (
    ActionOutcome,
    ActionStatus,
    SideEffect,
    VerificationOutcome,
)
from opsaccord.ids import utcnow
from opsaccord.persistence import repositories as repo
from opsaccord.persistence.db import Database
from opsaccord.persistence.locks import CONTROL_GATE_LOCK
from opsaccord.persistence.tables import ToolCallRow, WorkflowRow
from opsaccord.reasoning.deterministic import DeterministicAdapter
from opsaccord.scenarios.fixtures import SCENARIOS, build_environment_for_scenario
from opsaccord.tools import demo_environment as demo
from opsaccord.tools.gateway import ToolGateway
from opsaccord.workflow.controller import WorkflowController

pytestmark = pytest.mark.integration


class NonSimulatedAdapter:
    """A stand-in for a live adapter: everything delegates except provenance."""

    is_simulated = False

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def build_case(db: Database, settings: Settings, scenario_id: str = "P1-01"):
    spec = SCENARIOS[scenario_id]
    environment = build_environment_for_scenario(spec)
    gateway = ToolGateway(db=db, environment=environment, settings=settings)
    controller = WorkflowController(
        db=db, gateway=gateway, provider=DeterministicAdapter(), settings=settings
    )
    incident_id = controller.create_incident(
        title=spec.title, service=spec.service, severity=spec.severity, scenario_id=spec.id
    )
    return spec, environment, gateway, controller, incident_id


def pending_approval(db: Database, incident_id: str):
    with db.session() as session:
        approval = repo.pending_approval(session, incident_id)
    assert approval is not None
    return approval


def grant(db: Database, approval) -> None:
    with db.session() as session:
        repo.decide_approval(
            session,
            approval_id=approval.id,
            approver="on-call",
            decision=repo.ApprovalDecision.APPROVED,
            reason="hardening test approval",
        )


def first_action(db: Database, incident_id: str):
    with db.read_session() as session:
        actions = repo.list_actions(session, incident_id)
    assert len(actions) == 1
    return actions[0]


def workflow_id_of(db: Database, incident_id: str) -> str:
    with db.read_session() as session:
        workflow = repo.get_workflow(session, incident_id)
    assert workflow is not None
    return workflow.id


# --- verification freshness ----------------------------------------------------


def test_verification_without_a_completed_action_is_never_a_recovery(
    db: Database, settings: Settings
) -> None:
    """Freshness is relative to an action; with no action there is nothing fresh."""
    spec = SCENARIOS["P1-01"]
    gateway = ToolGateway(db=db, environment=demo.build_demo_environment(), settings=settings)
    controller = WorkflowController(
        db=db, gateway=gateway, provider=DeterministicAdapter(), settings=settings
    )
    incident_id = controller.create_incident(
        title=spec.title, service=spec.service, severity=spec.severity, scenario_id=spec.id
    )
    workflow_id = workflow_id_of(db, incident_id)

    orphan = gateway.verify_service(
        incident_id=incident_id,
        workflow_id=workflow_id,
        target="payment-service",
        action_id=None,
        action_completed_at=None,
    )
    assert orphan.outcome is VerificationOutcome.VERIFICATION_INCONCLUSIVE
    assert "at or after the action completed" in orphan.detail

    # The positive control: the same healthy probe *is* a recovery when it really
    # was collected after an action that completed.
    real = gateway.verify_service(
        incident_id=incident_id,
        workflow_id=workflow_id,
        target="payment-service",
        action_id=None,
        action_completed_at=utcnow() - timedelta(minutes=1),
    )
    assert real.outcome is VerificationOutcome.VERIFICATION_SUCCESS


# --- budget extensions ---------------------------------------------------------


def test_a_granted_extension_actually_buys_more_soft_budget(
    db: Database, settings: Settings
) -> None:
    """The soft checkpoint is a checkpoint, not a wall: an extension must permit work."""
    _, _, _, _, incident_id = build_case(db, settings)
    with db.session() as session:
        session.execute(
            sa.update(WorkflowRow)
            .where(WorkflowRow.incident_id == incident_id)
            .values(soft_step_budget=2, steps_used=2, hard_step_cap=10)
        )

    with db.session() as session, pytest.raises(repo.BudgetExhausted):
        repo.consume_step(session, incident_id=incident_id, kind="tool")

    with db.session() as session:
        granted = repo.grant_extension(
            session, incident_id=incident_id, actor="on-call", justification="one more read"
        )
    assert granted.extensions_granted == 1

    with db.session() as session:
        after = repo.consume_step(session, incident_id=incident_id, kind="tool")
    assert after.steps_used == 3


# --- post-dispatch ledger durability ------------------------------------------


def test_a_budget_error_after_dispatch_never_erases_the_ledger(
    db: Database, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The side effect already happened; a budget refusal must not hide it.

    Injection: the step that records a dispatch is the one that fails. The
    tool-call row and the action's final status must still be persisted.
    """
    _, environment, gateway, controller, incident_id = build_case(db, settings)
    controller.run(incident_id)
    grant(db, pending_approval(db, incident_id))
    action = first_action(db, incident_id)

    def exploding_consume_step(session, **kwargs):
        raise repo.BudgetExhausted("injected: hard cap reached after dispatch")

    monkeypatch.setattr(repo, "consume_step", exploding_consume_step)
    execution = gateway.execute_action(action_id=action.id)

    assert execution.executed is True
    assert execution.action.status is ActionStatus.SUCCEEDED
    assert environment.applied, "the external change really happened"
    assert "budget" in execution.detail

    with db.read_session() as session:
        ledger = session.scalars(
            sa.select(ToolCallRow).where(
                ToolCallRow.incident_id == incident_id,
                ToolCallRow.side_effect == SideEffect.WRITE,
            )
        ).all()
        stored = repo.get_action(session, action.id)
    assert len(ledger) == 1, "the dispatch must still have exactly one ledger row"
    assert ledger[0].succeeded is True
    assert stored is not None and stored.status is ActionStatus.SUCCEEDED


def test_verification_that_cannot_afford_a_step_is_inconclusive_not_fatal(
    db: Database, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unaffordable verification is an unverified result, not a crashed run."""
    _, _, gateway, controller, incident_id = build_case(db, settings)
    controller.run(incident_id)
    grant(db, pending_approval(db, incident_id))
    action = first_action(db, incident_id)
    execution = gateway.execute_action(action_id=action.id)
    assert execution.action.completed_at is not None

    def exploding_consume_step(session, **kwargs):
        raise repo.BudgetExhausted("injected: hard cap reached during verification")

    monkeypatch.setattr(repo, "consume_step", exploding_consume_step)
    result = gateway.verify_service(
        incident_id=incident_id,
        workflow_id=execution.action.workflow_id,
        target="payment-service",
        action_id=execution.action.id,
        action_completed_at=execution.action.completed_at,
    )

    assert result.outcome is VerificationOutcome.VERIFICATION_INCONCLUSIVE
    assert "no longer affordable" in result.detail


# --- revalidation at the moment of dispatch ------------------------------------


def test_a_posture_change_between_approval_and_dispatch_stops_the_side_effect(
    db: Database, settings: Settings
) -> None:
    """Policy is re-read in full immediately before the write, not only at request."""
    _, environment, gateway, controller, incident_id = build_case(db, settings)
    controller.run(incident_id)
    grant(db, pending_approval(db, incident_id))
    action = first_action(db, incident_id)

    # ``services`` is migration-seeded reference data and is deliberately *not*
    # truncated between tests, so the original posture must be put back.
    with db.session() as session:
        original = session.scalar(
            sa.text("SELECT auto_remediation FROM services WHERE name = 'payment-service'")
        )
    assert original is not None
    try:
        with db.session() as session:
            session.execute(
                sa.text(
                    "UPDATE services SET auto_remediation = 'none' WHERE name = 'payment-service'"
                )
            )

        execution = gateway.execute_action(action_id=action.id)

        assert execution.executed is False
        assert "revalidation" in execution.detail
        assert environment.applied == {}, "no side effect may reach the demo"
    finally:
        with db.session() as session:
            session.execute(
                sa.text("UPDATE services SET auto_remediation = :posture WHERE name = :name"),
                {"posture": original, "name": "payment-service"},
            )


def test_an_exhausted_hard_cap_stops_the_side_effect_at_dispatch(
    db: Database, settings: Settings
) -> None:
    """The hard wall is re-checked immediately before the write."""
    _, environment, gateway, controller, incident_id = build_case(db, settings)
    controller.run(incident_id)
    grant(db, pending_approval(db, incident_id))
    action = first_action(db, incident_id)

    with db.session() as session:
        steps = session.scalar(
            sa.select(WorkflowRow.steps_used).where(WorkflowRow.incident_id == incident_id)
        )
        session.execute(
            sa.update(WorkflowRow)
            .where(WorkflowRow.incident_id == incident_id)
            .values(hard_step_cap=steps, soft_step_budget=steps)
        )

    execution = gateway.execute_action(action_id=action.id)

    assert execution.executed is False
    assert environment.applied == {}


def test_a_human_control_that_commits_during_dispatch_is_not_ignored(
    db: Database, settings: Settings
) -> None:
    """A takeover is ordered against dispatch, and a committed one is never ignored.

    Two properties are established here, each with a bounded wait rather than a
    fixed sleep:

    1. *Ordering.* A control write cannot commit while an authorization transaction
       holds the control gate, so it cannot land in the window between a
       dispatcher's control read and its claim and be missed under READ COMMITTED.
       The gate holder stands in for that window, and the takeover writer is
       *observed waiting* on the gate - a blocked advisory-lock request appears in
       ``pg_locks`` with ``granted = false`` - rather than assumed to have arrived.
       That distinction is the whole point: "nothing committed in the last second"
       is also true of a writer that never reached the gate.
    2. *Safety.* A takeover that has committed before the authorization is observed
       by revalidation, so the side effect never reaches the demo.

    The dispatcher's half of the clock - ``execute_action`` holding the gate across
    its control read and its claim, and releasing it before the external call - is
    proven separately in
    ``test_dispatch_safety.py::test_the_authorization_holds_the_gate_and_releases_it_before_the_external_call``,
    because the gate holder here never dispatches.

    History, because the orchestration is the point: this test used to take the
    incident row lock itself and then call ``set_takeover`` inside the same
    transaction. Once every control write takes the control gate first, that
    composition is the reverse of the documented order (gate -> incident row ->
    workflow row), and with a dispatcher on the other side it is a deadlock instead
    of a result. The supported path is to call ``set_takeover`` and let the
    repository take the locks in order, which is what this test does now; the safety
    assertion it has always carried is unchanged.
    """
    _, environment, gateway, controller, incident_id = build_case(db, settings)
    controller.run(incident_id)
    grant(db, pending_approval(db, incident_id))
    action = first_action(db, incident_id)

    gate_held = threading.Event()
    release_gate = threading.Event()
    takeover_committed = threading.Event()
    worker_failures: list[BaseException] = []

    def gate_waiters() -> int:
        """How many backends are blocked on the control gate right now."""
        with db.read_session() as session:
            return int(
                session.execute(
                    sa.text(
                        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'"
                        " AND classid::bigint = :classid AND objid::bigint = :objid"
                        " AND granted = false"
                    ),
                    {"classid": CONTROL_GATE_LOCK[0], "objid": CONTROL_GATE_LOCK[1]},
                ).scalar_one()
            )

    def hold_the_gate() -> None:
        try:
            with db.session() as session:
                repo.lock_control_gate(session)
                gate_held.set()
                release_gate.wait(timeout=20)
        except BaseException as exc:  # reported below, never swallowed
            worker_failures.append(exc)
            gate_held.set()

    def take_over() -> None:
        try:
            writer = Database(settings.test_database_url)
            try:
                with writer.session() as session:
                    repo.set_takeover(session, incident_id=incident_id, actor="on-call")
            finally:
                writer.dispose()
        except BaseException as exc:  # reported below, never swallowed
            worker_failures.append(exc)
        finally:
            takeover_committed.set()

    holder = threading.Thread(target=hold_the_gate, daemon=True, name="gate-holder")
    writer = threading.Thread(target=take_over, daemon=True, name="takeover-writer")
    holder.start()
    try:
        assert gate_held.wait(timeout=20), "the test could not take the control gate"
        writer.start()
        # Bounded poll for positive evidence that the control write reached the gate
        # and is blocked on it. The cadence is a poll interval, not a delay the
        # result depends on: the loop exits the moment the waiter is visible.
        deadline = time.monotonic() + 20
        while gate_waiters() == 0:
            assert time.monotonic() < deadline, (
                "no backend ever waited for the control gate, so this test would have "
                "passed without ever contending for the ordering it claims to exercise"
            )
            time.sleep(0.05)
        committed_while_waiting = takeover_committed.is_set()
    finally:
        # Released even if an assertion above failed, and joined only if the thread
        # actually started, so cleanup cannot replace the real failure with a
        # "cannot join thread before it is started" error.
        release_gate.set()
        for worker in (holder, writer):
            if worker.ident is not None:
                worker.join(timeout=20)

    assert not holder.is_alive(), "the gate holder did not terminate"
    assert not writer.is_alive(), "the takeover writer did not terminate"
    assert worker_failures == [], f"a worker failed with a database error: {worker_failures!r}"
    assert committed_while_waiting is False, (
        "a takeover committed while the control gate was held: it could land between a "
        "dispatcher's control read and its claim and be missed"
    )
    assert takeover_committed.is_set(), "the takeover must commit once the gate is released"

    execution = gateway.execute_action(action_id=action.id)

    assert execution.executed is False
    assert "human takeover active" in execution.detail
    assert environment.applied == {}, "a superseded dispatch must not reach the demo"
    with db.read_session() as session:
        workflow = repo.get_workflow(session, incident_id)
        stored = repo.get_action(session, action.id)
    assert workflow is not None and workflow.takeover_actor == "on-call"
    assert stored is not None and stored.status is ActionStatus.APPROVED


# --- honest provenance ---------------------------------------------------------


def test_reconciliation_says_unknown_when_the_adapter_has_no_record(
    db: Database, settings: Settings
) -> None:
    """An in-memory adapter cannot claim a write did not happen after a restart."""
    environment = demo.build_demo_environment()
    outcome = environment.action_status("never-seen-key")
    assert outcome is ActionOutcome.UNKNOWN
    assert outcome is not ActionOutcome.CONFIRMED_NOT_APPLIED


def test_actions_taken_through_a_live_adapter_are_not_persisted_as_simulated(
    db: Database, settings: Settings
) -> None:
    """Provenance is stamped from the adapter, so a live write is not called fake."""
    spec = SCENARIOS["P1-01"]
    environment = NonSimulatedAdapter(build_environment_for_scenario(spec))
    gateway = ToolGateway(db=db, environment=environment, settings=settings)
    controller = WorkflowController(
        db=db, gateway=gateway, provider=DeterministicAdapter(), settings=settings
    )
    incident_id = controller.create_incident(
        title=spec.title, service=spec.service, severity=spec.severity, scenario_id=spec.id
    )
    controller.run(incident_id)
    grant(db, pending_approval(db, incident_id))
    action = first_action(db, incident_id)

    execution = gateway.execute_action(action_id=action.id)

    assert execution.action.is_simulated is False
    with db.read_session() as session:
        stored = repo.get_action(session, action.id)
    assert stored is not None and stored.is_simulated is False


# --- typed argument binding on the read path -----------------------------------


def test_a_read_target_must_match_its_validated_arguments(
    db: Database, settings: Settings, gateway: ToolGateway
) -> None:
    """Policy and the adapter must act on the same target value."""
    with pytest.raises(ValueError, match="does not match"):
        gateway.read(
            incident_id="inc-missing",
            workflow_id="wf-missing",
            tool_name="read_metrics",
            arguments={"target": "ad-service", "metric": "error_rate", "window_seconds": 300},
            target="payment-service",
        )


def test_a_read_argument_outside_the_typed_catalog_is_refused(
    db: Database, settings: Settings, gateway: ToolGateway
) -> None:
    with pytest.raises(ValueError, match="not allow-listed"):
        gateway.read(
            incident_id="inc-missing",
            workflow_id="wf-missing",
            tool_name="read_metrics",
            arguments={"target": "payment-service", "metric": "made-up", "window_seconds": 300},
            target="payment-service",
        )
