"""The runtime probe: what it asks Prometheus, and what an empty answer means.

The fixtures are shaped like the Prometheus HTTP API the demo's own adapter already
consumes (``test_live_adapter`` drives the same ``PrometheusClient``), and the
metric names are deliberately *both* shapes this stack can emit - the classic
``go_memstats_*`` families and the OTLP runtime instrumentation's
``go_memory_*`` - because no run has yet established which ones the store holds.
That uncertainty is the reason the script discovers names instead of naming them,
and these tests pin that it stays discovery-driven.

The selector assertions are the load-bearing ones. An earlier pass lost a live run
to a metric request that was refused with HTTP 400 because its selector had no
braces, so every query this script makes must be a braced series selector with the
service label inside it.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from opsaccord.config import REPO_ROOT
from opsaccord.tools.live_environment import LiveEnvironmentUnavailable, PrometheusClient

MODULE_PATH = REPO_ROOT / "scripts" / "diagnose-service-runtime.py"

ADVERTISED = [
    "go_gc_duration_seconds",
    "go_goroutines",
    "go_memstats_alloc_bytes",
    "go_memory_used",
    "process_cpu_seconds_total",
    "process_resident_memory_bytes",
    "http_server_duration_bucket",
    "rpc_server_duration_bucket",
    "traces_span_metrics_calls_total",
]

SERVICES = {"checkout", "frontend", "payment", "product-catalog"}

# value per metric name; anything not listed has no series in the store, which is
# how a service whose export path is broken appears.
VALUES = {
    "go_gc_duration_seconds": 3.5,
    "go_goroutines": 12.0,
    "process_cpu_seconds_total": 41.25,
}


def _module() -> Any:
    spec = importlib.util.spec_from_file_location("opsaccord_service_runtime", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class StubPrometheus:
    """A Prometheus that answers discovery, then the metrics it was given."""

    def __init__(self, *, services: set[str] | None = None, status: int = 200) -> None:
        self.services = SERVICES if services is None else services
        self.status = status
        self.queries: list[str] = []

    def client(self) -> PrometheusClient:
        return PrometheusClient("http://prometheus.test", client=self._http_client())

    def _http_client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))

    @staticmethod
    def _selector(params: dict[str, str]) -> str:
        query = params["query"]
        assert query.startswith("sum(") and query.endswith(")"), query
        return query[4:-1]

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.status != 200:
            return httpx.Response(self.status, json={"status": "error", "error": "nope"})
        path = request.url.path
        if path.endswith("/api/v1/label/__name__/values"):
            return httpx.Response(200, json={"status": "success", "data": ADVERTISED})
        if path.endswith("/api/v1/label/service_name/values"):
            return httpx.Response(200, json={"status": "success", "data": sorted(self.services)})
        if path.endswith("/api/v1/query"):
            selector = self._selector(dict(request.url.params))
            self.queries.append(selector)
            # The selector is a braced series selector: reject anything else the
            # way Prometheus itself would, so an unbraced form fails the test
            # instead of quietly answering.
            if not selector.endswith("}") or "{" not in selector:
                return httpx.Response(
                    400, json={"status": "error", "error": 'parse error: unexpected "="'}
                )
            name = selector.split("{", 1)[0]
            service = selector.split('service_name="', 1)[1].split('"', 1)[0]
            value = VALUES.get(name)
            if value is None or service not in self.services:
                return httpx.Response(200, json={"status": "success", "data": {"result": []}})
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {
                        "result": [{"metric": {"service_name": service}, "value": [1, str(value)]}]
                    },
                },
            )
        raise AssertionError(f"unexpected request to {path}")


def test_discovery_keeps_runtime_families_and_drops_application_metrics() -> None:
    module = _module()

    names = module.runtime_metric_names(set(ADVERTISED))

    assert "go_gc_duration_seconds" in names
    assert "process_cpu_seconds_total" in names
    assert "go_memory_used" in names
    assert "http_server_duration_bucket" not in names
    assert "traces_span_metrics_calls_total" not in names
    assert names == sorted(names)


def test_discovery_is_bounded() -> None:
    module = _module()

    names = module.runtime_metric_names({f"go_gc_metric_{i}" for i in range(200)}, limit=5)

    assert len(names) == 5


def test_every_query_is_a_braced_selector_scoped_to_the_service() -> None:
    module = _module()
    stub = StubPrometheus()

    document = module.collect(stub.client(), ("product-catalog",), 5.0)

    assert stub.queries, "no metric query was made"
    assert all('service_name="product-catalog"' in query for query in stub.queries)
    reading = document["services"][0]
    assert reading["service"] == "product-catalog"
    assert reading["failures"] == []
    assert document["service_name_label_values"] == sorted(SERVICES)


def test_a_metric_with_no_series_for_the_service_is_reported_absent() -> None:
    module = _module()
    stub = StubPrometheus()

    reading = module.collect(stub.client(), ("product-catalog",), 5.0)["services"][0]

    assert reading["values"]["go_goroutines"] == 12.0
    # Advertised by the store, published by nobody in this stub: absent, not zero.
    assert reading["metrics"]["go_memory_used"] is None
    assert "go_memory_used" not in reading["values"]


def test_a_service_that_reports_nothing_is_visible_as_such() -> None:
    module = _module()
    stub = StubPrometheus(services={"checkout"})

    document = module.collect(stub.client(), ("product-catalog",), 5.0)

    assert "product-catalog" not in document["service_name_label_values"]
    assert document["services"][0]["values"] == {}


def test_a_refused_query_is_a_failure_not_a_silent_zero() -> None:
    module = _module()
    stub = StubPrometheus(status=500)

    with pytest.raises(LiveEnvironmentUnavailable):
        module.collect(stub.client(), ("product-catalog",), 5.0)


def test_a_service_name_that_would_break_the_selector_is_refused() -> None:
    module = _module()
    stub = StubPrometheus()

    with pytest.raises(ValueError):
        module.read_service_runtime(
            stub.client(), 'product-catalog"} or x="1', ["go_goroutines"], 5.0
        )


def test_main_exits_three_without_a_prometheus_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _module()
    monkeypatch.delenv("OPSACCORD_PROMETHEUS_BASE_URL", raising=False)

    assert module.main([]) == 3


def test_main_exits_two_for_a_non_positive_timeout() -> None:
    module = _module()

    assert module.main(["--prometheus", "http://prometheus.test", "--timeout", "0"]) == 2


def test_main_reports_an_unreachable_prometheus_as_three(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _module()

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    failing = httpx.Client(transport=httpx.MockTransport(refuse))
    monkeypatch.setattr(
        module, "PrometheusClient", lambda url: PrometheusClient(url, client=failing)
    )
    try:
        assert module.main(["--prometheus", "http://prometheus.test"]) == 3
    finally:
        failing.close()


def test_main_writes_the_document_it_printed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    stub = StubPrometheus()
    monkeypatch.setattr(module, "PrometheusClient", lambda url: stub.client())
    out = tmp_path / "runtime.json"

    code = module.main(["--prometheus", "http://prometheus.test", "--out", str(out)])

    assert code == 0
    printed = capsys.readouterr().out
    assert "product-catalog" in printed
    written = out.read_text(encoding="utf-8")
    assert '"service": "product-catalog"' in written
