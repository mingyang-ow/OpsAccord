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

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from opsaccord.domain.enums import ActionOutcome, ActionStatus
from opsaccord.domain.models import ToolResult


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
    "DispatchFailure",
    "DispatchRefused",
    "DispatchTimeout",
    "Environment",
    "ReadTimeout",
    "UnknownDemoTarget",
    "classify_dispatch_failure",
]
