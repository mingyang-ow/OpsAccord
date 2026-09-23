"""Live integration tests against the real pinned Astronomy Shop stack.

These are opt-in. They are skipped unless ``OPSACCORD_LIVE_DEMO=1`` and the demo
stack actually answers, so the default suite stays offline and fast. Run them
with the endpoints file that ``scripts/demo.sh endpoints`` writes:

    scripts/demo.sh start
    scripts/demo.sh endpoints
    set -a; . .local/demo-endpoints.env; set +a
    OPSACCORD_LIVE_DEMO=1 uv run pytest -q -m live_demo

What they cover that the mocked unit tests cannot: the adapter's PromQL/span
names are the ones the pinned collector really exports, the flag document really
is 18 flags written read-modify-write, and the controller really reaches RESOLVED
on live evidence rather than on simulated numbers.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator

import pytest

from opsaccord.config import Settings
from opsaccord.reasoning.deterministic import DeterministicAdapter
from opsaccord.scenarios import live as live_scenarios
from opsaccord.scenarios.fixtures import SCENARIOS
from opsaccord.tools.live_environment import FLAG_SAFE_VARIANT, LiveEnvironment

# The whole module is opt-in. Nothing here may run as part of the default suite:
# these tests write real feature flags and wait on real telemetry.
pytestmark = [
    pytest.mark.live_demo,
    pytest.mark.skipif(
        os.environ.get("OPSACCORD_LIVE_DEMO") != "1",
        reason="set OPSACCORD_LIVE_DEMO=1 to run the live demo integration tests",
    ),
]


def _live_settings() -> Settings:
    """The same calibrated settings the CLI live run uses."""
    return live_scenarios.live_settings("test")


@pytest.fixture(scope="module")
def live_environment() -> Iterator[LiveEnvironment]:
    settings = _live_settings()
    environment = LiveEnvironment(settings=settings)
    try:
        environment.read_flag_document(timeout_seconds=5.0)
    except Exception as exc:
        pytest.skip(f"the live demo stack is not reachable: {exc}")
    try:
        yield environment
    finally:
        # Never leave an injected fault behind for the next run.
        for flag_id, safe_variant in FLAG_SAFE_VARIANT.items():
            with contextlib.suppress(Exception):
                environment.set_flag_variant(
                    flag_id=flag_id, variant=safe_variant, timeout_seconds=5.0
                )


@pytest.fixture
def live_settings() -> Settings:
    return _live_settings()


def test_live_preflight_finds_every_verified_flag(live_environment: LiveEnvironment) -> None:
    probe = live_scenarios.preflight(live_environment, timeout_seconds=8.0)

    assert probe["missing_verified_flags"] == []
    assert probe["flag_count"] >= len(FLAG_SAFE_VARIANT)
    assert probe["prometheus_series"] > 0


def test_live_reads_are_real_evidence_and_resolve_real_series(
    live_environment: LiveEnvironment,
) -> None:
    """The calibrated names must exist in the running stack, not just in a mock."""
    metrics = live_environment.read_metrics(
        target="payment-service", metric="error_rate", window_seconds=300, timeout_seconds=8.0
    )
    traces = live_environment.read_traces(
        target="payment-service", operation="Charge", window_seconds=300, timeout_seconds=8.0
    )
    health = live_environment.read_service_health(target="payment-service", timeout_seconds=8.0)

    for result in (metrics, traces, health):
        assert result.succeeded is True, result.error
        assert result.is_simulated is False

    assert "oteldemo.PaymentService/Charge" in str(metrics.value["queries"])
    assert traces.value["operation"] == "oteldemo.PaymentService/Charge"
    assert "source" in metrics.value and metrics.value["source"] == "prometheus"
    assert health.value["source"] == "prometheus"


def test_live_flag_write_touches_exactly_one_flag(live_environment: LiveEnvironment) -> None:
    """flagd-ui replaces the whole document, so the adapter must carry it through."""
    before = live_environment.read_flag_document(timeout_seconds=8.0)["flags"]
    try:
        live_environment.set_flag_variant(flag_id="adFailure", variant="on", timeout_seconds=8.0)
        after = live_environment.read_flag_document(timeout_seconds=8.0)["flags"]
        assert after["adFailure"]["defaultVariant"] == "on"
        changed = {
            key
            for key in before
            if before[key].get("defaultVariant") != after[key].get("defaultVariant")
        }
        assert changed == {"adFailure"}
    finally:
        live_environment.set_flag_variant(flag_id="adFailure", variant="off", timeout_seconds=8.0)


def test_live_payment_path_reaches_resolved_on_live_evidence(db, live_settings: Settings) -> None:
    """P1-01 end to end: real flag fault, real approval, real verification."""
    spec = SCENARIOS["P1-01"]
    plan = live_scenarios.LIVE_PLANS["P1-01"]
    result = live_scenarios.run_live_scenario(
        spec, plan, db=db, settings=live_settings, provider=DeterministicAdapter()
    )

    trace = result.trace
    assert trace is not None, result.reasons
    assert result.outcome.value == "PASS", result.reasons
    assert str(trace.incident.status) == "RESOLVED"
    assert len(trace.executed_actions) == 1
    assert trace.duplicate_side_effects == 0
    assert trace.forbidden_actions == []
    assert trace.resolved_without_verification is False
    assert [str(v.outcome) for v in trace.verifications] == ["VERIFICATION_SUCCESS"]
    # The evidence really came from the live stack.
    assert trace.evidence and all(item.is_simulated is False for item in trace.evidence)


def test_live_cart_path_hands_off_without_executing_anything(db, live_settings: Settings) -> None:
    """P1-03 live: cart has no allow-listed remediation, so it must hand off."""
    spec = SCENARIOS["P1-03"]
    plan = live_scenarios.LIVE_PLANS["P1-03"]
    result = live_scenarios.run_live_scenario(
        spec, plan, db=db, settings=live_settings, provider=DeterministicAdapter()
    )

    trace = result.trace
    assert trace is not None, result.reasons
    assert result.outcome.value == "PASS", result.reasons
    assert str(trace.incident.status) == "READY_FOR_HUMAN"
    assert trace.executed_actions == []
    assert trace.forbidden_actions == []


def test_live_metrics_timeout_escalates_without_guessing(db, live_settings: Settings) -> None:
    """P1-04 live: the telemetry endpoints genuinely do not answer."""
    spec = SCENARIOS["P1-04"]
    plan = live_scenarios.LIVE_PLANS["P1-04"]
    result = live_scenarios.run_live_scenario(
        spec, plan, db=db, settings=live_settings, provider=DeterministicAdapter()
    )

    trace = result.trace
    assert trace is not None, result.reasons
    assert str(trace.incident.status) in {"ESCALATED_INCOMPLETE", "READY_FOR_HUMAN"}
    assert trace.executed_actions == []
    assert any(item.kind.value == "COLLECTION_ERROR" for item in trace.evidence)
