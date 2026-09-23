"""Execution-surface contracts shared by the simulated and live adapters.

The gateway must be able to drive either surface, and it must be able to label
the evidence it persists honestly. Two things therefore live here rather than in
the simulated module:

* the exception types the gateway maps onto read and action outcomes, so a live
  adapter timeout follows exactly the same recovery path as a simulated one;
* a structural protocol describing every call the gateway is allowed to make.
  The protocol has no generic shell, HTTP, or container primitive: each method is
  a bounded, typed operation against one allow-listed demo target.

``is_simulated`` is part of the contract on purpose. Evidence, tool calls, and
actions are stamped from the adapter that produced them, so a live run can never
be persisted with simulated provenance (or the reverse).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from opsaccord.domain.enums import ActionOutcome
from opsaccord.domain.models import ToolResult


class ReadTimeout(RuntimeError):
    """A bounded read did not answer inside its budget."""


class DispatchTimeout(RuntimeError):
    """A side effect was dispatched but its outcome was not observed."""


class UnknownDemoTarget(KeyError):
    """The adapter has no verified mapping for the requested target."""


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
    "DispatchTimeout",
    "Environment",
    "ReadTimeout",
    "UnknownDemoTarget",
]
