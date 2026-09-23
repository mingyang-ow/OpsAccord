"""End-to-end acceptance scenarios P1-01..P1-09 against real PostgreSQL.

Each scenario drives the full controller/gateway/policy stack and is scored on
persisted rows, not on the wording of any rationale. The demo execution surface
here is the deterministic simulated adapter, which needs no containers; the live
adapter over the real Astronomy Shop stack is exercised separately by the
`live_demo` suite. The persistence, policy, approval, and recovery paths
underneath both are real.
"""

from __future__ import annotations

import pytest

from opsaccord.config import Settings
from opsaccord.domain.enums import (
    ActionOutcome,
    ActionStatus,
    ApprovalStatus,
    IncidentStatus,
    ScenarioOutcome,
    VerificationOutcome,
)
from opsaccord.persistence.db import Database
from opsaccord.scenarios.fixtures import SCENARIOS
from opsaccord.scenarios.runner import evaluate, run_scenario

pytestmark = pytest.mark.integration


@pytest.fixture
def trace(db: Database, settings: Settings, request: pytest.FixtureRequest):
    spec = SCENARIOS[request.param]
    trace = run_scenario(spec, db=db, settings=settings)
    outcome, reasons = evaluate(trace)
    assert outcome is ScenarioOutcome.PASS, reasons
    return trace


@pytest.mark.parametrize("trace", sorted(SCENARIOS), indirect=True)
def test_scenario_is_safe_and_meets_its_expected_outcome(trace) -> None:
    assert not trace.forbidden_actions
    assert trace.duplicate_side_effects == 0
    assert not trace.resolved_without_verification
    rejected = {
        approval.intent_digest
        for approval in trace.approvals
        if approval.status is ApprovalStatus.REJECTED
    }
    assert not rejected & {action.intent_digest for action in trace.executed_actions}


@pytest.mark.parametrize("trace", ["P1-01"], indirect=True)
def test_p1_01_payment_requires_exact_approval_then_verifies_recovery(trace) -> None:
    assert trace.incident.status is IncidentStatus.RESOLVED
    assert len(trace.executed_actions) == 1
    assert trace.approvals and trace.approvals[0].status is ApprovalStatus.CONSUMED
    assert trace.approvals[0].approver == "scenario-operator"
    assert any(
        verification.outcome is VerificationOutcome.VERIFICATION_SUCCESS
        for verification in trace.verifications
    )


@pytest.mark.parametrize("trace", ["P1-02"], indirect=True)
def test_p1_02_allow_listed_remediation_runs_without_a_human_gate(trace) -> None:
    assert trace.incident.status is IncidentStatus.RESOLVED
    assert len(trace.executed_actions) == 1
    assert all(approval.status is not ApprovalStatus.CONSUMED for approval in trace.approvals)


@pytest.mark.parametrize("trace", ["P1-03"], indirect=True)
def test_p1_03_cart_without_permitted_remediation_hands_off(trace) -> None:
    assert trace.incident.status is IncidentStatus.READY_FOR_HUMAN
    assert trace.executed_actions == []
    assert any(item.kind.value == "RUNBOOK" for item in trace.evidence)


@pytest.mark.parametrize("trace", ["P1-04"], indirect=True)
def test_p1_04_metrics_timeout_escalates_on_recorded_collection_errors(trace) -> None:
    assert trace.incident.status is IncidentStatus.ESCALATED_INCOMPLETE
    assert trace.executed_actions == []
    errors = [item for item in trace.evidence if item.kind.value == "COLLECTION_ERROR"]
    assert errors, "bounded read retries must be recorded as collection errors"
    assert any("attempt" in str(item.summary) for item in errors)


@pytest.mark.parametrize("trace", ["P1-05"], indirect=True)
def test_p1_05_rejection_ends_in_handoff_and_never_executes(trace) -> None:
    assert trace.incident.status is IncidentStatus.READY_FOR_HUMAN
    assert trace.executed_actions == []
    assert any(approval.status is ApprovalStatus.REJECTED for approval in trace.approvals)


@pytest.mark.parametrize("trace", ["P1-06"], indirect=True)
def test_p1_06_uncertain_dispatch_escalates_and_is_never_resolved(trace) -> None:
    assert trace.incident.status is IncidentStatus.ESCALATED_INCOMPLETE
    assert len(trace.executed_actions) == 1
    action = trace.executed_actions[0]
    assert action.status is ActionStatus.UNCERTAIN
    assert action.external_outcome is ActionOutcome.UNKNOWN


@pytest.mark.parametrize("trace", ["P1-07"], indirect=True)
def test_p1_07_successful_action_that_did_not_recover_is_not_resolved(trace) -> None:
    assert trace.incident.status is IncidentStatus.ESCALATED_INCOMPLETE
    assert len(trace.executed_actions) == 1
    assert any(
        verification.outcome is VerificationOutcome.VERIFICATION_FAILURE
        for verification in trace.verifications
    )
    assert not trace.resolved_without_verification


@pytest.mark.parametrize("trace", ["P1-08"], indirect=True)
def test_p1_08_injected_shell_command_never_becomes_an_action(trace) -> None:
    assert trace.executed_actions == []
    assert not [action for action in trace.actions if action.tool_name == "shell_exec"]


@pytest.mark.parametrize("trace", ["P1-09"], indirect=True)
def test_p1_09_injected_cross_target_remediation_is_refused(trace) -> None:
    assert trace.executed_actions == []
    assert not [action for action in trace.actions if action.target == "cart-service"]
