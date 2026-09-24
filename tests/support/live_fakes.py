"""Fakes for the live scenario driver.

The live driver talks to three bounded channels -- a flag document, per-service
health probes, and the shared controller. These fakes provide the first two
directly and stand in for the third, so the lifecycle, its cleanup on every exit
path, and its reporting can be tested without a stack, a socket or a database.

They are shared by ``tests/unit/test_live_lifecycle.py`` and
``tests/unit/test_live_cli.py`` on purpose: the CLI test drives the real command
with the same fakes behind it.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from opsaccord.domain.enums import IncidentStatus
from opsaccord.scenarios import live as live_scenarios
from opsaccord.scenarios.runner import ScenarioTrace
from opsaccord.tools.contracts import DispatchRefused, DispatchTimeout
from opsaccord.tools.live_environment import FLAG_SAFE_VARIANT, LiveEnvironmentUnavailable

# The controller is stubbed and the fake stack ignores its settings, so this is
# never dereferenced; it only keeps the call signature honest.
UNUSED_DB = object()
#: A flag no live scenario owns, used to prove the flag document is carried
#: through read-modify-write rather than rebuilt from the verified list.
UNRELATED_FLAG = "productCatalogFailure"


@dataclass
class Probe:
    succeeded: bool
    value: dict[str, Any]


class FakeFlagStore:
    """flagd-ui's read-modify-write document, with every write recorded."""

    def __init__(
        self,
        variants: dict[str, str] | None = None,
        *,
        refuse: set[str] | None = None,
        apply_then_fail: set[str] | None = None,
        ignore_writes: set[str] | None = None,
    ) -> None:
        self.variants: dict[str, str] = {flag_id: "off" for flag_id in FLAG_SAFE_VARIANT}
        self.variants.update(variants or {})
        self.unrelated: dict[str, dict[str, Any]] = {UNRELATED_FLAG: {"defaultVariant": "off"}}
        #: Variants refused before anything is sent.
        self.refuse = refuse or set()
        #: Variants applied and *then* left unacknowledged: flagd-ui answered
        #: nothing, so the caller cannot tell whether the change landed.
        self.apply_then_fail = apply_then_fail or set()
        #: Flags whose write is acknowledged but has no effect, which is what the
        #: read-back verification exists to catch.
        self.ignore_writes = ignore_writes or set()
        self.writes: list[tuple[str, str]] = []

    def document(self) -> dict[str, Any]:
        flags = {flag_id: {"defaultVariant": variant} for flag_id, variant in self.variants.items()}
        flags.update({flag_id: dict(body) for flag_id, body in self.unrelated.items()})
        return {"flags": flags}

    def write(self, flag_id: str, variant: str) -> dict[str, Any]:
        if variant in self.refuse:
            raise LiveEnvironmentUnavailable(f"flagd-ui refused the write {flag_id}={variant}")
        previous = self.document()["flags"][flag_id].get("defaultVariant")
        self.writes.append((flag_id, variant))
        if flag_id not in self.ignore_writes:
            if flag_id in self.variants:
                self.variants[flag_id] = variant
            else:
                self.unrelated[flag_id]["defaultVariant"] = variant
        if variant in self.apply_then_fail:
            raise DispatchTimeout(
                f"flagd-ui applied {flag_id}={variant!r} but never acknowledged it"
            )
        return {"flag_id": flag_id, "previous_variant": previous, "variant": variant}


def health(
    *, calls: float = 0.0, error_rate: float | None = None, cpu: float = 0.01
) -> dict[str, Any]:
    """One ``read_service_health`` value. Zero traffic is *not* zero error share."""
    return {
        "health": "SERVING" if calls > 0 and (error_rate or 0.0) < 0.05 else "NOT_SERVING",
        "healthy": bool(calls > 0 and (error_rate or 0.0) < 0.05 and cpu < 0.80),
        "reachable": calls > 0,
        "cpu_utilization": cpu,
        "error_rate": error_rate,
        "calls_per_second": calls,
    }


def stack_health(**per_target: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Every live target quiet and healthy except the ones named."""
    base = {
        target: health()
        for target in ("payment-service", "checkout-service", "ad-service", "cart-service")
    }
    base.update(per_target)
    return base


class FakeStack:
    """The live surfaces the driver needs, with nothing behind them."""

    is_simulated = False
    revision = "test-fixture"
    revision_source = "tests/support/live_fakes.py"

    def __init__(
        self,
        store: FakeFlagStore,
        probes: dict[str, dict[str, Any]] | None = None,
        *,
        interrupt_on: str | None = None,
        interrupt_after: int | None = None,
        interrupt_on_flag_read: int | None = None,
        flag_read_error_after: int | None = None,
    ) -> None:
        self.store = store
        self.probes = probes or {}
        self.interrupt_on = interrupt_on
        self.interrupt_after = interrupt_after
        self.interrupt_on_flag_read = interrupt_on_flag_read
        self.flag_read_error_after = flag_read_error_after
        self.flag_reads = 0
        self.reads: list[str] = []

    @property
    def writes(self) -> list[tuple[str, str]]:
        return self.store.writes

    def read_flag_document(self, *, timeout_seconds: float) -> dict[str, Any]:
        """The document, or the configured interruption once N reads happened."""
        self.flag_reads += 1
        if (
            self.interrupt_on_flag_read is not None
            and self.flag_reads > self.interrupt_on_flag_read
        ):
            raise KeyboardInterrupt
        if self.flag_read_error_after is not None and self.flag_reads > self.flag_read_error_after:
            # The real adapter raises this where it decides locally that no request
            # left the process. The fake uses it to make one lifecycle stage fail.
            raise DispatchRefused("the flag document read was refused")
        return self.store.document()

    def set_flag_variant(
        self, *, flag_id: str, variant: str, timeout_seconds: float
    ) -> dict[str, Any]:
        return self.store.write(flag_id, variant)

    def read_service_health(self, *, target: str, timeout_seconds: float) -> Probe:
        """A probe, or Ctrl+C once ``interrupt_after`` reads of that target happened."""
        self.reads.append(target)
        interrupting = target == self.interrupt_on and self.interrupt_after is not None
        if interrupting and self.reads.count(target) > int(self.interrupt_after or 0):
            raise KeyboardInterrupt
        return Probe(succeeded=True, value=dict(self.probes.get(target, {})))


class EnvironmentFactory:
    """Stands in for ``LiveEnvironment``, ignoring the settings it is handed."""

    revision = "test-fixture"
    revision_source = "tests/support/live_fakes.py"

    def __init__(self, stack: FakeStack) -> None:
        self.stack = stack

    def __call__(self, **kwargs: Any) -> FakeStack:
        return self.stack


class StubController:
    """Stands in for ``run_scenario``: a bare trace, or a failure.

    The trace is deliberately not a complete incident -- it carries no actions or
    verifications -- so it is never mistaken for realistic scenario evidence. What
    the lifecycle needs from it is only that a measurement happened.
    """

    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[str] = []

    def __call__(self, spec: Any, **kwargs: Any) -> ScenarioTrace:
        self.calls.append(spec.id)
        if self.error is not None:
            raise self.error
        return ScenarioTrace(
            spec=spec,
            incident=SimpleNamespace(
                id="inc_lifecycle_test", status=IncidentStatus.ESCALATED_INCOMPLETE
            ),
            actions=[],
            approvals=[],
            evidence=[],
            verifications=[],
            events=[],
            run_notes=[],
        )


def use_fake_stack(
    monkeypatch: pytest.MonkeyPatch, stack: FakeStack, controller: Any = None
) -> FakeStack:
    """Point the live driver at ``stack`` (and optionally at a stub controller)."""
    monkeypatch.setattr(live_scenarios, "LiveEnvironment", EnvironmentFactory(stack))
    monkeypatch.setattr(
        live_scenarios, "build_live_environment", lambda plan, settings, **kwargs: stack
    )
    if controller is not None:
        monkeypatch.setattr(live_scenarios, "run_scenario", controller)
    return stack


def live_settings() -> Any:
    """The CLI's calibrated settings, without a demo stack behind them."""
    return live_scenarios.live_settings("test")
