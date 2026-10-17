"""A refused verification read is described by its cause, offline.

The measured failure: the hard step cap refused the dependency-path read, and
the verdict said *"a human control blocked the dependency-path verification
read (hard step cap reached)"*. The outcome was correct (inconclusive, never
RESOLVED); the cause was not, because the engine's budget wall arrives as a
policy refusal and the gateway labelled every policy refusal a human control.

These tests drive the real round and the real verdict against a scripted
gateway, so the classification is pinned without a database: the acceptance
suite proves the same behaviour through persistence, and this file proves it
here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from opsaccord.config import Settings
from opsaccord.domain.enums import VerificationOutcome
from opsaccord.domain.models import PolicyDecision
from opsaccord.persistence import repositories as repo
from opsaccord.policy.engine import RefusalCause
from opsaccord.tools.contracts import ToolResult
from opsaccord.tools.gateway import PolicyBlocked, ReadOutcome, ToolGateway

#: The action completed before the round, so every read below is "fresh".
ACTION_COMPLETED_AT = datetime(2026, 9, 24, 8, 0, tzinfo=UTC)
COLLECTED_AT = ACTION_COMPLETED_AT + timedelta(seconds=30)

HEALTH = "read_service_health"
METRICS = "read_metrics"
EDGE = "read_dependency"


class _FakeSession:
    """The minimum `with db.session()` contract: enter, do nothing, leave."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: object) -> bool:
        return False


@dataclass
class _FakeDb:
    def session(self) -> _FakeSession:
        return _FakeSession()


@dataclass
class FakeGateway:
    """A gateway whose reads are scripted, with the round and verdict as real.

    Only ``read`` is replaced. ``_collect_verification_evidence`` and
    ``verify_service`` are the repository's own code, called unbound, so this is
    a test of those two paths rather than a re-implementation of them.
    """

    script: dict[str, Any]
    settings: Settings = field(default_factory=Settings)
    calls: list[str] = field(default_factory=list)
    db: Any = field(default_factory=_FakeDb)

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
        self.calls.append(tool_name)
        outcome = self.script[tool_name]
        if callable(outcome):
            outcome = outcome()
        if isinstance(outcome, BaseException):
            raise outcome
        return ReadOutcome(result=outcome, evidence_id=f"evd-{len(self.calls)}")

    def _collect_verification_evidence(self, **kwargs: Any) -> Any:
        # The real round, so `verify_service` exercises the code under test
        # rather than a stand-in for it.
        return ToolGateway._collect_verification_evidence(self, **kwargs)


def health_result() -> ToolResult:
    return ToolResult(
        tool_name=HEALTH,
        target="payment-service",
        succeeded=True,
        value={
            "health": "SERVING",
            "reachable": True,
            "healthy": True,
            "cpu_utilization": 0.1,
            "error_rate": 0.0,
        },
        is_simulated=True,
        collected_at=COLLECTED_AT,
    )


def metrics_result() -> ToolResult:
    return ToolResult(
        tool_name=METRICS,
        target="payment-service",
        succeeded=True,
        value={"metric": "error_rate", "value": 0.0},
        is_simulated=True,
        collected_at=COLLECTED_AT,
    )


def healthy_edge_result() -> ToolResult:
    return ToolResult(
        tool_name=EDGE,
        target="checkout-service",
        succeeded=True,
        value={
            "caller": "checkout-service",
            "destination": "payment-service",
            "operation": "Charge",
            "span_kind": "client",
            "attributable": True,
            "calls_per_second": 0.4,
            "errors_per_second": 0.0,
            "error_rate": 0.0,
        },
        is_simulated=True,
        collected_at=COLLECTED_AT,
    )


def budget_exhausted() -> repo.BudgetExhausted:
    return repo.BudgetExhausted("hard step cap reached (36); refusing further work")


def policy_refusal(rule: str, reason: str) -> PolicyBlocked:
    return PolicyBlocked(
        PolicyDecision(allowed=False, requires_approval=False, reasons=(reason,), matched_rule=rule)
    )


def round_for(gateway: FakeGateway) -> Any:
    return ToolGateway._collect_verification_evidence(
        gateway,
        incident_id="inc-1",
        workflow_id="wf-1",
        target="payment-service",
        action_completed_at=ACTION_COMPLETED_AT,
    )


def verdict_for(gateway: FakeGateway) -> Any:
    return ToolGateway.verify_service(
        gateway,
        incident_id="inc-1",
        workflow_id="wf-1",
        target="payment-service",
        action_id="act-1",
        action_completed_at=ACTION_COMPLETED_AT,
    )


def good_reads(edge: Any) -> dict[str, Any]:
    return {HEALTH: health_result(), METRICS: metrics_result(), EDGE: edge}


@pytest.fixture
def persisted(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture what ``verify_service`` would persist, without a database.

    The assertion that matters is on the recorded row, because that is what a
    human reads later; the repository call itself is unchanged.
    """
    rows: list[dict[str, Any]] = []

    def record(session: Any, **kwargs: Any) -> None:
        rows.append(kwargs)

    monkeypatch.setattr(repo, "add_verification", record)
    return rows


def test_a_budget_exception_is_a_budget_cause_not_a_human_one() -> None:
    """The repository's own wall: reached before the read, never charged to a human."""
    gateway = FakeGateway(script=good_reads(budget_exhausted()))
    round_ = round_for(gateway)

    assert round_.blocked is not None
    assert round_.blocked.cause is RefusalCause.BUDGET
    assert "no longer affordable" in round_.blocked.detail
    assert "human" not in round_.blocked.detail
    assert round_.dependency is not None and round_.dependency.indeterminate


def test_a_hard_cap_refused_by_policy_is_still_a_budget_cause() -> None:
    """The measured failure, in the exact shape the engine produced it.

    ``BUDGET_HARD_CAP`` arrives as a ``PolicyBlocked`` with the reason
    "hard step cap reached". Every word of that is a budget fact, so the verdict
    must not say a human control did it.
    """
    gateway = FakeGateway(
        script=good_reads(policy_refusal("BUDGET_HARD_CAP", "hard step cap reached"))
    )
    round_ = round_for(gateway)

    assert round_.blocked is not None
    assert round_.blocked.cause is RefusalCause.BUDGET
    assert round_.blocked.detail.startswith(
        "the dependency-path verification read is no longer affordable "
    )
    assert "hard step cap reached" in round_.blocked.detail
    assert "human" not in round_.blocked.detail


@pytest.mark.parametrize(
    ("rule", "reason"),
    [
        ("HUMAN_TAKEOVER", "human takeover is active (actor=alex); suspended"),
        ("CONSTRAINT_FREEZE_ACTIONS", "freeze-actions constraint c-1 is active"),
        ("CONSTRAINT_REQUIRE_MANUAL", "manual-handling constraint c-2 is active"),
    ],
    ids=["takeover", "freeze", "manual-handling"],
)
def test_a_human_control_is_reported_as_one(rule: str, reason: str) -> None:
    gateway = FakeGateway(script=good_reads(policy_refusal(rule, reason)))
    round_ = round_for(gateway)

    assert round_.blocked is not None
    assert round_.blocked.cause is RefusalCause.HUMAN_CONTROL
    assert round_.blocked.detail.startswith(
        "a human control blocked the dependency-path verification read"
    )
    assert reason in round_.blocked.detail


def test_another_policy_refusal_is_neither_a_human_nor_a_budget() -> None:
    """The third cause has to be reachable, or the other two are not honest.

    A target outside the allow-list is a policy refusal with no human and no
    budget behind it. Reporting it as either would be a misattribution of the
    same kind as the one this pass fixes.
    """
    gateway = FakeGateway(
        script=good_reads(policy_refusal("TARGET_NOT_ALLOW_LISTED", "target is not allowed"))
    )
    round_ = round_for(gateway)

    assert round_.blocked is not None
    assert round_.blocked.cause is RefusalCause.POLICY
    assert round_.blocked.detail.startswith("policy refused the dependency-path verification read")
    assert "human" not in round_.blocked.detail
    assert "no longer affordable" not in round_.blocked.detail


@pytest.mark.parametrize(
    ("rule", "reason", "cause", "expected"),
    [
        (
            "CONSTRAINT_FREEZE_ACTIONS",
            "freeze c-1 is active",
            RefusalCause.HUMAN_CONTROL,
            "a human control blocked the verification reads",
        ),
        (
            "BUDGET_HARD_CAP",
            "hard step cap reached",
            RefusalCause.BUDGET,
            "the verification reads are no longer affordable",
        ),
        (
            "TARGET_NOT_ALLOW_LISTED",
            "target is not allowed",
            RefusalCause.POLICY,
            "policy refused the verification reads",
        ),
    ],
    ids=["freeze", "hard-cap", "allow-list"],
)
def test_a_refused_probe_is_described_by_its_cause(
    rule: str, reason: str, cause: RefusalCause, expected: str
) -> None:
    """The probe reads carry the same three causes as the path read."""
    gateway = FakeGateway(
        script=good_reads(healthy_edge_result()) | {HEALTH: policy_refusal(rule, reason)}
    )
    round_ = round_for(gateway)

    assert round_.blocked is not None
    assert round_.blocked.cause is cause
    assert round_.blocked.detail == f"{expected} ({reason})"
    # The prose follows the cause in both directions: no budget wording on a
    # human act, and no human wording on a budget wall.
    assert ("human" in round_.blocked.detail) == (cause is RefusalCause.HUMAN_CONTROL)
    assert ("no longer affordable" in round_.blocked.detail) == (cause is RefusalCause.BUDGET)
    assert round_.health is None, "a refused probe must not be reported as a reading"


def test_a_spent_cap_on_a_probe_keeps_the_reads_that_did_succeed() -> None:
    """A refusal stops the round: no further read, and the cause stays the first one."""
    gateway = FakeGateway(script=good_reads(healthy_edge_result()) | {METRICS: budget_exhausted()})
    round_ = round_for(gateway)

    assert round_.blocked is not None and round_.blocked.cause is RefusalCause.BUDGET
    assert round_.health is not None, "the probe that did succeed is still evidence"
    assert round_.evidence_ids == ["evd-1"]
    assert gateway.calls == [HEALTH, METRICS], "a refused round must not read on"


def test_a_refused_read_never_becomes_success_and_is_not_retried(
    persisted: list[dict[str, Any]],
) -> None:
    """The safety property, at the verdict: inconclusive, and no read after the wall."""
    gateway = FakeGateway(
        script=good_reads(policy_refusal("BUDGET_HARD_CAP", "hard step cap reached"))
    )
    result = verdict_for(gateway)

    assert result.outcome is VerificationOutcome.VERIFICATION_INCONCLUSIVE
    assert "no longer affordable" in result.detail
    assert "human" not in result.detail
    # Exactly one round of reads, in order, and nothing attempted after the wall.
    assert gateway.calls == [HEALTH, METRICS, METRICS, EDGE]
    # And the row a human later reads says the same thing as the in-flight verdict.
    assert [row["result"].outcome for row in persisted] == [
        VerificationOutcome.VERIFICATION_INCONCLUSIVE
    ]
    assert "human" not in persisted[0]["result"].detail
    assert persisted[0]["action_id"] == "act-1"


def test_a_round_that_is_not_refused_still_resolves(persisted: list[dict[str, Any]]) -> None:
    """The fix must not turn a genuine recovery into a handoff."""
    round_ = round_for(FakeGateway(script=good_reads(healthy_edge_result())))
    assert round_.blocked is None
    assert round_.dependency is not None and round_.dependency.healthy

    gateway = FakeGateway(script=good_reads(healthy_edge_result()))
    result = verdict_for(gateway)
    assert result.outcome is VerificationOutcome.VERIFICATION_SUCCESS
    assert result.evidence_ids == ("evd-1", "evd-2", "evd-3", "evd-4")
    assert persisted[0]["result"].outcome is VerificationOutcome.VERIFICATION_SUCCESS
