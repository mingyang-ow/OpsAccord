"""An approved fix that is no longer needed is withheld, never run (evaluation concept #9).

The need is re-checked just before dispatch for any action that waited for a human. Only a
proven recovery withholds it; the incident still resolves only on full, fresh
verification; and withholding can never race a dispatch that has already claimed it.
"""

from __future__ import annotations

import pytest

from opsaccord.config import Settings
from opsaccord.domain.enums import ActionOutcome, ActionStatus, ApprovalStatus, VerificationOutcome
from opsaccord.persistence import repositories as repo
from opsaccord.persistence.db import Database
from opsaccord.scenarios.fixtures import SCENARIOS
from opsaccord.scenarios.runner import evaluate, run_scenario

pytestmark = pytest.mark.integration


def test_a_fix_that_is_no_longer_needed_is_withheld_and_recovery_is_verified(
    db: Database, settings: Settings
) -> None:
    trace = run_scenario(SCENARIOS["P2-01"], db=db, settings=settings)

    outcome, reasons = evaluate(trace)
    assert str(outcome) == "PASS", reasons
    assert trace.executed_actions == []
    (action,) = trace.actions
    assert action.status is ActionStatus.CANCELLED
    assert action.external_outcome is ActionOutcome.CONFIRMED_NOT_APPLIED
    assert action.dispatch_started_at is None
    # The human approved, but the approval was never consumed by a dispatch.
    assert [a.status for a in trace.approvals] == [ApprovalStatus.APPROVED]
    # RESOLVED rests on verification, not on the need check.
    assert any(v.outcome is VerificationOutcome.VERIFICATION_SUCCESS for v in trace.verifications)
    assert any(note.startswith("withheld: ") for note in trace.run_notes)


def test_a_fix_that_is_still_needed_runs_as_approved(db: Database, settings: Settings) -> None:
    trace = run_scenario(SCENARIOS["P1-01"], db=db, settings=settings)

    assert len(trace.executed_actions) == 1
    assert any(
        note.startswith("need check: payment-service still needs") for note in trace.run_notes
    )


def test_an_autonomous_action_skips_the_need_check(db: Database, settings: Settings) -> None:
    """No human wait, so no window for the fault to clear: no extra reads are spent."""
    trace = run_scenario(SCENARIOS["P1-02"], db=db, settings=settings)

    assert len(trace.executed_actions) == 1
    assert not any(note.startswith(("need check:", "withheld:")) for note in trace.run_notes)


def test_withholding_never_touches_an_action_a_dispatch_has_claimed(
    db: Database, settings: Settings
) -> None:
    trace = run_scenario(SCENARIOS["P1-01"], db=db, settings=settings)
    (action,) = trace.executed_actions

    with db.session() as session:
        assert repo.withhold_action(session, action_id=action.id, reason="too late") is None
        after = repo.get_action(session, action.id)
    assert after is not None and after.status is not ActionStatus.CANCELLED
