"""Pre-fix sensitivity harness for the two safety defects closed in `b508a0f`.

This file is deliberately **portable across the fixed revision and the pre-fix
revision `5cbcce2`**. That is the whole point: the regression tests in
``tests/integration/test_dispatch_safety.py`` import
``opsaccord.persistence.locks`` and pass ``commit_context=`` to
``repositories.claim_action``, neither of which exists before the fix, so at
`5cbcce2` that file fails to *import*. An import failure is not a defect
reproduction, so this harness restricts itself to APIs that both revisions have:

* the advisory-lock key is written out as a literal (``GATE_KEY``) instead of
  being imported from the module the fix introduced;
* ``repositories.claim_action`` is called by the production code, not by this
  file, and is only ever *monkeypatched* as an observation point;
* the faulty adapter is defined here rather than taken from the demo adapter's
  new fault switch.

Run it with ``scripts/prefix-sensitivity.sh``. Expected results:

| Test | `5cbcce2` | fixed |
| --- | --- | --- |
| committed global freeze blocks a later authorization | passes | passes |
| a global freeze cannot commit inside the authorization window | **FAILS** | passes |
| an error after the change landed stays uncertain | **FAILS** | passes |

The first test is the control: in both revisions a freeze that is already
committed before the dispatcher reads the control set blocks the action. It is
here so that "everything failed" cannot be mistaken for "the harness is broken".
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
)
from opsaccord.persistence import repositories as repo
from opsaccord.persistence.db import Database
from opsaccord.reasoning.deterministic import DeterministicAdapter
from opsaccord.scenarios.fixtures import SCENARIOS, build_environment_for_scenario
from opsaccord.tools.gateway import ToolGateway
from opsaccord.workflow.controller import WorkflowController

pytestmark = pytest.mark.integration

#: The control gate's advisory-lock key, as a literal. Importing the registry
#: module would make this harness unimportable on the pre-fix revision, which is
#: exactly the failure mode it exists to avoid.
GATE_KEY = (11296, 1)


class AckFailingEnvironment:
    """The demo adapter, except it fails while building its acknowledgement.

    The change is applied first, so an error raised here proves nothing about
    whether the external write happened. This is the adapter behaviour that the
    pre-fix general handler recorded as a definite non-execution.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def apply_remediation(self, *, idempotency_key: str, timeout_seconds: float, **arguments: Any):
        applied = self._inner.apply_remediation(
            idempotency_key=idempotency_key,
            timeout_seconds=timeout_seconds,
            **arguments,
        )
        assert applied.succeeded, "the harness expects the adapter to apply the change first"
        raise ValueError(
            f"malformed acknowledgement for {arguments.get('remediation_id')!r}: "
            "the response body could not be parsed"
        )


def build_case(
    db: Database, settings: Settings, scenario_id: str = "P1-01", environment: Any | None = None
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


def prepare_approved_action(db: Database, settings: Settings):
    """Run to the approval interrupt, grant it, and return the approved action."""
    environment, gateway, controller, incident_id = build_case(db, settings)
    controller.run(incident_id)
    with db.session() as session:
        approval = repo.pending_approval(session, incident_id)
        assert approval is not None
        repo.decide_approval(
            session,
            approval_id=approval.id,
            approver="on-call",
            decision=ApprovalDecision.APPROVED,
            reason="prefix sensitivity harness",
        )
    with db.read_session() as session:
        actions = repo.list_actions(session, incident_id)
    assert len(actions) == 1
    return environment, gateway, controller, incident_id, actions[0]


def gate_is_free(settings: Settings) -> bool:
    """Ask for the control gate from another backend, without ever blocking."""
    other = Database(settings.test_database_url)
    try:
        with other.session() as session:
            acquired = session.execute(
                sa.text("SELECT pg_try_advisory_xact_lock(:classid, :objid)"),
                {"classid": GATE_KEY[0], "objid": GATE_KEY[1]},
            ).scalar()
        return bool(acquired)
    finally:
        other.dispose()


def write_a_global_freeze(settings: Settings, finished: threading.Event) -> None:
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
    finally:
        finished.set()


# --- control: the harness works on both revisions -----------------------------


def test_a_committed_global_freeze_blocks_a_later_authorization(
    db: Database, settings: Settings
) -> None:
    """Control case. Passes before and after the fix, so a failing run is readable.

    The constraint table starts empty and a global constraint has no incident row,
    which is the case a row lock cannot cover.
    """
    environment, gateway, _, incident_id, action = prepare_approved_action(db, settings)
    with db.session() as session:
        assert repo.list_active_constraints(session, incident_id) == []
        repo.add_constraint(
            session,
            kind=ConstraintKind.FREEZE_ACTIONS,
            actor="on-call",
            incident_id=None,
            note="global freeze while the alert volume is checked",
        )

    execution = gateway.execute_action(action_id=action.id)

    assert execution.executed is False
    assert "freeze-actions constraint is active" in execution.detail
    assert environment.applied == {}
    with db.read_session() as session:
        stored = repo.get_action(session, action.id)
    assert stored is not None and stored.status is ActionStatus.APPROVED


# --- defect A2: the freeze could land inside the authorization window ---------


def test_a_global_freeze_cannot_commit_inside_the_authorization_window(
    db: Database, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fails on `5cbcce2`: the insert took no lock and committed at once.

    The observation point is the claim, which happens after revalidation has read
    the control set and before the authorization transaction commits. A global
    freeze committing in that interval is invisible to the dispatcher under READ
    COMMITTED, so the side effect goes out with an active freeze on record.
    """
    _, gateway, _, _, action = prepare_approved_action(db, settings)

    real_claim = repo.claim_action
    writers: list[tuple[threading.Thread, threading.Event]] = []
    observed: dict[str, Any] = {}

    def claim_with_a_racing_freeze_writer(session: Any, **kwargs: Any):
        observed["gate_was_free_inside_the_window"] = gate_is_free(settings)
        finished = threading.Event()
        thread = threading.Thread(
            target=write_a_global_freeze, args=(settings, finished), daemon=True
        )
        thread.start()
        writers.append((thread, finished))
        observed["freeze_committed_inside_the_window"] = finished.wait(timeout=1.0)
        return real_claim(session, **kwargs)

    monkeypatch.setattr(repo, "claim_action", claim_with_a_racing_freeze_writer)

    execution = gateway.execute_action(action_id=action.id)

    for thread, finished in writers:
        thread.join(timeout=20)
        assert finished.is_set(), (
            "the freeze writer never finished: after the authorization transaction committed, the "
            "control must be able to commit"
        )

    assert execution.executed is True
    problems = []
    if observed["gate_was_free_inside_the_window"] is not False:
        problems.append(
            "no exclusive lock was held across the revalidate-and-claim window, so a global "
            "control could commit inside it"
        )
    if observed["freeze_committed_inside_the_window"] is not False:
        problems.append(
            "a global freeze committed while an authorization was between its control read and "
            "its claim, and the dispatch went out anyway"
        )
    assert problems == [], "; ".join(problems)


# --- defect A1: an error after the change landed was called a non-execution ---


def test_an_error_after_the_change_landed_stays_uncertain(db: Database, settings: Settings) -> None:
    """Fails on `5cbcce2`: the general handler recorded FAILED / CONFIRMED_NOT_APPLIED."""
    environment, _, _, _, action = prepare_approved_action(db, settings)
    faulty = AckFailingEnvironment(environment)
    gateway = ToolGateway(db=db, environment=faulty, settings=settings)

    gateway.execute_action(action_id=action.id)

    assert environment.applied != {}, "the external change must have been applied before the error"
    with db.read_session() as session:
        stored = repo.get_action(session, action.id)
    assert stored is not None
    problems = []
    if stored.status is not ActionStatus.UNCERTAIN:
        problems.append(f"the action was recorded as {stored.status}, not UNCERTAIN")
    if stored.external_outcome is not ActionOutcome.UNKNOWN:
        problems.append(
            f"the external outcome was recorded as {stored.external_outcome}, not UNKNOWN"
        )
    assert problems == [], (
        "an error raised after the change was applied must stay unknown: " + "; ".join(problems)
    )
