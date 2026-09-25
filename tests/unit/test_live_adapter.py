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
import re
from typing import Any

import httpx
import pytest

from opsaccord.config import Settings
from opsaccord.domain.enums import ActionOutcome
from opsaccord.tools.contracts import (
    DispatchTimeout,
    ReadTimeout,
    UnknownDemoTarget,
    classify_dependency_edge,
)
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

#: The span-metric label schema the pinned revision exposes. ``read_dependency``
#: reads this from the server instead of assuming it, so the fake has to answer
#: the schema endpoints too; a fake that only answered value queries would let a
#: hard-coded label name pass these tests.
SPAN_LABELS = frozenset({"__name__", "service_name", "span_kind", "span_name", "status_code"})
SPAN_KIND_VALUES = ("SPAN_KIND_SERVER", "SPAN_KIND_CLIENT")


def _is_valid_series_selector(selector: str) -> bool:
    """Does Prometheus's ``match[]`` parser accept this series selector?

    ``/api/v1/series`` parses ``match[]`` as a series selector: either a braced
    label selector or a bare metric name. A bare ``label='value'`` is a parse
    error there, and the API answers 400 rather than an empty series list --
    which is exactly how the live edge read failed, so the fake refuses the same
    shape instead of answering every selector the adapter happens to send.
    """
    if len(selector) > 1 and selector.startswith("{") and selector.endswith("}"):
        return True
    return bool(re.fullmatch(r"[a-zA-Z_:][a-zA-Z0-9_:]*", selector))


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
        span_labels: set[str] | None = None,
        span_kind_values: list[str] | None = None,
        errors_by_service: dict[str, float] | None = None,
    ) -> None:
        self.calls = calls
        self.errors = errors
        self.cpu = cpu
        self.traces = traces if traces is not None else []
        self.flags = json.loads(json.dumps(flags if flags is not None else FLAG_DOCUMENT))
        self.fail = fail
        self.span_labels = set(SPAN_LABELS if span_labels is None else span_labels)
        self.span_kind_values = list(
            SPAN_KIND_VALUES if span_kind_values is None else span_kind_values
        )
        #: Per-emitter error rates for one operation, keyed by ``service_name``.
        #: The observed telemetry has both sides of the Charge RPC in one series
        #: set (``checkout`` CLIENT and ``payment`` SERVER), so this models what a
        #: scoped and an unscoped reading each return: an unscoped selector sums
        #: every emitter, as Prometheus does.
        self.errors_by_service = errors_by_service
        self.queries: list[str] = []
        self.writes: list[dict[str, Any]] = []
        self.series_selectors: list[str] = []

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
                value: float | None = self._errors_for(query)
            elif "histogram_quantile" in query:
                value = 42.0
            elif CPU_COUNTER in query or CPU in query:
                value = self.cpu
            elif "span_name=" in query:
                value = self._calls_for(query)
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
        if path == "/api/v1/series":
            selector = request.url.params.get("match[]", "")
            self.series_selectors.append(selector)
            if not _is_valid_series_selector(selector):
                # The real server's answer to this: bad_data, not an empty result.
                return httpx.Response(
                    400,
                    json={
                        "status": "error",
                        "errorType": "bad_data",
                        "error": 'invalid parameter "match[]": parse error: unexpected '
                        "character: '='",
                    },
                )
            data = [dict.fromkeys(sorted(self.span_labels))] if self.span_labels else []
            return httpx.Response(200, json={"status": "success", "data": data})
        if path.startswith("/api/v1/label/") and path.endswith("/values"):
            label = path.removeprefix("/api/v1/label/").removesuffix("/values")
            values = self.span_kind_values if label == "span_kind" else []
            return httpx.Response(200, json={"status": "success", "data": values})
        raise AssertionError(f"unexpected request to {path}")

    @staticmethod
    def _scoped_service(query: str) -> str | None:
        match = re.search(r"service_name='([^']+)'", query)
        return match.group(1) if match else None

    def _calls_for(self, query: str) -> float:
        if self.errors_by_service is None or self._scoped_service(query) is not None:
            return self.calls
        return self.calls * max(len(self.errors_by_service), 1)

    def _errors_for(self, query: str) -> float:
        if self.errors_by_service is None:
            return self.errors
        scoped = self._scoped_service(query)
        if scoped is not None:
            return self.errors_by_service.get(scoped, 0.0)
        return sum(self.errors_by_service.values())


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


def test_health_is_scoped_to_the_target_service_s_own_spans() -> None:
    """An operation name is not a service: the caller's spans are not the callee's.

    Measured on the pinned stack (2026-09-25): ``oteldemo.PaymentService/Charge``
    is exported with a ``checkout`` CLIENT series and a ``payment`` SERVER series,
    and ``oteldemo.CheckoutService/PlaceOrder`` with a ``checkout`` SERVER series
    and a ``frontend`` CLIENT series. Before this change an unscoped selector
    matched all of them, so a health verdict about ``payment-service`` could be
    decided by a failing ``checkout``.
    """
    stack = FakeStack(calls=1.0, errors_by_service={"checkout": 1.0, "payment": 0.0})
    environment = build(stack)

    result = environment.read_service_health(target="payment-service", timeout_seconds=2.0)

    assert result.value["healthy"] is True
    assert result.value["health"] == "SERVING"
    assert result.value["error_rate"] == 0.0
    assert result.value["service"] == "payment"
    assert result.value["service_scope"] == "service_name='payment'"
    assert any("service_name='payment'" in query for query in stack.queries)


def test_health_reports_not_serving_when_the_target_s_own_spans_error() -> None:
    """Scoping must not hide a fault on the target itself."""
    stack = FakeStack(calls=1.0, errors_by_service={"checkout": 1.0, "payment": 1.0})
    environment = build(stack)

    result = environment.read_service_health(target="payment-service", timeout_seconds=2.0)

    assert result.value["healthy"] is False
    assert result.value["health"] == "NOT_SERVING"
    assert result.value["error_rate"] == 1.0


def test_the_error_rate_metric_is_scoped_to_the_target_service() -> None:
    """The metric read answers for the target service, not for the operation name."""
    stack = FakeStack(calls=1.0, errors_by_service={"checkout": 1.0, "payment": 0.0})
    environment = build(stack)

    result = environment.read_metrics(
        target="payment-service", metric="error_rate", window_seconds=300, timeout_seconds=2.0
    )

    assert result.value["value"] == 0.0
    assert any("service_name='payment'" in query for query in stack.queries)


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


# --- the caller's own view of one dependency edge -----------------------------
#
# This is the read the focused Payment run added. It never ran against the stack
# in the offline suite, so its schema resolution and its query construction are
# pinned here instead: the live stage proves the numbers, these prove that the
# read asks the right question and refuses to answer when it cannot.


def test_the_edge_read_pins_the_callers_own_client_side_spans() -> None:
    """The attribution is the triple (caller, operation, client side).

    Reading the operation without the caller and the span kind is what let the
    pre-fix evidence mix the callee's server spans into the caller's share.
    """
    stack = FakeStack(calls=0.4, errors=0.4)
    environment = build(stack)

    result = environment.read_dependency(
        target="checkout-service",
        dependency="payment-service",
        window_seconds=120,
        timeout_seconds=2.0,
    )

    assert result.succeeded is True
    assert result.is_simulated is False
    value = result.value
    assert value["caller"] == "checkout-service"
    assert value["destination"] == "payment-service"
    assert value["operation"] == "Charge"
    assert value["span_kind"] == "client"
    assert value["attributable"] is True
    assert value["error_rate"] == pytest.approx(1.0)
    # A braced series selector: the unbraced form is a PromQL parse error, and
    # Prometheus answers it with 400 rather than "no series".
    assert stack.series_selectors == ["{span_name='oteldemo.PaymentService/Charge'}"]
    calls_query, errors_query = value["queries"]
    for query in (calls_query, errors_query):
        assert "span_name='oteldemo.PaymentService/Charge'" in query
        assert "service_name='checkout'" in query
        assert "span_kind='SPAN_KIND_CLIENT'" in query
    assert "status_code='STATUS_CODE_ERROR'" in errors_query
    assert "status_code=" not in calls_query
    assert classify_dependency_edge(value).failing


@pytest.mark.parametrize(
    ("stack_kwargs", "expected"),
    [
        (
            {"span_labels": set()},
            "no series matches span_name=",
        ),
        (
            {"span_labels": {"__name__", "span_name", "span_kind", "status_code"}},
            "expose no service label",
        ),
        (
            {"span_labels": {"__name__", "span_name", "service_name", "status_code"}},
            "expose no span-kind label",
        ),
        (
            {"span_kind_values": ["SPAN_KIND_SERVER"]},
            "has no client-side value",
        ),
    ],
    ids=["no-series", "no-service-label", "no-span-kind-label", "no-client-kind-value"],
)
def test_an_unattributable_edge_says_why_and_never_claims_health(
    stack_kwargs: dict[str, Any], expected: str
) -> None:
    """An instrumentation gap is reported as a gap, not as a quiet healthy edge."""
    stack = FakeStack(**stack_kwargs)
    environment = build(stack)

    result = environment.read_dependency(
        target="checkout-service",
        dependency="payment-service",
        window_seconds=120,
        timeout_seconds=2.0,
    )

    assert result.succeeded is True
    value = result.value
    assert value["attributable"] is False
    assert expected in value["reason"]
    # No share is invented, and no value query is built from labels the server
    # did not confirm: the read stays inside its declared boundary.
    assert "calls_per_second" not in value
    assert "error_rate" not in value
    assert stack.queries == []
    verdict = classify_dependency_edge(value)
    assert not verdict.failing and not verdict.healthy


def test_a_quiet_edge_is_not_a_recovered_edge() -> None:
    """A window with no calls proves neither failure nor recovery."""
    environment = build(FakeStack(calls=0.0, errors=0.0))

    result = environment.read_dependency(
        target="checkout-service",
        dependency="payment-service",
        window_seconds=120,
        timeout_seconds=2.0,
    )

    value = result.value
    assert value["attributable"] is True
    assert value["calls_per_second"] == 0.0
    assert value["error_rate"] is None
    verdict = classify_dependency_edge(value)
    assert not verdict.failing and not verdict.healthy


def test_the_schema_lookup_sends_a_selector_prometheus_can_parse() -> None:
    """The live failure this pass fixes: the schema query was a parse error.

    ``/api/v1/series?match[]=span_name='oteldemo.PaymentService/Charge'`` answered
    ``400 Bad Request`` against the pinned stack, so the edge schema was never
    resolved, every dependency read failed, and recovery stayed unproven even
    though the remediation had landed. The selector is now braced, and the fake
    rejects the unbraced form exactly as the server does, so the shape cannot
    regress unnoticed again.
    """
    stack = FakeStack()
    environment = build(stack)

    result = environment.read_dependency(
        target="checkout-service",
        dependency="payment-service",
        window_seconds=120,
        timeout_seconds=2.0,
    )

    assert result.succeeded is True
    assert result.value["attributable"] is True
    assert stack.series_selectors == ["{span_name='oteldemo.PaymentService/Charge'}"]


def test_the_fake_refuses_a_selector_the_real_server_refuses() -> None:
    """Guards the guard: unbraced label matchers are a bad request, not a miss.

    If the fake answered this with an empty series list, the adapter's selector
    could drift back to the form that cost a live run to diagnose.
    """
    stack = FakeStack()
    client = httpx.Client(transport=httpx.MockTransport(stack.handler))

    unbraced = "span_name='oteldemo.PaymentService/Charge'"
    response = client.get("http://prometheus/api/v1/series", params={"match[]": unbraced})

    assert response.status_code == 400
    assert response.json()["errorType"] == "bad_data"
    assert "parse error" in response.json()["error"]


def test_a_refused_query_reports_the_servers_own_reason() -> None:
    """A 4xx must carry the body, not just the status code.

    The live evidence said only "Client error '400 Bad Request'", which does not
    say whether the selector was unparseable, the parameter unsupported or the
    query refused. The reason is in the body.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={"status": "error", "errorType": "bad_data", "error": "invalid parameter x"},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(LiveEnvironmentUnavailable) as raised:
        LiveEnvironment(settings=Settings(), http_client=client).prometheus.series_labels(
            "{span_name='x'}", timeout_seconds=1.0
        )

    message = str(raised.value)
    assert "/api/v1/series" in message
    assert "422" in message
    assert "invalid parameter x" in message


def test_the_edge_read_refuses_an_unverified_pair() -> None:
    """Attribution is the declared pair; another pair has no evidence to read."""
    stack = FakeStack()
    environment = build(stack)

    with pytest.raises(UnknownDemoTarget, match="not an allow-listed dependency edge"):
        environment.read_dependency(
            target="checkout-service",
            dependency="ad-service",
            window_seconds=120,
            timeout_seconds=2.0,
        )
    assert stack.queries == []
