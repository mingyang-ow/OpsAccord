"""Live-adapter contract tests against a mocked demo stack.

These tests never need Docker. They pin the adapter contract that the live
integration run depends on:

* reads are answered from Prometheus/Jaeger HTTP APIs and are labelled as real
  evidence (``is_simulated`` is False), never as simulator output;
* the closed metric/flag enumerations are enforced before any query is built;
* the flag remediation is a read-modify-write that touches exactly one flag;
* an unreachable stack fails closed instead of degrading into simulated numbers.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from opsaccord.config import Settings
from opsaccord.domain.enums import ActionOutcome
from opsaccord.tools.contracts import DispatchTimeout, ReadTimeout, UnknownDemoTarget
from opsaccord.tools.live_environment import LiveEnvironment, LiveEnvironmentUnavailable

CALLS = "traces_span_metrics_calls_total"
DURATION = "traces_span_metrics_duration_milliseconds_bucket"
# The adapter differentiates the cumulative counter rather than trusting the
# container_cpu_utilization_ratio gauge, which does not track the container.
CPU_COUNTER = "container_cpu_usage_nanoseconds_total"
CPU = "container_cpu_utilization_ratio"

FLAG_DOCUMENT = {
    "flags": {
        "adHighCpu": {"defaultVariant": "off", "variants": {"off": False, "on": True}},
        "paymentUnreachable": {
            "defaultVariant": "on",
            "variants": {"off": False, "on": True},
        },
        "cartFailure": {"defaultVariant": "off", "variants": {"off": 0, "100%": 1}},
    }
}


class FakeStack:
    """Minimal Prometheus + Jaeger + flagd-ui stand-in."""

    def __init__(
        self,
        *,
        calls: float = 1.5,
        errors: float = 0.0,
        cpu: float | None = 0.2,
        traces: list[dict[str, Any]] | None = None,
        flags: dict[str, Any] | None = None,
        fail: Exception | None = None,
    ) -> None:
        self.calls = calls
        self.errors = errors
        self.cpu = cpu
        self.traces = traces if traces is not None else []
        self.flags = json.loads(json.dumps(flags if flags is not None else FLAG_DOCUMENT))
        self.fail = fail
        self.queries: list[str] = []
        self.writes: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.fail is not None:
            raise self.fail
        path = request.url.path
        if path == "/api/v1/label/__name__/values":
            return httpx.Response(
                200,
                json={"status": "success", "data": [CALLS, DURATION, CPU, CPU_COUNTER, "up"]},
            )
        if path == "/api/v1/query":
            query = request.url.params.get("query", "")
            self.queries.append(query)
            if query.startswith("count("):
                return httpx.Response(200, json={"status": "success", "data": {"result": []}})
            if "status_code='STATUS_CODE_ERROR'" in query:
                value: float | None = self.errors
            elif "histogram_quantile" in query:
                value = 42.0
            elif CPU_COUNTER in query or CPU in query:
                value = self.cpu
            elif "span_name=" in query:
                value = self.calls
            else:
                value = 0.0
            sample = {"metric": {}, "value": [1_700_000_000.0, str(value)]}
            result = [] if value is None else [sample]
            return httpx.Response(
                200,
                json={"status": "success", "data": {"resultType": "vector", "result": result}},
            )
        if path.endswith("/api/traces"):
            return httpx.Response(200, json={"data": self.traces})
        if path.endswith("/api/read"):
            return httpx.Response(200, json=self.flags)
        if path.endswith("/api/write"):
            body = json.loads(request.content.decode())
            self.writes.append(body)
            self.flags = body["data"]
            return httpx.Response(200, json={})
        raise AssertionError(f"unexpected request to {path}")


def build(stack: FakeStack, **kwargs: Any) -> LiveEnvironment:
    client = httpx.Client(transport=httpx.MockTransport(stack.handler))
    return LiveEnvironment(settings=Settings(), http_client=client, **kwargs)


def test_cart_target_maps_to_the_span_the_fault_actually_breaks() -> None:
    """cartFailure routes EmptyCart to the bad store; GetCart is untouched.

    Reading GetCart for a cart incident would collect a healthy span while the
    fault is active, so the adapter's cart mapping is pinned to the span that
    carries the fault.
    """
    environment = build(FakeStack())
    traces = environment.read_traces(
        target="cart-service", operation="EmptyCart", window_seconds=300, timeout_seconds=2.0
    )

    assert traces.succeeded is True
    assert traces.value["operation"] == "POST /oteldemo.CartService/EmptyCart"
    assert traces.value["operation_alias"] == "EmptyCart"


def test_probe_windows_respect_the_measured_export_cadence() -> None:
    """Span metrics publish every ~60s, so no probe may ask for less than 120s.

    Measured on the pinned stack: a [60s] window returned no data 24 times out of
    24, and [90s] half the time. A shorter window would read as "no traffic".
    """
    stack = FakeStack()
    environment = build(stack)

    for target in ("payment-service", "ad-service", "cart-service"):
        environment.prometheus._metric_cache.clear()
        stack.queries.clear()
        environment.read_service_health(target=target, timeout_seconds=2.0)
        windows = [
            int(query.split("[")[1].split("s]")[0])
            for query in stack.queries
            if "traces_span_metrics_calls_total" in query
        ]
        assert windows, target
        assert min(windows) >= 120, (target, windows)


def test_metrics_read_is_labelled_real_and_built_from_the_closed_enum() -> None:
    stack = FakeStack(calls=2.0, errors=0.5)
    environment = build(stack)

    result = environment.read_metrics(
        target="payment-service", metric="error_rate", window_seconds=300, timeout_seconds=2.0
    )

    assert result.succeeded is True
    assert result.is_simulated is False
    assert result.value["value"] == pytest.approx(0.25)
    assert any("span_name='oteldemo.PaymentService/Charge'" in query for query in stack.queries)
    assert result.observation_window_start is not None


def test_unknown_metric_is_refused_without_querying_the_stack() -> None:
    stack = FakeStack()
    environment = build(stack)

    result = environment.read_metrics(
        target="payment-service", metric="cpu_saturation", window_seconds=300, timeout_seconds=2.0
    )

    assert result.succeeded is False
    assert "no live mapping" in (result.error or "")
    assert stack.queries == []


def test_unknown_target_is_refused() -> None:
    environment = build(FakeStack())
    with pytest.raises(UnknownDemoTarget):
        environment.read_metrics(
            target="email-service", metric="error_rate", window_seconds=300, timeout_seconds=2.0
        )


def test_traces_read_counts_error_spans_from_jaeger_tags() -> None:
    stack = FakeStack(
        traces=[
            {
                "spans": [
                    {
                        "operationName": "oteldemo.PaymentService/Charge",
                        "duration": 2_500,
                        "tags": [{"key": "otel.status_code", "value": "ERROR"}],
                    },
                    {
                        "operationName": "oteldemo.PaymentService/Charge",
                        "duration": 1_500,
                        "tags": [],
                    },
                ]
            }
        ]
    )
    environment = build(stack)

    result = environment.read_traces(
        target="payment-service", operation="Charge", window_seconds=300, timeout_seconds=2.0
    )

    assert result.succeeded is True
    assert result.is_simulated is False
    # The tool contract keeps the short alias; the evidence records the real name.
    assert result.value["operation"] == "oteldemo.PaymentService/Charge"
    assert result.value["operation_alias"] == "Charge"
    assert result.value["span_count"] == 2
    assert result.value["error_spans"] == 1


def test_traces_read_refuses_an_operation_the_revision_does_not_export() -> None:
    """An unverified span name must fail closed rather than query blind."""
    stack = FakeStack()
    environment = build(stack)

    result = environment.read_traces(
        target="ad-service", operation="Charge", window_seconds=300, timeout_seconds=2.0
    )

    assert result.succeeded is False
    assert "no verified span" in (result.error or "")
    assert stack.queries == []


def test_health_is_not_serving_when_the_dependency_call_fails() -> None:
    environment = build(FakeStack(calls=1.0, errors=1.0, cpu=0.1))
    result = environment.read_service_health(target="payment-service", timeout_seconds=2.0)
    assert result.value["healthy"] is False
    assert result.value["reachable"] is True
    assert result.value["health"] == "NOT_SERVING"


def test_health_is_not_serving_while_the_container_burns_cpu() -> None:
    environment = build(FakeStack(calls=4.0, errors=0.0, cpu=0.95))
    result = environment.read_service_health(target="ad-service", timeout_seconds=2.0)
    assert result.value["healthy"] is False
    assert result.value["cpu_utilization"] == pytest.approx(0.95)


def test_health_is_serving_when_calls_succeed_and_cpu_is_normal() -> None:
    environment = build(FakeStack(calls=4.0, errors=0.0, cpu=0.3))
    result = environment.read_service_health(target="ad-service", timeout_seconds=2.0)
    assert result.value["healthy"] is True
    assert result.value["health"] == "SERVING"


def test_remediation_rewrites_only_the_requested_flag() -> None:
    stack = FakeStack()
    environment = build(stack)

    result = environment.apply_remediation(
        idempotency_key="act-1",
        target="payment-service",
        remediation_id="disable_feature_flag",
        flag_id="paymentUnreachable",
        timeout_seconds=2.0,
    )

    assert result.succeeded is True
    assert result.is_simulated is False
    written = stack.writes[0]["data"]["flags"]
    assert written["paymentUnreachable"]["defaultVariant"] == "off"
    assert written["adHighCpu"] == FLAG_DOCUMENT["flags"]["adHighCpu"]
    assert written["cartFailure"] == FLAG_DOCUMENT["flags"]["cartFailure"]
    assert set(written) == set(FLAG_DOCUMENT["flags"])


def test_remediation_refuses_a_flag_that_is_not_the_target_fault_flag() -> None:
    stack = FakeStack()
    environment = build(stack)

    result = environment.apply_remediation(
        idempotency_key="act-2",
        target="ad-service",
        remediation_id="disable_feature_flag",
        flag_id="paymentUnreachable",
        timeout_seconds=2.0,
    )

    assert result.succeeded is False
    assert stack.writes == []


def test_remediation_refuses_an_unverified_flag_id() -> None:
    environment = build(FakeStack())
    result = environment.apply_remediation(
        idempotency_key="act-3",
        target="ad-service",
        remediation_id="disable_feature_flag",
        flag_id="productCatalogFailure",
        timeout_seconds=2.0,
    )
    assert result.succeeded is False
    assert "not the fault flag" in (result.error or "")


def test_dispatch_timeout_is_raised_so_the_gateway_reconciles() -> None:
    stack = FakeStack(fail=httpx.ReadTimeout("write timed out"))
    environment = build(stack)

    with pytest.raises((DispatchTimeout, ReadTimeout)):
        environment.apply_remediation(
            idempotency_key="act-4",
            target="payment-service",
            remediation_id="disable_feature_flag",
            flag_id="paymentUnreachable",
            timeout_seconds=0.05,
        )


def test_action_status_confirms_the_requested_state_and_refuses_to_infer_the_rest() -> None:
    """The two directions are not symmetric, and the asymmetry is the point.

    The requested value being present proves the state this action wanted is in
    force. The requested value being *absent* proves nothing about whether our
    write ever happened - another actor may have changed the flag afterwards - so
    it is UNKNOWN, not CONFIRMED_NOT_APPLIED.
    """
    stack = FakeStack()
    environment = build(stack)
    environment.read_flag_document(timeout_seconds=2.0)
    environment.apply_remediation(
        idempotency_key="act-5",
        target="payment-service",
        remediation_id="disable_feature_flag",
        flag_id="paymentUnreachable",
        timeout_seconds=2.0,
    )

    assert environment.action_status("act-5") is ActionOutcome.CONFIRMED_APPLIED

    stack.flags["flags"]["paymentUnreachable"]["defaultVariant"] = "on"
    assert environment.action_status("act-5") is ActionOutcome.UNKNOWN
    assert environment.action_status("act-5") is not ActionOutcome.CONFIRMED_NOT_APPLIED


def test_action_status_is_unknown_when_the_probe_is_disabled_or_unknown() -> None:
    stack = FakeStack()
    disabled = build(stack, reconcile_via_external_state=False)
    assert disabled.action_status("act-6") is ActionOutcome.UNKNOWN
    assert build(stack).action_status("never-seen") is ActionOutcome.UNKNOWN


def test_unreachable_stack_fails_closed_and_never_simulates() -> None:
    environment = build(FakeStack(fail=httpx.ConnectError("connection refused")))

    with pytest.raises(LiveEnvironmentUnavailable):
        environment.read_metrics(
            target="payment-service", metric="error_rate", window_seconds=300, timeout_seconds=2.0
        )


def test_timeout_is_reported_as_a_read_timeout() -> None:
    environment = build(FakeStack(fail=httpx.ReadTimeout("too slow")))
    with pytest.raises(ReadTimeout):
        environment.read_metrics(
            target="payment-service", metric="error_rate", window_seconds=300, timeout_seconds=0.05
        )
