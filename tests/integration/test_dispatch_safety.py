"""The execution commitment point, the control gate, and uncertain outcomes.

These tests pin the safety pass that closed the two gaps recorded in
``docs/reports/phase1-implementation.md`` at ``5cbcce2``:

* a **global** constraint (``incident_id IS NULL``) had no incident row to lock, so
  it was not serialised against a dispatcher's revalidate-and-claim window;
* the general exception handler around the dispatch call recorded
  ``FAILED`` / ``CONFIRMED_NOT_APPLIED``, which asserts non-execution for an error
  that may have happened *after* the change was applied.

Concurrency is driven by threads, barriers and events - never by sleeping for an
interleaving to occur - and the two timing-sensitive gates use a *bounded* wait
that asserts the negative (the writer has not committed yet) and then asserts
progress once the lock is released. Every test states whether it fails on the
pre-fix code or guards behaviour that must not regress.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest
import sqlalchemy as sa

from opsaccord.config import Settings
from opsaccord.domain.enums import (
    ActionOutcome,
    ActionStatus,
    ApprovalDecision,
    ConstraintKind,
    IncidentStatus,
)
from opsaccord.domain.models import ToolResult
from opsaccord.ids import utcnow
from opsaccord.persistence import repositories as repo
from opsaccord.persistence.db import Database
from opsaccord.persistence.locks import CONTROL_GATE_LOCK
from opsaccord.reasoning.deterministic import DeterministicAdapter
from opsaccord.scenarios.fixtures import SCENARIOS, build_environment_for_scenario
from opsaccord.tools.contracts import DispatchRefused
from opsaccord.tools.gateway import ActionExecution, ToolGateway
from opsaccord.workflow.controller import WorkflowController

pytestmark = pytest.mark.integration


def build_case(
    db: Database,
    settings: Settings,
    scenario_id: str = "P1-01",
    environment: Any | None = None,
) -> tuple[Any, ToolGateway, WorkflowController, str]:
    spec = SCENARIOS[scenario_id]
    surface = environment if environment is not None else build_environment_for_scenario(spec)
    gateway = ToolGateway(db=db, environment=surface, settings=settings)
    controller = WorkflowController(
        db=db, gateway=gateway, provider=DeterministicAdapter(), settings=settings
    )
    incident_id = controller.create_incident(
        title=spec.title, service=spec.service, severity=spec.severity, scenario_id=spec.id
    )
    return surface, gateway, controller, incident_id


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
            decision=ApprovalDecision.APPROVED,
            reason="dispatch safety test approval",
        )


def reject(db: Database, approval) -> None:
    with db.session() as session:
        repo.decide_approval(
            session,
            approval_id=approval.id,
            approver="on-call",
            decision=ApprovalDecision.REJECTED,
            reason="not authorised tonight",
        )


def first_action(db: Database, incident_id: str):
    with db.read_session() as session:
        actions = repo.list_actions(session, incident_id)
    assert len(actions) == 1
    return actions[0]


def probe_gate_is_free(settings: Settings) -> bool:
    """Ask for the control gate from a different backend, without ever blocking.

    ``pg_try_advisory_xact_lock`` is the only way to observe the gate from inside a
    test: it returns immediately, so it can be used while another transaction holds
    the gate without deadlocking against it.
    """
    other = Database(settings.test_database_url)
    try:
        with other.session() as session:
            acquired = session.execute(
                sa.text("SELECT pg_try_advisory_xact_lock(:classid, :objid)"),
                {"classid": CONTROL_GATE_LOCK[0], "objid": CONTROL_GATE_LOCK[1]},
            ).scalar()
        return bool(acquired)
    finally:
        other.dispose()


def prepare_approved_action(db: Database, settings: Settings, scenario_id: str = "P1-01"):
    """Run to the approval interrupt, grant it, and return the approved action."""
    environment, gateway, controller, incident_id = build_case(db, settings, scenario_id)
    controller.run(incident_id)
    grant(db, pending_approval(db, incident_id))
    return environment, gateway, controller, incident_id, first_action(db, incident_id)


# --- the ordering guarantee: global controls versus dispatch -------------------


def test_a_global_freeze_committed_before_authorization_blocks_the_action(
    db: Database, settings: Settings
) -> None:
    """Control case: the freeze is the *only* constraint, so there is nothing to lock.

    A row lock cannot cover this, which is why the gate exists. Before the gate the
    freeze was still honoured *here* - committed before the dispatcher read the
    control set - so this test does not fail pre-fix; it pins the behaviour the
    gate must not weaken.
    """
    environment, gateway, _, incident_id, action = prepare_approved_action(db, settings)

    with db.session() as session:
        assert repo.list_active_constraints(session, incident_id) == []
        repo.add_constraint(
            session,
            kind=ConstraintKind.FREEZE_ACTIONS,
            actor="on-call",
            incident_id=None,
            note="freeze every incident while I check the alert volume",
        )

    execution = gateway.execute_action(action_id=action.id)

    assert execution.executed is False
    assert "freeze-actions constraint is active" in execution.detail
    assert environment.applied == {}
    with db.read_session() as session:
        stored = repo.get_action(session, action.id)
        constraints = repo.list_active_constraints(session, incident_id)
    assert stored is not None and stored.status is ActionStatus.APPROVED
    assert [constraint.kind for constraint in constraints] == [ConstraintKind.FREEZE_ACTIONS]


def test_one_global_freeze_blocks_actions_in_two_different_incidents(
    db: Database, settings: Settings
) -> None:
    """The global scope is not per-incident: it reaches every incident's action."""
    first, first_gateway, _, first_incident, first_row = prepare_approved_action(db, settings)
    second, second_gateway, _, second_incident, second_row = prepare_approved_action(db, settings)
    assert first_incident != second_incident

    with db.session() as session:
        repo.add_constraint(
            session, kind=ConstraintKind.FREEZE_ACTIONS, actor="on-call", incident_id=None
        )

    first_execution = first_gateway.execute_action(action_id=first_row.id)
    second_execution = second_gateway.execute_action(action_id=second_row.id)

    assert first_execution.executed is False
    assert second_execution.executed is False
    assert first.applied == {}
    assert second.applied == {}
    for incident_id, row in ((first_incident, first_row), (second_incident, second_row)):
        with db.read_session() as session:
            stored = repo.get_action(session, row.id)
        assert stored is not None and stored.status is ActionStatus.APPROVED
        assert incident_id in stored.incident_id


def test_the_authorization_holds_the_gate_and_releases_it_before_the_external_call(
    db: Database, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fails on the pre-fix code: nothing held a global lock across that window.

    The probe is taken after revalidation has read the control set and before the
    claim commits - exactly the window a global freeze used to slip into - and then
    again after ``execute_action`` returns, which is what proves no lock survives
    into the external call.
    """
    environment, gateway, _, _, action = prepare_approved_action(db, settings)

    real_claim = repo.claim_action
    probe_during_commit: list[bool] = []

    def claim_with_probe(session, **kwargs):
        probe_during_commit.append(probe_gate_is_free(settings))
        return real_claim(session, **kwargs)

    monkeypatch.setattr(repo, "claim_action", claim_with_probe)

    execution = gateway.execute_action(action_id=action.id)

    assert execution.executed is True
    assert environment.applied != {}
    assert probe_during_commit == [False], (
        "the authorization transaction must hold the control gate from before the control read "
        "until the claim commits"
    )
    assert probe_gate_is_free(settings) is True, (
        "the gate must be released with the authorization transaction, never held across the "
        "external call"
    )


def test_a_global_freeze_cannot_commit_inside_an_authorization_window(
    db: Database, settings: Settings
) -> None:
    """Fails on the pre-fix code, where the insert took no lock and committed at once."""
    gate_held = threading.Event()
    release = threading.Event()
    writer_done = threading.Event()
    writer_failures: list[BaseException] = []

    def hold_the_gate() -> None:
        with db.session() as session:
            repo.lock_control_gate(session)
            gate_held.set()
            release.wait(timeout=20)

    def write_a_global_freeze() -> None:
        try:
            writer = Database(settings.test_database_url)
            try:
                with writer.session() as session:
                    repo.add_constraint(
                        session,
                        kind=ConstraintKind.FREEZE_ACTIONS,
                        actor="on-call",
                        incident_id=None,
                        note="written while an authorization transaction is open",
                    )
            finally:
                writer.dispose()
        except BaseException as exc:  # surfaced by the assertions below
            writer_failures.append(exc)
        finally:
            writer_done.set()

    holder = threading.Thread(target=hold_the_gate, daemon=True)
    holder.start()
    assert gate_held.wait(timeout=20), "the test could not take the control gate"

    writer_thread = threading.Thread(target=write_a_global_freeze, daemon=True)
    writer_thread.start()
    try:
        committed_while_the_gate_was_held = writer_done.wait(timeout=1.0)
    finally:
        release.set()
        holder.join(timeout=20)
        writer_thread.join(timeout=20)

    assert committed_while_the_gate_was_held is False, (
        "a global freeze committed while the control gate was held: it could land between a "
        "dispatcher's control read and its claim, and be missed"
    )
    assert writer_failures == []
    assert writer_done.is_set(), "the writer must proceed once the gate is released"
    with db.read_session() as session:
        stored = repo.list_active_constraints(session, "inc_unrelated_incident")
    assert [constraint.kind for constraint in stored] == [ConstraintKind.FREEZE_ACTIONS]


# --- incident-scoped controls and competing workers stay correct ---------------


def test_an_incident_scoped_freeze_applies_only_to_its_own_incident(
    db: Database, settings: Settings
) -> None:
    """Guards the per-incident half of the ordering guarantee against regression."""
    first, first_gateway, _, first_incident, first_row = prepare_approved_action(db, settings)
    second, second_gateway, _, _, second_row = prepare_approved_action(db, settings)

    with db.session() as session:
        repo.add_constraint(
            session,
            kind=ConstraintKind.FREEZE_ACTIONS,
            actor="on-call",
            incident_id=first_incident,
            note="freeze this incident only",
        )

    blocked = first_gateway.execute_action(action_id=first_row.id)
    allowed = second_gateway.execute_action(action_id=second_row.id)

    assert blocked.executed is False
    assert first.applied == {}
    assert allowed.executed is True, "an incident-scoped control must not stop another incident"
    assert allowed.action.status is ActionStatus.SUCCEEDED
    assert second_row.idempotency_key in second.applied


def test_two_workers_racing_to_dispatch_one_action_apply_it_once(
    db: Database, settings: Settings
) -> None:
    """A barrier, not a sleep: both authorizations are in flight before either claims."""
    environment, _, _, _, action = prepare_approved_action(db, settings)

    barrier = threading.Barrier(2)
    guard = threading.Lock()
    executions: list[ActionExecution] = []
    failures: list[BaseException] = []

    def worker() -> None:
        database = Database(settings.test_database_url)
        try:
            worker_gateway = ToolGateway(db=database, environment=environment, settings=settings)
            barrier.wait(timeout=10)
            execution = worker_gateway.execute_action(action_id=action.id, actor="worker")
        except BaseException as exc:
            with guard:
                failures.append(exc)
        else:
            with guard:
                executions.append(execution)
        finally:
            database.dispose()

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert failures == []
    assert len(executions) == 2
    assert sorted(execution.executed for execution in executions) == [False, True]
    assert len(environment.applied) == 1
    with db.read_session() as session:
        stored = repo.get_action(session, action.id)
    assert stored is not None and stored.attempt == 1


# --- uncertainty after an execution error -------------------------------------


def test_a_change_that_landed_then_raised_is_not_recorded_as_not_applied(
    db: Database, settings: Settings
) -> None:
    """Fails on the pre-fix code, which recorded FAILED / CONFIRMED_NOT_APPLIED."""
    environment, gateway, controller, incident_id = build_case(db, settings)
    environment.faults.action_response_error = True
    controller.run(incident_id)
    grant(db, pending_approval(db, incident_id))
    action = first_action(db, incident_id)

    execution = gateway.execute_action(action_id=action.id)

    assert execution.executed is True
    assert execution.action.status is ActionStatus.UNCERTAIN
    assert execution.action.external_outcome is ActionOutcome.UNKNOWN
    assert action.idempotency_key in environment.applied
    assert environment.services["payment-service"].reachable is True
    with db.read_session() as session:
        actions = repo.list_actions(session, incident_id)
        stored = repo.get_action(session, action.id)
    assert len(actions) == 1
    assert stored is not None and stored.attempt == 1
    assert stored.status is not ActionStatus.FAILED


def test_an_uncertain_action_after_apply_is_escalated_rather_than_resolved(
    db: Database, settings: Settings
) -> None:
    """A service that looks recovered does not resolve an unresolved side effect."""
    environment, gateway, controller, incident_id = build_case(db, settings)
    environment.faults.action_response_error = True
    controller.run(incident_id)
    grant(db, pending_approval(db, incident_id))
    action = first_action(db, incident_id)
    gateway.execute_action(action_id=action.id)

    outcome = controller.resume(incident_id)

    with db.read_session() as session:
        incident = repo.get_incident(session, incident_id)
        actions = repo.list_actions(session, incident_id)
        stored = repo.get_action(session, action.id)
    assert incident is not None
    assert outcome.status is IncidentStatus.ESCALATED_INCOMPLETE
    assert incident.status is not IncidentStatus.RESOLVED
    assert len(actions) == 1, "an unresolved intent must never receive a fresh action id"
    assert stored is not None and stored.attempt == 1
    assert len(environment.applied) == 1


def test_uncertainty_survives_a_restart_because_the_rows_are_the_durable_evidence(
    db: Database, settings: Settings
) -> None:
    """A fresh adapter has no memory of the key, so the ambiguity must persist."""
    environment, gateway, controller, incident_id = build_case(db, settings)
    environment.faults.action_response_error = True
    controller.run(incident_id)
    grant(db, pending_approval(db, incident_id))
    action = first_action(db, incident_id)
    gateway.execute_action(action_id=action.id)

    # A restarted process rebuilds its adapters; only the database survives.
    restarted_environment = build_environment_for_scenario(SCENARIOS["P1-01"])
    assert restarted_environment.applied == {}
    restarted_gateway = ToolGateway(db=db, environment=restarted_environment, settings=settings)
    restarted_controller = WorkflowController(
        db=db, gateway=restarted_gateway, provider=DeterministicAdapter(), settings=settings
    )

    notes = restarted_controller.reconcile(incident_id)

    with db.read_session() as session:
        stored = repo.get_action(session, action.id)
    assert notes, "reconciliation must report what it did with the in-flight action"
    assert stored is not None
    assert stored.status is ActionStatus.UNCERTAIN
    assert stored.external_outcome is ActionOutcome.UNKNOWN


def test_a_lost_execution_result_after_the_side_effect_is_recovered_not_repeated(
    db: Database, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The crash-after-dispatch case: the change landed and the ledger write did not."""
    environment, gateway, controller, incident_id, action = prepare_approved_action(db, settings)

    def lose_the_ledger_write(*args: object, **kwargs: object) -> None:
        raise RuntimeError("connection lost while writing the tool-call ledger")

    monkeypatch.setattr(repo, "record_tool_call", lose_the_ledger_write)
    with pytest.raises(RuntimeError):
        gateway.execute_action(action_id=action.id)
    monkeypatch.undo()

    assert len(environment.applied) == 1
    with db.read_session() as session:
        stranded = repo.get_action(session, action.id)
    assert stranded is not None
    assert stranded.status is ActionStatus.DISPATCHING

    notes = controller.reconcile(incident_id)

    with db.read_session() as session:
        recovered = repo.get_action(session, action.id)
        actions = repo.list_actions(session, incident_id)
    assert notes
    assert recovered is not None
    assert recovered.status is ActionStatus.SUCCEEDED
    assert recovered.external_outcome is ActionOutcome.CONFIRMED_APPLIED
    assert recovered.attempt == 1
    assert len(actions) == 1
    assert len(environment.applied) == 1


class RefusingAdapter:
    """An adapter that can prove the request was never built, let alone sent."""

    is_simulated = True

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def apply_remediation(self, **kwargs: Any) -> ToolResult:
        raise DispatchRefused(f"refusing {kwargs.get('target')!r} before building a request")


class NotAppliedAdapter(RefusingAdapter):
    """The other honest refusal form: a result that says the change did not happen."""

    def apply_remediation(self, **kwargs: Any) -> ToolResult:
        return ToolResult(
            tool_name="apply_remediation",
            target=str(kwargs.get("target")),
            succeeded=False,
            error="the adapter declined to send this remediation",
            is_simulated=True,
            collected_at=utcnow(),
        )


@pytest.mark.parametrize("adapter", [RefusingAdapter, NotAppliedAdapter])
def test_a_proven_pre_dispatch_refusal_is_recorded_as_a_definite_non_execution(
    db: Database, settings: Settings, adapter: type
) -> None:
    """A refusal the adapter can prove stays a failure; it must not become uncertain."""
    inner = build_environment_for_scenario(SCENARIOS["P1-01"])
    surface = adapter(inner)
    _, gateway, controller, incident_id = build_case(db, settings, "P1-01", environment=surface)
    controller.run(incident_id)
    grant(db, pending_approval(db, incident_id))
    action = first_action(db, incident_id)

    execution = gateway.execute_action(action_id=action.id)

    assert execution.executed is True
    assert execution.action.status is ActionStatus.FAILED
    assert execution.action.external_outcome is ActionOutcome.CONFIRMED_NOT_APPLIED
    assert inner.applied == {}, "a refusal must not reach the environment underneath"
    assert gateway.environment is surface


# --- the paths that must not change -------------------------------------------


def test_the_successful_dispatch_path_is_unchanged(db: Database, settings: Settings) -> None:
    environment, gateway, _, _, action = prepare_approved_action(db, settings)

    execution = gateway.execute_action(action_id=action.id)

    assert execution.executed is True
    assert execution.action.status is ActionStatus.SUCCEEDED
    assert execution.action.external_outcome is ActionOutcome.CONFIRMED_APPLIED
    assert action.idempotency_key in environment.applied


def test_a_rejected_approval_still_never_dispatches(db: Database, settings: Settings) -> None:
    environment, gateway, controller, incident_id = build_case(db, settings)
    controller.run(incident_id)
    action = first_action(db, incident_id)
    reject(db, pending_approval(db, incident_id))

    # Checked before the graph resumes, so the refusal comes from the rejection
    # itself rather than from the terminal handoff that follows it.
    execution = gateway.execute_action(action_id=action.id)
    assert execution.executed is False
    assert "rejected" in execution.detail

    controller.resume(incident_id)

    assert environment.applied == {}
    with db.read_session() as session:
        stored = repo.get_action(session, action.id)
        actions = repo.list_actions(session, incident_id)
    assert stored is not None and stored.status is ActionStatus.REJECTED
    assert len(actions) == 1
