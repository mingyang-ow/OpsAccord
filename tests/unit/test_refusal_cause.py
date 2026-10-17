"""A refusal's cause is read from the verdict, never from its prose.

The measured failure this file pins: a focused Payment verification hit the hard
step cap, and the user-visible verdict read *"a human control blocked the
dependency-path verification read (hard step cap reached)"*. The safety outcome
was right -- inconclusive, never RESOLVED -- but the cause was not: the engine
refuses a read at the cap with rule ``BUDGET_HARD_CAP``, and the gateway
described every refused read as a human control. A budget wall must never be
reported as a human act, and a human act must never be reported as a budget.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from opsaccord.config import Settings
from opsaccord.domain.enums import ConstraintKind
from opsaccord.domain.models import ActionProposal, PolicyDecision, ServiceRecord
from opsaccord.policy import engine
from opsaccord.policy.engine import RefusalCause
from opsaccord.tools.registry import ToolRegistry
from support.factories import budget, constraint, workflow

ENGINE_SOURCE = Path(engine.__file__).read_text()


def refused(rule: str, reason: str = "refused") -> PolicyDecision:
    return PolicyDecision(
        allowed=False, requires_approval=False, reasons=(reason,), matched_rule=rule
    )


@pytest.mark.parametrize(
    ("rule", "cause"),
    [
        ("BUDGET_HARD_CAP", RefusalCause.BUDGET),
        ("BUDGET_SOFT_EXHAUSTED", RefusalCause.BUDGET),
        ("HUMAN_TAKEOVER", RefusalCause.HUMAN_CONTROL),
        ("CONSTRAINT_FREEZE_ACTIONS", RefusalCause.HUMAN_CONTROL),
        ("CONSTRAINT_REQUIRE_MANUAL", RefusalCause.HUMAN_CONTROL),
        ("CONSTRAINT_DENY_ACTION", RefusalCause.HUMAN_CONTROL),
        ("CONSTRAINT_DENY_TARGET", RefusalCause.HUMAN_CONTROL),
        ("REJECTED_INTENT", RefusalCause.HUMAN_CONTROL),
        ("TOOL_MISMATCH", RefusalCause.POLICY),
        ("TOOL_TARGET_BINDING", RefusalCause.POLICY),
        ("TARGET_NOT_ALLOW_LISTED", RefusalCause.POLICY),
        ("TARGET_NOT_REGISTERED", RefusalCause.POLICY),
        ("REMEDIATION_NOT_PERMITTED", RefusalCause.POLICY),
        ("AUTONOMOUS_REMEDIATION_NOT_ALLOW_LISTED", RefusalCause.POLICY),
    ],
)
def test_each_real_rule_id_maps_to_its_cause(rule: str, cause: RefusalCause) -> None:
    assert engine.refusal_cause(refused(rule)) is cause


def test_an_unclassified_rule_is_a_policy_refusal_never_a_human_or_a_budget() -> None:
    """The fallback has to fail safe: an unknown rule is not evidence of a human.

    Nothing classifies an unknown id as ``HUMAN_CONTROL`` or ``BUDGET``, so a rule
    added without being classified cannot be described as something it is not.
    """
    assert engine.refusal_cause(refused("SOME_FUTURE_RULE")) is RefusalCause.POLICY


def _refusal_rules_in_the_engine_source() -> set[str]:
    """Every ``matched_rule`` the engine returns with ``allowed=False``.

    Read from the source rather than from a hand-kept list so that a rule added
    later is caught here instead of silently becoming an unclassified refusal.
    The one templated rule (``f"CONSTRAINT_{constraint.kind}"``) is expanded from
    the enum, so renaming a constraint kind cannot slip past either.
    """
    found: set[str] = set()
    for block in ENGINE_SOURCE.split("PolicyDecision(")[1:]:
        if "allowed=False" not in block:
            continue
        for line in block.splitlines():
            match = re.search(r'matched_rule=(?:f)?"([^"]+)"', line)
            if match is None:
                continue
            raw = match.group(1)
            template = re.fullmatch(r"CONSTRAINT_\{(.*)\}", raw)
            if template is not None:
                assert template.group(1) == "constraint.kind", raw
                found.update(f"CONSTRAINT_{kind.value}" for kind in ConstraintKind)
            else:
                found.add(raw)
            break
    return found


def test_the_classification_covers_every_refusal_rule_the_engine_can_return() -> None:
    """A rule id the engine returns without a cause is a classification bug.

    This is what keeps the mapping honest: the three sets below are the whole
    vocabulary of refusals, in both directions, so neither a new rule nor a
    deleted one can drift away from the cause a human is shown.
    """
    found = _refusal_rules_in_the_engine_source()
    assert len(found) >= 13, f"the source scan looks stale: {sorted(found)}"
    classified = engine.BUDGET_RULES | engine.HUMAN_CONTROL_RULES | engine.POLICY_RULES
    assert found - classified == set(), f"refusal rules with no cause: {sorted(found - classified)}"
    assert classified - found == set(), (
        f"classified rules the engine no longer returns: {sorted(classified - found)}"
    )


# --- the engine's actual verdicts for a read ------------------------------------
#
# The mapping above is only useful if the engine really returns those rule ids for
# the refusals a verification read can meet. These build real PolicyRequests (a
# real tool spec, a real workflow, real constraints), so a rule rename or a
# reordering fails here rather than in a live run.


def read_request(
    *,
    constraints: tuple[object, ...] = (),
    budget_state: object | None = None,
    takeover: str | None = None,
) -> engine.PolicyRequest:
    proposal = ActionProposal(
        tool_name="read_dependency",
        target="checkout-service",
        arguments={
            "target": "checkout-service",
            "dependency": "payment-service",
            "window_seconds": 120,
        },
        rationale="unit-test read",
    )
    return engine.PolicyRequest(
        proposal=proposal,
        tool=ToolRegistry.default().require("read_dependency"),
        service=ServiceRecord(
            name="checkout-service",
            display_name="Checkout",
            tier="critical",
            owner="checkout-oncall",
            auto_remediation="forbidden",
            description="Takes orders and calls the payment service.",
        ),
        workflow=workflow(takeover_actor=takeover),
        incident_service="checkout-service",
        constraints=constraints,
        rejected_intents=frozenset(),
        budget=budget_state or budget(),
        settings=Settings(),
    )


def test_a_spent_cap_refuses_a_read_with_the_budget_rule() -> None:
    """The measured failure's verdict: a budget wall, in policy-engine clothing."""
    decision = engine.evaluate(read_request(budget_state=budget(steps_used=16, hard_step_cap=16)))

    assert decision.allowed is False
    assert decision.matched_rule == "BUDGET_HARD_CAP"
    assert engine.refusal_cause(decision) is RefusalCause.BUDGET


def test_a_tool_scoped_human_denial_refuses_a_read_with_the_constraint_rule() -> None:
    decision = engine.evaluate(
        read_request(
            constraints=(constraint(kind=ConstraintKind.DENY_ACTION, tool_name="read_dependency"),)
        )
    )

    assert decision.allowed is False
    assert decision.matched_rule == "CONSTRAINT_DENY_ACTION"
    assert engine.refusal_cause(decision) is RefusalCause.HUMAN_CONTROL


def test_a_takeover_refuses_a_read_as_a_human_cause() -> None:
    decision = engine.evaluate(read_request(takeover="on-call"))

    assert decision.allowed is False
    assert decision.matched_rule == "HUMAN_TAKEOVER"
    assert engine.refusal_cause(decision) is RefusalCause.HUMAN_CONTROL


def test_a_read_inside_its_budget_is_still_allowed() -> None:
    """The fix classifies refusals; it must not create one."""
    decision = engine.evaluate(read_request())

    assert decision.allowed is True
    assert decision.matched_rule == "ALLOW_READ_ONLY"
