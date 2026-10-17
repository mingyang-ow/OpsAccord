"""Execution-surface contracts shared by the simulated and live adapters.

The gateway must be able to drive either surface, and it must be able to label
the evidence it persists honestly. Two things therefore live here rather than in
the simulated module:

* the exception types the gateway maps onto read and action outcomes, so a live
  adapter timeout follows exactly the same recovery path as a simulated one;
  :func:`classify_dispatch_failure` is the single place that mapping is decided;
* a structural protocol describing every call the gateway is allowed to make.
  The protocol has no generic shell, HTTP, or container primitive: each method is
  a bounded, typed operation against one allow-listed demo target.

An adapter's second obligation is to be honest about *what it knows*. It must
report a definite non-execution only when it can prove one, because the gateway
records what the adapter says as the outcome of a real side effect:

* return ``ToolResult(succeeded=False)`` only when the change provably did not
  happen (the refusal was decided before any request was sent);
* raise :class:`DispatchRefused` when the request was never handed to the external
  system at all - a local validation failure, an unknown target, a missing
  mapping - and the adapter knows it;
* raise :class:`DispatchTimeout` when the request went out and the outcome was
  not observed;
* anything else it raises is treated as unknown, because an adapter can apply a
  change and then fail while reading, parsing, constructing, or recording its
  response. That case must not be reported as "nothing happened".

``is_simulated`` is part of the contract on purpose. Evidence, tool calls, and
actions are stamped from the adapter that produced them, so a live run can never
be persisted with simulated provenance (or the reverse).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from opsaccord.domain.enums import ActionOutcome, ActionStatus
from opsaccord.domain.models import ToolResult

#: Failure share at or above which a signal is read as failing rather than as
#: healthy traffic. The live health probe calls a service serving only when fewer
#: than this share of its attempts in the window fail, so this is the same bound
#: the probe uses: "the edge is failing" and "the service is not serving" then
#: mean the same thing everywhere, and a diagnosis cannot rest on a share that the
#: probe itself would call healthy.
TELEMETRY_ERROR_BOUND = 0.05


class ReadTimeout(RuntimeError):
    """A bounded read did not answer inside its budget."""


class DispatchTimeout(RuntimeError):
    """A side effect was dispatched but its outcome was not observed."""


class DispatchRefused(RuntimeError):
    """The adapter proved the side-effect request was never sent.

    Raise this only where the adapter has decided locally that no request left the
    process: an argument it cannot map, a target it does not control, a guard that
    fired before the write. It is the one honest way to report a definite
    non-execution by raising, and the gateway records it as ``FAILED`` /
    ``CONFIRMED_NOT_APPLIED``.
    """


class UnknownDemoTarget(KeyError):
    """The adapter has no verified mapping for the requested target."""


#: Allow-listed service-to-service dependency edges, as
#: ``(caller, callee, operation_alias)``.
#:
#: A dependency-path fault needs the *caller's* own observation of the RPC. When a
#: caller cannot reach a callee, the callee receives nothing, so it emits no
#: server span at all: asking only about the callee's own telemetry cannot see
#: this class of failure, and a healthy callee reading is not evidence that the
#: caller-to-callee path works. The client span is the observation that carries
#: destination and operation, and only edges verified against the pinned revision
#: are listed here (see docs/astronomy-shop.md, "Dependency attribution").
DEPENDENCY_EDGES: tuple[tuple[str, str, str], ...] = (
    ("checkout-service", "payment-service", "Charge"),
)


def dependency_callees(caller: str) -> tuple[str, ...]:
    """Allow-listed callees for one caller, in declaration order."""
    return tuple(callee for edge_caller, callee, _ in DEPENDENCY_EDGES if edge_caller == caller)


def dependency_callers(callee: str) -> tuple[str, ...]:
    """Callers whose own client spans can attribute a fault at ``callee``."""
    return tuple(caller for caller, edge_callee, _ in DEPENDENCY_EDGES if edge_callee == callee)


def dependency_operation(caller: str, callee: str) -> str | None:
    """The operation alias that identifies this edge, or None if it is not one."""
    for edge_caller, edge_callee, operation in DEPENDENCY_EDGES:
        if edge_caller == caller and edge_callee == callee:
            return operation
    return None


@dataclass(frozen=True)
class DependencyVerdict:
    """What one dependency-edge read establishes, and why."""

    state: str
    detail: str

    @property
    def failing(self) -> bool:
        return self.state == "failing"

    @property
    def healthy(self) -> bool:
        return self.state == "healthy"

    @property
    def indeterminate(self) -> bool:
        return self.state == "indeterminate"


def classify_dependency_edge(value: Mapping[str, Any]) -> DependencyVerdict:
    """Read one dependency-edge observation as failing, healthy, or unproven.

    The order of the checks is the whole contract, because the failure modes here
    are the ones that make an incident lie to its operator:

    * an edge that could not be attributed (``attributable`` is not true) is
      **not** a healthy edge - the telemetry simply cannot answer the question,
      and the caller must be told that instead of being reassured;
    * a read that declares no usable observation window is malformed for the same
      reason: there is no window to have measured anything in;
    * a window with no calls proves nothing in either direction. Zero traffic is
      neither a fault nor a recovery, so it is indeterminate, never healthy and
      never failing;
    * a missing or non-numeric error share is indeterminate for the same reason an
      absent metric is not a zero;
    * only then is the share compared against :data:`TELEMETRY_ERROR_BOUND`.
    """
    if not value.get("attributable"):
        reason = str(value.get("reason") or "the schema does not expose this attribution")
        return DependencyVerdict(
            state="indeterminate",
            detail=f"the caller's own calls to this dependency cannot be attributed: {reason}",
        )
    window = value.get("window_seconds")
    if window is not None and not (
        isinstance(window, (int, float)) and not isinstance(window, bool) and float(window) > 0
    ):
        return DependencyVerdict(
            state="indeterminate",
            detail=(
                f"the dependency read declares a window of {window!r}, which is not a usable "
                "observation window"
            ),
        )
    calls = _as_float(value.get("calls_per_second"))
    if calls <= 0:
        return DependencyVerdict(
            state="indeterminate",
            detail=(
                "the caller made no calls to this dependency inside the window, so there is "
                "neither a failure to confirm nor a recovery to verify"
            ),
        )
    rate = value.get("error_rate")
    if rate is None or isinstance(rate, bool) or not isinstance(rate, (int, float, str)):
        return DependencyVerdict(
            state="indeterminate",
            detail="the dependency edge returned no failure share for the window",
        )
    try:
        share = float(rate)
    except ValueError:
        return DependencyVerdict(
            state="indeterminate",
            detail="the dependency edge returned a failure share that is not a number",
        )
    where = (
        f"{value.get('caller') or 'the caller'} -> {value.get('destination') or 'the dependency'}"
    )
    if share >= TELEMETRY_ERROR_BOUND:
        return DependencyVerdict(
            state="failing",
            detail=(
                f"{where} failed on {share:.2%} of {calls:.3f} calls/second in the window, "
                f"above the {TELEMETRY_ERROR_BOUND:.0%} telemetry bound"
            ),
        )
    return DependencyVerdict(
        state="healthy",
        detail=f"{where} carried {calls:.3f} calls/second with a {share:.2%} failure share",
    )


def _as_float(value: object, default: float = 0.0) -> float:
    """Telemetry is untrusted input; a non-numeric value is not a number."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return default
    try:
        return float(value)
    except ValueError:
        return default


@dataclass(frozen=True)
class DispatchFailure:
    """How a raised dispatch error is recorded on the action row."""

    status: ActionStatus
    outcome: ActionOutcome
    error: str


def classify_dispatch_failure(exc: BaseException) -> DispatchFailure:
    """Map a raised dispatch error onto the recorded action outcome.

    The whole outcome contract lives here so that the handler in the gateway
    cannot drift from it:

    * :class:`DispatchRefused` - no request was sent, so this is a real definite
      non-execution and the ledger may say so;
    * :class:`DispatchTimeout` - the request went out and nothing was observed, so
      the outcome is unknown and must be reconciled rather than retried;
    * anything else - the gateway cannot tell whether the request went out. The
      conservative default is ``UNCERTAIN`` / ``UNKNOWN``: an adapter may apply a
      change and then fail while constructing or recording its acknowledgement,
      so claiming ``CONFIRMED_NOT_APPLIED`` here would assert a non-execution the
      code has no evidence for. Ambiguity escalates; it never resolves.
    """
    if isinstance(exc, DispatchRefused):
        return DispatchFailure(
            status=ActionStatus.FAILED,
            outcome=ActionOutcome.CONFIRMED_NOT_APPLIED,
            error=f"{type(exc).__name__}: {exc} (refused before dispatch)",
        )
    if isinstance(exc, DispatchTimeout):
        return DispatchFailure(
            status=ActionStatus.UNCERTAIN,
            outcome=ActionOutcome.UNKNOWN,
            error=str(exc) or f"{type(exc).__name__}: no acknowledgement observed",
        )
    return DispatchFailure(
        status=ActionStatus.UNCERTAIN,
        outcome=ActionOutcome.UNKNOWN,
        error=(
            f"{type(exc).__name__}: {exc} (unclassified error - the external call may already "
            "have applied a change, so the outcome is unknown and must be reconciled)"
        ),
    )


@runtime_checkable
class Environment(Protocol):
    """The typed operations the gateway may perform on a demo environment."""

    is_simulated: bool

    @property
    def revision(self) -> str: ...

    def read_metrics(
        self, *, target: str, metric: str, window_seconds: int, timeout_seconds: float
    ) -> ToolResult: ...

    def read_traces(
        self, *, target: str, operation: str, window_seconds: int, timeout_seconds: float
    ) -> ToolResult: ...

    def read_service_health(self, *, target: str, timeout_seconds: float) -> ToolResult: ...

    def read_dependency(
        self,
        *,
        target: str,
        dependency: str,
        window_seconds: int,
        timeout_seconds: float,
    ) -> ToolResult: ...

    def read_recent_changes(self, *, target: str, timeout_seconds: float) -> ToolResult: ...

    def read_runbook(self, *, target: str, timeout_seconds: float) -> ToolResult: ...

    def apply_remediation(
        self,
        *,
        idempotency_key: str,
        target: str,
        remediation_id: str,
        flag_id: str,
        timeout_seconds: float,
    ) -> ToolResult: ...

    def action_status(self, idempotency_key: str) -> ActionOutcome: ...


__all__ = [
    "DEPENDENCY_EDGES",
    "TELEMETRY_ERROR_BOUND",
    "DependencyVerdict",
    "DispatchFailure",
    "DispatchRefused",
    "DispatchTimeout",
    "Environment",
    "ReadTimeout",
    "UnknownDemoTarget",
    "classify_dependency_edge",
    "classify_dispatch_failure",
    "dependency_callees",
    "dependency_callers",
    "dependency_operation",
]
