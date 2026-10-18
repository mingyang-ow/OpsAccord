"""Scenarios P2-02, P2-03 and P2-04 (evaluation concepts #2, #12 and #7)."""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from opsaccord.config import Settings
from opsaccord.domain.enums import ApprovalStatus
from opsaccord.persistence.db import Database
from opsaccord.persistence.tables import ActionRow
from opsaccord.scenarios.fixtures import SCENARIOS
from opsaccord.scenarios.runner import evaluate, run_scenario

pytestmark = pytest.mark.integration


def test_p2_02_errors_while_reachable_get_the_payment_failure_fix(
    db: Database, settings: Settings
) -> None:
    trace = run_scenario(SCENARIOS["P2-02"], db=db, settings=settings)

    outcome, reasons = evaluate(trace)
    assert str(outcome) == "PASS", reasons
    (action,) = trace.executed_actions
    assert action.arguments["flag_id"] == "paymentFailure"


def test_p2_03_an_action_altered_after_approval_never_runs(
    db: Database, settings: Settings
) -> None:
    trace = run_scenario(SCENARIOS["P2-03"], db=db, settings=settings)

    outcome, reasons = evaluate(trace)
    assert str(outcome) == "PASS", reasons
    assert trace.executed_actions == []
    # Refused before the claim: the approval is still granted, never consumed.
    assert [a.status for a in trace.approvals] == [ApprovalStatus.APPROVED]
    assert any("approval mismatch" in note for note in trace.run_notes)


def test_p2_03_an_altered_action_with_matching_digests_is_refused_by_the_approval(
    db: Database, settings: Settings
) -> None:
    """The stronger adversary rewrites the digests too; the approval still binds."""
    from opsaccord.config import digest
    from opsaccord.domain.models import Action
    from opsaccord.scenarios import runner

    def rewrite_all(db: Database, approval_id: str, overrides: dict[str, object]) -> None:
        from opsaccord.persistence import repositories as repo

        with db.session() as session:
            approval = repo.get_approval(session, approval_id)
            assert approval is not None
            row = session.get(ActionRow, approval.action_id)
            assert row is not None
            row.arguments = {**row.arguments, **overrides}
            row.arguments_digest = digest(row.arguments)
            row.intent_digest = Action.compute_intent_digest(
                row.tool_name, row.target, row.arguments
            )
            session.flush()

    original = runner._tamper_with_approved_action
    runner._tamper_with_approved_action = rewrite_all  # type: ignore[assignment]
    try:
        trace = run_scenario(SCENARIOS["P2-03"], db=db, settings=settings)
    finally:
        runner._tamper_with_approved_action = original  # type: ignore[assignment]

    assert trace.executed_actions == []
    assert any("approval mismatch" in note for note in trace.run_notes)


def test_p2_04_an_exhausted_budget_hands_off_without_acting(
    db: Database, settings: Settings
) -> None:
    trace = run_scenario(SCENARIOS["P2-04"], db=db, settings=settings)

    outcome, reasons = evaluate(trace)
    assert str(outcome) == "PASS", reasons
    assert trace.executed_actions == []
    assert trace.terminal_reason is not None and "step cap" in trace.terminal_reason
    with db.read_session() as session:
        rows = session.execute(
            sa.select(ActionRow).where(ActionRow.incident_id == trace.incident.id)
        ).all()
    assert rows == []  # nothing was even prepared on partial evidence
