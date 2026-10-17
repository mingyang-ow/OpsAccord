"""Enumerations plus the explicit, single source of truth for legal transitions."""

from __future__ import annotations

from enum import StrEnum


class IncidentStatus(StrEnum):
    NEW = "NEW"
    INVESTIGATING = "INVESTIGATING"
    DIAGNOSED = "DIAGNOSED"
    WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"
    ACTIONING = "ACTIONING"
    VERIFYING = "VERIFYING"
    RESOLVED = "RESOLVED"
    READY_FOR_HUMAN = "READY_FOR_HUMAN"
    ESCALATED_INCOMPLETE = "ESCALATED_INCOMPLETE"


TERMINAL_STATUSES = frozenset(
    {IncidentStatus.RESOLVED, IncidentStatus.READY_FOR_HUMAN, IncidentStatus.ESCALATED_INCOMPLETE}
)

# Explicit allowed edges. Anything not listed here is rejected by the controller.
ALLOWED_TRANSITIONS: dict[IncidentStatus, frozenset[IncidentStatus]] = {
    IncidentStatus.NEW: frozenset({IncidentStatus.INVESTIGATING, IncidentStatus.READY_FOR_HUMAN}),
    IncidentStatus.INVESTIGATING: frozenset(
        {
            IncidentStatus.DIAGNOSED,
            IncidentStatus.INVESTIGATING,
            IncidentStatus.READY_FOR_HUMAN,
            IncidentStatus.ESCALATED_INCOMPLETE,
        }
    ),
    IncidentStatus.DIAGNOSED: frozenset(
        {
            IncidentStatus.WAITING_FOR_APPROVAL,
            IncidentStatus.ACTIONING,
            IncidentStatus.VERIFYING,
            IncidentStatus.INVESTIGATING,
            IncidentStatus.READY_FOR_HUMAN,
            IncidentStatus.ESCALATED_INCOMPLETE,
        }
    ),
    IncidentStatus.WAITING_FOR_APPROVAL: frozenset(
        {
            IncidentStatus.ACTIONING,
            IncidentStatus.DIAGNOSED,
            IncidentStatus.INVESTIGATING,
            IncidentStatus.READY_FOR_HUMAN,
            IncidentStatus.ESCALATED_INCOMPLETE,
        }
    ),
    IncidentStatus.ACTIONING: frozenset(
        {
            IncidentStatus.VERIFYING,
            IncidentStatus.INVESTIGATING,
            IncidentStatus.READY_FOR_HUMAN,
            IncidentStatus.ESCALATED_INCOMPLETE,
        }
    ),
    IncidentStatus.VERIFYING: frozenset(
        {
            IncidentStatus.RESOLVED,
            IncidentStatus.INVESTIGATING,
            IncidentStatus.DIAGNOSED,
            IncidentStatus.READY_FOR_HUMAN,
            IncidentStatus.ESCALATED_INCOMPLETE,
        }
    ),
    IncidentStatus.RESOLVED: frozenset(),
    IncidentStatus.READY_FOR_HUMAN: frozenset(),
    IncidentStatus.ESCALATED_INCOMPLETE: frozenset(),
}


def transition_allowed(current: IncidentStatus, target: IncidentStatus) -> bool:
    return target in ALLOWED_TRANSITIONS.get(current, frozenset())


class WorkflowOutcome(StrEnum):
    IN_PROGRESS = "IN_PROGRESS"
    RESOLVED = "RESOLVED"
    HANDOFF = "HANDOFF"
    ESCALATED_INCOMPLETE = "ESCALATED_INCOMPLETE"


class EvidenceKind(StrEnum):
    METRIC = "METRIC"
    TRACE = "TRACE"
    LOG = "LOG"
    HEALTH = "HEALTH"
    RUNBOOK = "RUNBOOK"
    CHANGE = "CHANGE"
    COLLECTION_ERROR = "COLLECTION_ERROR"


class DecisionType(StrEnum):
    DIAGNOSIS = "DIAGNOSIS"
    PLAN = "PLAN"
    HANDOFF = "HANDOFF"
    INVESTIGATE = "INVESTIGATE"


class ActionStatus(StrEnum):
    PROPOSED = "PROPOSED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    REJECTED = "REJECTED"
    APPROVED = "APPROVED"
    DISPATCHING = "DISPATCHING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    UNCERTAIN = "UNCERTAIN"
    CANCELLED = "CANCELLED"


class ActionOutcome(StrEnum):
    """Reconciled external outcome, independent of the local row state."""

    CONFIRMED_APPLIED = "CONFIRMED_APPLIED"
    CONFIRMED_NOT_APPLIED = "CONFIRMED_NOT_APPLIED"
    UNKNOWN = "UNKNOWN"


class ApprovalDecision(StrEnum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class ApprovalStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CONSUMED = "CONSUMED"
    EXPIRED = "EXPIRED"
    INVALIDATED = "INVALIDATED"


class ConstraintKind(StrEnum):
    DENY_ACTION = "DENY_ACTION"
    DENY_TARGET = "DENY_TARGET"
    REQUIRE_MANUAL = "REQUIRE_MANUAL"
    FREEZE_ACTIONS = "FREEZE_ACTIONS"


class ActorType(StrEnum):
    HUMAN = "HUMAN"
    AGENT = "AGENT"
    SYSTEM = "SYSTEM"


class VerificationOutcome(StrEnum):
    """Member names mirror their values: SQLAlchemy persists enum member names."""

    VERIFICATION_SUCCESS = "VERIFICATION_SUCCESS"
    VERIFICATION_FAILURE = "VERIFICATION_FAILURE"
    VERIFICATION_INCONCLUSIVE = "VERIFICATION_INCONCLUSIVE"


class ScenarioOutcome(StrEnum):
    """Authoritative evaluation outcomes. Safety failures are never averaged away."""

    PASS = "PASS"
    SAFE_ESCALATION = "SAFE_ESCALATION"
    INEFFECTIVE_FAILURE = "INEFFECTIVE_FAILURE"
    UNSAFE_FAILURE = "UNSAFE_FAILURE"


class SideEffect(StrEnum):
    READ = "READ"
    WRITE = "WRITE"
