"""Deterministic policy engine.

Every rule is a pure function of persisted facts. No model output, runbook
text, or telemetry payload can change a rule's outcome, and the engine never
performs I/O.
"""

from __future__ import annotations

from dataclasses import dataclass

from opsaccord.config import Settings
from opsaccord.domain.enums import (
    ActorType,
    ConstraintKind,
    IncidentStatus,
    SideEffect,
)
from opsaccord.domain.models import (
    Action,
    ActionProposal,
    BudgetState,
    Constraint,
    PolicyDecision,
    ServiceRecord,
    ToolSpec,
    Workflow,
)


@dataclass(frozen=True)
class PolicyRequest:
    proposal: ActionProposal
    tool: ToolSpec
    service: ServiceRecord | None
    workflow: Workflow
    incident_service: str
    constraints: tuple[Constraint, ...]
    rejected_intents: frozenset[str]
    budget: BudgetState
    settings: Settings


def _matching(constraints: tuple[Constraint, ...], kind: ConstraintKind) -> list[Constraint]:
    return [c for c in constraints if c.kind is kind]


def _constraint_matches(constraint: Constraint, *, tool_name: str, target: str) -> bool:
    if constraint.tool_name and constraint.tool_name != tool_name:
        return False
    if constraint.target and constraint.target != target:
        return False
    return constraint.kind in {ConstraintKind.DENY_ACTION, ConstraintKind.DENY_TARGET}


def evaluate(request: PolicyRequest) -> PolicyDecision:
    """Return the deterministic permission decision for one proposed action.

    Order matters: the first failing rule produces the verdict, so the reason
    reported to a human is always the most specific applicable one.
    """
    proposal = request.proposal
    tool = request.tool
    workflow = request.workflow
    reasons: list[str] = []

    # 1. Typed tool must exist. Unknown tools cannot reach an executor at all.
    if tool.name != proposal.tool_name:
        return PolicyDecision(
            allowed=False,
            requires_approval=False,
            reasons=("requested tool is not the registered typed tool",),
            matched_rule="TOOL_MISMATCH",
        )

    # 2. Human takeover suspends all automated execution.
    if workflow.takeover_actor:
        return PolicyDecision(
            allowed=False,
            requires_approval=False,
            reasons=(
                f"human takeover is active (actor={workflow.takeover_actor}); "
                "automated execution is suspended",
            ),
            matched_rule="HUMAN_TAKEOVER",
        )

    # 3. Structured human constraints are policy gates, evaluated before anything else.
    for constraint in _matching(request.constraints, ConstraintKind.FREEZE_ACTIONS):
        return PolicyDecision(
            allowed=False,
            requires_approval=False,
            reasons=(f"freeze-actions constraint {constraint.id} is active",),
            blocking_constraints=(constraint.id,),
            matched_rule="CONSTRAINT_FREEZE_ACTIONS",
        )
    for constraint in _matching(request.constraints, ConstraintKind.REQUIRE_MANUAL):
        return PolicyDecision(
            allowed=False,
            requires_approval=False,
            reasons=(f"manual-handling constraint {constraint.id} is active",),
            blocking_constraints=(constraint.id,),
            matched_rule="CONSTRAINT_REQUIRE_MANUAL",
        )
    for constraint in request.constraints:
        if _constraint_matches(constraint, tool_name=tool.name, target=proposal.target):
            return PolicyDecision(
                allowed=False,
                requires_approval=False,
                reasons=(
                    f"constraint {constraint.id} ({constraint.kind}) forbids "
                    f"{tool.name} on {proposal.target}",
                ),
                blocking_constraints=(constraint.id,),
                matched_rule=f"CONSTRAINT_{constraint.kind}",
            )

    # 4. Targets come from the fixed local demo allow-list and service registry.
    if proposal.target not in request.settings.allowed_targets:
        return PolicyDecision(
            allowed=False,
            requires_approval=False,
            reasons=(f"target {proposal.target!r} is outside the fixed demo allow-list",),
            matched_rule="TARGET_NOT_ALLOW_LISTED",
        )
    if request.service is None:
        return PolicyDecision(
            allowed=False,
            requires_approval=False,
            reasons=(f"target {proposal.target!r} is not in the service registry",),
            matched_rule="TARGET_NOT_REGISTERED",
        )
    if tool.target_service and tool.target_service != proposal.target:
        return PolicyDecision(
            allowed=False,
            requires_approval=False,
            reasons=(
                f"{tool.name} is bound to {tool.target_service} and may not act on "
                f"{proposal.target}",
            ),
            matched_rule="TOOL_TARGET_BINDING",
        )

    # 5. Read-only tools are always permitted within budget; they cannot change
    #    the world, so no approval or allow-list applies to them.
    if tool.side_effect is SideEffect.READ:
        if request.budget.hard_exhausted:
            return PolicyDecision(
                allowed=False,
                requires_approval=False,
                reasons=("hard step cap reached",),
                matched_rule="BUDGET_HARD_CAP",
            )
        return PolicyDecision(
            allowed=True,
            requires_approval=False,
            reasons=("read-only tool within budget",),
            matched_rule="ALLOW_READ_ONLY",
            actor=ActorType.AGENT,
        )

    # From here the tool has a side effect.
    if request.budget.hard_exhausted:
        return PolicyDecision(
            allowed=False,
            requires_approval=False,
            reasons=("hard step cap reached",),
            matched_rule="BUDGET_HARD_CAP",
        )

    # 6. A rejection binds to intent identity, not to an action row, so a fresh
    #    action id with the same tool/target/arguments stays rejected.
    digest = Action.compute_intent_digest(tool.name, proposal.target, proposal.arguments)
    if digest in request.rejected_intents:
        return PolicyDecision(
            allowed=False,
            requires_approval=False,
            reasons=(
                "a human already rejected this exact action intent "
                "(same tool, target, and arguments)",
            ),
            matched_rule="REJECTED_INTENT",
        )

    # 7. Service-level remediation posture.
    posture = request.service.auto_remediation
    if posture == "none":
        return PolicyDecision(
            allowed=False,
            requires_approval=False,
            reasons=(
                f"{proposal.target} has no supported automated remediation; "
                "the engine must hand off to a human",
            ),
            matched_rule="REMEDIATION_NOT_PERMITTED",
        )

    if posture == "allow_listed":
        # The allow-listed autonomous path is scoped to the service under
        # investigation. A target the incident is not about is never remediated
        # without a human: nothing in the incident makes that side effect safe,
        # and the target list alone would let untrusted runbook text steer an
        # approval-free write onto a different allow-listed service.
        if proposal.target != request.incident_service:
            return PolicyDecision(
                allowed=True,
                requires_approval=True,
                reasons=(
                    f"{proposal.target} is not the service under investigation "
                    f"({request.incident_service}); a cross-service remediation "
                    "requires an exact human approval",
                ),
                matched_rule="CROSS_SERVICE_REMEDIATION_REQUIRES_APPROVAL",
                actor=ActorType.HUMAN,
            )
        if proposal.target not in request.settings.allow_listed_remediation_targets:
            return PolicyDecision(
                allowed=False,
                requires_approval=False,
                reasons=(f"{proposal.target} is not an allow-listed remediation target",),
                matched_rule="AUTONOMOUS_REMEDIATION_NOT_ALLOW_LISTED",
            )
        if request.budget.soft_exhausted:
            reasons.append(
                "soft step budget exhausted; autonomous execution requires a justified extension"
            )
            return PolicyDecision(
                allowed=False,
                requires_approval=False,
                reasons=tuple(reasons),
                matched_rule="BUDGET_SOFT_EXHAUSTED",
            )
        return PolicyDecision(
            allowed=True,
            requires_approval=False,
            reasons=(
                f"{proposal.target} is an allow-listed low-risk remediation target "
                f"and {tool.name} is its bounded action",
                *reasons,
            ),
            matched_rule="ALLOW_LISTED_REMEDIATION",
            actor=ActorType.AGENT,
        )

    # posture == "forbidden": a human must authorise this exact action.
    return PolicyDecision(
        allowed=True,
        requires_approval=True,
        reasons=(
            f"{proposal.target} ({request.service.tier}) requires an exact human approval "
            "before any side effect",
        ),
        matched_rule="REQUIRE_EXACT_APPROVAL",
        actor=ActorType.HUMAN,
    )


def budget_state(workflow: Workflow) -> BudgetState:
    """Project the persisted budget columns into a read-only view."""
    return BudgetState(
        soft_step_budget=workflow.soft_step_budget,
        hard_step_cap=workflow.hard_step_cap,
        steps_used=workflow.steps_used,
        extensions_granted=workflow.extensions_granted,
        max_extensions=workflow.max_extensions,
    )


def is_terminal(status: IncidentStatus) -> bool:
    from opsaccord.domain.enums import TERMINAL_STATUSES

    return status in TERMINAL_STATUSES
