"""Live scenario driver: Phase 1 paths against the real pinned demo stack.

The live driver is deliberately thin. It only does the two things the simulator
does for itself -- put the demo into the scenario's starting state, and hand an
execution surface to ``run_scenario``. The controller, policy engine, gateway,
checkpointing, and objective scoring are the same code the offline suite runs, so
"live" is a property of the evidence (``ToolResult.is_simulated is False`` and
real Prometheus/Jaeger/flagd-ui reads), never of a parallel implementation.

Two ground rules keep this honest:

* the demo state that a scenario needs is a *verified* flag of the pinned
  revision, written through flagd-ui's read-modify-write API; no scenario invents
  a fault mechanism;
* an induced failure is recorded as an induced failure. ``P1-04`` and ``P1-06``
  need a fault that cannot be produced by a feature flag at all, so they point
  the adapter at an endpoint that genuinely does not answer and the report says
  so, rather than dressing a mock up as a live run.

A live run is also a *sequence* with consequences: it writes real feature flags,
so each scenario restores what it armed before the next one starts, an interrupted
run records how far it got instead of a result it never reached, and a run that
cannot establish a safe baseline stops rather than measuring a fault it did not
inject. Progress and the evidence document are written as the run proceeds; see
:mod:`opsaccord.scenarios.live_evidence`.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from opsaccord.config import Settings
from opsaccord.domain.enums import ScenarioOutcome
from opsaccord.persistence.db import Database
from opsaccord.reasoning.base import ReasoningProvider
from opsaccord.reasoning.deterministic import (
    CPU_SATURATION_CEILING,
    TELEMETRY_ERROR_BOUND,
    DeterministicAdapter,
)
from opsaccord.scenarios.fixtures import SCENARIOS, ScenarioSpec
from opsaccord.scenarios.live_evidence import (
    COMPLETED,
    INTERRUPTED,
    MEASURED,
    NOT_MEASURED,
    RUNNING,
    STOPPED,
    LiveEvidenceWriter,
    LiveProgress,
    utc_now,
    utc_stamp,
)
from opsaccord.scenarios.runner import ScenarioTrace, evaluate, run_scenario
from opsaccord.tools.contracts import Environment
from opsaccord.tools.live_environment import (
    FLAG_SAFE_VARIANT,
    LiveEnvironment,
    LiveEnvironmentUnavailable,
)

# Fault position of each verified flag. Read from src/flagd/demo.flagd.json at the
# pinned revision: every one of these flags is ENABLED with an "off" variant that
# clears the fault.
FAULT_VARIANTS: dict[str, str] = {
    "paymentUnreachable": "on",
    "paymentFailure": "100%",
    "adHighCpu": "on",
    "adFailure": "on",
    "adManualGc": "on",
    "cartFailure": "100%",
}

# An unroutable RFC 5737 address: the adapter's own bounded timeout, not a mock,
# is what stops these reads. Nothing listens there.
UNROUTABLE_HOST = "http://10.255.255.1"

# How long a scenario waits for the demo's own telemetry to show the fault it just
# armed, and how often it asks. The collector publishes span metrics on a 60s
# cadence and the adapter averages over a 300s lookback, so a fault written to
# flagd is not visible in Prometheus for a minute or two. Waiting for it is the
# difference between measuring the scenario and measuring the previous one.
FAULT_VISIBILITY_TIMEOUT_SECONDS = 420
FAULT_VISIBILITY_POLL_SECONDS = 20

# How long a scenario waits for the demo's own telemetry to start exercising the
# target before it injects a fault. The load generator in the pinned revision is
# bursty: an idle window is not a symptom, and injecting into an idle demo
# measures nothing.
TRAFFIC_VISIBILITY_TIMEOUT_SECONDS = 300

# Every stage wait above is a ceiling, and these two budgets bound the run as a
# whole. A stage may use only what is left of its scenario's budget, and nothing
# restarts a deadline: a stage that polls for minutes cannot extend the run, and a
# retry inside a stage cannot buy another full timeout.
#
# The measured healthy cost of one scenario on this stack is an immediate traffic
# check, one to two minutes for the fault to appear in a 120s span-metric window,
# and roughly four minutes of workflow and settling. Fifteen minutes per scenario
# leaves room for the span-metric cadence, and an hour for the suite stops a
# pathological run from occupying the demo for the better part of a day. Cleanup
# is deliberately *outside* both budgets: a scenario that ran out of time must
# still restore the baseline it installed.
#
# What a budget does *not* do is cancel work. The suite budget is checked between
# scenarios and it clamps each stage wait, so a scenario that has already started
# can overrun it inside the workflow or the verification settling window; nothing
# here claims a timeout stops an external operation that is already in flight.
LIVE_SCENARIO_BUDGET_SECONDS = 900
LIVE_SUITE_BUDGET_SECONDS = 3600

# One flag write plus a read-back, bounded. Used for the baseline check, the fault
# injection, and the cleanup restore, so all three fail in the same time.
FAULT_WRITE_TIMEOUT_SECONDS = 10.0

# Live-run calibration (see docs/astronomy-shop.md, "Live adapter calibration").
#
# Verification settling: a live probe averages over a real lookback window, so a
# remediation needs a bounded moment to show up in the telemetry before the
# verdict is recorded. Six rounds of 30s cover the 120s span-metric lookback plus
# one export interval. This only refreshes evidence -- never a second side effect
# and never a substitute for fresh verification.
LIVE_SETTLE_ATTEMPTS = 6
LIVE_SETTLE_SECONDS = 30.0
# Span metrics are exported every ~60s on this stack, so a shorter window returns
# no data at all rather than a fresher number.
LIVE_VERIFICATION_WINDOW_SECONDS = 120
# Budgets for a live run: the investigation costs 5 reads, dispatch 1, and up to
# six settle rounds of 3 reads each can follow, so the hard cap has to sit above
# that sum for bounded settling to be possible at all. Both remain enforced.
LIVE_SOFT_STEP_BUDGET = 12
LIVE_HARD_STEP_CAP = 32


AppEnv = Literal["dev", "test", "ci", "scenario"]


def live_settings(app_env: AppEnv = "dev", **overrides: Any) -> Settings:
    """Settings for a live run, calibrated against the pinned demo stack.

    Kept in one place so the CLI, the live integration tests, and the report all
    describe the same run; the simulator's defaults are untouched.
    """
    return Settings(
        app_env=app_env,
        demo_mode="live",
        verification_settle_attempts=LIVE_SETTLE_ATTEMPTS,
        verification_settle_seconds=LIVE_SETTLE_SECONDS,
        verification_window_seconds=LIVE_VERIFICATION_WINDOW_SECONDS,
        soft_step_budget=LIVE_SOFT_STEP_BUDGET,
        hard_step_cap=LIVE_HARD_STEP_CAP,
        **overrides,
    )


class LiveRunAborted(RuntimeError):
    """A live scenario stopped before it produced a result.

    Carries the stage it stopped at, the last thing the harness observed, and what
    cleanup did about the fault, so the record explains itself rather than
    reporting a bare exception. The raise site knows only why it gave up; the
    scenario lifecycle fills the rest in on the way out.
    """

    def __init__(self, message: str, *, stage: str = "setup") -> None:
        super().__init__(message)
        self.stage = stage
        self.observation: str | None = None
        self.cleanup: str | None = None


class LiveSetupFailed(LiveRunAborted):
    """The demo never showed the state a scenario needs, so the run proves nothing."""


class LiveBaselineUnavailable(LiveRunAborted):
    """The demo is not in a safe baseline, so nothing measured after it is trustworthy."""


class LiveCleanupFailed(RuntimeError):
    """A scenario could not return the demo to its baseline.

    Raised with the original failure attached as ``__cause__`` when there was one,
    so both survive in the report. It always stops the suite: the next scenario
    would otherwise be measured against a demo that is not in its starting state.
    ``result`` carries the scenario's own measurement when the workflow had
    already completed, because a cleanup failure is not a reason to discard a
    finished measurement -- it is a reason to stop and tell the operator.
    """

    def __init__(
        self, message: str, *, stage: str = "cleanup", result: LiveResult | None = None
    ) -> None:
        super().__init__(message)
        self.stage = stage
        self.cleanup = message
        self.result = result


@dataclass
class _ScenarioFault:
    """What a scenario armed, and therefore what it is allowed to restore.

    Ownership is what keeps cleanup honest in both directions: a scenario never
    writes back a fault it did not install, and it never leaves one it did install
    behind. ``armed`` is set only after the write is acknowledged.

    ``attempted`` is a second, weaker state and it is the one cleanup keys off,
    because "no acknowledgement" and "no change" are different statements. A
    flagd-ui write that is applied but whose response is lost raises a dispatch
    timeout through the adapter, so a scenario that tried to arm a fault restores
    it defensively and says that it cannot prove which position the demo was in.
    """

    flag_id: str | None = None
    armed_variant: str | None = None
    attempted: bool = False
    armed: bool = False

    @property
    def owned(self) -> bool:
        return self.attempted and self.flag_id is not None


@dataclass(frozen=True)
class FaultRestoration:
    """The outcome of returning one scenario's fault flag to its safe variant."""

    flag_id: str | None
    restored: bool
    detail: str
    previous_variant: str | None = None
    observed_variant: str | None = None
    error: str | None = None

    @property
    def failed(self) -> bool:
        return not self.restored


@dataclass(frozen=True)
class LivePlan:
    """How to put the real demo into the scenario's starting state."""

    scenario_id: str
    setup: str
    fault_flag: str | None = None
    induced_failure: str | None = None
    live: bool = True
    # Which measured signal proves the armed fault is visible. "error_rate" waits
    # for failing RPCs, "cpu" for a CPU-saturated container. Both are positive
    # observations: a quiet demo must never be mistaken for a fault.
    fault_signal: str = "error_rate"
    # A second target whose own failing RPCs also prove this fault exists.
    #
    # ``paymentUnreachable`` is read by checkout, so the failing RPCs are
    # checkout's own client calls, and the armed Charge call rate drops to about
    # 0.042/s -- a 120s window can legitimately contain no Charge call at all, and
    # the harness must not wait for evidence the fault makes intermittent. This
    # target is a *corroborating* observation, recorded with its own attribution,
    # never a substitute for one: zero traffic never counts, on either target.
    corroborating_target: str | None = None


LIVE_PLANS: dict[str, LivePlan] = {
    "P1-01": LivePlan(
        scenario_id="P1-01",
        setup="flagd-ui: paymentUnreachable=on",
        fault_flag="paymentUnreachable",
        corroborating_target="checkout-service",
    ),
    "P1-02": LivePlan(
        scenario_id="P1-02",
        setup="flagd-ui: adHighCpu=on",
        fault_flag="adHighCpu",
        fault_signal="cpu",
    ),
    "P1-03": LivePlan(
        scenario_id="P1-03",
        setup="flagd-ui: cartFailure=100%",
        fault_flag="cartFailure",
    ),
    "P1-04": LivePlan(
        scenario_id="P1-04",
        setup="Prometheus and Jaeger pointed at an unroutable address",
        induced_failure="telemetry_read_timeout",
    ),
    "P1-05": LivePlan(
        scenario_id="P1-05",
        setup="flagd-ui: paymentUnreachable=on; human decision is REJECTED",
        fault_flag="paymentUnreachable",
        corroborating_target="checkout-service",
    ),
    "P1-06": LivePlan(
        scenario_id="P1-06",
        setup="flagd-ui: paymentUnreachable=on; flag control plane unroutable after approval",
        fault_flag="paymentUnreachable",
        induced_failure="control_plane_timeout",
        corroborating_target="checkout-service",
    ),
    "P1-07": LivePlan(
        scenario_id="P1-07",
        setup="flagd-ui: paymentUnreachable=on; the fault is re-armed right after dispatch",
        fault_flag="paymentUnreachable",
        induced_failure="fault_rearmed_after_dispatch",
        corroborating_target="checkout-service",
    ),
}


class FaultRearmingEnvironment:
    """Live surface that restores the injected fault immediately after dispatch.

    Adversarial harness for ``P1-07``. The remediation really is written and really
    is confirmed applied, but the fault is back before verification runs, so the
    service is genuinely unhealthy when the fresh evidence is collected. The
    incident must refuse RESOLVED on that evidence; an implementation that treated
    a successful write as proof of recovery would fail this.
    """

    is_simulated = False

    def __init__(self, *, environment: LiveEnvironment, flag_id: str, fault_variant: str) -> None:
        self._environment = environment
        self._flag_id = flag_id
        self._fault_variant = fault_variant
        self.rearmed = False

    @property
    def revision(self) -> str:
        return self._environment.revision

    @property
    def revision_source(self) -> str:
        return self._environment.revision_source

    def apply_remediation(self, **kwargs: Any) -> Any:
        result = self._environment.apply_remediation(**kwargs)
        if getattr(result, "succeeded", False):
            self._environment.set_flag_variant(
                flag_id=self._flag_id,
                variant=self._fault_variant,
                timeout_seconds=self._environment._http_timeout,
            )
            self.rearmed = True
        return result

    def __getattr__(self, name: str) -> Any:
        return getattr(self._environment, name)


def reset_fault_flags(
    environment: Environment, *, timeout_seconds: float = FAULT_WRITE_TIMEOUT_SECONDS
) -> dict[str, str | None]:
    """Return every verified fault flag to its safe variant.

    Called before and after a live run so one scenario cannot inherit the fault of
    the previous one, and so the demo is left clean for the operator. Only the six
    verified flags of the pinned revision are ever written, and each write is a
    read-modify-write of the whole document, so every unrelated flag is carried
    through unchanged.
    """
    live = _as_live(environment)
    document = live.read_flag_document(timeout_seconds=timeout_seconds)
    flags = document.get("flags", {})
    previous: dict[str, str | None] = {}
    for flag_id, safe_variant in FLAG_SAFE_VARIANT.items():
        if flag_id not in flags:
            continue
        previous[flag_id] = flags[flag_id].get("defaultVariant")
        if previous[flag_id] != safe_variant:
            live.set_flag_variant(
                flag_id=flag_id, variant=safe_variant, timeout_seconds=timeout_seconds
            )
    return previous


def detect_residual_faults(
    environment: Environment, *, timeout_seconds: float = FAULT_WRITE_TIMEOUT_SECONDS
) -> dict[str, str]:
    """Verified fault flags that are not in their safe position right now.

    An interrupted run, a killed process or a host restart can leave a fault
    armed, and a fault nobody injected is not a measurement. The set is reported
    rather than assumed: a flag that is absent from the document is not claimed to
    be at its safe variant.

    Absence is not safety, so a document that does not contain every verified flag
    raises :class:`LiveBaselineUnavailable` instead of reporting an empty residual
    set. The demo's baseline cannot be established from flags the harness never
    observed, and "no residual faults" is a claim about state it read.
    """
    document = _as_live(environment).read_flag_document(timeout_seconds=timeout_seconds)
    flags = document.get("flags", {})
    missing = sorted(set(FLAG_SAFE_VARIANT) - set(flags))
    if missing:
        raise LiveBaselineUnavailable(
            f"the flag document is missing {len(missing)} of the {len(FLAG_SAFE_VARIANT)} "
            f"verified scenario flags ({missing}), so the demo's baseline cannot be "
            "established",
            stage="baseline",
        )
    residual: dict[str, str] = {}
    for flag_id, safe_variant in FLAG_SAFE_VARIANT.items():
        current = flags[flag_id].get("defaultVariant")
        if current != safe_variant:
            residual[flag_id] = str(current)
    return residual


def restore_owned_fault(
    environment: Environment,
    fault: _ScenarioFault,
    *,
    timeout_seconds: float = FAULT_WRITE_TIMEOUT_SECONDS,
) -> FaultRestoration:
    """Return *this scenario's* fault flag to its safe variant, and verify it.

    The document is re-read first, so the write carries every other flag through
    unchanged and a flag this scenario did not arm is never touched. The read-back
    is what makes the recorded value an observation rather than a request: a write
    that is acknowledged but not applied is reported as a failure, not as a clean
    baseline.
    """
    if not fault.owned or fault.flag_id is None:
        return FaultRestoration(
            flag_id=fault.flag_id,
            restored=True,
            detail="no fault was armed by this scenario, so nothing needed restoring",
        )
    flag_id = fault.flag_id
    safe_variant = FLAG_SAFE_VARIANT[flag_id]
    caveat = (
        ""
        if fault.armed
        else (
            "; this scenario's arm write was never acknowledged, so the position the "
            "demo held between the attempt and this restore is not established"
        )
    )
    live = _as_live(environment)
    try:
        written = live.set_flag_variant(
            flag_id=flag_id, variant=safe_variant, timeout_seconds=timeout_seconds
        )
        document = live.read_flag_document(timeout_seconds=timeout_seconds)
        observed = document.get("flags", {}).get(flag_id, {}).get("defaultVariant")
    except Exception as exc:
        # Every failure here means the same operational thing: the demo may not be
        # back at its baseline, so the caller must stop rather than continue.
        failure = f"{type(exc).__name__}: {exc}"
        return FaultRestoration(
            flag_id=flag_id,
            restored=False,
            detail=(
                f"{flag_id} could not be restored to {safe_variant!r} "
                f"({failure}); the demo is not in its baseline{caveat}"
            ),
            error=failure,
        )
    previous = written.get("previous_variant") if isinstance(written, dict) else None
    if observed != safe_variant:
        return FaultRestoration(
            flag_id=flag_id,
            restored=False,
            detail=(
                f"{flag_id} was written back as {safe_variant!r} but reads as {observed!r}; "
                f"the demo is not in its baseline{caveat}"
            ),
            previous_variant=previous,
            observed_variant=observed,
            error=f"read-back mismatch: {observed!r}",
        )
    return FaultRestoration(
        flag_id=flag_id,
        restored=True,
        detail=(
            f"{flag_id} restored to {safe_variant!r} (was {previous!r}); "
            f"read back {observed!r}{caveat}"
        ),
        previous_variant=previous,
        observed_variant=observed,
    )


def arm_fault(
    environment: Environment,
    *,
    flag_id: str,
    variant: str,
    timeout_seconds: float = FAULT_WRITE_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Switch one verified fault flag to its fault position."""
    return _as_live(environment).set_flag_variant(
        flag_id=flag_id, variant=variant, timeout_seconds=timeout_seconds
    )


def preflight(environment: Environment, *, timeout_seconds: float = 5.0) -> dict[str, Any]:
    """Prove the three live channels answer before a run is allowed to start."""
    live = _as_live(environment)
    document = live.read_flag_document(timeout_seconds=timeout_seconds)
    flags = document.get("flags", {})
    missing = sorted(set(FLAG_SAFE_VARIANT) - set(flags))
    return {
        "flag_count": len(flags),
        "missing_verified_flags": missing,
        "prometheus_series": len(live.prometheus.series_names(timeout_seconds)),
    }


def _as_live(environment: Environment) -> Any:
    """The surface that owns the live flag document, or a refusal.

    The check is structural rather than an ``isinstance`` of one class, so the
    lifecycle can be driven by a test double without a running stack, while the
    one thing that must never happen stays impossible: a live plan may not write a
    fault through a *simulated* surface, where the write would be a no-op and the
    scenario would measure nothing. ``is_simulated`` is that discriminator, which
    is the same flag every record already carries.

    The two adversarial wrappers of this module hold the real environment in
    ``_environment`` and are unwrapped, so a write goes to the real flag document
    even when the wrapper is what fails the remediation dispatch.
    """
    candidate: Any = environment
    inner = getattr(environment, "_environment", None)
    if inner is not None and getattr(inner, "is_simulated", True) is False:
        candidate = inner
    if getattr(candidate, "is_simulated", True) is not False:
        raise LiveEnvironmentUnavailable("live driver requires a live execution surface")
    if not callable(getattr(candidate, "set_flag_variant", None)) or not callable(
        getattr(candidate, "read_flag_document", None)
    ):
        raise LiveEnvironmentUnavailable(
            "live driver requires a surface with the flag document API"
        )
    return candidate


def _as_float(value: Any, default: float = 0.0) -> float:
    """Telemetry arrives as untrusted JSON; a non-numeric value is not a number."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def wait_for_traffic(
    environment: Environment,
    *,
    target: str,
    timeout_seconds: float | None = None,
    poll_seconds: float | None = None,
    observe: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Block until the demo is actually exercising the target.

    A fault can only be observed on traffic that exists. Waiting here keeps a
    quiet load generator from being reported as a fault, and from being reported
    as a failed run. ``timeout_seconds`` is computed by the caller from what is
    left of the scenario budget, so the wait cannot outlive the scenario.
    """
    timeout_seconds = (
        TRAFFIC_VISIBILITY_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
    )
    poll_seconds = FAULT_VISIBILITY_POLL_SECONDS if poll_seconds is None else poll_seconds
    deadline = time.monotonic() + timeout_seconds
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        health = environment.read_service_health(target=target, timeout_seconds=8.0)
        last = _health_snapshot(health.value)
        if health.succeeded and _as_float(health.value.get("calls_per_second")) > 0.0:
            return last
        if observe is not None:
            observe(last)
        time.sleep(poll_seconds)
    raise LiveSetupFailed(
        f"{target} saw no traffic within {timeout_seconds}s, so a fault could not be "
        f"measured; last probe: {last}"
    )


def _fault_visible(signal: str, probe: dict[str, Any]) -> bool:
    """True when a probe shows the armed fault as a positive observation."""
    if signal == "cpu":
        return _as_float(probe.get("cpu_utilization")) >= CPU_SATURATION_CEILING
    error_rate = probe.get("error_rate")
    return error_rate is not None and _as_float(error_rate) > TELEMETRY_ERROR_BOUND


def _health_snapshot(value: dict[str, Any]) -> dict[str, Any]:
    """The fields a health probe reports, copied out of the untrusted value."""
    return {
        key: value.get(key)
        for key in (
            "health",
            "healthy",
            "reachable",
            "cpu_utilization",
            "error_rate",
            "calls_per_second",
        )
    }


def wait_for_fault(
    environment: Environment,
    *,
    target: str,
    signal: str = "error_rate",
    corroborating_target: str | None = None,
    corroborating_before: dict[str, Any] | None = None,
    timeout_seconds: float | None = None,
    poll_seconds: float | None = None,
    observe: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Block until the target's own health probe reports the armed fault.

    These probes are harness readiness checks, not evidence: they go straight to
    the adapter and are deliberately *not* recorded through the gateway. Evidence
    collection stays the controller's job, so a scenario cannot seed its own
    evidence by waiting.

    The contract differs per fault, and the difference is the point. An
    ``adHighCpu`` burn is a resource fault that the ad container itself shows. A
    ``paymentUnreachable`` fault is a connection failure *checkout* observes, and
    its armed Charge call rate (~0.042/s, see the calibration table) is low enough
    that a single 120s window can contain no Charge call at all, so waiting only
    for the payment target's own error share can wait for an observation the fault
    makes intermittent. ``corroborating_target`` adds that service's own failing
    RPCs as a second positive observation, and the returned probe always says
    which series carried it.

    What does *not* change: an absent series is not a zero, and zero traffic is
    never read as a fault, on either target. The predicate is an error share
    strictly above the telemetry bound, so a quiet demo keeps waiting and the
    failure mode is an honest setup failure rather than a fabricated one.

    The corroborating observation is differential. ``corroborating_before`` carries
    that target's error share from *before* this scenario armed anything, and the
    caller's share has to be both above the bound and higher than that baseline.
    Without it, a checkout container failing for a reason this scenario did not
    cause would satisfy the premise: the corroborating series is a caller-side
    signal, and a caller can be broken on its own.
    """
    timeout_seconds = (
        FAULT_VISIBILITY_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
    )
    poll_seconds = FAULT_VISIBILITY_POLL_SECONDS if poll_seconds is None else poll_seconds
    deadline = time.monotonic() + timeout_seconds
    last: dict[str, Any] = {}
    started = time.monotonic()
    while time.monotonic() < deadline:
        health = environment.read_service_health(target=target, timeout_seconds=8.0)
        last = {
            "target": target,
            "signal": signal,
            "attribution": "target",
            "probe": _health_snapshot(health.value),
            "corroborating": None,
            "corroborating_before": corroborating_before,
        }
        if health.succeeded and _fault_visible(signal, last["probe"]):
            last["elapsed_seconds"] = round(time.monotonic() - started, 1)
            return last
        if corroborating_target is not None:
            caller = environment.read_service_health(
                target=corroborating_target, timeout_seconds=8.0
            )
            last["corroborating"] = {
                "target": corroborating_target,
                "probe": _health_snapshot(caller.value),
            }
            share = _as_float(last["corroborating"]["probe"].get("error_rate"))
            before = _as_float((corroborating_before or {}).get("error_rate"))
            if (
                caller.succeeded
                and _fault_visible("error_rate", last["corroborating"]["probe"])
                and share > before
            ):
                last["attribution"] = "caller"
                last["elapsed_seconds"] = round(time.monotonic() - started, 1)
                return last
        if observe is not None:
            observe(last)
        time.sleep(poll_seconds)
    raise LiveSetupFailed(
        f"{target} never showed the armed fault ({signal}) within {timeout_seconds}s; "
        f"last probe: {last}"
    )


def build_live_environment(
    plan: LivePlan, settings: Settings, *, http_client: Any | None = None
) -> Environment:
    """Build the execution surface a live plan needs, including induced faults."""
    if plan.induced_failure == "telemetry_read_timeout":
        # Metric and trace reads go nowhere, so the adapter's own deadline and
        # retry bound is what ends each attempt. Flag reads still work, which is
        # exactly the "metrics timed out but configuration is readable" case.
        black_hole = settings.model_copy(
            update={
                "prometheus_base_url": f"{UNROUTABLE_HOST}:9090",
                "jaeger_base_url": f"{UNROUTABLE_HOST}:16686",
            }
        )
        return LiveEnvironment(settings=black_hole, http_client=http_client)

    environment = LiveEnvironment(settings=settings, http_client=http_client)

    if plan.induced_failure == "control_plane_timeout":
        return _ControlPlaneTimeoutEnvironment(environment)

    if plan.induced_failure == "fault_rearmed_after_dispatch":
        assert plan.fault_flag is not None
        return FaultRearmingEnvironment(
            environment=environment,
            flag_id=plan.fault_flag,
            fault_variant=FAULT_VARIANTS[plan.fault_flag],
        )

    return environment


class _ControlPlaneTimeoutEnvironment:
    """Live reads, but the flag control plane never answers the write.

    Adversarial harness for ``P1-06``: evidence collection is real and the
    approval gate is real, but the dispatch cannot be confirmed. The action must
    keep its identity, stay UNCERTAIN, be asked about again through the external
    state question, and escalate -- never be blindly repeated.
    """

    is_simulated = False

    def __init__(self, environment: LiveEnvironment) -> None:
        self._environment = environment
        self._timeout = environment._http_timeout

    @property
    def revision(self) -> str:
        return self._environment.revision

    @property
    def revision_source(self) -> str:
        return self._environment.revision_source

    def apply_remediation(self, **kwargs: Any) -> Any:
        from opsaccord.tools.contracts import DispatchTimeout

        raise DispatchTimeout(
            f"flagd-ui write exceeded {self._timeout}s (induced control-plane timeout)"
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._environment, name)


@dataclass
class LiveResult:
    scenario_id: str
    title: str
    setup: str
    outcome: ScenarioOutcome
    reasons: list[str]
    simulated: bool
    # None when the scenario's premise never appeared and nothing was measured.
    trace: ScenarioTrace | None = None
    fault_visible: dict[str, Any] | None = None
    # Lifecycle bookkeeping: how far the scenario got, what happened to the demo
    # afterwards, and what it cost. A record with no trace is not a measurement.
    stage: str = "complete"
    cleanup: str | None = None
    elapsed_seconds: float | None = None

    @property
    def status(self) -> str:
        return MEASURED if self.trace is not None else NOT_MEASURED

    def to_record(self) -> dict[str, Any]:
        """The persisted shape of one scenario, measured or not.

        Both cases carry the same keys, so a reader can diff two records without
        first working out which kind it has, and a missing measurement is explicit
        rather than inferred from an absent field.
        """
        record: dict[str, Any] = {
            "scenario_id": self.scenario_id,
            "title": self.title,
            "setup": self.setup,
            "outcome": "NOT_MEASURED" if self.trace is None else str(self.outcome),
            "status": self.status,
            "stage": self.stage,
            "cleanup": self.cleanup,
            "elapsed_seconds": self.elapsed_seconds,
            "fault_visible": self.fault_visible,
            "simulated": self.simulated,
            "reasons": list(self.reasons),
        }
        trace = self.trace
        if trace is None:
            record.update(
                {
                    "incident_id": None,
                    "final_status": None,
                    "executed_actions": None,
                    "duplicate_side_effects": None,
                    "forbidden_actions": None,
                    "resolved_without_verification": None,
                    "evidence": None,
                    "simulated_evidence": None,
                    "approvals": None,
                    "verifications": [],
                    "run_notes": [],
                }
            )
            return record
        record.update(
            {
                "incident_id": trace.incident.id,
                "final_status": str(trace.incident.status),
                "executed_actions": len(trace.executed_actions),
                "duplicate_side_effects": trace.duplicate_side_effects,
                "forbidden_actions": len(trace.forbidden_actions),
                "resolved_without_verification": trace.resolved_without_verification,
                "evidence": len(trace.evidence),
                "simulated_evidence": sum(1 for item in trace.evidence if item.is_simulated),
                "approvals": len(trace.approvals),
                "verifications": [str(v.outcome) for v in trace.verifications],
                "run_notes": list(trace.run_notes),
            }
        )
        return record


@dataclass
class LiveRun:
    """What one suite invocation did, including whether it finished."""

    selected: list[str]
    results: list[LiveResult]
    status: str
    started_at: str
    notes: list[str]
    evidence_path: Path | None = None
    #: Reporting failures that did not stop the run. A command reports these as a
    #: failed run: a live result that could not be persisted is not a delivered
    #: result, and the demo may hold a fault nobody wrote down.
    evidence_errors: list[str] = field(default_factory=list)

    @property
    def measured(self) -> list[LiveResult]:
        return [result for result in self.results if result.trace is not None]

    @property
    def failures(self) -> list[LiveResult]:
        return [result for result in self.results if result.outcome is not ScenarioOutcome.PASS]

    @property
    def finished(self) -> bool:
        return self.status == COMPLETED


class _ScenarioBudget:
    """One deadline, computed once and never restarted.

    Every stage clamps its own wait to what is left, so a stage that polls for
    minutes cannot extend the run and a retry inside a stage cannot buy another
    full timeout. Cleanup is deliberately outside this budget: a scenario that ran
    out of time must still restore the baseline it installed.
    """

    def __init__(self, seconds: float) -> None:
        self._seconds = float(seconds)
        self._started = time.monotonic()

    @property
    def remaining(self) -> float:
        # Never negative: the value is printed as a stage budget and used as a
        # timeout, and "less than nothing left" is a worse description than zero.
        return max(0.0, self._seconds - (time.monotonic() - self._started))

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._started

    def clamp(self, requested: float) -> float:
        return max(0.0, min(float(requested), self.remaining))


def run_live_scenario(
    spec: ScenarioSpec,
    plan: LivePlan,
    *,
    db: Database,
    settings: Settings,
    provider: ReasoningProvider | None = None,
    approve: bool | None = None,
    progress: LiveProgress | None = None,
    budget_seconds: float | None = None,
    poll_seconds: float | None = None,
) -> LiveResult:
    """Put the demo into the scenario's starting state, run it, and clean up after it.

    The shape is an explicit lifecycle -- baseline, traffic, fault injection, fault
    visibility, workflow, verification, cleanup, outcome -- and cleanup is *not*
    conditional on the earlier stages succeeding. A ``LiveSetupFailed`` raised
    after the fault was armed, an unexpected exception from the workflow, and
    Ctrl+C all leave through the same path, so a fault this scenario installed is
    never left armed for the next one.

    Cleanup failures are not swallowed either: they raise
    :class:`LiveCleanupFailed` with the original failure attached as ``__cause__``,
    which stops the suite, because a scenario measured against a demo that is not
    in its baseline proves nothing about the *next* scenario. A measurement that
    had already completed is carried out on the exception rather than discarded.

    ``poll_seconds`` is the readiness-probe cadence. Production uses the calibrated
    value; the lifecycle tests use a short one so they can exercise every exit path
    without waiting through a real telemetry window. Both defaults are the module
    constants, resolved here rather than bound into the signature, so a caller can
    bound a run without editing the driver.
    """
    budget_seconds = LIVE_SCENARIO_BUDGET_SECONDS if budget_seconds is None else budget_seconds
    poll_seconds = FAULT_VISIBILITY_POLL_SECONDS if poll_seconds is None else poll_seconds
    reporter = progress or LiveProgress()
    budget = _ScenarioBudget(budget_seconds)
    environment = build_live_environment(plan, settings)
    fault = _ScenarioFault(
        flag_id=plan.fault_flag,
        armed_variant=FAULT_VARIANTS.get(plan.fault_flag) if plan.fault_flag else None,
    )
    if plan.fault_flag is not None and fault.armed_variant is None:
        # Fail closed on a plan whose premise cannot be injected. Running it anyway
        # would measure an unfaulted demo and call the result a live path.
        raise LiveSetupFailed(
            f"{spec.id} arms {plan.fault_flag!r}, which is not one of the verified fault "
            f"flags {sorted(FAULT_VARIANTS)}; the scenario's premise cannot be injected",
            stage="fault-injection",
        )
    fault_visible: dict[str, Any] | None = None
    trace: ScenarioTrace | None = None
    outcome: ScenarioOutcome | None = None
    reasons: list[str] = []
    stage = "baseline"
    body_error: BaseException | None = None

    try:
        reporter.enter_stage(
            "baseline",
            budget_seconds=budget.remaining,
            message="checking that the demo is in its baseline",
        )
        _establish_scenario_baseline(
            reporter, environment=environment, scenario_id=spec.id, reasons=reasons
        )

        if plan.fault_flag is not None and fault.armed_variant is not None:
            # Traffic first, then the fault: the demo has to be exercising the
            # target for the injected fault to be measurable at all.
            stage = "traffic-wait"
            traffic_budget = budget.clamp(TRAFFIC_VISIBILITY_TIMEOUT_SECONDS)
            reporter.enter_stage(
                stage,
                budget_seconds=traffic_budget,
                message=f"waiting for {spec.service} to be exercised before injecting a fault",
            )
            wait_for_traffic(
                environment,
                target=spec.service,
                timeout_seconds=traffic_budget,
                poll_seconds=poll_seconds,
                observe=lambda probe: reporter.observation(
                    _traffic_observation(spec.service, probe)
                ),
            )

            stage = "fault-injection"
            reporter.enter_stage(
                stage,
                budget_seconds=budget.remaining,
                message=f"arming {plan.fault_flag}={fault.armed_variant}",
            )
            # Recorded before the write is attempted, not after it is acknowledged:
            # if the write is applied and its response is lost, the scenario must
            # still restore what it may have installed.
            fault.attempted = True
            arm_fault(
                environment,
                flag_id=plan.fault_flag,
                variant=fault.armed_variant,
                timeout_seconds=FAULT_WRITE_TIMEOUT_SECONDS,
            )
            fault.armed = True

            # The caller's own error share *before* the fault was armed, so the
            # corroborating observation below is a rise and not just a level.
            corroborating_before = _corroborating_baseline(environment, plan.corroborating_target)

            stage = "fault-visibility"
            visibility_budget = budget.clamp(FAULT_VISIBILITY_TIMEOUT_SECONDS)
            corroborating = f" or {plan.corroborating_target}" if plan.corroborating_target else ""
            reporter.enter_stage(
                stage,
                budget_seconds=visibility_budget,
                message=f"waiting for the armed fault to appear on {spec.service}{corroborating}",
            )
            fault_visible = wait_for_fault(
                environment,
                target=spec.service,
                signal=plan.fault_signal,
                corroborating_target=plan.corroborating_target,
                corroborating_before=corroborating_before,
                timeout_seconds=visibility_budget,
                poll_seconds=poll_seconds,
                observe=lambda probe: reporter.observation(_fault_observation(probe)),
            )

        stage = "workflow"
        reporter.enter_stage(
            stage,
            budget_seconds=budget.remaining,
            message="executing the scenario through the controller",
        )
        trace = run_scenario(
            spec,
            db=db,
            settings=settings,
            provider=provider or DeterministicAdapter(),
            approve=approve,
            environment=environment,
        )

        stage = "verification"
        reporter.enter_stage(
            stage,
            budget_seconds=budget.remaining,
            message="scoring the persisted trace against the objective conditions",
        )
        outcome, evaluation_reasons = evaluate(trace)
        reasons.extend(evaluation_reasons)
    except BaseException as exc:
        # Ctrl+C lands here too: it is recorded and re-raised, never converted
        # into a result, but the cleanup below still runs.
        body_error = exc

    failure_stage = stage
    last_observation = reporter.last_observation
    reporter.mark_body_finished(failure_stage)
    reporter.enter_stage("cleanup", message="restoring this scenario's fault flag")
    cleanup = restore_owned_fault(environment, fault, timeout_seconds=FAULT_WRITE_TIMEOUT_SECONDS)
    reporter.observation(f"cleanup: {cleanup.detail}")

    if isinstance(body_error, LiveRunAborted):
        body_error.stage = failure_stage
        body_error.observation = last_observation
        body_error.cleanup = cleanup.detail
    elif body_error is not None:
        # An unexpected exception -- or Ctrl+C -- carries the same lifecycle facts
        # out on the object that is re-raised, so the suite records the stage it
        # really happened in and the cleanup that really ran, instead of labelling
        # every unexpected failure "workflow" and dropping both.
        _attach_lifecycle(
            body_error,
            stage=failure_stage,
            observation=last_observation,
            cleanup=cleanup.detail,
        )

    result: LiveResult | None = None
    if body_error is None:
        # Reaching here means the body ran to completion: both of these are set
        # together on the verification stage, and any other exit raised above.
        assert trace is not None and outcome is not None
        result = _measured_result(
            spec,
            plan,
            outcome,
            trace,
            reasons=reasons,
            fault_visible=fault_visible,
            cleanup=cleanup,
            settings=settings,
            elapsed_seconds=budget.elapsed,
        )
    if cleanup.failed:
        raise LiveCleanupFailed(cleanup.detail, stage=failure_stage, result=result) from body_error
    if body_error is not None:
        raise body_error

    reporter.enter_stage("complete", message=f"outcome {outcome}")
    assert result is not None
    return result


def _measured_result(
    spec: ScenarioSpec,
    plan: LivePlan,
    outcome: ScenarioOutcome,
    trace: ScenarioTrace,
    *,
    reasons: list[str],
    fault_visible: dict[str, Any] | None,
    cleanup: FaultRestoration,
    settings: Settings,
    elapsed_seconds: float,
) -> LiveResult:
    """Finish a measured scenario: settling note, fault attribution, provenance."""
    reasons.extend(
        [
            f"verification settling: {settings.verification_settle_attempts} attempt(s) "
            f"x {settings.verification_settle_seconds}s",
            _fault_reason(fault_visible) if fault_visible else "no fault armed",
            f"cleanup: {cleanup.detail}",
        ]
    )

    # Provenance is read back out of the evidence that was actually collected, not
    # asserted by the driver. If a live run ever collected simulator evidence, the
    # result says so instead of claiming to be live.
    simulated_evidence = [item for item in trace.evidence if item.is_simulated]
    if simulated_evidence:
        reasons.append(
            f"provenance: {len(simulated_evidence)} of {len(trace.evidence)} evidence record(s) "
            "came from a simulator during a live run; this is a defect, not a live result"
        )
    if not trace.evidence:
        reasons.append("provenance: the live run collected no evidence at all")

    return LiveResult(
        scenario_id=spec.id,
        title=spec.title,
        setup=plan.setup,
        outcome=outcome,
        reasons=reasons,
        simulated=bool(simulated_evidence),
        trace=trace,
        fault_visible=fault_visible,
        stage="complete",
        cleanup=cleanup.detail,
        elapsed_seconds=round(elapsed_seconds, 1),
    )


def _establish_scenario_baseline(
    reporter: LiveProgress,
    *,
    environment: Environment,
    scenario_id: str,
    reasons: list[str],
) -> None:
    """Refuse to measure a scenario on a demo that is already carrying a fault.

    A residual flag would make this scenario measure something it did not inject,
    so it is repaired here and read back. If it survives the repair, the scenario
    stops the suite rather than continuing against a demo whose state cannot be
    established.
    """
    residual = detect_residual_faults(environment, timeout_seconds=FAULT_WRITE_TIMEOUT_SECONDS)
    if not residual:
        return
    reporter.observation(f"residual scenario faults before {scenario_id}: {residual}; resetting")
    reset_fault_flags(environment, timeout_seconds=FAULT_WRITE_TIMEOUT_SECONDS)
    still_set = detect_residual_faults(environment, timeout_seconds=FAULT_WRITE_TIMEOUT_SECONDS)
    if still_set:
        raise LiveBaselineUnavailable(
            f"residual scenario faults could not be cleared before {scenario_id}: {still_set}",
            stage="baseline",
        )
    reasons.append(f"baseline repaired before the run: {residual} -> safe")


def _traffic_observation(target: str, probe: dict[str, Any]) -> str:
    return (
        f"{target}: waiting for traffic (calls_per_second={probe.get('calls_per_second')} "
        f"reachable={probe.get('reachable')})"
    )


def _corroborating_baseline(environment: Environment, target: str | None) -> dict[str, Any] | None:
    """One target's health *before* a scenario arms anything.

    Used as the differential baseline for the corroborating observation. A probe
    that does not answer is not a baseline of zero: it is no baseline, and the
    differential test is then skipped rather than invented.
    """
    if target is None:
        return None
    probe = environment.read_service_health(target=target, timeout_seconds=8.0)
    if not probe.succeeded:
        return None
    return _health_snapshot(probe.value)


def _fault_observation(probe: dict[str, Any]) -> str:
    seen = probe.get("probe") or {}
    text = (
        f"{probe.get('target')}: waiting for the armed fault "
        f"(error_rate={seen.get('error_rate')} cpu_utilization={seen.get('cpu_utilization')} "
        f"calls_per_second={seen.get('calls_per_second')})"
    )
    corroborating = probe.get("corroborating")
    if corroborating:
        caller = corroborating.get("probe") or {}
        text += (
            f"; {corroborating.get('target')} error_rate={caller.get('error_rate')} "
            f"calls_per_second={caller.get('calls_per_second')}"
        )
    return text


def _fault_reason(fault_visible: dict[str, Any]) -> str:
    """One recorded line saying which series proved the fault, and what it read."""
    if fault_visible.get("attribution") == "caller":
        corroborating = fault_visible.get("corroborating") or {}
        return (
            "fault visible after arming on the caller's own RPCs: "
            f"{corroborating.get('target')} probe {corroborating.get('probe')}; "
            f"target probe {fault_visible.get('probe')}"
        )
    return f"fault visible after arming: {fault_visible.get('probe')}"


def _unmeasured_result(
    *,
    scenario_id: str,
    reason: str,
    stage: str,
    cleanup: str | None = None,
    observation: str | None = None,
) -> LiveResult:
    """A scenario that produced no result, recorded as such.

    This is never a scored outcome: calling a missing premise an
    ``INEFFECTIVE_FAILURE`` would blame the system for a demo that did not show
    the fault, and calling it a pass would be worse.
    """
    spec = SCENARIOS[scenario_id]
    plan = LIVE_PLANS[scenario_id]
    reasons = [reason]
    if observation:
        reasons.append(f"last observation: {observation}")
    if cleanup:
        reasons.append(f"cleanup: {cleanup}")
    return LiveResult(
        scenario_id=scenario_id,
        title=spec.title,
        setup=plan.setup,
        outcome=ScenarioOutcome.INEFFECTIVE_FAILURE,
        reasons=reasons,
        simulated=False,
        trace=None,
        fault_visible=None,
        stage=stage,
        cleanup=cleanup,
    )


def _cause_suffix(exc: BaseException) -> str:
    """Name the failure a cleanup failure replaced, so neither one is lost.

    ``run_live_scenario`` raises :class:`LiveCleanupFailed` *from* the original
    failure, which makes it ``__cause__``. The record has to carry it explicitly:
    a reader of the evidence file does not have the traceback.
    """
    cause = exc.__cause__
    if cause is None:
        return ""
    return f" (the scenario had already failed: {type(cause).__name__}: {cause})"


def _attach_lifecycle(
    exc: BaseException, *, stage: str, observation: str | None, cleanup: str | None
) -> None:
    """Attach the lifecycle facts to an exception that is about to be re-raised.

    The exception's own type and message stay intact -- a caller still sees the
    failure that actually happened -- and the suite reads these attributes when it
    records the scenario. Attaching them is best effort: an exception type with no
    instance dictionary loses the annotation, which degrades the record and never
    the run.
    """
    for name, value in (("stage", stage), ("observation", observation), ("cleanup", cleanup)):
        try:
            setattr(exc, name, value)
        except (AttributeError, TypeError):  # pragma: no cover - exotic exception type
            return


def run_live_suite(
    *,
    db: Database,
    settings: Settings,
    scenario_ids: list[str] | None = None,
    provider: ReasoningProvider | None = None,
    progress: LiveProgress | None = None,
    evidence_path: Path | str | None = None,
    scenario_budget_seconds: float | None = None,
    suite_budget_seconds: float | None = None,
    poll_seconds: float | None = None,
) -> LiveRun:
    """Run the selected live plans, restoring the demo after each one.

    The failure policy is explicit, because the wrong one is expensive here:

    * a scenario whose fault premise never appeared is recorded as not measured and
      the suite continues -- an absent premise is scenario-specific;
    * a scenario that could not restore the baseline, or whose baseline was already
      contaminated, stops the suite, because every later scenario would be
      measured against a demo that is not in its starting state;
    * an unexpected exception is recorded and stops the suite, rather than
      repeating a broken run once per scenario;
    * Ctrl+C records what completed plus an explicit interrupted record for the
      active scenario and is re-raised, never converted into a result.

    The evidence document is rewritten after every event, so an interrupted run
    leaves a complete file that describes exactly how far it got.
    """
    scenario_budget_seconds = (
        LIVE_SCENARIO_BUDGET_SECONDS if scenario_budget_seconds is None else scenario_budget_seconds
    )
    suite_budget_seconds = (
        LIVE_SUITE_BUDGET_SECONDS if suite_budget_seconds is None else suite_budget_seconds
    )
    poll_seconds = FAULT_VISIBILITY_POLL_SECONDS if poll_seconds is None else poll_seconds
    selected = (
        list(scenario_ids)
        if scenario_ids
        else [sid for sid in sorted(LIVE_PLANS) if sid in SCENARIOS]
    )
    unknown = [sid for sid in selected if sid not in LIVE_PLANS or sid not in SCENARIOS]
    if unknown:
        raise ValueError(f"no live plan is defined for scenario(s) {unknown}")

    started_at = utc_stamp(utc_now())
    reporter = progress or LiveProgress()
    writer = LiveEvidenceWriter(
        evidence_path,
        selected=selected,
        started_at=started_at,
        revision=LiveEnvironment.revision,
    )
    results: list[LiveResult] = []
    records: list[dict[str, Any]] = []
    notes: list[str] = []
    pending = list(selected)
    suite_budget = _ScenarioBudget(suite_budget_seconds)
    run_status = {"value": RUNNING}

    def persist() -> None:
        try:
            writer.write(
                status=run_status["value"],
                records=records,
                notes=notes,
                active=reporter.active_snapshot(status=run_status["value"]),
                pending=pending,
            )
        except Exception as exc:
            # Losing the evidence file is bad. Leaving a fault armed because the
            # evidence file could not be refreshed is worse, so this is reported and
            # carried out on the run rather than raised from inside the lifecycle.
            reporter.fail(
                f"the evidence document could not be written: {type(exc).__name__}: {exc}"
            )

    def record(result: LiveResult) -> None:
        results.append(result)
        records.append(result.to_record())
        if result.scenario_id in pending:
            pending.remove(result.scenario_id)

    # Every progress line also refreshes the evidence document, so the file on disk
    # can never lag the log by more than one event.
    reporter.listen(persist)

    baseline_error: LiveRunAborted | None = None
    reporter.enter_stage(
        "baseline", message=f"live suite start: {len(selected)} scenario(s) selected"
    )
    try:
        baseline_environment = LiveEnvironment(settings=settings)
        residual = detect_residual_faults(
            baseline_environment, timeout_seconds=FAULT_WRITE_TIMEOUT_SECONDS
        )
        if residual:
            reporter.observation(f"residual scenario faults at startup: {residual}; resetting")
            reset_fault_flags(baseline_environment, timeout_seconds=FAULT_WRITE_TIMEOUT_SECONDS)
            after = detect_residual_faults(
                baseline_environment, timeout_seconds=FAULT_WRITE_TIMEOUT_SECONDS
            )
            notes.append(
                f"residual scenario faults at startup: {residual}; "
                f"after the repair: {after or 'none'}"
            )
            if after:
                raise LiveBaselineUnavailable(
                    f"residual scenario faults could not be cleared at startup: {after}",
                    stage="baseline",
                )
            reporter.observation("baseline restored before the first scenario")
        else:
            clean = "baseline clean at startup: every scenario fault flag is at its safe variant"
            notes.append(clean)
            reporter.observation(clean)
    except LiveEnvironmentUnavailable as exc:
        baseline_error = LiveBaselineUnavailable(
            f"the demo stack did not answer the baseline check: {exc}", stage="baseline"
        )
    except LiveRunAborted as exc:
        baseline_error = exc
    except KeyboardInterrupt:
        # Interrupted before any scenario started: there is no active scenario to
        # record, but the document must not be left saying RUNNING either, because
        # a reader cannot tell "still going" from "the process is gone".
        run_status["value"] = INTERRUPTED
        notes.append(
            "the run was interrupted during the startup baseline check, before any "
            "scenario started; the demo was left as that check found it"
        )
        reporter.notice("interrupted during the startup baseline check")
        persist()
        raise
    except Exception as exc:
        # Same reasoning for an unexpected failure here: the run is over, and the
        # document has to say so rather than stay at RUNNING.
        run_status["value"] = STOPPED
        notes.append(
            f"the startup baseline check raised {type(exc).__name__}: {exc}; "
            "no scenario was started"
        )
        reporter.notice(f"startup baseline check failed: {type(exc).__name__}: {exc}")
        persist()
        raise

    if baseline_error is not None:
        notes.append(str(baseline_error))
        for scenario_id in selected:
            record(
                _unmeasured_result(
                    scenario_id=scenario_id,
                    reason=f"NOT MEASURED: {baseline_error}",
                    stage="baseline",
                    cleanup=getattr(baseline_error, "cleanup", None),
                )
            )
        run_status["value"] = STOPPED
        reporter.clear()
        persist()
        return LiveRun(
            selected=selected,
            results=results,
            status=STOPPED,
            started_at=started_at,
            notes=notes,
            evidence_path=writer.path,
            evidence_errors=list(reporter.errors),
        )

    stopped_reason: str | None = None
    try:
        for position, scenario_id in enumerate(selected, start=1):
            spec = SCENARIOS[scenario_id]
            plan = LIVE_PLANS[scenario_id]
            if suite_budget.remaining <= 0:
                stopped_reason = (
                    f"the suite budget of {suite_budget_seconds:.0f}s ran out before {scenario_id}"
                )
                reporter.notice(f"{stopped_reason}; later scenarios were not started")
                break

            reporter.begin_scenario(scenario_id, position, len(selected), setup=plan.setup)
            # P1-05 is the rejected-approval variation; the rest use the fixture's decision.
            approve = False if scenario_id == "P1-05" else None
            try:
                result = run_live_scenario(
                    spec,
                    plan,
                    db=db,
                    settings=settings,
                    provider=provider,
                    approve=approve,
                    progress=reporter,
                    budget_seconds=min(scenario_budget_seconds, suite_budget.remaining),
                    poll_seconds=poll_seconds,
                )
            except LiveCleanupFailed as exc:
                stopped_reason = (
                    f"the demo could not be restored to its baseline after {scenario_id}: {exc}"
                )
                if exc.result is not None:
                    # The scenario's own measurement stands; only the demo's state
                    # after it is in question, and that stops the suite.
                    exc.result.reasons.append(f"run stopped: {stopped_reason}")
                    record(exc.result)
                else:
                    record(
                        _unmeasured_result(
                            scenario_id=scenario_id,
                            reason=(
                                f"NOT MEASURED: cleanup failed after the {exc.stage} stage: {exc}"
                                + _cause_suffix(exc)
                            ),
                            stage=exc.stage,
                            cleanup=exc.cleanup,
                        )
                    )
                reporter.notice(f"{stopped_reason}; later scenarios were not started")
                if isinstance(exc.__cause__, KeyboardInterrupt):
                    # Ctrl+C interrupted a scenario whose cleanup also failed. Both
                    # survive: the record above keeps the cleanup failure, and the
                    # interrupt is re-raised so the run ends as an interrupt (exit
                    # 130) instead of as an ordinary stopped run.
                    run_status["value"] = INTERRUPTED
                    notes.append(
                        f"{stopped_reason}; the run had been interrupted, so it ended as an "
                        "interrupt with a cleanup failure rather than a stopped run"
                    )
                    reporter.notice("recording the interrupt together with the cleanup failure")
                    persist()
                    # ``from`` keeps the cleanup failure visible as the reason the
                    # interrupt was recorded this way.
                    raise exc.__cause__ from exc
                break
            except LiveBaselineUnavailable as exc:
                record(
                    _unmeasured_result(
                        scenario_id=scenario_id,
                        reason=f"NOT MEASURED: {exc}",
                        stage=exc.stage,
                        cleanup=exc.cleanup,
                        observation=exc.observation,
                    )
                )
                stopped_reason = (
                    f"a safe baseline could not be established before {scenario_id}: {exc}"
                )
                reporter.notice(f"{stopped_reason}; later scenarios were not started")
                break
            except LiveSetupFailed as exc:
                record(
                    _unmeasured_result(
                        scenario_id=scenario_id,
                        reason=f"NOT MEASURED at the {exc.stage} stage: {exc}",
                        stage=exc.stage,
                        cleanup=exc.cleanup,
                        observation=exc.observation,
                    )
                )
                reporter.notice(f"{scenario_id} was not measured: {exc}")
                persist()
                continue
            except Exception as exc:
                # The stage, the last observation and the cleanup text ride out on
                # the exception itself, so an unexpected failure is recorded where it
                # happened instead of being labelled a workflow failure.
                stage = getattr(exc, "stage", "workflow")
                reason = f"NOT MEASURED at the {stage} stage: {type(exc).__name__}: {exc}"
                record(
                    _unmeasured_result(
                        scenario_id=scenario_id,
                        reason=reason,
                        stage=stage,
                        cleanup=getattr(exc, "cleanup", None),
                        observation=getattr(exc, "observation", None),
                    )
                )
                stopped_reason = f"{reason}, so later scenarios were not started"
                reporter.notice(stopped_reason)
                break

            record(result)
            reporter.finish_scenario(result.outcome)
            persist()
    except KeyboardInterrupt:
        run_status["value"] = INTERRUPTED
        notes.append(
            "the run was interrupted; completed scenarios are recorded and the active "
            "scenario is recorded as interrupted rather than as a result"
        )
        reporter.notice("interrupted: recording the active scenario in the evidence document")
        persist()
        raise

    if stopped_reason is not None:
        notes.append(stopped_reason)
        for scenario_id in list(pending):
            record(
                _unmeasured_result(
                    scenario_id=scenario_id,
                    reason=(
                        f"NOT MEASURED: the suite stopped before this scenario ({stopped_reason})"
                    ),
                    stage="suite",
                )
            )
        run_status["value"] = STOPPED
    else:
        run_status["value"] = COMPLETED
    reporter.clear()
    persist()
    return LiveRun(
        selected=selected,
        results=results,
        status=run_status["value"],
        started_at=started_at,
        notes=notes,
        evidence_path=writer.path,
        evidence_errors=list(reporter.errors),
    )


__all__ = [
    "FAULT_VARIANTS",
    "FAULT_WRITE_TIMEOUT_SECONDS",
    "LIVE_HARD_STEP_CAP",
    "LIVE_PLANS",
    "LIVE_SCENARIO_BUDGET_SECONDS",
    "LIVE_SETTLE_ATTEMPTS",
    "LIVE_SETTLE_SECONDS",
    "LIVE_SOFT_STEP_BUDGET",
    "LIVE_SUITE_BUDGET_SECONDS",
    "LIVE_VERIFICATION_WINDOW_SECONDS",
    "FaultRestoration",
    "LiveBaselineUnavailable",
    "LiveCleanupFailed",
    "LivePlan",
    "LiveProgress",
    "LiveResult",
    "LiveRun",
    "LiveRunAborted",
    "LiveSetupFailed",
    "arm_fault",
    "build_live_environment",
    "detect_residual_faults",
    "live_settings",
    "preflight",
    "reset_fault_flags",
    "restore_owned_fault",
    "run_live_scenario",
    "run_live_suite",
    "wait_for_fault",
    "wait_for_traffic",
]
