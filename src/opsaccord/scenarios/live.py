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
from opsaccord.reasoning.deterministic import CPU_SATURATION_CEILING, DeterministicAdapter
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
from opsaccord.scenarios.model_eval import decision_summary
from opsaccord.scenarios.runner import ScenarioTrace, evaluate, run_scenario
from opsaccord.scenarios.traffic import CheckoutDriver, describe, storefront_origin
from opsaccord.tools.contracts import (
    TELEMETRY_ERROR_BOUND,
    Environment,
    classify_dependency_edge,
    dependency_callers,
)
from opsaccord.tools.live_environment import (
    CALLS_METRIC_CANDIDATES,
    FLAG_SAFE_VARIANT,
    LIVE_HEALTH_WINDOW_SECONDS,
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
# P2-01: how long the harness waits, after clearing the fault behind a pending approval,
# for recovery to be observable on the caller's path. The verification window is 120 s,
# so the failures need that long to age out of it, plus export cadence.
EXTERNAL_RECOVERY_TIMEOUT_SECONDS = 300
FAULT_VISIBILITY_POLL_SECONDS = 20

# Window for the caller-side dependency reading the payment premise requires. The
# same span-metric floor as a health probe: the pipeline publishes every ~60s, so a
# shorter window reads as "no data" rather than as "no failure".
DEPENDENCY_PREMISE_WINDOW_SECONDS = LIVE_HEALTH_WINDOW_SECONDS

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
# Budgets for a live run. The intended payment path costs six investigation reads,
# one model decision, one dispatch, and up to six settle rounds of four reads each
# (health, two metrics, and the caller-side dependency read) -- 32 steps in the
# worst case. The hard cap sits above that sum on purpose: a cap with no headroom
# would turn a real recovery into a verification that ran out of budget, which is
# the failure the four reads exist to make visible. Both remain enforced.
LIVE_SOFT_STEP_BUDGET = 12
LIVE_HARD_STEP_CAP = 36


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
    # A second target whose own failing RPCs are recorded as context for this
    # fault.
    #
    # ``paymentUnreachable`` is read by checkout, so the demo degrades on checkout's
    # own RPCs. That is *context*: a failing parent span says the caller is unhappy,
    # not that the Payment path is why, and the controller is required to refuse
    # exactly that inference. When ``corroborating_dependency`` is set, a rising
    # reading here can not satisfy the premise.
    corroborating_target: str | None = None
    # The caller whose *own client call* to this scenario's service must also show
    # the fault. This is the observation the incident's own diagnostic surface acts
    # on: ``read_dependency`` attributes a failure to the edge only from the
    # caller's client spans, so a premise the controller cannot reproduce from the
    # same evidence is a premise this harness has no business accepting.
    #
    # The 2026-09-24 run is the measurement: the generic caller-side probe rose to
    # 17.7% while the caller's own Charge client share stayed at 0.0, and the
    # scenario went on to measure a fault its own controller could not attribute.
    corroborating_dependency: str | None = None
    # Whether the harness places its own bounded orders while this plan runs. The
    # Payment plans are measured on checkout's Charge client spans, which exist only
    # when an order reaches payment; see opsaccord.scenarios.traffic for why the load
    # generator alone cannot be relied on for that.
    drive_checkout: bool = False


#: Builds the synthetic traffic for one plan, or returns None for no driver. The CLI
#: passes :func:`checkout_driver_factory`; the default is no driver, so a test that
#: drives the lifecycle never sends a request to a real shop by accident.
CheckoutDriverFactory = Callable[["LivePlan"], "CheckoutDriver | None"]


LIVE_PLANS: dict[str, LivePlan] = {
    "P1-01": LivePlan(
        scenario_id="P1-01",
        setup="flagd-ui: paymentUnreachable=on",
        fault_flag="paymentUnreachable",
        corroborating_target="checkout-service",
        corroborating_dependency="checkout-service",
        drive_checkout=True,
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
        corroborating_dependency="checkout-service",
        drive_checkout=True,
    ),
    "P1-06": LivePlan(
        scenario_id="P1-06",
        setup="flagd-ui: paymentUnreachable=on; flag control plane unroutable after approval",
        fault_flag="paymentUnreachable",
        induced_failure="control_plane_timeout",
        corroborating_target="checkout-service",
        corroborating_dependency="checkout-service",
        drive_checkout=True,
    ),
    "P2-01": LivePlan(
        scenario_id="P2-01",
        setup=(
            "flagd-ui: paymentUnreachable=on; cleared by the harness while the approval is "
            "pending, then approved"
        ),
        fault_flag="paymentUnreachable",
        induced_failure="fault_cleared_while_pending",
        corroborating_target="checkout-service",
        corroborating_dependency="checkout-service",
        drive_checkout=True,
    ),
    "P1-07": LivePlan(
        scenario_id="P1-07",
        setup="flagd-ui: paymentUnreachable=on; the fault is re-armed right after dispatch",
        fault_flag="paymentUnreachable",
        induced_failure="fault_rearmed_after_dispatch",
        corroborating_target="checkout-service",
        corroborating_dependency="checkout-service",
        drive_checkout=True,
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


class ExternalRecoveryEnvironment:
    """Live surface on which the fault is fixed by someone else while a fix awaits approval.

    Harness for ``P2-01``. ``clear_fault_externally`` turns the fault flag off, as an
    operator outside OpsAccord would, and reads it back. It then waits, bounded, until
    the caller's own calls to the target are observably succeeding again over the
    verification window, so the approval that follows comes after a realistic delay.
    It is not an action: it writes no ledger row, and the incident must find out from
    fresh evidence that its approved fix is no longer needed.

    If recovery never becomes observable inside the bound, it returns anyway. The need
    check then cannot prove recovery, the approved fix runs, and the scenario fails on
    its own expectation, which is the honest outcome.
    """

    is_simulated = False

    def __init__(
        self,
        *,
        environment: LiveEnvironment,
        timeout_seconds: float = EXTERNAL_RECOVERY_TIMEOUT_SECONDS,
        poll_seconds: float = FAULT_VISIBILITY_POLL_SECONDS,
        progress: LiveProgress | None = None,
    ) -> None:
        self._environment = environment
        self._timeout_seconds = timeout_seconds
        self._poll_seconds = poll_seconds
        self._progress = progress
        self.cleared: dict[str, Any] | None = None

    @property
    def revision(self) -> str:
        return self._environment.revision

    @property
    def revision_source(self) -> str:
        return self._environment.revision_source

    def _say(self, message: str) -> None:
        if self._progress is not None:
            self._progress.observation(message)

    def clear_fault_externally(self, *, target: str, flag_id: str) -> None:
        self._environment.set_flag_variant(
            flag_id=flag_id,
            variant=FLAG_SAFE_VARIANT[flag_id],
            timeout_seconds=FAULT_WRITE_TIMEOUT_SECONDS,
        )
        self._say(f"external: {flag_id} cleared outside OpsAccord while the fix awaits approval")
        callers = dependency_callers(target)
        deadline = time.monotonic() + self._timeout_seconds
        verdict = None
        while callers and time.monotonic() < deadline:
            edge = self._environment.read_dependency(
                target=callers[0],
                dependency=target,
                window_seconds=LIVE_VERIFICATION_WINDOW_SECONDS,
                timeout_seconds=8.0,
            )
            verdict = classify_dependency_edge(edge.value) if edge.succeeded else None
            self._say(
                f"external: waiting for {callers[0]} -> {target} to recover: "
                + (verdict.detail if verdict is not None else f"read failed ({edge.error})")
            )
            if verdict is not None and verdict.healthy:
                break
            time.sleep(self._poll_seconds)
        self.cleared = {
            "flag_id": flag_id,
            "target": target,
            "recovered_before_decision": bool(verdict is not None and verdict.healthy),
        }

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


def _reading(value: Any) -> float | None:
    """One telemetry reading as a number, or ``None`` when it is not one.

    ``_as_float`` answers a question about arithmetic and has a default; this
    answers a question about evidence, where "not a number" and "absent" are the
    same answer and neither of them is a zero.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _signal_key(signal: str) -> str:
    return "cpu_utilization" if signal == "cpu" else "error_rate"


def _fault_is_new(signal: str, probe: dict[str, Any], baseline: dict[str, Any] | None) -> bool:
    """True when the probe shows a fault the pre-injection reading did not.

    A scenario's premise is a *transition*, not a level: the demo was not showing
    this fault before the write and shows it after. A level test cannot tell the
    two apart, and the 2026-09-24 run is the measurement of exactly that failure -
    the observation the harness accepted was collected 0.0s after the write and
    read a 100% error share that the pre-injection window already carried, so the
    scenario "proved" its premise with telemetry the write had not caused, and the
    controller was then handed a fault it could not attribute.

    Missing evidence is never a zero here. Without a pre-injection reading there is
    no rise to observe, so the observation is not accepted rather than accepted on
    the strength of an assumed baseline of zero - which is also the hole this
    closes in the corroborating branch, where an absent baseline used to read as
    ``0.0`` and made every caller-side failure a rise.
    """
    if baseline is None:
        return False
    key = _signal_key(signal)
    current = _reading(probe.get(key))
    previous = _reading(baseline.get(key))
    if current is None or previous is None:
        return False
    return current > previous


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
            # The window the reading averages over. It is part of the claim: a
            # reading this harness accepts averages a minute or two, so a reader
            # of the record can see how far back the evidence reaches.
            "window_seconds",
            # Which service the reading is about, and the selector scope that
            # makes it about that service. An operation name is emitted by both
            # sides of an RPC, so a health verdict without this is a verdict
            # about a set of services.
            "service",
            "service_scope",
        )
    }


def _dependency_snapshot(value: dict[str, Any]) -> dict[str, Any]:
    """The fields a caller-side dependency read reports, copied out of the value.

    ``attributable`` and ``reason`` travel with the numbers on purpose: a share
    that could not be attributed to the caller's own spans is not a reading about
    the edge, and the record has to say which of the two it holds.
    """
    return {
        key: value.get(key)
        for key in (
            "caller",
            "destination",
            "operation",
            "span_kind",
            "attributable",
            "reason",
            "calls_per_second",
            "errors_per_second",
            "error_rate",
            "window_seconds",
        )
    }


def _dependency_fault_visible(probe: dict[str, Any]) -> bool:
    """True when the caller's own client spans show a positive failure share.

    The same discipline as every other live predicate here: an unattributable
    read, a window with no calls, and a missing share are all *not* a fault. The
    share is compared against the telemetry bound rather than against zero, so a
    couple of stray errors do not arm a scenario.
    """
    if probe.get("attributable") is not True:
        return False
    calls = _reading(probe.get("calls_per_second"))
    rate = _reading(probe.get("error_rate"))
    if calls is None or calls <= 0 or rate is None:
        return False
    return rate > TELEMETRY_ERROR_BOUND


def wait_for_fault(
    environment: Environment,
    *,
    target: str,
    signal: str = "error_rate",
    target_before: dict[str, Any] | None,
    corroborating_target: str | None = None,
    corroborating_before: dict[str, Any] | None,
    corroborating_dependency: tuple[str, str] | None = None,
    dependency_before: dict[str, Any] | None = None,
    dependency_window_seconds: int = LIVE_HEALTH_WINDOW_SECONDS,
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
    makes intermittent. ``corroborating_target`` records that service's own failing
    RPCs, and the returned probe always says which series carried it.

    The observation that *satisfies* a premise whose fault is caller-side is
    ``corroborating_dependency``: the caller's own client call to this service, read
    through the same ``read_dependency`` attribution the incident's diagnosis uses.
    A generic parent-span rise is recorded and never sufficient, because a failing
    caller RPC is not evidence that this dependency is why -- the 2026-09-24 run
    accepted exactly that inference and handed the controller a fault it could not
    attribute from its own evidence.

    What does *not* change: an absent series is not a zero, and zero traffic is
    never read as a fault, on either target. The predicate is an error share
    strictly above the telemetry bound, so a quiet demo keeps waiting and the
    failure mode is an honest setup failure rather than a fabricated one.

    Every observation here is differential, and that is the whole premise.
    ``target_before``, ``corroborating_before`` and ``dependency_before`` carry each
    reading from *before* this scenario armed anything, and an observation only
    counts when it is above the bound **and** higher than its own pre-injection
    reading. A
    level test alone cannot tell an injected fault from a demo that was already
    failing: the 2026-09-24 run accepted payment's 100% error share 0.0s after the
    write, on a window that began two minutes before the write, so the scenario
    proved a fault it had not caused and could not have caused. Without a
    baseline there is no rise, so an unreadable baseline keeps the harness waiting
    and ends in an honest setup failure rather than in an invented baseline of
    zero.
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
        probe = _health_snapshot(health.value)
        last = {
            "target": target,
            "signal": signal,
            "attribution": "target",
            "probe": probe,
            "target_before": target_before,
            "corroborating": None,
            "corroborating_before": corroborating_before,
            "dependency": None,
            "dependency_before": dependency_before,
        }
        if (
            health.succeeded
            and _fault_visible(signal, probe)
            and _fault_is_new(signal, probe, target_before)
        ):
            last["elapsed_seconds"] = round(time.monotonic() - started, 1)
            return last
        if corroborating_target is not None:
            caller = environment.read_service_health(
                target=corroborating_target, timeout_seconds=8.0
            )
            caller_probe = _health_snapshot(caller.value)
            last["corroborating"] = {"target": corroborating_target, "probe": caller_probe}
            # Recorded always; sufficient only when this plan has no attributable
            # observation to require. A caller's generic failure is context.
            if (
                corroborating_dependency is None
                and caller.succeeded
                and _fault_visible("error_rate", caller_probe)
                and _fault_is_new("error_rate", caller_probe, corroborating_before)
            ):
                last["attribution"] = "caller"
                last["elapsed_seconds"] = round(time.monotonic() - started, 1)
                return last
        if corroborating_dependency is not None:
            dependency_caller, dependency_callee = corroborating_dependency
            edge = environment.read_dependency(
                target=dependency_caller,
                dependency=dependency_callee,
                window_seconds=dependency_window_seconds,
                timeout_seconds=8.0,
            )
            edge_probe = _dependency_snapshot(edge.value)
            last["dependency"] = {
                "caller": dependency_caller,
                "destination": dependency_callee,
                "probe": edge_probe,
            }
            if (
                edge.succeeded
                and _dependency_fault_visible(edge_probe)
                and _fault_is_new("error_rate", edge_probe, dependency_before)
            ):
                last["attribution"] = "dependency"
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

    if plan.induced_failure == "fault_cleared_while_pending":
        return ExternalRecoveryEnvironment(environment=environment)

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
    # The harness's own orders, if any were placed. Recorded on every result so a
    # reader knows the measured traffic was partly synthetic.
    synthetic_traffic: dict[str, Any] | None = None
    # What the reasoning provider concluded and proposed, and which layer refused it.
    model_decision: dict[str, Any] | None = None

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
            "synthetic_traffic": self.synthetic_traffic,
            "model_decision": self.model_decision,
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
    checkout_driver: CheckoutDriverFactory | None = None,
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
    if isinstance(environment, ExternalRecoveryEnvironment):
        # Its bounded wait for recovery is part of this scenario's progress.
        environment._progress = reporter
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
    driver: CheckoutDriver | None = None
    traffic: dict[str, Any] | None = None

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
            if checkout_driver is not None and plan.drive_checkout:
                driver = checkout_driver(plan)
                if driver is not None:
                    driver.start()
                    reporter.observation(
                        f"synthetic checkout traffic: placing an order through "
                        f"{driver.base_url} every {driver.interval_seconds}s "
                        f"(at most {driver.max_checkouts}) until the scenario body ends"
                    )
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
            # Both pre-injection readings, taken *before* the write and adjacent to
            # it. The premise below is a rise, so a reading taken after the write
            # would make the differential compare the fault with itself - which is
            # how an already-failing demo came to satisfy a scenario's premise.
            target_before = _health_baseline(environment, spec.service)
            corroborating_before = _health_baseline(environment, plan.corroborating_target)
            dependency_before = _dependency_baseline(
                environment,
                caller=plan.corroborating_dependency,
                callee=spec.service,
                window_seconds=DEPENDENCY_PREMISE_WINDOW_SECONDS,
            )
            reporter.observation(
                "pre-injection readings: "
                f"{spec.service}={target_before}"
                + (
                    f"; {plan.corroborating_target}={corroborating_before}"
                    if plan.corroborating_target
                    else ""
                )
                + (
                    f"; {plan.corroborating_dependency} -> {spec.service} client="
                    f"{dependency_before}"
                    if plan.corroborating_dependency
                    else ""
                )
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

            stage = "fault-visibility"
            visibility_budget = budget.clamp(FAULT_VISIBILITY_TIMEOUT_SECONDS)
            corroborating = f" or {plan.corroborating_target}" if plan.corroborating_target else ""
            required = (
                f", and attributably on the {plan.corroborating_dependency} -> {spec.service} "
                "client call"
                if plan.corroborating_dependency
                else ""
            )
            reporter.enter_stage(
                stage,
                budget_seconds=visibility_budget,
                message=(
                    f"waiting for the armed fault to appear on {spec.service}{corroborating}"
                    f"{required}"
                ),
            )
            fault_visible = wait_for_fault(
                environment,
                target=spec.service,
                signal=plan.fault_signal,
                target_before=target_before,
                corroborating_target=plan.corroborating_target,
                corroborating_before=corroborating_before,
                corroborating_dependency=(
                    (plan.corroborating_dependency, spec.service)
                    if plan.corroborating_dependency
                    else None
                ),
                dependency_before=dependency_before,
                dependency_window_seconds=DEPENDENCY_PREMISE_WINDOW_SECONDS,
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
    if driver is not None:
        # Stopped before cleanup, on every exit path: the orders exist to make the
        # premise and the verification observable, not to load a restored demo.
        traffic = driver.stop()
        reporter.observation(describe(traffic))
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
        if traffic is not None:
            result.reasons.append(describe(traffic))
            result.synthetic_traffic = traffic
        try:
            result.model_decision = decision_summary(db, trace.incident.id, trace.run_notes)
        except Exception as exc:
            # The decision enriches the evidence; it is not the measurement. A read
            # that fails is said so on the record instead of failing the scenario.
            result.reasons.append(
                f"model decision could not be read back: {type(exc).__name__}: {exc}"
            )
    elif traffic is not None:
        _attach_traffic(body_error, traffic)
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


def _health_baseline(environment: Environment, target: str | None) -> dict[str, Any] | None:
    """One target's health *before* a scenario arms anything.

    Read for both the fault target and the corroborating target, and read before
    the write: the visibility predicate is a rise, so a baseline taken afterwards
    would compare the fault with itself. A probe that does not answer is not a
    baseline of zero: it is no baseline, and the observation it would have
    qualified is then not accepted rather than accepted against an invented zero.
    """
    if target is None:
        return None
    probe = environment.read_service_health(target=target, timeout_seconds=8.0)
    if not probe.succeeded:
        return None
    return _health_snapshot(probe.value)


def _dependency_baseline(
    environment: Environment, *, caller: str | None, callee: str, window_seconds: int
) -> dict[str, Any] | None:
    """The caller's own view of the managed edge *before* a scenario arms anything.

    Read before the write for the same reason the health baselines are: the premise
    is a rise, so a baseline taken afterwards would compare the fault with itself.
    A read that does not answer is no baseline, and the observation it would have
    qualified is then not accepted rather than accepted against an invented zero.
    """
    if caller is None:
        return None
    read = environment.read_dependency(
        target=caller, dependency=callee, window_seconds=window_seconds, timeout_seconds=8.0
    )
    if not read.succeeded:
        return None
    return _dependency_snapshot(read.value)


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
    dependency = probe.get("dependency")
    if dependency:
        edge = dependency.get("probe") or {}
        text += (
            f"; {dependency.get('caller')} -> {dependency.get('destination')} client "
            f"error_rate={edge.get('error_rate')} calls_per_second="
            f"{edge.get('calls_per_second')} attributable={edge.get('attributable')}"
        )
    return text


def _fault_reason(fault_visible: dict[str, Any]) -> str:
    """One recorded line saying which series proved the fault, and what it rose from.

    The baseline is part of the claim, not decoration. A probe reading on its own
    cannot show that the fault is the one this scenario armed - that is the reading
    the 2026-09-24 run accepted - so the record carries both sides of the rise.
    """
    if fault_visible.get("attribution") == "dependency":
        dependency = fault_visible.get("dependency") or {}
        return (
            "fault visible after arming on the caller's own client spans for the managed "
            f"edge: {dependency.get('caller')} -> {dependency.get('destination')} client read "
            f"{dependency.get('probe')} above its pre-injection reading "
            f"{fault_visible.get('dependency_before')}; target probe "
            f"{fault_visible.get('probe')} against its pre-injection reading "
            f"{fault_visible.get('target_before')}; caller-side parent-span context "
            f"{fault_visible.get('corroborating')}"
        )
    if fault_visible.get("attribution") == "caller":
        corroborating = fault_visible.get("corroborating") or {}
        return (
            "fault visible after arming on the caller's own RPCs: "
            f"{corroborating.get('target')} probe {corroborating.get('probe')} above its "
            f"pre-injection reading {fault_visible.get('corroborating_before')}; "
            f"target probe {fault_visible.get('probe')} against its pre-injection reading "
            f"{fault_visible.get('target_before')}"
        )
    return (
        f"fault visible after arming: {fault_visible.get('probe')} above its "
        f"pre-injection reading {fault_visible.get('target_before')}"
    )


def _unmeasured_result(
    *,
    scenario_id: str,
    reason: str,
    stage: str,
    cleanup: str | None = None,
    observation: str | None = None,
    synthetic_traffic: dict[str, Any] | None = None,
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
    if synthetic_traffic is not None:
        reasons.append(describe(synthetic_traffic))
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
        synthetic_traffic=synthetic_traffic,
    )


def _attach_traffic(exc: BaseException, traffic: dict[str, Any]) -> None:
    """Carry the driver's summary out on a failure, like the lifecycle facts."""
    try:
        exc.synthetic_traffic = traffic  # type: ignore[attr-defined]
    except (AttributeError, TypeError):  # pragma: no cover - exotic exception type
        return


def checkout_driver_factory(settings: Settings) -> CheckoutDriverFactory:
    """The production factory: one driver per Payment plan, against the shop's origin."""
    origin = storefront_origin(settings)

    def build(plan: LivePlan) -> CheckoutDriver | None:
        return CheckoutDriver(origin) if plan.drive_checkout else None

    return build


#: Above this share of failing catalog calls, the storefront cannot place an order,
#: so a Payment path would be starved of the traffic its premise needs.
STOREFRONT_ERROR_SHARE_LIMIT = 0.5
STOREFRONT_WINDOW = "5m"


def _fetch_status(url: str, timeout_seconds: float) -> int:
    """One bounded GET; the HTTP status, or an exception on timeout or transport failure."""
    import httpx

    with httpx.Client(timeout=timeout_seconds) as client:
        return client.get(url).status_code


def storefront_preflight(
    environment: Environment,
    *,
    timeout_seconds: float = 8.0,
    storefront_url: str | None = None,
    fetch: Callable[[str, float], int] | None = None,
) -> dict[str, Any]:
    """Decide, read-only, whether the storefront can reach ``product-catalog``.

    Two readings, because the catalog has failed in two ways:

    * **Failing** (the 2026-10-17 5/7 run): catalog calls completed with errors. The
      frontend's catalog client spans show it as an error share.
    * **Hung** (the 2026-10-17 live-model run): catalog calls never completed, so they
      produced *no* spans at all. The span reading saw "no calls" and could not judge,
      and a 35-minute run started against a storefront whose every order timed out.

    So, when ``storefront_url`` is given, the shop's own ``GET /api/products`` is called
    once with a bounded timeout. A timeout, a transport failure or an error status means
    the storefront cannot serve the path every order takes, and that is a refusal
    whatever the spans say. ``healthy`` is None only when neither reading can judge.
    """
    probe: dict[str, Any] | None = None
    if storefront_url is not None:
        url = storefront_url.rstrip("/") + "/api/products"
        try:
            status = (fetch or _fetch_status)(url, timeout_seconds)
            probe = {"url": url, "status": status, "ok": status < 400}
        except Exception as exc:
            probe = {"url": url, "status": None, "ok": False, "error": type(exc).__name__}
        if not probe["ok"]:
            reason = (
                f"HTTP {probe['status']}"
                if probe["status"] is not None
                else f"{probe['error']} within {timeout_seconds:.0f}s"
            )
            return {
                "healthy": False,
                "calls_per_minute": None,
                "error_share": None,
                "probe": probe,
                "detail": f"GET /api/products failed ({reason}): the catalog is hung or down",
            }

    live = _as_live(environment)
    metric = live.prometheus.resolve_metric(CALLS_METRIC_CANDIDATES, timeout_seconds)
    calls = errors = None
    if metric is not None:
        selector = "service_name='frontend',span_name=~'oteldemo.ProductCatalogService/.*'"
        window = STOREFRONT_WINDOW
        calls = live.prometheus.scalar(
            f"sum(rate({metric}{{{selector}}}[{window}]))*60", timeout_seconds
        )
        errors = live.prometheus.scalar(
            f"sum(rate({metric}{{{selector},status_code='STATUS_CODE_ERROR'}}[{window}]))*60",
            timeout_seconds,
        )
    if not calls:
        answered = probe is not None and probe["ok"]
        return {
            "healthy": True if answered else None,
            "calls_per_minute": calls,
            "error_share": None,
            "probe": probe,
            "detail": (
                f"no frontend -> product-catalog spans in the last {STOREFRONT_WINDOW}"
                + ("; GET /api/products answered" if answered else "")
            ),
        }
    share = (errors or 0.0) / calls
    return {
        "healthy": share < STOREFRONT_ERROR_SHARE_LIMIT,
        "calls_per_minute": round(calls, 2),
        "error_share": round(share, 3),
        "probe": probe,
        "detail": (
            f"frontend -> product-catalog: {calls:.1f} calls/min, "
            f"{share:.1%} failing over the last {STOREFRONT_WINDOW}"
            + ("; GET /api/products answered" if probe else "")
        ),
    }


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
    checkout_driver: CheckoutDriverFactory | None = None,
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
                    checkout_driver=checkout_driver,
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
                            synthetic_traffic=getattr(exc, "synthetic_traffic", None),
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
                        synthetic_traffic=getattr(exc, "synthetic_traffic", None),
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
                        synthetic_traffic=getattr(exc, "synthetic_traffic", None),
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
                        synthetic_traffic=getattr(exc, "synthetic_traffic", None),
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
    "checkout_driver_factory",
    "detect_residual_faults",
    "live_settings",
    "preflight",
    "reset_fault_flags",
    "restore_owned_fault",
    "run_live_scenario",
    "run_live_suite",
    "storefront_preflight",
    "wait_for_fault",
    "wait_for_traffic",
]
