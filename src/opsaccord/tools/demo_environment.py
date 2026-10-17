"""Deterministic in-process model of the pinned Astronomy Shop demo.

This is the *simulated* execution surface. It exists so that the controller,
policy engine, gateway, recovery logic, and acceptance scenarios can be
exercised end to end without Docker, and so that failure modes (read timeouts,
ambiguous side-effect outcomes, ineffective remediation) are reproducible on
demand.

Fault and remediation identifiers mirror the pinned upstream revision; see
docs/astronomy-shop.md for provenance. It is explicitly *not* a live adapter
and every record it produces is labelled ``is_simulated=True``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from opsaccord.domain.enums import ActionOutcome, SideEffect
from opsaccord.domain.models import ToolResult
from opsaccord.tools.contracts import (
    DispatchRefused,
    DispatchTimeout,
    ReadTimeout,
    dependency_operation,
)

REVISION = "3.1.0"
REVISION_SOURCE = "https://github.com/open-telemetry/opentelemetry-demo"


@dataclass
class ServiceState:
    reachable: bool = True
    health: str = "SERVING"
    cpu_utilization: float = 0.24
    memory_utilization: float = 0.42
    error_rate: float = 0.0
    latency_p95_ms: float = 42.0
    request_rate: float = 130.0
    container_restarts: int = 0

    @property
    def healthy(self) -> bool:
        return (
            self.reachable
            and self.health == "SERVING"
            and self.cpu_utilization < 0.80
            and self.error_rate < 0.05
        )


@dataclass
class FaultInjection:
    """Scenario knobs. Every one of them is a *simulated* condition."""

    metrics_timeout_attempts: int = 0
    traces_timeout_attempts: int = 0
    health_timeout_attempts: int = 0
    remediation_ineffective: bool = False
    action_dispatch_timeout: bool = False
    # The change landed, then the adapter failed while constructing its
    # acknowledgement. Distinct from a dispatch timeout on purpose: the caller
    # observes an unclassifiable error, which is exactly the case that must not be
    # recorded as a definite non-execution.
    action_response_error: bool = False
    status_query_available: bool = True
    runbook_text: dict[str, str] = field(default_factory=dict)


DEFAULT_RUNBOOKS = {
    "payment-service": (
        "Payment runbook. If Charge fails, confirm the paymentUnreachable feature flag, "
        "then disable it after operator approval, and re-run a Charge probe."
    ),
    "ad-service": "Ad runbook. High CPU is usually the adHighCpu flag. Disable it to recover.",
    "cart-service": (
        "Cart runbook. cartFailure is graded by percentage. There is no supported automated "
        "remediation; collect evidence and hand off to the commerce on-call."
    ),
}


class DemoEnvironment:
    """The simulated isolated demo environment."""

    is_simulated = True
    revision = REVISION
    revision_source = REVISION_SOURCE

    def __init__(
        self,
        *,
        services: dict[str, ServiceState] | None = None,
        faults: FaultInjection | None = None,
    ) -> None:
        self.services: dict[str, ServiceState] = services or {}
        self.faults = faults or FaultInjection()
        self.applied: dict[str, dict[str, object]] = {}
        self.attempted: dict[str, dict[str, object]] = {}
        self.call_counts: dict[str, int] = {}
        self.read_calls: list[tuple[str, str, str]] = []

    # --- helpers ----------------------------------------------------------

    def _state(self, target: str) -> ServiceState:
        if target not in self.services:
            raise KeyError(f"unknown demo target {target!r}")
        return self.services[target]

    def _bump(self, key: str) -> int:
        self.call_counts[key] = self.call_counts.get(key, 0) + 1
        return self.call_counts[key]

    def install_from(self, other: DemoEnvironment) -> None:
        """Adopt another simulated environment state in place.

        The API hands one environment instance to the gateway, so installing a
        built-in scenario has to mutate that instance rather than replace it.
        Only the simulated surface supports this: a live stack fault is injected
        in the stack itself, never by rewriting adapter state.
        """
        self.services = other.services
        self.faults = other.faults

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)

    # --- read tools -------------------------------------------------------

    def read_metrics(
        self, *, target: str, metric: str, window_seconds: int, timeout_seconds: float
    ) -> ToolResult:
        self.read_calls.append(("read_metrics", target, metric))
        if self._bump("read_metrics") <= self.faults.metrics_timeout_attempts:
            raise ReadTimeout(f"metrics query for {target}.{metric} exceeded {timeout_seconds}s")
        state = self._state(target)
        value = float(getattr(state, metric))
        now = self._now()
        return ToolResult(
            tool_name="read_metrics",
            target=target,
            succeeded=True,
            value={"metric": metric, "value": value, "window_seconds": window_seconds},
            attempts=1,
            is_simulated=True,
            collected_at=now,
            observation_window_start=now,
            observation_window_end=now,
        )

    def read_traces(
        self, *, target: str, operation: str, window_seconds: int, timeout_seconds: float
    ) -> ToolResult:
        self.read_calls.append(("read_traces", target, operation))
        if self._bump("read_traces") <= self.faults.traces_timeout_attempts:
            raise ReadTimeout(f"trace query for {target}.{operation} exceeded {timeout_seconds}s")
        state = self._state(target)
        now = self._now()
        failed = (not state.reachable) or state.error_rate > 0.05
        spans = (
            [
                {
                    "name": operation,
                    "status": "ERROR",
                    "duration_ms": round(state.latency_p95_ms * 3, 1),
                }
            ]
            if failed
            else [{"name": operation, "status": "OK", "duration_ms": state.latency_p95_ms}]
        )
        return ToolResult(
            tool_name="read_traces",
            target=target,
            succeeded=True,
            value={"operation": operation, "spans": spans, "error_spans": 1 if failed else 0},
            is_simulated=True,
            collected_at=now,
            observation_window_start=now,
            observation_window_end=now,
        )

    def read_service_health(self, *, target: str, timeout_seconds: float) -> ToolResult:
        self.read_calls.append(("read_service_health", target, "health"))
        if self._bump("read_service_health") <= self.faults.health_timeout_attempts:
            raise ReadTimeout(f"health probe for {target} exceeded {timeout_seconds}s")
        state = self._state(target)
        now = self._now()
        return ToolResult(
            tool_name="read_service_health",
            target=target,
            succeeded=True,
            value={
                "health": state.health,
                "reachable": state.reachable,
                "cpu_utilization": state.cpu_utilization,
                "error_rate": state.error_rate,
                "healthy": state.healthy,
            },
            is_simulated=True,
            collected_at=now,
        )

    def read_dependency(
        self, *, target: str, dependency: str, window_seconds: int, timeout_seconds: float
    ) -> ToolResult:
        """The caller's own view of one dependency edge (simulated).

        The simulated surface models the edge from the callee's condition: a callee
        that is unreachable cannot be reached by its caller, and a callee that is
        failing has failing callers. That is the fixture's fault model, and it is
        the fixture's job to be explicit about it -- the value still reaches
        reasoning only through this typed read, with the caller, destination and
        operation recorded, never as hidden scenario knowledge.

        A caller with no allow-listed edge is a programming error, not a quiet
        zero: an absent edge must never be read as "healthy".
        """
        self.read_calls.append(("read_dependency", target, dependency))
        # The read is span-derived, so the simulated trace-timeout knob governs it:
        # a scenario that makes traces time out must not get a dependency answer
        # from a different channel than the one it broke.
        if self._bump("read_dependency") <= self.faults.traces_timeout_attempts:
            raise ReadTimeout(
                f"dependency query for {target} -> {dependency} exceeded {timeout_seconds}s"
            )
        operation = dependency_operation(target, dependency)
        if operation is None:
            return ToolResult(
                tool_name="read_dependency",
                target=target,
                succeeded=False,
                error=(
                    f"{target} -> {dependency} is not an allow-listed dependency edge; "
                    "an unverified edge has no attributable telemetry"
                ),
                is_simulated=True,
                collected_at=self._now(),
            )
        state = self._state(dependency)
        now = self._now()
        # An unreachable callee is still *called*: the caller makes the attempt and
        # it fails. Calls are therefore non-zero in both conditions, which is what
        # keeps "no traffic" meaning no traffic rather than "the callee is down".
        calls_per_second = 0.2
        error_rate = 1.0 if not state.reachable else state.error_rate
        return ToolResult(
            tool_name="read_dependency",
            target=target,
            succeeded=True,
            value={
                "caller": target,
                "destination": dependency,
                "operation": operation,
                "span_kind": "client",
                "attributable": True,
                "calls_per_second": calls_per_second,
                "error_rate": error_rate,
                "window_seconds": window_seconds,
                "source": "simulated-spans",
                "queries": [],
            },
            is_simulated=True,
            collected_at=now,
            observation_window_start=now,
            observation_window_end=now,
        )

    def read_recent_changes(self, *, target: str, timeout_seconds: float) -> ToolResult:
        self.read_calls.append(("read_recent_changes", target, "changes"))
        now = self._now()
        return ToolResult(
            tool_name="read_recent_changes",
            target=target,
            succeeded=True,
            value={"changes": [], "note": f"no deploy recorded for {target} in the window"},
            is_simulated=True,
            collected_at=now,
        )

    def read_runbook(self, *, target: str, timeout_seconds: float) -> ToolResult:
        self.read_calls.append(("read_runbook", target, "runbook"))
        text = self.faults.runbook_text.get(target) or DEFAULT_RUNBOOKS.get(
            target, "No runbook found."
        )
        now = self._now()
        return ToolResult(
            tool_name="read_runbook",
            target=target,
            succeeded=True,
            value={"text": text, "trusted": False},
            is_simulated=True,
            collected_at=now,
        )

    # --- side-effect tool -------------------------------------------------

    def apply_remediation(
        self,
        *,
        idempotency_key: str,
        target: str,
        remediation_id: str,
        flag_id: str,
        timeout_seconds: float,
    ) -> ToolResult:
        """Apply one remediation. Idempotent on ``idempotency_key``.

        Adapter guarantee: the external write is keyed by the action id, so a
        repeated call with the same key does not apply a second change. That
        guarantee only holds while the adapter is reachable; it is not a
        substitute for reconciling an ambiguous outcome.

        Outcome contract: an unmapped target is refused before anything happens
        (:class:`DispatchRefused`); an unsupported remediation is a
        ``ToolResult(succeeded=False)`` refusal; a lost acknowledgement after the
        change landed is :class:`DispatchTimeout`; and a failure while building the
        acknowledgement is an ordinary error, which the gateway records as an
        unknown outcome rather than as a non-execution.
        """
        if target not in self.services:
            # Refused locally, before any state change: nothing was attempted, so
            # this is a proved non-execution rather than an ambiguous outcome.
            raise DispatchRefused(f"demo adapter has no mapping for target {target!r}")
        if remediation_id not in {"disable_feature_flag", "restart_workload"}:
            return ToolResult(
                tool_name="apply_remediation",
                target=target,
                succeeded=False,
                error=f"unsupported remediation {remediation_id!r}",
                is_simulated=True,
                collected_at=self._now(),
            )
        # ``attempted`` means a state change was about to be made, so a key that
        # appears here without appearing in ``applied`` is a change this adapter
        # knows it did not make. Refusals above are recorded nowhere, because no
        # attempt happened.
        self.attempted[idempotency_key] = {
            "target": target,
            "remediation_id": remediation_id,
            "flag_id": flag_id,
        }
        if idempotency_key in self.applied:
            return ToolResult(
                tool_name="apply_remediation",
                target=target,
                succeeded=True,
                value={**self.applied[idempotency_key], "replayed": True},
                is_simulated=True,
                collected_at=self._now(),
            )

        if remediation_id == "disable_feature_flag":
            self._disable_flag(target=target, flag_id=flag_id)
        else:
            self._restart(target=target)

        record: dict[str, object] = {
            "remediation_id": remediation_id,
            "flag_id": flag_id,
            "applied_at": self._now().isoformat(),
        }
        self.applied[idempotency_key] = record

        if self.faults.action_dispatch_timeout:
            # The change landed, but the caller never saw the acknowledgement.
            raise DispatchTimeout(
                f"dispatch of {remediation_id} on {target} exceeded {timeout_seconds}s"
            )
        if self.faults.action_response_error:
            # The change landed, and then the adapter failed while building its
            # acknowledgement - the malformed-response case. It raises an ordinary
            # error on purpose: the gateway must treat an error it cannot classify
            # as an unknown outcome, not as proof that nothing happened.
            raise ValueError(
                f"malformed acknowledgement for {remediation_id} on {target}: "
                "the response body could not be parsed"
            )
        return ToolResult(
            tool_name="apply_remediation",
            target=target,
            succeeded=True,
            value=record,
            is_simulated=True,
            collected_at=self._now(),
        )

    def _disable_flag(self, *, target: str, flag_id: str) -> None:
        if self.faults.remediation_ineffective:
            return
        state = self._state(target)
        if flag_id == "adHighCpu":
            state.cpu_utilization = 0.21
        elif flag_id == "paymentUnreachable":
            state.reachable = True
            state.health = "SERVING"
            state.error_rate = 0.0
            state.latency_p95_ms = 45.0
        elif flag_id == "cartFailure":
            state.error_rate = 0.0

    def _restart(self, *, target: str) -> None:
        state = self._state(target)
        if self.faults.remediation_ineffective:
            state.container_restarts += 1
            return
        state.reachable = True
        state.health = "SERVING"
        state.error_rate = 0.0
        state.container_restarts += 1

    # --- reconciliation surface ------------------------------------------

    def action_status(self, idempotency_key: str) -> ActionOutcome:
        """Ask the external system what actually happened."""
        if not self.faults.status_query_available:
            return ActionOutcome.UNKNOWN
        if idempotency_key in self.applied:
            return ActionOutcome.CONFIRMED_APPLIED
        if idempotency_key in self.attempted:
            return ActionOutcome.CONFIRMED_NOT_APPLIED
        # No record of this key at all. The adapter's applied/attempted maps are
        # in-memory, so after a restart it genuinely cannot tell "the write never
        # happened" from "the write happened in a process that is gone". Claiming
        # CONFIRMED_NOT_APPLIED here would turn an unknown outcome into a
        # confident failure; the live adapter answers UNKNOWN for the same reason.
        return ActionOutcome.UNKNOWN


def faulted_payment_unreachable() -> DemoEnvironment:
    """The P1-01 starting state: payment is unreachable."""
    return DemoEnvironment(
        services={
            "payment-service": ServiceState(
                reachable=False,
                health="NOT_SERVING",
                error_rate=1.0,
                latency_p95_ms=1500.0,
                request_rate=0.0,
            ),
            "ad-service": ServiceState(),
            "cart-service": ServiceState(),
            "checkout-service": ServiceState(),
        }
    )


def build_demo_environment(
    *, faults: FaultInjection | None = None, services: dict[str, ServiceState] | None = None
) -> DemoEnvironment:
    if services is None:
        services = {
            "payment-service": ServiceState(),
            "ad-service": ServiceState(),
            "cart-service": ServiceState(),
            "checkout-service": ServiceState(),
        }
    return DemoEnvironment(services=services, faults=faults)


__all__ = [
    "DEFAULT_RUNBOOKS",
    "DEMO_SIDE_EFFECT",
    "REVISION",
    "REVISION_SOURCE",
    "DemoEnvironment",
    "DispatchRefused",
    "DispatchTimeout",
    "FaultInjection",
    "ReadTimeout",
    "ServiceState",
    "build_demo_environment",
    "faulted_payment_unreachable",
]

DEMO_SIDE_EFFECT = SideEffect.WRITE
