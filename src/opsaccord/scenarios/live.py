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
"""

from __future__ import annotations

import time
from dataclasses import dataclass
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


class LiveSetupFailed(RuntimeError):
    """The demo never showed the fault a scenario needs, so the run proves nothing."""


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


LIVE_PLANS: dict[str, LivePlan] = {
    "P1-01": LivePlan(
        scenario_id="P1-01",
        setup="flagd-ui: paymentUnreachable=on",
        fault_flag="paymentUnreachable",
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
    ),
    "P1-06": LivePlan(
        scenario_id="P1-06",
        setup="flagd-ui: paymentUnreachable=on; flag control plane unroutable after approval",
        fault_flag="paymentUnreachable",
        induced_failure="control_plane_timeout",
    ),
    "P1-07": LivePlan(
        scenario_id="P1-07",
        setup="flagd-ui: paymentUnreachable=on; the fault is re-armed right after dispatch",
        fault_flag="paymentUnreachable",
        induced_failure="fault_rearmed_after_dispatch",
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
    environment: Environment, *, timeout_seconds: float = 5.0
) -> dict[str, str | None]:
    """Return every verified fault flag to its safe variant.

    Called before and after a live run so one scenario cannot inherit the fault of
    the previous one, and so the demo is left clean for the operator.
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


def arm_fault(
    environment: Environment, *, flag_id: str, variant: str, timeout_seconds: float = 5.0
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


def _as_live(environment: Environment) -> LiveEnvironment:
    if isinstance(environment, LiveEnvironment):
        return environment
    inner = getattr(environment, "_environment", None)
    if isinstance(inner, LiveEnvironment):
        return inner
    raise LiveEnvironmentUnavailable("live driver requires a LiveEnvironment execution surface")


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
    timeout_seconds: int = TRAFFIC_VISIBILITY_TIMEOUT_SECONDS,
    poll_seconds: int = FAULT_VISIBILITY_POLL_SECONDS,
) -> dict[str, Any]:
    """Block until the demo is actually exercising the target.

    A fault can only be observed on traffic that exists. Waiting here keeps a
    quiet load generator from being reported as a fault, and from being reported
    as a failed run.
    """
    deadline = time.monotonic() + timeout_seconds
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        health = environment.read_service_health(target=target, timeout_seconds=8.0)
        last = {
            "calls_per_second": health.value.get("calls_per_second"),
            "reachable": health.value.get("reachable"),
        }
        if health.succeeded and _as_float(health.value.get("calls_per_second")) > 0.0:
            return last
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


def wait_for_fault(
    environment: Environment,
    *,
    target: str,
    signal: str = "error_rate",
    timeout_seconds: int = FAULT_VISIBILITY_TIMEOUT_SECONDS,
    poll_seconds: int = FAULT_VISIBILITY_POLL_SECONDS,
) -> dict[str, Any]:
    """Block until the target's own health probe reports the armed fault.

    These probes are harness readiness checks, not evidence: they go straight to
    the adapter and are deliberately *not* recorded through the gateway. Evidence
    collection stays the controller's job, so a scenario cannot seed its own
    evidence by waiting.
    """
    deadline = time.monotonic() + timeout_seconds
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        health = environment.read_service_health(target=target, timeout_seconds=8.0)
        last = {
            "healthy": health.value.get("healthy"),
            "health": health.value.get("health"),
            "cpu_utilization": health.value.get("cpu_utilization"),
            "error_rate": health.value.get("error_rate"),
            "calls_per_second": health.value.get("calls_per_second"),
        }
        if health.succeeded and _fault_visible(signal, last):
            return last
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


def run_live_scenario(
    spec: ScenarioSpec,
    plan: LivePlan,
    *,
    db: Database,
    settings: Settings,
    provider: ReasoningProvider | None = None,
    approve: bool | None = None,
) -> LiveResult:
    """Arm the demo, run one scenario through the shared controller, score it."""
    environment = build_live_environment(plan, settings)
    fault_visible: dict[str, Any] | None = None
    if plan.fault_flag is not None:
        # Traffic first, then the fault: the demo has to be exercising the target
        # for the injected fault to be measurable at all.
        wait_for_traffic(environment, target=spec.service)
        arm_fault(environment, flag_id=plan.fault_flag, variant=FAULT_VARIANTS[plan.fault_flag])
        fault_visible = wait_for_fault(environment, target=spec.service, signal=plan.fault_signal)
    elif plan.induced_failure is None:
        reset_fault_flags(environment)

    trace = run_scenario(
        spec,
        db=db,
        settings=settings,
        provider=provider or DeterministicAdapter(),
        approve=approve,
        environment=environment,
    )
    outcome, reasons = evaluate(trace)

    # Leave the demo clean for the next scenario and for the operator, even when
    # the scenario itself failed: a stuck fault flag would silently poison the
    # baseline that P1-01 relies on.
    if plan.fault_flag is not None:
        try:
            reset_fault_flags(environment)
        except LiveEnvironmentUnavailable:
            reasons.append("demo fault flags could not be cleared after the run")

    reasons.extend(
        [
            f"verification settling: {settings.verification_settle_attempts} attempt(s) "
            f"x {settings.verification_settle_seconds}s",
            f"fault visible after arming: {fault_visible}" if fault_visible else "no fault armed",
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
    )


def run_live_suite(
    *,
    db: Database,
    settings: Settings,
    scenario_ids: list[str] | None = None,
    provider: ReasoningProvider | None = None,
) -> list[LiveResult]:
    selected = scenario_ids or [sid for sid in sorted(LIVE_PLANS) if sid in SCENARIOS]
    results: list[LiveResult] = []
    for scenario_id in selected:
        spec = SCENARIOS[scenario_id]
        plan = LIVE_PLANS[scenario_id]
        # P1-05 is the rejected-approval variation; the rest use the fixture's decision.
        approve = False if scenario_id == "P1-05" else None
        try:
            results.append(
                run_live_scenario(
                    spec, plan, db=db, settings=settings, provider=provider, approve=approve
                )
            )
        except LiveSetupFailed as exc:
            # A scenario whose premise never appeared is reported as not measured
            # rather than scored: calling it a failure would blame the system for
            # a demo that did not show the fault.
            results.append(
                LiveResult(
                    scenario_id=scenario_id,
                    title=spec.title,
                    setup=plan.setup,
                    outcome=ScenarioOutcome.INEFFECTIVE_FAILURE,
                    reasons=[f"NOT MEASURED: setup failed: {exc}"],
                    simulated=False,
                )
            )
    return results


__all__ = [
    "FAULT_VARIANTS",
    "LIVE_HARD_STEP_CAP",
    "LIVE_PLANS",
    "LIVE_SETTLE_ATTEMPTS",
    "LIVE_SETTLE_SECONDS",
    "LIVE_SOFT_STEP_BUDGET",
    "LIVE_VERIFICATION_WINDOW_SECONDS",
    "LivePlan",
    "LiveResult",
    "LiveSetupFailed",
    "arm_fault",
    "build_live_environment",
    "live_settings",
    "preflight",
    "reset_fault_flags",
    "run_live_scenario",
    "run_live_suite",
    "wait_for_fault",
]
