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
from opsaccord.tools.contracts import DispatchTimeout, ReadTimeout, UnknownDemoTarget
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
        """
        if flag_id not in FLAG_SAFE_VARIANT:
            raise UnknownDemoTarget(f"{flag_id!r} is not a verified flag of the pinned revision")
        document = self.read_flag_document(timeout_seconds=timeout_seconds)
        flags = document["flags"]
        if flag_id not in flags:
            raise LiveEnvironmentUnavailable(f"flag {flag_id!r} is absent from the live document")
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
        live = self._target(target)
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

        This adapter answers a state question, not an authorship question: it
        reports whether the flag carries the value this action asked for. That is
        the honest limit of a flag-document API, and it is enough to resolve an
        ambiguous dispatch, because the alternative -- assuming the write either
        landed or did not -- is exactly the guess the controller must not make.
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
        return ActionOutcome.CONFIRMED_NOT_APPLIED


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
    "LiveEnvironment",
    "LiveEnvironmentUnavailable",
    "LiveTarget",
    "PrometheusClient",
]
