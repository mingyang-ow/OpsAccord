"""Deterministic policy rules. No database, no model, no I/O."""

from __future__ import annotations

import pytest

from opsaccord.domain.enums import ConstraintKind, IncidentStatus
from opsaccord.domain.models import ActionProposal
from opsaccord.policy import engine
from support.factories import budget, constraint, policy_request, workflow


def remediation(target: str, flag: str) -> ActionProposal:
    return ActionProposal(
        tool_name="apply_remediation",
        target=target,
        arguments={
            "target": target,
            "remediation_id": "disable_feature_flag",
            "flag_id": flag,
        },
        rationale=f"disable {flag} on {target}",
    )


def test_read_only_tools_are_permitted_within_budget() -> None:
    proposal = ActionProposal(
        tool_name="read_metrics",
        target="payment-service",
        arguments={"target": "payment-service", "metric": "error_rate", "window_seconds": 300},
        rationale="collect error-rate evidence",
    )
    decision = engine.evaluate(policy_request(proposal=proposal))
    assert decision.allowed is True
    assert decision.requires_approval is False
    assert decision.matched_rule == "ALLOW_READ_ONLY"


def test_target_outside_the_fixed_demo_allow_list_is_denied() -> None:
    proposal = remediation("shipping-service", "paymentUnreachable")
    request = policy_request(proposal=proposal, service="payment-service")
    decision = engine.evaluate(request)
    assert decision.allowed is False
    assert decision.matched_rule == "TARGET_NOT_ALLOW_LISTED"


def test_unregistered_service_is_denied() -> None:
    proposal = remediation("payment-service", "paymentUnreachable")
    request = policy_request(proposal=proposal, service="not-a-service")
    decision = engine.evaluate(request)
    assert decision.allowed is False
    assert decision.matched_rule == "TARGET_NOT_REGISTERED"


def test_human_takeover_suspends_automatic_execution() -> None:
    proposal = remediation("ad-service", "adHighCpu")
    request = policy_request(
        proposal=proposal, service="ad-service", wf=workflow(takeover_actor="on-call")
    )
    decision = engine.evaluate(request)
    assert decision.allowed is False
    assert decision.matched_rule == "HUMAN_TAKEOVER"


@pytest.mark.parametrize(
    ("kind", "matched_rule"),
    [
        (ConstraintKind.FREEZE_ACTIONS, "CONSTRAINT_FREEZE_ACTIONS"),
        (ConstraintKind.REQUIRE_MANUAL, "CONSTRAINT_REQUIRE_MANUAL"),
    ],
)
def test_global_human_constraints_precede_every_other_permission(
    kind: ConstraintKind, matched_rule: str
) -> None:
    proposal = remediation("ad-service", "adHighCpu")
    request = policy_request(
        proposal=proposal,
        service="ad-service",
        constraints=(constraint(kind=kind, tool_name="apply_remediation"),),
    )
    decision = engine.evaluate(request)
    assert decision.allowed is False
    assert decision.matched_rule == matched_rule
    assert decision.blocking_constraints


def test_deny_target_constraint_blocks_only_that_target() -> None:
    blocked = engine.evaluate(
        policy_request(
            proposal=remediation("ad-service", "adHighCpu"),
            service="ad-service",
            constraints=(constraint(kind=ConstraintKind.DENY_TARGET, target="ad-service"),),
        )
    )
    assert blocked.allowed is False
    assert blocked.matched_rule == "CONSTRAINT_DENY_TARGET"

    other = engine.evaluate(
        policy_request(
            proposal=remediation("ad-service", "adHighCpu"),
            service="ad-service",
            constraints=(constraint(kind=ConstraintKind.DENY_TARGET, target="cart-service"),),
        )
    )
    assert other.allowed is True


def test_allow_listed_target_runs_without_a_human_approval() -> None:
    decision = engine.evaluate(
        policy_request(proposal=remediation("ad-service", "adHighCpu"), service="ad-service")
    )
    assert decision.allowed is True
    assert decision.requires_approval is False
    assert decision.matched_rule == "ALLOW_LISTED_REMEDIATION"


def test_service_without_supported_remediation_hands_off() -> None:
    decision = engine.evaluate(
        policy_request(proposal=remediation("cart-service", "cartFailure"), service="cart-service")
    )
    assert decision.allowed is False
    assert decision.matched_rule == "REMEDIATION_NOT_PERMITTED"


def test_allow_listed_posture_without_allow_listed_target_is_denied() -> None:
    """A permissive posture is not enough; the target allow-list still applies."""
    from opsaccord.config import Settings

    narrowed = Settings(allow_listed_remediation_targets=())
    decision = engine.evaluate(
        policy_request(
            proposal=remediation("ad-service", "adHighCpu"),
            service="ad-service",
            settings=narrowed,
        )
    )
    assert decision.allowed is False
    assert decision.matched_rule == "AUTONOMOUS_REMEDIATION_NOT_ALLOW_LISTED"


def test_critical_target_requires_an_exact_human_approval() -> None:
    decision = engine.evaluate(
        policy_request(
            proposal=remediation("payment-service", "paymentUnreachable"),
            service="payment-service",
        )
    )
    assert decision.allowed is True
    assert decision.requires_approval is True
    assert decision.matched_rule == "REQUIRE_EXACT_APPROVAL"


def test_rejected_intent_cannot_be_re_proposed_under_a_new_row() -> None:
    proposal = remediation("payment-service", "paymentUnreachable")
    digest = engine.Action.compute_intent_digest(
        proposal.tool_name, proposal.target, proposal.arguments
    )
    decision = engine.evaluate(
        policy_request(
            proposal=proposal,
            service="payment-service",
            rejected_intents=frozenset({digest}),
        )
    )
    assert decision.allowed is False
    assert decision.matched_rule == "REJECTED_INTENT"


def test_budget_walls_are_hard() -> None:
    soft = engine.evaluate(
        policy_request(
            proposal=remediation("ad-service", "adHighCpu"),
            service="ad-service",
            budget_state=budget(steps_used=8, soft_step_budget=8, hard_step_cap=16),
        )
    )
    assert soft.allowed is False
    assert soft.matched_rule == "BUDGET_SOFT_EXHAUSTED"

    hard = engine.evaluate(
        policy_request(
            proposal=remediation("ad-service", "adHighCpu"),
            service="ad-service",
            budget_state=budget(steps_used=16, soft_step_budget=8, hard_step_cap=16),
        )
    )
    assert hard.allowed is False
    assert hard.matched_rule == "BUDGET_HARD_CAP"


def test_hard_cap_also_blocks_reads() -> None:
    proposal = ActionProposal(
        tool_name="read_service_health",
        target="ad-service",
        arguments={"target": "ad-service"},
        rationale="collect health evidence",
    )
    decision = engine.evaluate(
        policy_request(
            proposal=proposal,
            service="ad-service",
            budget_state=budget(steps_used=16, hard_step_cap=16),
        )
    )
    assert decision.allowed is False
    assert decision.matched_rule == "BUDGET_HARD_CAP"


def test_terminal_statuses_are_recognised() -> None:
    assert engine.is_terminal(IncidentStatus.RESOLVED)
    assert engine.is_terminal(IncidentStatus.READY_FOR_HUMAN)
    assert not engine.is_terminal(IncidentStatus.INVESTIGATING)
