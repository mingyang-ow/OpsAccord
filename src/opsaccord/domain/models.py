"""Typed contracts shared by the controller, policy engine, gateway, and API."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from opsaccord.config import canonical_json, digest
from opsaccord.domain.enums import (
    ActionOutcome,
    ActionStatus,
    ActorType,
    ApprovalDecision,
    ApprovalStatus,
    ConstraintKind,
    DecisionType,
    EvidenceKind,
    IncidentStatus,
    ScenarioOutcome,
    SideEffect,
    VerificationOutcome,
    WorkflowOutcome,
)

_ID_PATTERN = r"^[A-Za-z0-9_.:-]{3,128}$"


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Incident(Frozen):
    id: str = Field(pattern=_ID_PATTERN)
    title: str
    service: str
    severity: Literal["SEV1", "SEV2", "SEV3"]
    status: IncidentStatus
    summary: str | None = None
    scenario_id: str | None = None
    created_at: datetime
    updated_at: datetime


class Workflow(Frozen):
    id: str = Field(pattern=_ID_PATTERN)
    incident_id: str
    status: IncidentStatus
    outcome: WorkflowOutcome
    thread_id: str
    steps_used: int
    model_calls: int
    tool_calls: int
    extensions_granted: int
    soft_step_budget: int
    hard_step_cap: int
    max_extensions: int
    takeover_actor: str | None = None
    takeover_at: datetime | None = None
    terminal_reason: str | None = None
    created_at: datetime
    updated_at: datetime


class Evidence(Frozen):
    id: str = Field(pattern=_ID_PATTERN)
    incident_id: str
    workflow_id: str
    kind: EvidenceKind
    source: str
    service: str
    summary: str
    value: dict[str, Any] = Field(default_factory=dict)
    observation_window_start: datetime | None = None
    observation_window_end: datetime | None = None
    collected_at: datetime
    tool_call_id: str | None = None
    is_simulated: bool = True


class ModelDecision(Frozen):
    id: str = Field(pattern=_ID_PATTERN)
    incident_id: str
    workflow_id: str
    decision_type: DecisionType
    provider: str
    model_id: str
    rationale: str
    payload: dict[str, Any] = Field(default_factory=dict)
    evidence_ids: tuple[str, ...] = ()
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    latency_ms: int | None = None
    created_at: datetime


class ToolCallRecord(Frozen):
    id: str = Field(pattern=_ID_PATTERN)
    incident_id: str
    workflow_id: str
    tool_name: str
    target: str
    arguments: dict[str, Any]
    arguments_digest: str
    side_effect: SideEffect
    attempt: int
    succeeded: bool
    result: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    started_at: datetime
    finished_at: datetime
    is_simulated: bool = True


class Action(Frozen):
    id: str = Field(pattern=_ID_PATTERN)
    incident_id: str
    workflow_id: str
    tool_name: str
    target: str
    arguments: dict[str, Any]
    arguments_digest: str
    intent_digest: str
    rationale: str
    status: ActionStatus
    decision_id: str | None = None
    approval_id: str | None = None
    attempt: int
    idempotency_key: str
    dispatch_started_at: datetime | None = None
    completed_at: datetime | None = None
    result: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    external_outcome: ActionOutcome | None = None
    is_simulated: bool = True
    created_at: datetime
    updated_at: datetime

    @property
    def intent_key(self) -> tuple[str, str, str]:
        return (self.tool_name, self.target, canonical_json(self.arguments))

    @staticmethod
    def compute_intent_digest(tool_name: str, target: str, arguments: dict[str, Any]) -> str:
        """Intent identity is independent of action_id, so a fresh row cannot bypass a rejection."""
        return digest({"tool": tool_name, "target": target, "arguments": arguments})


class Approval(Frozen):
    id: str = Field(pattern=_ID_PATTERN)
    incident_id: str
    workflow_id: str
    action_id: str
    intent_digest: str
    target: str
    arguments_digest: str
    requested_by: str
    approver: str | None = None
    decision: ApprovalDecision | None = None
    status: ApprovalStatus
    reason: str | None = None
    expires_at: datetime
    decided_at: datetime | None = None
    consumed_at: datetime | None = None
    created_at: datetime


class Constraint(Frozen):
    id: str = Field(pattern=_ID_PATTERN)
    incident_id: str | None
    kind: ConstraintKind
    tool_name: str | None = None
    target: str | None = None
    note: str | None = None
    created_by: str
    active: bool
    created_at: datetime

    @model_validator(mode="after")
    def _require_selector(self) -> Constraint:
        if self.kind is ConstraintKind.DENY_ACTION and not self.tool_name:
            raise ValueError("DENY_ACTION requires tool_name")
        if self.kind is ConstraintKind.DENY_TARGET and not self.target:
            raise ValueError("DENY_TARGET requires target")
        return self


class ServiceRecord(Frozen):
    name: str
    display_name: str
    tier: Literal["critical", "standard", "supporting"]
    owner: str
    auto_remediation: Literal["forbidden", "allow_listed", "none"]
    description: str


def _coerce(key: str, spec: dict[str, Any], value: Any) -> Any:
    """Validate one argument against its declared schema."""
    expected = spec.get("type")
    if expected == "string":
        if not isinstance(value, str):
            raise ValueError(f"{key} must be a string")
        if len(value) > 256:
            raise ValueError(f"{key} exceeds 256 characters")
        return value
    if expected == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{key} must be an integer")
        low, high = spec.get("minimum"), spec.get("maximum")
        if low is not None and value < low:
            raise ValueError(f"{key} below minimum {low}")
        if high is not None and value > high:
            raise ValueError(f"{key} above maximum {high}")
        return value
    if expected == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{key} must be a number")
        return float(value)
    if expected == "boolean":
        if not isinstance(value, bool):
            raise ValueError(f"{key} must be a boolean")
        return value
    raise ValueError(f"{key} has unsupported schema type {expected!r}")


class ToolSpec(Frozen):
    """A typed tool.

    ``arguments_schema`` declares the complete argument surface. ``fixed_arguments``
    pins a value globally; ``per_target_arguments`` pins values per target. An
    argument that is not declared, or a value outside its pinned set, is rejected
    before any executor sees the request, so a model cannot steer a tool into an
    arbitrary shell command, URL, container, or flag.
    """

    name: str
    description: str
    side_effect: SideEffect
    target_service: str | None = None
    arguments_schema: dict[str, dict[str, Any]] = Field(default_factory=dict)
    fixed_arguments: dict[str, tuple[Any, ...]] = Field(default_factory=dict)
    per_target_arguments: dict[str, dict[str, tuple[Any, ...]]] = Field(default_factory=dict)
    timeout_seconds: float = 5.0
    max_attempts: int = 1
    requires_approval: bool = False

    def validate_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        unknown = set(arguments) - set(self.arguments_schema)
        if unknown:
            raise ValueError(f"unsupported arguments for {self.name}: {sorted(unknown)}")

        resolved: dict[str, Any] = {}
        for key, spec in self.arguments_schema.items():
            if key in arguments:
                value = arguments[key]
            elif "default" in spec:
                value = spec["default"]
            else:
                raise ValueError(f"{self.name} requires argument '{key}'")
            resolved[key] = _coerce(key, spec, value)

        for key, allowed in self.fixed_arguments.items():
            if resolved.get(key) not in allowed:
                raise ValueError(
                    f"{self.name} argument {key}={resolved.get(key)!r} is not allow-listed "
                    f"(allowed: {sorted(allowed)})"
                )

        target = resolved.get("target")
        for per_target_key, allowed in self.per_target_arguments.get(str(target), {}).items():
            if resolved.get(per_target_key) not in allowed:
                raise ValueError(
                    f"{self.name} argument {per_target_key}={resolved.get(per_target_key)!r} "
                    f"is not allow-listed for target {target!r} "
                    f"(allowed: {sorted(allowed)})"
                )
        return resolved


class ToolResult(Frozen):
    tool_name: str
    target: str
    succeeded: bool
    value: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    attempts: int = 1
    is_simulated: bool = True
    collected_at: datetime
    observation_window_start: datetime | None = None
    observation_window_end: datetime | None = None


# --- model proposals (untrusted input) -------------------------------------


class ActionProposal(Frozen):
    """What a model may propose. It carries no authority of its own."""

    tool_name: str
    target: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    rationale: str = Field(min_length=3, max_length=600)

    @field_validator("rationale")
    @classmethod
    def _strip(cls, value: str) -> str:
        return " ".join(value.split())


class DiagnosisProposal(Frozen):
    summary: str = Field(min_length=3, max_length=1200)
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_ids: tuple[str, ...] = ()
    proposed_action: ActionProposal | None = None
    escalate: bool = False
    escalate_reason: str | None = None


class WorkflowState(Frozen):
    """Graph state. Authoritative facts live in PostgreSQL, never only here."""

    incident_id: str
    workflow_id: str
    thread_id: str
    status: IncidentStatus
    outcome: WorkflowOutcome
    steps_used: int = 0
    model_calls: int = 0
    tool_calls: int = 0
    evidence_ids: tuple[str, ...] = ()
    decisions: tuple[str, ...] = ()
    action_id: str | None = None
    approval_id: str | None = None
    verification: VerificationOutcome | None = None
    terminal_reason: str | None = None
    notes: tuple[str, ...] = ()


class VerificationResult(Frozen):
    outcome: VerificationOutcome
    service: str
    evidence_ids: tuple[str, ...] = ()
    detail: str
    checked_at: datetime


class PolicyDecision(Frozen):
    allowed: bool
    requires_approval: bool
    reasons: tuple[str, ...] = ()
    blocking_constraints: tuple[str, ...] = ()
    matched_rule: str
    actor: ActorType | None = None


# Additional soft-budget headroom one justified extension buys. The soft budget
# is a checkpoint rather than a wall, so an extension has to permit more work --
# otherwise the extension machinery, and the policy verdict that asks for one,
# are inert.
EXTENSION_STEP_ALLOWANCE = 5


class BudgetState(Frozen):
    soft_step_budget: int
    hard_step_cap: int
    steps_used: int
    extensions_granted: int
    max_extensions: int

    @property
    def effective_soft_budget(self) -> int:
        """The soft checkpoint, including every extension granted so far."""
        return self.soft_step_budget + self.extensions_granted * EXTENSION_STEP_ALLOWANCE

    @property
    def soft_exhausted(self) -> bool:
        return self.steps_used >= self.effective_soft_budget

    @property
    def hard_exhausted(self) -> bool:
        return self.steps_used >= self.hard_step_cap

    @property
    def can_extend(self) -> bool:
        return (
            self.soft_exhausted
            and not self.hard_exhausted
            and (self.extensions_granted < self.max_extensions)
        )


class ScenarioResult(Frozen):
    scenario_id: str
    outcome: ScenarioOutcome
    incident_id: str
    workflow_id: str
    final_status: IncidentStatus
    detail: str
    action_count: int
    executed_action_count: int
    forbidden_action_count: int
    duplicate_side_effect_count: int
    resolved: bool
    resolved_without_verification: bool
    is_simulated: bool = True
    evidence_ids: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


class IncidentSnapshot(Frozen):
    """Read model returned by the API and rendered by the UI."""

    incident: Incident
    workflow: Workflow | None
    evidence: tuple[Evidence, ...]
    decisions: tuple[ModelDecision, ...]
    actions: tuple[Action, ...]
    approvals: tuple[Approval, ...]
    constraints: tuple[Constraint, ...]
    events: tuple[dict[str, Any], ...]
    pending_approval_id: str | None = None
    awaiting_resume: bool = False
