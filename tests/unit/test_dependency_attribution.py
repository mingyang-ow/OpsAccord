"""The dependency-attribution contract, offline.

A caller can fail to reach a callee without the callee emitting any error span at
all: it simply receives nothing. Server-scoped telemetry is therefore blind to
this class of failure, and the caller's own client-side observation of the edge is
the only evidence that can attribute it. These tests pin that contract where it
can be checked without a stack, a socket or a database:

* the classifier's five answers, including the two that are explicitly *not*
  "healthy" (unattributable and zero-traffic);
* the simulated edge surface that the acceptance scenarios and the offline
  controller run against, and its refusal to answer for an unverified edge;
* the typed registry that decides which caller may be asked about which callee.

The values are representative payloads with nothing sensitive in them. They are
fixtures, not live evidence: a live run records ``is_simulated=False`` and the
real span-metric numbers.
"""

from __future__ import annotations

import pytest

from opsaccord.domain.enums import SideEffect
from opsaccord.tools import demo_environment as demo
from opsaccord.tools.contracts import (
    DEPENDENCY_EDGES,
    TELEMETRY_ERROR_BOUND,
    classify_dependency_edge,
    dependency_callees,
    dependency_callers,
    dependency_operation,
)
from opsaccord.tools.registry import ToolRegistry

EDGE = {
    "caller": "checkout-service",
    "destination": "payment-service",
    "operation": "Charge",
    "span_kind": "client",
    "attributable": True,
    "calls_per_second": 0.2,
    "error_rate": 1.0,
    "window_seconds": 300,
}


def test_the_edge_list_is_allow_listed_and_bidirectional() -> None:
    assert DEPENDENCY_EDGES == (("checkout-service", "payment-service", "Charge"),)
    assert dependency_callees("checkout-service") == ("payment-service",)
    assert dependency_callees("payment-service") == ()
    assert dependency_callers("payment-service") == ("checkout-service",)
    assert dependency_callers("checkout-service") == ()
    assert dependency_operation("checkout-service", "payment-service") == "Charge"
    assert dependency_operation("payment-service", "checkout-service") is None
    assert dependency_operation("checkout-service", "ad-service") is None


def test_a_failing_edge_is_failing() -> None:
    verdict = classify_dependency_edge(EDGE)
    assert verdict.state == "failing"
    assert verdict.failing and not verdict.healthy and not verdict.indeterminate
    assert "checkout-service -> payment-service" in verdict.detail
    assert "100.00%" in verdict.detail


def test_an_edge_below_the_bound_is_healthy() -> None:
    verdict = classify_dependency_edge({**EDGE, "error_rate": TELEMETRY_ERROR_BOUND - 0.01})
    assert verdict.state == "healthy"
    assert verdict.healthy and not verdict.failing


def test_the_bound_itself_counts_as_failing() -> None:
    """The classifier and the live health probe must agree on one bound."""
    assert classify_dependency_edge({**EDGE, "error_rate": TELEMETRY_ERROR_BOUND}).failing


def test_an_unattributable_edge_is_indeterminate_not_healthy() -> None:
    verdict = classify_dependency_edge(
        {**EDGE, "attributable": False, "reason": "no client-side series is exported"}
    )
    assert verdict.indeterminate
    assert "no client-side series is exported" in verdict.detail
    assert not verdict.healthy


def test_no_traffic_proves_neither_failure_nor_recovery() -> None:
    for value in (
        {**EDGE, "calls_per_second": 0.0, "error_rate": None},
        {**EDGE, "calls_per_second": 0.0, "error_rate": 0.0},
        {**EDGE, "calls_per_second": -1.0},
    ):
        verdict = classify_dependency_edge(value)
        assert verdict.indeterminate, value
        assert "no calls" in verdict.detail


def test_a_missing_or_malformed_share_is_indeterminate() -> None:
    for value in (
        {**EDGE, "error_rate": None},
        {**EDGE, "error_rate": "not-a-number"},
        {**EDGE, "error_rate": True},
        {**EDGE, "calls_per_second": "many", "error_rate": 0.5},
        {**EDGE, "window_seconds": 0},
        {**EDGE, "window_seconds": "five minutes"},
    ):
        assert classify_dependency_edge(value).indeterminate, value


def test_an_undeclared_window_is_not_by_itself_malformed() -> None:
    """The window is metadata; the attribution comes from the pair and the share.

    A read that does not declare a window is still usable, so this is deliberately
    lenient. A read that declares an *unusable* window is not: it claims a window
    and then names something no observation could have been taken in.
    """
    assert classify_dependency_edge({**EDGE, "window_seconds": None}).state == "failing"
    assert (
        classify_dependency_edge({k: v for k, v in EDGE.items() if k != "window_seconds"}).state
        == "failing"
    )


def test_a_numeric_string_share_is_read_as_a_number() -> None:
    """Prometheus answers JSON numbers, but a string form is not a failure."""
    assert classify_dependency_edge({**EDGE, "error_rate": "0.5"}).failing
    assert classify_dependency_edge({**EDGE, "calls_per_second": "0.4"}).state == "failing"


# --- the simulated surface used by the offline controller run -----------------


def unreachable_payment_edge() -> demo.DemoEnvironment:
    return demo.DemoEnvironment(
        services={
            "checkout-service": demo.ServiceState(),
            "payment-service": demo.ServiceState(
                reachable=False, health="NOT_SERVING", error_rate=1.0, request_rate=0.0
            ),
        }
    )


def test_the_simulated_edge_reports_the_callers_view_of_a_down_callee() -> None:
    """A down callee is still *attempted*: the caller's calls exist and fail.

    Zero calls is what an idle window looks like, so the fixture has to keep the
    two apart; otherwise "the callee cannot be reached" would be indistinguishable
    from "nobody called" and the tests could not tell them apart either.
    """
    environment = unreachable_payment_edge()
    result = environment.read_dependency(
        target="checkout-service",
        dependency="payment-service",
        window_seconds=300,
        timeout_seconds=1.0,
    )
    assert result.succeeded and result.is_simulated
    value = result.value
    assert value["attributable"] is True
    assert value["calls_per_second"] > 0.0
    assert value["error_rate"] == 1.0
    assert value["caller"] == "checkout-service"
    assert value["destination"] == "payment-service"
    assert value["span_kind"] == "client"
    assert classify_dependency_edge(value).failing


def test_the_simulated_edge_is_healthy_once_the_callee_recovers() -> None:
    environment = unreachable_payment_edge()
    environment.services["payment-service"] = demo.ServiceState()
    result = environment.read_dependency(
        target="checkout-service",
        dependency="payment-service",
        window_seconds=300,
        timeout_seconds=1.0,
    )
    assert classify_dependency_edge(result.value).healthy


def test_an_unverified_edge_is_refused_and_never_read_as_a_zero() -> None:
    environment = unreachable_payment_edge()
    result = environment.read_dependency(
        target="checkout-service", dependency="ad-service", window_seconds=300, timeout_seconds=1.0
    )
    assert result.succeeded is False
    assert "not an allow-listed dependency edge" in (result.error or "")


# --- the typed registry boundary ---------------------------------------------


@pytest.fixture
def registry() -> ToolRegistry:
    return ToolRegistry.default()


def spec(registry: ToolRegistry):
    return registry.require("read_dependency")


def test_the_dependency_read_is_a_bounded_read_tool(registry: ToolRegistry) -> None:
    read_spec = spec(registry)
    assert read_spec.side_effect is SideEffect.READ
    assert read_spec.max_attempts > 1
    assert "target" in read_spec.arguments_schema
    assert "dependency" in read_spec.arguments_schema
    window = read_spec.arguments_schema["window_seconds"]
    assert window["minimum"] >= 30 and window["maximum"] <= 900


def test_the_dependency_read_pins_the_caller_and_its_verified_callee(
    registry: ToolRegistry,
) -> None:
    read_spec = spec(registry)
    assert read_spec.fixed_arguments["target"] == ("checkout-service",)
    assert read_spec.per_target_arguments["checkout-service"]["dependency"] == ("payment-service",)
    validated = read_spec.validate_arguments(
        {"target": "checkout-service", "dependency": "payment-service", "window_seconds": 120}
    )
    assert validated["target"] == "checkout-service"
    assert validated["dependency"] == "payment-service"


@pytest.mark.parametrize(
    "arguments",
    [
        {"target": "checkout-service", "dependency": "ad-service", "window_seconds": 120},
        {"target": "cart-service", "dependency": "payment-service", "window_seconds": 120},
        {"target": "payment-service", "dependency": "payment-service", "window_seconds": 120},
    ],
)
def test_the_dependency_read_refuses_any_other_pair(
    registry: ToolRegistry, arguments: dict[str, object]
) -> None:
    """Attribution is the pair. An unverified pair has no attributable telemetry."""
    with pytest.raises(ValueError, match="not allow-listed"):
        spec(registry).validate_arguments(arguments)
