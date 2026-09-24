"""Contract tests for the live run plan and its calibrated budgets.

A live scenario has to stay inside the workflow's step budget while still being
allowed to settle a real telemetry window. These tests pin that arithmetic and
the published calibration values, so a later change cannot quietly make the run
impossible (or unbounded).
"""

from __future__ import annotations

import pytest

from opsaccord.scenarios import live as live_scenarios
from opsaccord.workflow.controller import READ_PLAN

TARGET_SERVICES = ("payment-service", "ad-service", "cart-service")


def test_every_live_plan_names_a_verified_flag_or_an_induced_failure() -> None:
    for scenario_id, plan in live_scenarios.LIVE_PLANS.items():
        assert plan.fault_flag or plan.induced_failure, scenario_id
        if plan.fault_flag:
            assert plan.fault_flag in live_scenarios.FAULT_VARIANTS, scenario_id


def test_fault_signal_is_a_known_predicate() -> None:
    for plan in live_scenarios.LIVE_PLANS.values():
        assert plan.fault_signal in {"error_rate", "cpu"}


def test_settling_fits_inside_the_live_hard_cap() -> None:
    """Investigation + dispatch + settle rounds must fit the hard step cap.

    Each settle round costs four reads: health, two metrics, and the caller-side
    dependency read that a callee-scoped probe cannot replace. The cap must sit
    above the whole sum with headroom, because verification that runs out of
    budget reads as inconclusive and would turn a real recovery into a handoff.
    """
    worst_investigation = max(len(plan) for plan in READ_PLAN.values())
    verification_reads = live_scenarios.LIVE_SETTLE_ATTEMPTS * 4
    model_decision = 1
    dispatch = 1
    worst_case = worst_investigation + model_decision + dispatch + verification_reads
    assert worst_case <= live_scenarios.LIVE_HARD_STEP_CAP
    assert worst_case + 4 <= live_scenarios.LIVE_HARD_STEP_CAP


def test_live_settings_publish_the_calibrated_values() -> None:
    settings = live_scenarios.live_settings("test")

    assert settings.demo_mode == "live"
    assert settings.verification_settle_attempts == live_scenarios.LIVE_SETTLE_ATTEMPTS
    assert settings.verification_settle_seconds == live_scenarios.LIVE_SETTLE_SECONDS
    assert settings.verification_window_seconds == live_scenarios.LIVE_VERIFICATION_WINDOW_SECONDS
    assert settings.hard_step_cap == live_scenarios.LIVE_HARD_STEP_CAP
    assert settings.soft_step_budget == live_scenarios.LIVE_SOFT_STEP_BUDGET
    assert settings.test_database_url.endswith("opsaccord_test")


def test_live_verification_window_can_carry_span_metric_samples() -> None:
    """The span-metrics pipeline exports every ~60s; shorter windows read empty."""
    assert live_scenarios.LIVE_VERIFICATION_WINDOW_SECONDS >= 120


def test_fault_visibility_never_fires_on_a_quiet_demo() -> None:
    quiet = {"healthy": False, "error_rate": None, "cpu_utilization": 0.01}
    assert live_scenarios._fault_visible("error_rate", quiet) is False
    assert live_scenarios._fault_visible("cpu", quiet) is False

    failing = {"healthy": False, "error_rate": 0.33, "cpu_utilization": 0.1}
    assert live_scenarios._fault_visible("error_rate", failing) is True

    burning = {"healthy": False, "error_rate": 0.0, "cpu_utilization": 1.06}
    assert live_scenarios._fault_visible("cpu", burning) is True


def test_wait_for_traffic_refuses_a_silent_demo(monkeypatch: pytest.MonkeyPatch) -> None:
    """A target with no traffic is a setup failure, not a measured fault."""

    class Result:
        def __init__(self) -> None:
            self.succeeded = True
            self.value: dict[str, object] = {"calls_per_second": 0.0, "reachable": False}

    class QuietEnvironment:
        def read_service_health(self, *, target: str, timeout_seconds: float) -> Result:
            return Result()

    with pytest.raises(live_scenarios.LiveSetupFailed):
        live_scenarios.wait_for_traffic(
            QuietEnvironment(), target="payment-service", timeout_seconds=0, poll_seconds=0
        )
