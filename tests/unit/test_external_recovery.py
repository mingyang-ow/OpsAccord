"""The live P2-01 harness: the fault is cleared outside OpsAccord while a fix awaits approval.

It must clear the flag as an outside change (no ledger row), wait for the caller's path to
recover observably, and report honestly when it does not inside the bound.
"""

from __future__ import annotations

from opsaccord.scenarios import live as live_scenarios
from support.live_fakes import FakeFlagStore, FakeStack, health, live_settings, stack_health


def _payment_down() -> FakeStack:
    store = FakeFlagStore({"paymentUnreachable": "on"})
    probes = stack_health(
        **{
            "payment-service": health(calls=0.2, error_rate=0.0),
            "checkout-service": health(calls=0.2, error_rate=0.5),
        }
    )
    return FakeStack(store, probes)


def test_clearing_turns_the_flag_off_and_waits_for_the_callers_path_to_recover() -> None:
    stack = _payment_down()
    env = live_scenarios.ExternalRecoveryEnvironment(
        environment=stack,  # type: ignore[arg-type]
        timeout_seconds=2.0,
        poll_seconds=0.01,
    )

    env.clear_fault_externally(target="payment-service", flag_id="paymentUnreachable")

    assert stack.store.variants["paymentUnreachable"] == "off"
    assert env.cleared == {
        "flag_id": "paymentUnreachable",
        "target": "payment-service",
        "recovered_before_decision": True,
    }
    assert ("checkout-service", "payment-service") in stack.dependency_reads


def test_a_path_that_never_recovers_is_reported_and_not_waited_on_forever() -> None:
    stack = _payment_down()
    stack.dependency_error_share = 1.0  # the caller keeps failing even with the flag off
    env = live_scenarios.ExternalRecoveryEnvironment(
        environment=stack,  # type: ignore[arg-type]
        timeout_seconds=0.05,
        poll_seconds=0.01,
    )

    env.clear_fault_externally(target="payment-service", flag_id="paymentUnreachable")

    assert env.cleared is not None and env.cleared["recovered_before_decision"] is False


def test_the_p2_01_plan_gets_the_external_recovery_surface() -> None:
    plan = live_scenarios.LIVE_PLANS["P2-01"]
    built = live_scenarios.build_live_environment(plan, live_settings())

    assert isinstance(built, live_scenarios.ExternalRecoveryEnvironment)
    assert plan.fault_flag == "paymentUnreachable" and plan.drive_checkout
