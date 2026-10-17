"""Typed factories for pure unit tests.

These build domain records directly so that policy, registry, and adapter rules
can be tested without a database. They deliberately construct real pydantic
models rather than stand-ins, so a contract change fails the unit suite too.
"""

from __future__ import annotations

from datetime import UTC, datetime

from opsaccord.domain.enums import (
    ConstraintKind,
    IncidentStatus,
    WorkflowOutcome,
)
from opsaccord.domain.models import (
    ActionProposal,
    BudgetState,
    Constraint,
    ServiceRecord,
    Workflow,
)
from opsaccord.policy.engine import PolicyRequest
from opsaccord.tools.registry import ToolRegistry

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

SERVICES: dict[str, ServiceRecord] = {
    "payment-service": ServiceRecord(
        name="payment-service",
        display_name="Payment",
        tier="critical",
        owner="payments-oncall",
        auto_remediation="forbidden",
        description="Authorises and captures customer payments.",
    ),
    "ad-service": ServiceRecord(
        name="ad-service",
        display_name="Ad",
        tier="standard",
        owner="growth-oncall",
        auto_remediation="allow_listed",
        description="Serves contextual advertisements.",
    ),
    "cart-service": ServiceRecord(
        name="cart-service",
        display_name="Cart",
        tier="standard",
        owner="commerce-oncall",
        auto_remediation="none",
        description="Maintains shopping carts.",
    ),
}


def workflow(
    *,
    status: IncidentStatus = IncidentStatus.DIAGNOSED,
    steps_used: int = 0,
    soft_step_budget: int = 8,
    hard_step_cap: int = 16,
    extensions_granted: int = 0,
    max_extensions: int = 2,
    takeover_actor: str | None = None,
) -> Workflow:
    return Workflow(
        id="wf_unit_test",
        incident_id="inc_unit_test",
        status=status,
        outcome=WorkflowOutcome.IN_PROGRESS,
        thread_id="incident:inc_unit_test",
        steps_used=steps_used,
        model_calls=0,
        tool_calls=0,
        extensions_granted=extensions_granted,
        soft_step_budget=soft_step_budget,
        hard_step_cap=hard_step_cap,
        max_extensions=max_extensions,
        takeover_actor=takeover_actor,
        takeover_at=NOW if takeover_actor else None,
        terminal_reason=None,
        created_at=NOW,
        updated_at=NOW,
    )


def budget(
    *,
    steps_used: int = 0,
    soft_step_budget: int = 8,
    hard_step_cap: int = 16,
    extensions_granted: int = 0,
    max_extensions: int = 2,
) -> BudgetState:
    return BudgetState(
        soft_step_budget=soft_step_budget,
        hard_step_cap=hard_step_cap,
        steps_used=steps_used,
        extensions_granted=extensions_granted,
        max_extensions=max_extensions,
    )


def constraint(
    *,
    kind: ConstraintKind,
    tool_name: str | None = None,
    target: str | None = None,
) -> Constraint:
    return Constraint(
        id=f"con_{kind.lower()}_test",
        incident_id="inc_unit_test",
        kind=kind,
        tool_name=tool_name,
        target=target,
        note="unit-test constraint",
        created_by="operator",
        active=True,
        created_at=NOW,
    )


def policy_request(
    *,
    proposal: ActionProposal | None = None,
    service: str = "payment-service",
    incident_service: str | None = None,
    wf: Workflow | None = None,
    budget_state: BudgetState | None = None,
    constraints: tuple[Constraint, ...] = (),
    rejected_intents: frozenset[str] = frozenset(),
    settings: object | None = None,
    target_telemetry: int = 1,
) -> PolicyRequest:
    from opsaccord.config import Settings

    chosen = proposal or ActionProposal(
        tool_name="apply_remediation",
        target="payment-service",
        arguments={
            "target": "payment-service",
            "remediation_id": "disable_feature_flag",
            "flag_id": "paymentUnreachable",
        },
        rationale="unit-test proposal",
    )
    resolved_settings = settings or Settings()
    return PolicyRequest(
        proposal=chosen,
        tool=ToolRegistry.default().require(chosen.tool_name),
        service=SERVICES.get(service),
        workflow=wf or workflow(),
        incident_service=incident_service or service,
        constraints=constraints,
        rejected_intents=rejected_intents,
        budget=budget_state or budget(),
        settings=resolved_settings,  # type: ignore[arg-type]
        # The premise of most policy tests is an incident that did collect telemetry;
        # tests of the evidence rule itself pass 0.
        target_telemetry=target_telemetry,
    )


__all__ = [
    "NOW",
    "SERVICES",
    "budget",
    "constraint",
    "policy_request",
    "workflow",
]
