"""The live P2-01 harness: the fault is cleared outside OpsAccord while a fix awaits approval.

It must clear the flag as an outside change (no ledger row), wait for the caller's path to
recover observably, and report honestly when it does not inside the bound.
"""

from __future__ import annotations

from typing import Any

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


# -- the payment-path preflight ----------------------------------------------------------


def _all_off(calls: float, share: float | None) -> FakeStack:
    stack = FakeStack(
        FakeFlagStore(),
        stack_health(**{"checkout-service": health(calls=calls, error_rate=0.0)}),
    )
    stack.dependency_error_share = share
    return stack


def test_a_failing_payment_path_with_every_flag_off_is_refused() -> None:
    """2026-10-18: checkout's flag cache kept paymentUnreachable "on" after the store read
    "off", so every Charge failed and five paths waited out their traffic budget."""
    result = live_scenarios.payment_path_preflight(_all_off(calls=0.2, share=1.0))  # type: ignore[arg-type]

    assert result["healthy"] is False
    assert "stale flag value" in result["detail"]


def test_a_succeeding_payment_path_passes() -> None:
    result = live_scenarios.payment_path_preflight(_all_off(calls=0.2, share=None))  # type: ignore[arg-type]
    assert result["healthy"] is True


def test_a_quiet_payment_path_cannot_be_judged() -> None:
    result = live_scenarios.payment_path_preflight(_all_off(calls=0.0, share=None))  # type: ignore[arg-type]
    assert result["healthy"] is None


def test_the_cli_refuses_a_stale_payment_path_with_the_recovery_command(
    monkeypatch: Any, capsys: Any
) -> None:
    from opsaccord import cli

    monkeypatch.setattr(
        cli,
        "preflight",
        lambda env, **kw: {
            "flag_count": 18,
            "missing_verified_flags": [],
            "prometheus_series": 300,
        },
    )
    monkeypatch.setattr(
        cli,
        "storefront_preflight",
        lambda env, **kw: {
            "healthy": True,
            "calls_per_minute": 30.0,
            "error_share": 0.0,
            "detail": "ok",
        },
    )
    monkeypatch.setattr(
        cli,
        "payment_path_preflight",
        lambda env, **kw: {
            "healthy": False,
            "detail": "checkout -> payment is failing with every fault flag off",
        },
    )
    monkeypatch.setattr(cli, "LiveEnvironment", lambda **kw: object())
    ran: list[str] = []
    monkeypatch.setattr(cli, "run_live_suite", lambda **kw: ran.append("ran"))

    assert cli.main(["run-live-scenarios"]) == 2
    out = capsys.readouterr().out
    assert "restart checkout" in out and ran == []


# -- a premise is a real rise, and Payment paths start from a clean edge -----------------


def test_floating_point_noise_is_not_a_rise() -> None:
    """The exact readings of live P2-01, 2026-10-18: P1-07's failures still in the window."""
    before = {"error_rate": 0.5714285714285712}
    after = {"error_rate": 0.5714285714285714}
    assert not live_scenarios._fault_is_new("error_rate", after, before)


def test_a_real_rise_is_a_rise() -> None:
    assert live_scenarios._fault_is_new("error_rate", {"error_rate": 0.9}, {"error_rate": 0.0})
    assert not live_scenarios._fault_is_new("error_rate", {"error_rate": 0.04}, {"error_rate": 0.0})
    assert live_scenarios._fault_is_new("cpu", {"cpu_utilization": 1.2}, {"cpu_utilization": 0.01})


def test_the_previous_paths_tail_is_waited_out_before_arming() -> None:
    stack = _all_off(calls=0.2, share=1.0)  # the edge still carries the last path's failures
    seen: list[str] = []

    def age_out(message: str) -> None:
        seen.append(message)
        stack.dependency_error_share = 0.0  # the tail ages out of the window

    result = live_scenarios.wait_for_clean_edge(
        stack,  # type: ignore[arg-type]
        caller="checkout-service",
        callee="payment-service",
        window_seconds=120,
        timeout_seconds=2.0,
        poll_seconds=0.01,
        observe=age_out,
    )
    assert seen and "age out" in seen[0]
    assert result["error_rate"] == 0.0


def test_a_tail_that_never_ages_out_refuses_to_arm() -> None:
    import pytest

    with pytest.raises(live_scenarios.LiveSetupFailed, match="could not be told apart"):
        live_scenarios.wait_for_clean_edge(
            _all_off(calls=0.2, share=1.0),  # type: ignore[arg-type]
            caller="checkout-service",
            callee="payment-service",
            window_seconds=120,
            timeout_seconds=0.05,
            poll_seconds=0.01,
        )
