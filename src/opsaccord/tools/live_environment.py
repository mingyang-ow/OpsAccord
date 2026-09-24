"""Live execution surface: the pinned Astronomy Shop stack.

This adapter drives the real containerised demo through three bounded,
allow-listed channels and nothing else:

* Prometheus HTTP API for metric series (``/api/v1/query``);
* Jaeger query API for recent spans of one operation;
* flagd-ui read/write API for the feature-flag document.

There is no shell, no generic URL fetch, and no Docker API call. Targets are
resolved through the fixed catalog below, so a model can never name an arbitrary
container or metric.

Two properties matter for the report and are enforced here:

* provenance is explicit: every record produced here is real telemetry, and
  ``ToolResult.is_simulated`` is set to ``False`` so live evidence can never be
  persisted as simulated evidence;
* live mode never falls back to the in-process simulator. If the stack is
  unreachable, reads raise and the gateway records collection-error evidence.

The PromQL selectors and API shapes below were calibrated against the running
stack; see docs/astronomy-shop.md ("Live adapter calibration") for the commands
and the observed series names.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from opsaccord.config import Settings
from opsaccord.domain.enums import ActionOutcome
from opsaccord.domain.models import ToolResult
from opsaccord.tools.contracts import (
    DispatchRefused,
    DispatchTimeout,
    ReadTimeout,
    UnknownDemoTarget,
    dependency_operation,
)
from opsaccord.tools.demo_environment import DEFAULT_RUNBOOKS, REVISION, REVISION_SOURCE

# Metric and span series names come from the OTLP -> Prometheus pipeline and the
# spanmetrics connector of the pinned collector config. More than one name is
# listed where upstream renamed a metric across minor versions; the adapter
# resolves the first name the server actually has and caches the answer.
CALLS_METRIC_CANDIDATES = ("traces_span_metrics_calls_total", "calls_total")
DURATION_METRIC_CANDIDATES = (
    "traces_span_metrics_duration_milliseconds_bucket",
    "duration_milliseconds_bucket",
)
# Container metrics come from the collector's docker_stats receiver, keyed by a
# ``container_name`` label.
#
# CPU is read from the *cumulative* counter and differentiated, not from the
# ``container_cpu_utilization_ratio`` gauge. Measured against the running stack,
# that gauge does not track the container: with the ad service idle (docker stats
# 0.11-0.31% CPU) it still reported 4.015, and it stayed there for minutes after a
# CPU burn had stopped. ``rate(container_cpu_usage_nanoseconds_total) / 1e9``
# answers in cores and matched docker stats to ~2 decimal places at every sample.
# A stale health signal is worse than none: it would fail a recovered service.
CONTAINER_CPU_COUNTER_CANDIDATES = (
    "container_cpu_usage_nanoseconds_total",
    "container_cpu_usage_usermode_nanoseconds_total",
)
CONTAINER_MEMORY_CANDIDATES = (
    "container_memory_usage_total_bytes",
    "container_memory_percent_ratio",
)
CONTAINER_RESTART_CANDIDATES = ("container_restarts_total", "container_restart_count")

# Window used by the health probe's own "is it healthy right now" question.
#
# Measured against the running stack: the span-metrics pipeline exports every
# ~60s, so a rate() window has to span at least two exports. Sampled 24 times,
# [60s] returned no data 100% of the time and [90s] 50% of the time, while
# [120s] and longer never did. 120s is therefore the shortest reliable window for
# span-derived signals, and it is deliberately separate from the window a metrics
# *read* asks for: the probe is a fresh-state question, the evidence is a
# requested observation window.
#
# Container metrics are a different pipeline with a much faster cadence
# (``container_cpu_usage_nanoseconds_total`` samples are never older than ~8s), so
# CPU questions can use shorter windows -- see the read plan in
# ``opsaccord.workflow.controller``.
LIVE_HEALTH_WINDOW_SECONDS = 120

# Span-metrics labels that can carry the two facts an edge attribution needs:
# which service emitted the span, and whether it was the client or the server
# side of the RPC. Candidates are tried against the schema the server actually
# exposes (``/api/v1/series`` for the operation), never assumed: if neither name
# is present the adapter reports that the edge cannot be attributed instead of
# guessing a label and reading a number that means something else.
SERVICE_LABEL_CANDIDATES = ("service_name", "service")
SPAN_KIND_LABEL_CANDIDATES = ("span_kind", "span_kind_client")
# A span-kind value counts as the client side when it contains this token,
# case-insensitively (observed form: ``SPAN_KIND_CLIENT``).
CLIENT_SPAN_KIND_TOKEN = "client"

# Feature-flag variants that mean "the fault is switched off". Verified against
# src/flagd/demo.flagd.json at the pinned revision: every entry below is a
# boolean-style flag whose "off" variant disables the injected fault.
FLAG_SAFE_VARIANT = {
    "adFailure": "off",
    "adHighCpu": "off",
    "adManualGc": "off",
    "cartFailure": "off",
    "paymentFailure": "off",
    "paymentUnreachable": "off",
}


@dataclass(frozen=True)
class LiveTarget:
    """Verified mapping from an OpsAccord target to its Astronomy Shop identity.

    ``operation`` is the real span name the collector exports, and ``operation_alias``
    is the short name the typed tool contract uses. The registry keeps the short
    names so the tool surface does not leak one demo's naming conventions; the
    adapter is where that alias is resolved against the pinned revision.
    """

    target: str
    service_name: str
    container: str
    operation: str
    operation_alias: str
    fault_flag: str
    probe_window_seconds: int = LIVE_HEALTH_WINDOW_SECONDS


LIVE_TARGETS: dict[str, LiveTarget] = {
    "payment-service": LiveTarget(
        target="payment-service",
        service_name="payment",
        container="payment",
        operation="oteldemo.PaymentService/Charge",
        operation_alias="Charge",
        fault_flag="paymentUnreachable",
    ),
    "ad-service": LiveTarget(
        target="ad-service",
        service_name="ad",
        container="ad",
        operation="oteldemo.AdService/GetAds",
        operation_alias="GetAds",
        fault_flag="adHighCpu",
    ),
    "cart-service": LiveTarget(
        target="cart-service",
        service_name="cart",
        container="cart",
        # cartFailure in the pinned revision routes EmptyCart to the bad cart
        # store; GetCart is untouched by the flag. The fault-bearing span is the
        # cart service's own ASP.NET server span, and EmptyCart traffic is sparse
        # (about one call every one to two minutes), so the probe window is longer
        # than the span-metric minimum.
        operation="POST /oteldemo.CartService/EmptyCart",
        operation_alias="EmptyCart",
        fault_flag="cartFailure",
        probe_window_seconds=300,
    ),
    "checkout-service": LiveTarget(
        target="checkout-service",
        service_name="checkout",
        container="checkout",
        operation="oteldemo.CheckoutService/PlaceOrder",
        operation_alias="PlaceOrder",
        fault_flag="paymentUnreachable",
    ),
}


class LiveEnvironmentUnavailable(RuntimeError):
    """The live demo stack did not answer, so no live evidence can be produced."""


def _op_selector(metric: str, operation: str, window_seconds: int, **labels: str) -> str:
    """Build one bounded rate() selector for an operation (and optional labels)."""
    parts = [f"span_name='{operation}'"]
    parts.extend(f"{name}='{value}'" for name, value in sorted(labels.items()))
    return f"sum(rate({metric}{{{', '.join(parts)}}}[{window_seconds}s]))"


def _response_body(response: httpx.Response, *, limit: int = 200) -> str:
    """The server's own explanation for a failed request, bounded to one line.

    Prometheus answers a bad query with ``{"status": "error", "error": "..."}``
    and the reason is in the body, not in the status code: a 400 could be a parse
    error, an unsupported parameter or a refused selector. Without the body the
    only thing a run records is "400 Bad Request", which cost this pass a live
    run to work around.
    """
    try:
        text = response.text
    except Exception:  # a body that cannot be decoded is still a failed request
        return "<undecodable>"
    return " ".join(text.split())[:limit] or "<empty>"


class PrometheusClient:
    """Bounded, typed access to the Prometheus HTTP API."""

    def __init__(self, base_url: str, *, client: httpx.Client | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = client
        self._metric_cache: dict[str, str | None] = {}

    def _get(self, path: str, params: dict[str, Any], timeout_seconds: float) -> dict[str, Any]:
        client = self._client or httpx.Client(timeout=timeout_seconds, trust_env=False)
        try:
            response = client.get(f"{self.base_url}{path}", params=params)
            response.raise_for_status()
            payload = response.json()
        except httpx.TimeoutException as exc:
            raise ReadTimeout(f"prometheus {path} exceeded {timeout_seconds}s") from exc
        except httpx.HTTPStatusError as exc:
            raise LiveEnvironmentUnavailable(
                f"prometheus {path} failed: {exc}; body={_response_body(exc.response)}"
            ) from exc
        except httpx.HTTPError as exc:
            raise LiveEnvironmentUnavailable(f"prometheus {path} failed: {exc}") from exc
        finally:
            if self._client is None:
                client.close()
        if payload.get("status") != "success":
            raise LiveEnvironmentUnavailable(
                f"prometheus {path} returned status {payload.get('status')!r}"
            )
        return dict(payload)

    def series_names(self, timeout_seconds: float) -> set[str]:
        """Every metric series name the server currently exposes."""
        payload = self._get("/api/v1/label/__name__/values", {}, timeout_seconds=timeout_seconds)
        return {str(name) for name in payload.get("data", [])}

    def resolve_metric(self, candidates: tuple[str, ...], timeout_seconds: float) -> str | None:
        """Return the first candidate series the server actually has, then cache it."""
        key = "|".join(candidates)
        if key not in self._metric_cache:
            names = self.series_names(timeout_seconds)
            self._metric_cache[key] = next((name for name in candidates if name in names), None)
        return self._metric_cache[key]

    def has_series(self, selector: str, timeout_seconds: float) -> bool:
        payload = self._get(
            "/api/v1/query", {"query": f"count({selector})"}, timeout_seconds=timeout_seconds
        )
        return bool(payload.get("data", {}).get("result"))

    def series_labels(self, selector: str, timeout_seconds: float) -> set[str]:
        """Every label name the server attaches to series matching ``selector``."""
        payload = self._get(
            "/api/v1/series", {"match[]": selector}, timeout_seconds=timeout_seconds
        )
        names: set[str] = set()
        for series in payload.get("data") or []:
            names.update(str(key) for key in series)
        return names

    def label_values(self, label: str, timeout_seconds: float) -> set[str]:
        """Every value the server currently holds for one label name."""
        payload = self._get(f"/api/v1/label/{label}/values", {}, timeout_seconds=timeout_seconds)
        return {str(value) for value in payload.get("data") or []}

    def scalar(self, query: str, timeout_seconds: float) -> float | None:
        """Evaluate one PromQL expression and return a single scalar, or None."""
        payload = self._get("/api/v1/query", {"query": query}, timeout_seconds=timeout_seconds)
        result = payload.get("data", {}).get("result") or []
        if not result:
            return None
        sample = result[0]
        if sample.get("value") is not None:
            raw = sample["value"][1]
        elif sample.get("values"):
            raw = sample["values"][-1][1]
        else:
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            # PromQL answers NaN for an undefined ratio; undefined is not a number.
            return None


class LiveEnvironment:
    """The real Astronomy Shop demo stack, reached over allow-listed HTTP APIs."""

    is_simulated = False
    revision = REVISION
    revision_source = REVISION_SOURCE

    def __init__(
        self,
        *,
        settings: Settings,
        http_client: httpx.Client | None = None,
        reconcile_via_external_state: bool = True,
    ) -> None:
        self.settings = settings
        self.prometheus = PrometheusClient(settings.prometheus_base_url, client=http_client)
        self.jaeger_base_url = settings.jaeger_base_url.rstrip("/")
        self.flag_base_url = settings.flag_control_base_url.rstrip("/")
        self._client = http_client
        self._http_timeout = 5.0
        self._applied: dict[str, dict[str, Any]] = {}
        # Reconciling from external state is the honest default: it asks the flag
        # document whether the desired change is in place. Turning it off is a
        # documented harness fault injection for the "outcome cannot be
        # reconciled" variation, never a normal execution mode.
        self.reconcile_via_external_state = reconcile_via_external_state

    # --- helpers ----------------------------------------------------------

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)

    def _target(self, target: str) -> LiveTarget:
        try:
            return LIVE_TARGETS[target]
        except KeyError as exc:
            raise UnknownDemoTarget(f"no verified live mapping for target {target!r}") from exc

    def _window(self, window_seconds: int) -> tuple[datetime, datetime]:
        end = self._now()
        return end - timedelta(seconds=window_seconds), end

    def _json(
        self,
        url: str,
        *,
        timeout_seconds: float,
        method: str = "GET",
        json_body: dict[str, Any] | None = None,
        timeout_error: type[Exception] = ReadTimeout,
    ) -> Any:
        client = self._client or httpx.Client(timeout=timeout_seconds, trust_env=False)
        try:
            response = client.request(method, url, json=json_body)
            response.raise_for_status()
            return response.json()
        except httpx.TimeoutException as exc:
            raise timeout_error(f"{url} exceeded {timeout_seconds}s") from exc
        except httpx.HTTPError as exc:
            raise LiveEnvironmentUnavailable(f"{url} failed: {exc}") from exc
        finally:
            if self._client is None:
                client.close()

    def _calls_metric(self, timeout_seconds: float) -> str:
        name = self.prometheus.resolve_metric(CALLS_METRIC_CANDIDATES, timeout_seconds)
        if name is None:
            raise LiveEnvironmentUnavailable(
                "the demo stack exports no span-metrics series; "
                f"looked for {CALLS_METRIC_CANDIDATES!r} in Prometheus"
            )
        return name

    def _call_counts(
        self, *, metric: str, operation: str, window_seconds: int, timeout_seconds: float
    ) -> tuple[float, float]:
        """Return (calls_per_second, errors_per_second) for one operation."""
        calls = self.prometheus.scalar(
            _op_selector(metric, operation, window_seconds), timeout_seconds
        )
        errors = self.prometheus.scalar(
            _op_selector(metric, operation, window_seconds, status_code="STATUS_CODE_ERROR"),
            timeout_seconds,
        )
        return calls or 0.0, errors or 0.0

    # --- read tools -------------------------------------------------------

    def read_metrics(
        self, *, target: str, metric: str, window_seconds: int, timeout_seconds: float
    ) -> ToolResult:
        live = self._target(target)
        start, end = self._window(window_seconds)
        queries: list[str] = []
        if metric == "error_rate":
            metric_name = self._calls_metric(timeout_seconds)
            calls, errors = self._call_counts(
                metric=metric_name,
                operation=live.operation,
                window_seconds=window_seconds,
                timeout_seconds=timeout_seconds,
            )
            queries = [_op_selector(metric_name, live.operation, window_seconds)]
            value = (errors / calls) if calls > 0 else None
        elif metric in {"request_rate", "cpu_utilization", "memory_utilization"}:
            value, queries = self._resource_metric(live, metric, window_seconds, timeout_seconds)
        elif metric == "request_latency_p95_ms":
            value, queries = self._latency_p95(live, window_seconds, timeout_seconds)
        elif metric == "container_restarts":
            value, queries = self._container_restarts(live, timeout_seconds)
        else:
            return ToolResult(
                tool_name="read_metrics",
                target=target,
                succeeded=False,
                error=f"metric {metric!r} has no live mapping for {target!r}",
                is_simulated=False,
                collected_at=self._now(),
            )

        return ToolResult(
            tool_name="read_metrics",
            target=target,
            succeeded=True,
            value={
                "metric": metric,
                "value": value,
                "window_seconds": window_seconds,
                "present": value is not None,
                "source": "prometheus",
                "queries": queries,
            },
            is_simulated=False,
            collected_at=self._now(),
            observation_window_start=start,
            observation_window_end=end,
        )

    def _resource_metric(
        self, live: LiveTarget, metric: str, window_seconds: int, timeout_seconds: float
    ) -> tuple[float | None, list[str]]:
        """One container resource signal, in the unit the health rule expects.

        ``cpu_utilization`` is cores, so 0.24 is a mostly-idle container and 4.0 is
        a four-thread burn; the 0.80 health ceiling is a fraction-of-one-core bound
        and reads the same way it does against the simulated surface.
        """
        if metric == "request_rate":
            name = self._calls_metric(timeout_seconds)
            query = _op_selector(name, live.operation, window_seconds)
            return self.prometheus.scalar(query, timeout_seconds), [query]
        if metric == "cpu_utilization":
            counter = self.prometheus.resolve_metric(
                CONTAINER_CPU_COUNTER_CANDIDATES, timeout_seconds
            )
            if counter is None:
                return None, []
            query = (
                f"sum(rate({counter}{{container_name='{live.container}'}}"
                f"[{window_seconds}s])) / 1e9"
            )
            return self.prometheus.scalar(query, timeout_seconds), [query]
        series = self.prometheus.resolve_metric(CONTAINER_MEMORY_CANDIDATES, timeout_seconds)
        if series is None:
            return None, []
        query = (
            f"max(avg_over_time({series}{{container_name='{live.container}'}}[{window_seconds}s]))"
        )
        return self.prometheus.scalar(query, timeout_seconds), [query]

    def _latency_p95(
        self, live: LiveTarget, window_seconds: int, timeout_seconds: float
    ) -> tuple[float | None, list[str]]:
        name = self.prometheus.resolve_metric(DURATION_METRIC_CANDIDATES, timeout_seconds)
        if name is None:
            return None, []
        query = (
            "histogram_quantile(0.95, "
            f"sum(rate({name}{{span_name='{live.operation}'}}[{window_seconds}s])) by (le))"
        )
        return self.prometheus.scalar(query, timeout_seconds), [query]

    def _container_restarts(
        self, live: LiveTarget, timeout_seconds: float
    ) -> tuple[float | None, list[str]]:
        name = self.prometheus.resolve_metric(CONTAINER_RESTART_CANDIDATES, timeout_seconds)
        if name is None:
            return None, []
        query = f"max({name}{{container_name='{live.container}'}})"
        return self.prometheus.scalar(query, timeout_seconds), [query]

    def read_traces(
        self, *, target: str, operation: str, window_seconds: int, timeout_seconds: float
    ) -> ToolResult:
        live = self._target(target)
        span_name = self._span_name(live, operation)
        if span_name is None:
            return ToolResult(
                tool_name="read_traces",
                target=target,
                succeeded=False,
                error=(
                    f"operation {operation!r} has no verified span on {target!r}; "
                    f"the pinned revision exports {live.operation!r}"
                ),
                is_simulated=False,
                collected_at=self._now(),
            )
        start, end = self._window(window_seconds)
        traces = self._jaeger_traces(live, span_name, window_seconds, timeout_seconds)
        spans: list[dict[str, Any]] = []
        error_spans = 0
        for trace in traces:
            for span in trace.get("spans", []):
                if span.get("operationName") != span_name:
                    continue
                failed = _span_failed(span)
                error_spans += 1 if failed else 0
                spans.append(
                    {
                        "name": span.get("operationName"),
                        "status": "ERROR" if failed else "OK",
                        "duration_ms": round(float(span.get("duration", 0)) / 1000.0, 3),
                    }
                )
        return ToolResult(
            tool_name="read_traces",
            target=target,
            succeeded=True,
            value={
                "operation": span_name,
                "operation_alias": live.operation_alias,
                "spans": spans[:20],
                "span_count": len(spans),
                "error_spans": error_spans,
                "source": "jaeger",
            },
            is_simulated=False,
            collected_at=self._now(),
            observation_window_start=start,
            observation_window_end=end,
        )

    def _span_name(self, live: LiveTarget, operation: str) -> str | None:
        """Map the tool contract's short operation name onto the real span name."""
        if operation == live.operation:
            return live.operation
        if operation == live.operation_alias:
            return live.operation
        return None

    def _jaeger_traces(
        self, live: LiveTarget, span_name: str, window_seconds: int, timeout_seconds: float
    ) -> list[dict[str, Any]]:
        client = self._client or httpx.Client(timeout=timeout_seconds, trust_env=False)
        try:
            response = client.get(
                f"{self.jaeger_base_url}/api/traces",
                params={
                    "service": live.service_name,
                    "operation": span_name,
                    "lookback": f"{max(window_seconds, 60)}s",
                    "limit": 20,
                },
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.TimeoutException as exc:
            raise ReadTimeout(f"jaeger query exceeded {timeout_seconds}s") from exc
        except httpx.HTTPError as exc:
            raise LiveEnvironmentUnavailable(f"jaeger query failed: {exc}") from exc
        finally:
            if self._client is None:
                client.close()
        return list(payload.get("data") or [])

    def read_service_health(self, *, target: str, timeout_seconds: float) -> ToolResult:
        """One bounded health probe built from spans and container metrics.

        The definition is fixed and explainable: a target is serving when the RPC
        that represents its dependency is being attempted in the window, those
        attempts are mostly succeeding, and its container is not pinned at high
        CPU. That covers both directions of the demo faults -- a caller-visible
        connection failure and a server-side resource burn -- without inventing a
        health endpoint the pinned revision does not expose.
        """
        live = self._target(target)
        window_seconds = live.probe_window_seconds
        start, end = self._window(window_seconds)
        metric = self._calls_metric(timeout_seconds)
        calls, errors = self._call_counts(
            metric=metric,
            operation=live.operation,
            window_seconds=window_seconds,
            timeout_seconds=timeout_seconds,
        )
        cpu_utilization, cpu_query = self._resource_metric(
            live, "cpu_utilization", window_seconds, timeout_seconds
        )
        error_rate = (errors / calls) if calls > 0 else None
        cpu_below_limit = cpu_utilization is None or cpu_utilization < 0.80
        reachable = calls > 0
        healthy = bool(calls > 0 and (error_rate or 0.0) < 0.05 and cpu_below_limit)
        return ToolResult(
            tool_name="read_service_health",
            target=target,
            succeeded=True,
            value={
                "health": "SERVING" if healthy else "NOT_SERVING",
                "reachable": reachable,
                "healthy": healthy,
                "cpu_utilization": cpu_utilization,
                "error_rate": error_rate,
                "calls_per_second": calls,
                "window_seconds": window_seconds,
                "source": "prometheus",
                "queries": [cpu_query] if cpu_query else [],
            },
            is_simulated=False,
            collected_at=self._now(),
            observation_window_start=start,
            observation_window_end=end,
        )

    # --- dependency attribution ------------------------------------------

    @staticmethod
    def _series_selector(**labels: str) -> str:
        """Build one series selector for the ``/api/v1/series`` schema lookup.

        Prometheus parses ``match[]`` as a *series selector*, so the labels have
        to be wrapped in braces. A bare ``span_name='...'`` is a PromQL parse
        error rather than a selector that matches nothing -- the API answers 400,
        not an empty series list -- which is how the live edge read failed: the
        schema could never be resolved, so every dependency read failed and
        recovery stayed unproven while the remediation had already landed.
        """
        return "{" + ", ".join(f"{name}='{value}'" for name, value in sorted(labels.items())) + "}"

    def _resolve_edge_labels(
        self, *, operation: str, timeout_seconds: float
    ) -> tuple[str, str, str] | str:
        """Resolve (service_label, kind_label, client_value) or return a reason.

        The schema is read from the server rather than assumed. Only two label
        names are needed -- which service emitted the span, and which side of the
        RPC it observed -- and if the exposed schema cannot supply them, the edge
        is not attributable from span metrics and the caller must say so.
        """
        selector = self._series_selector(span_name=operation)
        labels = self.prometheus.series_labels(selector, timeout_seconds)
        if not labels:
            return (
                f"no series matches span_name={operation!r} in Prometheus, so the edge "
                "cannot be attributed"
            )
        service_label = next((name for name in SERVICE_LABEL_CANDIDATES if name in labels), None)
        if service_label is None:
            return (
                f"the series for span_name={operation!r} expose no service label "
                f"(looked for {list(SERVICE_LABEL_CANDIDATES)}; saw {sorted(labels)}), so the "
                "caller cannot be separated from the callee"
            )
        kind_label = next((name for name in SPAN_KIND_LABEL_CANDIDATES if name in labels), None)
        if kind_label is None:
            return (
                f"the series for span_name={operation!r} expose no span-kind label "
                f"(looked for {list(SPAN_KIND_LABEL_CANDIDATES)}; saw {sorted(labels)}), so a "
                "client-side call cannot be told from a server-side one"
            )
        client_value = next(
            (
                value
                for value in self.prometheus.label_values(kind_label, timeout_seconds)
                if CLIENT_SPAN_KIND_TOKEN in value.lower()
            ),
            None,
        )
        if client_value is None:
            return (
                f"the {kind_label!r} label has no client-side value, so no caller-side call "
                "is recorded for this operation"
            )
        return service_label, kind_label, client_value

    @staticmethod
    def _edge_selector(
        metric: str,
        *,
        operation: str,
        service_label: str,
        caller_service: str,
        kind_label: str,
        client_value: str,
        window_seconds: int,
        **labels: str,
    ) -> str:
        parts = [
            f"span_name='{operation}'",
            f"{service_label}='{caller_service}'",
            f"{kind_label}='{client_value}'",
        ]
        parts.extend(f"{name}='{value}'" for name, value in sorted(labels.items()))
        return f"sum(rate({metric}{{{', '.join(parts)}}}[{window_seconds}s]))"

    def read_dependency(
        self, *, target: str, dependency: str, window_seconds: int, timeout_seconds: float
    ) -> ToolResult:
        """The caller's own client-side view of one allow-listed dependency edge.

        This is the read that can see a caller-to-callee failure, and it is
        deliberately not the callee's health: when a caller cannot reach a callee
        the callee emits nothing at all, so every callee-scoped series stays quiet
        and looks healthy. The attribution comes from three facts recorded on the
        evidence -- caller, destination and operation -- and it is only claimed
        when the exposed span-metric schema can supply the caller's own client
        spans. When it cannot, the read succeeds and reports
        ``attributable: false`` with the reason, which reasoning must treat as an
        instrumentation gap rather than as a healthy edge.

        A window with no calls reports no calls. It never reports a zero failure
        share, and a quiet window is not a recovery.
        """
        caller = self._target(target)
        callee = self._target(dependency)
        start, end = self._window(window_seconds)
        operation = dependency_operation(target, dependency)
        if operation is None:
            # The registry refuses unverified edges before dispatch; this is the
            # adapter's own guard for a direct call.
            raise UnknownDemoTarget(
                f"{target} -> {dependency} is not an allow-listed dependency edge"
            )
        span_name = callee.operation
        resolved = self._resolve_edge_labels(operation=span_name, timeout_seconds=timeout_seconds)
        if isinstance(resolved, str):
            return ToolResult(
                tool_name="read_dependency",
                target=target,
                succeeded=True,
                value={
                    "caller": target,
                    "destination": dependency,
                    "operation": operation,
                    "span_kind": "client",
                    "attributable": False,
                    "reason": resolved,
                    "window_seconds": window_seconds,
                    "source": "jaeger+prometheus-schema",
                    "queries": [],
                },
                is_simulated=False,
                collected_at=self._now(),
                observation_window_start=start,
                observation_window_end=end,
            )
        service_label, kind_label, client_value = resolved
        metric = self._calls_metric(timeout_seconds)
        calls_query = self._edge_selector(
            metric,
            operation=span_name,
            service_label=service_label,
            caller_service=caller.service_name,
            kind_label=kind_label,
            client_value=client_value,
            window_seconds=window_seconds,
        )
        errors_query = self._edge_selector(
            metric,
            status_code="STATUS_CODE_ERROR",
            operation=span_name,
            service_label=service_label,
            caller_service=caller.service_name,
            kind_label=kind_label,
            client_value=client_value,
            window_seconds=window_seconds,
        )
        calls = self.prometheus.scalar(calls_query, timeout_seconds) or 0.0
        errors = self.prometheus.scalar(errors_query, timeout_seconds) or 0.0
        return ToolResult(
            tool_name="read_dependency",
            target=target,
            succeeded=True,
            value={
                "caller": target,
                "destination": dependency,
                "operation": operation,
                "span_kind": "client",
                "attributable": True,
                "calls_per_second": calls,
                "errors_per_second": errors,
                "error_rate": (errors / calls) if calls > 0 else None,
                "window_seconds": window_seconds,
                "source": "prometheus-span-metrics",
                "queries": [calls_query, errors_query],
            },
            is_simulated=False,
            collected_at=self._now(),
            observation_window_start=start,
            observation_window_end=end,
        )

    def read_recent_changes(self, *, target: str, timeout_seconds: float) -> ToolResult:
        """Report whether the target's fault flag is currently in its fault position.

        The pinned revision has no deploy feed, so configuration state is the only
        real change signal available. Reading it is deliberate: a fault flag that is
        on *is* a change someone made, and this stays bounded to the verified flag
        ids of the pinned revision.
        """
        live = self._target(target)
        document = self.read_flag_document(timeout_seconds=timeout_seconds)
        flags = document.get("flags", {})
        current = flags.get(live.fault_flag, {}).get("defaultVariant")
        changes = [
            {
                "kind": "feature_flag",
                "flag_id": live.fault_flag,
                "state": current,
                "fault_active": current != FLAG_SAFE_VARIANT.get(live.fault_flag),
            }
        ]
        return ToolResult(
            tool_name="read_recent_changes",
            target=target,
            succeeded=True,
            value={
                "changes": changes,
                "source": "flagd-ui",
                "note": "config state only: the demo has no deploy feed",
            },
            is_simulated=False,
            collected_at=self._now(),
        )

    def read_runbook(self, *, target: str, timeout_seconds: float) -> ToolResult:
        """Local operator text. It is not telemetry and it stays untrusted."""
        text = DEFAULT_RUNBOOKS.get(target, "No runbook found.")
        return ToolResult(
            tool_name="read_runbook",
            target=target,
            succeeded=True,
            value={"text": text, "trusted": False, "source": "local-runbook"},
            is_simulated=False,
            collected_at=self._now(),
        )

    # --- flag document ----------------------------------------------------

    def read_flag_document(self, *, timeout_seconds: float) -> dict[str, Any]:
        payload = self._json(f"{self.flag_base_url}/api/read", timeout_seconds=timeout_seconds)
        flags = payload.get("flags")
        if not isinstance(flags, dict):
            raise LiveEnvironmentUnavailable("flagd-ui read did not return a flag document")
        return {"flags": flags}

    def set_flag_variant(
        self, *, flag_id: str, variant: str, timeout_seconds: float
    ) -> dict[str, Any]:
        """Set one flag variant with a read-modify-write of the whole document.

        flagd-ui replaces the stored document wholesale, so every other flag is
        carried through unchanged and only the single requested key is modified.

        The two validations below and the document read all happen *before* the
        single write, so a failure there is a proved non-execution and is reported
        as :class:`DispatchRefused`. Everything after the request is issued is
        either an acknowledgement or an unknown outcome; nothing in between is
        claimed.
        """
        if flag_id not in FLAG_SAFE_VARIANT:
            raise DispatchRefused(
                f"{flag_id!r} is not a verified flag of the pinned revision; no write was sent"
            )
        document = self.read_flag_document(timeout_seconds=timeout_seconds)
        flags = document["flags"]
        if flag_id not in flags:
            raise DispatchRefused(
                f"flag {flag_id!r} is absent from the live document; no write was sent"
            )
        previous = flags[flag_id].get("defaultVariant")
        flags[flag_id] = {**flags[flag_id], "defaultVariant": variant}
        self._json(
            f"{self.flag_base_url}/api/write",
            method="POST",
            json_body={"data": document},
            timeout_seconds=timeout_seconds,
            timeout_error=DispatchTimeout,
        )
        return {"flag_id": flag_id, "previous_variant": previous, "variant": variant}

    # --- side-effect tool -------------------------------------------------

    def apply_remediation(
        self,
        *,
        idempotency_key: str,
        target: str,
        remediation_id: str,
        flag_id: str,
        timeout_seconds: float,
    ) -> ToolResult:
        try:
            live = self._target(target)
        except UnknownDemoTarget as exc:
            # No verified mapping means no request is built, let alone sent.
            raise DispatchRefused(str(exc)) from exc
        if remediation_id != "disable_feature_flag":
            return ToolResult(
                tool_name="apply_remediation",
                target=target,
                succeeded=False,
                error=f"live mode only implements disable_feature_flag, not {remediation_id!r}",
                is_simulated=False,
                collected_at=self._now(),
            )
        if flag_id != live.fault_flag:
            return ToolResult(
                tool_name="apply_remediation",
                target=target,
                succeeded=False,
                error=f"flag {flag_id!r} is not the fault flag of {target!r}",
                is_simulated=False,
                collected_at=self._now(),
            )
        safe_variant = FLAG_SAFE_VARIANT[flag_id]
        self._applied[idempotency_key] = {
            "target": target,
            "flag_id": flag_id,
            "variant": safe_variant,
        }
        change = self.set_flag_variant(
            flag_id=flag_id, variant=safe_variant, timeout_seconds=timeout_seconds
        )
        return ToolResult(
            tool_name="apply_remediation",
            target=target,
            succeeded=True,
            value={
                **change,
                "remediation_id": remediation_id,
                "applied_at": self._now().isoformat(),
                "source": "flagd-ui",
            },
            is_simulated=False,
            collected_at=self._now(),
        )

    def action_status(self, idempotency_key: str) -> ActionOutcome:
        """Ask the external system whether the desired state is in place.

        This adapter answers a *state* question, not an authorship question. The
        flag-document API says what the flag is set to; it does not say who set it
        or whether an earlier write of ours ever landed. The two directions are
        therefore not symmetric, and the asymmetry is the point:

        * the requested value being **present** is ``CONFIRMED_APPLIED`` - the
          state this action intended is in force, which is what the workflow needs;
        * the requested value being **absent** is ``UNKNOWN``, not
          ``CONFIRMED_NOT_APPLIED``. Something else may have changed the flag after
          a write of ours landed, so "not the value we asked for" is not evidence
          that our request was never applied. Claiming non-execution here would be
          the same unsupported inference the gateway used to make for unclassified
          errors, and it would let an ambiguous action resolve as a definite fail.
        """
        if not self.reconcile_via_external_state:
            return ActionOutcome.UNKNOWN
        applied = self._applied.get(idempotency_key)
        if applied is None:
            return ActionOutcome.UNKNOWN
        try:
            document = self.read_flag_document(timeout_seconds=self._http_timeout)
        except (ReadTimeout, LiveEnvironmentUnavailable):
            return ActionOutcome.UNKNOWN
        current = document.get("flags", {}).get(applied["flag_id"], {}).get("defaultVariant")
        if current == applied["variant"]:
            return ActionOutcome.CONFIRMED_APPLIED
        return ActionOutcome.UNKNOWN


def _span_failed(span: dict[str, Any]) -> bool:
    for tag in span.get("tags") or []:
        key = str(tag.get("key", ""))
        value = tag.get("value")
        if key in {"otel.status_code", "status.code"} and str(value).upper() in {
            "ERROR",
            "STATUS_CODE_ERROR",
        }:
            return True
        if key == "error" and str(value).lower() in {"true", "1"}:
            return True
    return False


__all__ = [
    "CALLS_METRIC_CANDIDATES",
    "FLAG_SAFE_VARIANT",
    "LIVE_TARGETS",
    "DispatchRefused",
    "LiveEnvironment",
    "LiveEnvironmentUnavailable",
    "LiveTarget",
    "PrometheusClient",
]
