"""The trace-path exporter: what it asks Jaeger, and what an empty answer means.

The fixtures here are labelled rather than guessed, following
``test_fault_evidence``'s convention:

* the *shape* of every fixture - a ``data`` list of traces whose spans carry
  ``traceID``/``spanID``/``operationName``/``processID``/``startTime``/``duration``
  in microseconds with ``processes[processID].serviceName`` - is the
  Jaeger v1-compatible query API shape the demo's own telemetry tests consume
  (``test/telemetry/test_traces.py``, ``test/telemetry/test_traces_edges.py``);
* the *service names* are measured: ``checkout`` and ``payment`` appear in the
  ``services_visible`` list of the 2026-09-25 capture and in the demo's own
  signal matrix;
* the *operation names* are modelled, not measured. No run has yet returned a
  span whose live ``operationName`` is known, so the fixtures use the pinned
  registry's expected name and a ``POST /`` form the pinned semconv transform
  could plausibly produce. Nothing here asserts which one the live stack stores:
  the sequence under test exists to measure that instead of assuming it.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from opsaccord.config import REPO_ROOT
from opsaccord.tools.live_environment import JaegerClient

MODULE_PATH = REPO_ROOT / "scripts" / "diagnose-payment-trace-path.py"
PLACE_ORDER_EXPECTED = "oteldemo.CheckoutService/PlaceOrder"
PLACE_ORDER_ALTERNATE = "POST /oteldemo.CheckoutService/PlaceOrder"
CHARGE = "oteldemo.PaymentService/Charge"
SERVICES = ["ad", "cart", "checkout", "frontend", "payment"]
ADVERTISED = sorted([PLACE_ORDER_ALTERNATE, "oteldemo.CheckoutService/ListOrders"])
SECOND = 1_000_000
#: A fixed pair of intervals, in microseconds, well apart from each other.
HISTORICAL = (1_790_300_000 * SECOND, 1_790_300_600 * SECOND)
RECENT = (1_790_310_000 * SECOND, 1_790_313_600 * SECOND)


def load_module() -> Any:
    """Import the hyphenated script by path, the way an operator runs it."""
    spec = importlib.util.spec_from_file_location("opsaccord_trace_path_export", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # ``dataclasses`` resolves annotations through ``sys.modules``, so the module
    # has to be registered before it is executed.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(name="export")
def export_fixture() -> Any:
    return load_module()


def iso_of(us: int) -> str:
    """The exporter's own rendering of a microsecond timestamp, for comparison."""
    return datetime.fromtimestamp(us / 1e6, tz=UTC).isoformat(timespec="seconds")


def iso_to_micros(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1_000_000)


def place_order_trace(
    trace_id: str,
    *,
    start_us: int,
    root_operation: str = PLACE_ORDER_EXPECTED,
    charge_status: str = "OK",
    charge_present: bool = True,
    root_status: str = "ERROR",
) -> dict[str, Any]:
    """One checkout ``PlaceOrder`` trace with its ``Charge`` client span under it."""
    root = {
        "traceID": trace_id,
        "spanID": f"{trace_id}-root",
        "operationName": root_operation,
        "processID": "checkout",
        "startTime": start_us,
        "duration": 5_000,
        "tags": [{"key": "otel.status_code", "value": root_status}],
    }
    spans = [root]
    if charge_present:
        spans.append(
            {
                "traceID": trace_id,
                "spanID": f"{trace_id}-charge",
                "operationName": CHARGE,
                "processID": "checkout",
                "startTime": start_us + 100,
                "duration": 300,
                "references": [{"refType": "CHILD_OF", "spanID": root["spanID"]}],
                "tags": [{"key": "otel.status_code", "value": charge_status}],
            }
        )
    return {
        "traceID": trace_id,
        "spans": spans,
        "processes": {"checkout": {"serviceName": "checkout"}},
    }


class FakeJaeger:
    """The three query routes the probe uses, with per-route behaviour.

    ``/api/traces`` matches the way the real API documents it: a trace comes back
    only when it holds a span from the requested service and, if one was given,
    from that service under the requested operation name. That is what lets a
    test reproduce "the filter is wrong" and "the store holds nothing" as two
    different situations instead of asserting the difference.
    """

    def __init__(
        self,
        *,
        traces: list[dict[str, Any]] | None = None,
        operations: list[str] | None = None,
        status: int = 200,
        traces_status: int = 200,
        traces_without_data: bool = False,
    ) -> None:
        self.traces = list(traces or [])
        self.operations = [PLACE_ORDER_EXPECTED] if operations is None else list(operations)
        self.status = status
        self.traces_status = traces_status
        self.traces_without_data = traces_without_data
        self.trace_requests: list[dict[str, Any]] = []

    def client(self) -> JaegerClient:
        return JaegerClient("http://jaeger.test/jaeger/ui", client=self._http_client())

    def _http_client(self) -> httpx.Client:
        return httpx.Client(
            transport=httpx.MockTransport(self.handler),
            base_url="http://jaeger.test/jaeger/ui",
        )

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if self.status != 200:
            return httpx.Response(self.status, json={"data": None})
        if path.endswith("/api/services"):
            return httpx.Response(200, json={"data": SERVICES})
        if path.endswith("/api/operations"):
            return httpx.Response(200, json={"data": self.operations})
        if path.endswith("/api/traces"):
            params = dict(request.url.params)
            self.trace_requests.append(params)
            if self.traces_status != 200:
                return httpx.Response(self.traces_status, json={"data": None})
            if self.traces_without_data:
                return httpx.Response(200, json={"total": 0})
            return httpx.Response(200, json={"data": self._matching(params)})
        raise AssertionError(f"unexpected request to {path}")

    def _matching(self, params: dict[str, str]) -> list[dict[str, Any]]:
        service = params["service"]
        operation = params.get("operation")
        start, end, limit = int(params["start"]), int(params["end"]), int(params["limit"])
        matched = [
            trace
            for trace in self.traces
            if self._has_span(trace, service, operation) and start <= self._started(trace) <= end
        ]
        return matched[:limit]

    @staticmethod
    def _has_span(trace: dict[str, Any], service: str, operation: str | None) -> bool:
        for span in trace.get("spans") or []:
            process = (trace.get("processes") or {}).get(str(span.get("processID"))) or {}
            if process.get("serviceName") != service:
                continue
            if operation is None or span.get("operationName") == operation:
                return True
        return False

    @staticmethod
    def _started(trace: dict[str, Any]) -> int:
        return min(int(span["startTime"]) for span in trace["spans"])


def request_for(export: Any, **overrides: Any) -> Any:
    fields: dict[str, Any] = {
        "window_start_us": HISTORICAL[0],
        "window_end_us": HISTORICAL[1],
        "recent_start_us": RECENT[0],
        "recent_end_us": RECENT[1],
        "limit": 40,
    }
    fields.update(overrides)
    return export_module_request(export, fields)


def export_module_request(export: Any, fields: dict[str, Any]) -> Any:
    return export.ProbeRequest(**fields)


def probe(export: Any, fake: FakeJaeger, **overrides: Any) -> dict[str, Any]:
    return export.run_probe(fake.client(), request_for(export, **overrides))


# --- the request the exporter makes ------------------------------------------


def test_the_interval_is_requested_with_explicit_microsecond_bounds(export: Any) -> None:
    fake = FakeJaeger(traces=[place_order_trace("t1", start_us=HISTORICAL[0] + 5)])

    result = probe(export, fake)

    historical = [
        call for call in fake.trace_requests if call.get("operation") == PLACE_ORDER_EXPECTED
    ]
    assert historical, fake.trace_requests
    asked = historical[0]
    assert asked["start"] == str(HISTORICAL[0])
    assert asked["end"] == str(HISTORICAL[1])
    assert asked["service"] == "checkout"
    assert "lookback" not in asked
    stage = result["stages"]["historical"]
    assert stage["requested_window"]["start"] == iso_of(HISTORICAL[0])
    assert stage["requested_window"]["end"] == iso_of(HISTORICAL[1])
    assert stage["count"] == 1
    assert stage["trace_ids"] == ["t1"]


def test_the_service_and_operation_asked_for_come_from_the_runtime_registry(
    export: Any,
) -> None:
    fake = FakeJaeger(traces=[place_order_trace("t1", start_us=HISTORICAL[0])])

    result = probe(export, fake)

    assert result["operation_analysed"] == PLACE_ORDER_EXPECTED
    assert result["stages"]["entry_operation"]["expected"] == PLACE_ORDER_EXPECTED
    assert result["stages"]["operations_for_service"]["service"] == "checkout"
    assert all(call["service"] == "checkout" for call in fake.trace_requests)


def test_the_advertised_operation_names_are_recorded_verbatim(export: Any) -> None:
    fake = FakeJaeger(operations=[PLACE_ORDER_ALTERNATE, "oteldemo.CheckoutService/ListOrders"])

    result = probe(export, fake)

    stage = result["stages"]["operations_for_service"]
    assert stage["names"] == ADVERTISED
    assert stage["advertises_expected"] is False
    assert stage["names_containing_alias"] == [PLACE_ORDER_ALTERNATE]


# --- an operation filter that does not match what the store holds ------------


def test_an_unexpected_operation_name_is_taken_from_what_a_trace_actually_shows(
    export: Any,
) -> None:
    """A wrong filter must be discoverable, not silently reported as "no traces"."""
    fake = FakeJaeger(
        traces=[
            place_order_trace(
                "recent-1", start_us=RECENT[0] + 30, root_operation=PLACE_ORDER_ALTERNATE
            ),
            place_order_trace("t1", start_us=HISTORICAL[0], root_operation=PLACE_ORDER_ALTERNATE),
        ],
        operations=[PLACE_ORDER_ALTERNATE],
    )

    result = probe(export, fake)

    assert result["stages"]["recent_unfiltered"]["caller_operations_seen"] == sorted(
        [PLACE_ORDER_ALTERNATE, CHARGE]
    )
    assert result["operation_analysed"] == PLACE_ORDER_ALTERNATE
    assert "observed" in result["stages"]["entry_operation"]["basis"]
    assert result["stages"]["historical"]["count"] == 1


def test_the_expected_name_is_marked_unverified_when_nothing_confirms_it(export: Any) -> None:
    fake = FakeJaeger(operations=["oteldemo.CheckoutService/ListOrders"])

    result = probe(export, fake)

    assert result["stages"]["entry_operation"]["basis"].startswith("unverified")
    assert result["stages"]["historical"]["count"] == 0


# --- the bounded retrieval check --------------------------------------------


def test_a_known_trace_is_retrieved_again_inside_its_own_window(export: Any) -> None:
    fake = FakeJaeger(traces=[place_order_trace("recent-1", start_us=RECENT[0] + 30)])

    result = probe(export, fake)

    check = result["stages"]["bounded_retrieval_check"]
    assert check["anchor_trace_id"] == "recent-1"
    assert check["retrieved"] is True
    assert check["trace_ids"] == ["recent-1"]
    assert result["stages"]["historical"]["count"] == 0


def test_an_empty_interval_beside_a_working_query_is_not_called_eviction(export: Any) -> None:
    fake = FakeJaeger(traces=[place_order_trace("recent-1", start_us=RECENT[0] + 30)])

    result = probe(export, fake)
    reason = export.empty_result_reason(result["stages"], result["failures"])

    assert "evict" not in reason.lower()
    assert "not measured here" in reason
    assert result["stages"]["bounded_retrieval_check"]["retrieved"] is True


def test_an_unproven_retrieval_leaves_both_causes_open(export: Any) -> None:
    fake = FakeJaeger(traces=[place_order_trace("recent-1", start_us=RECENT[0] + 30)])
    # The store stops answering the bounded re-query, so nothing is demonstrated.
    original = fake.handler

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api/traces") and request.url.params.get("operation"):
            fake.trace_requests.append(dict(request.url.params))
            return httpx.Response(200, json={"data": []})
        return original(request)

    fake.handler = handler  # type: ignore[method-assign]

    result = probe(export, fake)
    reason = export.empty_result_reason(result["stages"], result["failures"])

    assert result["stages"]["bounded_retrieval_check"]["retrieved"] is False
    assert "no retrieval was demonstrated" in reason


# --- empty answers versus API errors ----------------------------------------


def test_an_api_error_is_a_recorded_failure_not_an_empty_store(export: Any) -> None:
    fake = FakeJaeger(status=500)

    result = probe(export, fake)

    assert set(result["failures"]) >= {"services", "operations_for_service", "historical"}
    assert "historical" not in result["stages"]
    assert result["traces"] == []
    reason = export.empty_result_reason(result["stages"], result["failures"])
    assert "could not be queried" in reason


def test_a_reply_without_a_data_list_is_not_read_as_empty(export: Any) -> None:
    fake = FakeJaeger(traces_without_data=True)

    result = probe(export, fake)

    assert result["failures"]["recent_unfiltered"].startswith("LiveEnvironmentUnavailable")
    assert result["failures"]["historical"].startswith("LiveEnvironmentUnavailable")
    # The stage has a failure and no result: an unanswered query is not a
    # zero-count answer.
    assert "recent_unfiltered" not in result["stages"]
    assert "historical" not in result["stages"]
    assert "could not be queried" in export.empty_result_reason(
        result["stages"], result["failures"]
    )


def test_a_failed_stage_records_a_failure_and_no_result(export: Any) -> None:
    """One failing route must not read as "that route answered nothing"."""
    fake = FakeJaeger(traces=[place_order_trace("t1", start_us=HISTORICAL[0])], traces_status=503)

    result = probe(export, fake)

    # The unfiltered replay only runs beside a *successful* empty answer: a failed
    # query is already interpretable, and re-asking it adds no information.
    assert set(result["failures"]) == {"recent_unfiltered", "historical"}
    assert "recent_unfiltered" not in result["stages"]
    assert "historical" not in result["stages"]
    # Discovery stages still answered and are still recorded.
    assert result["stages"]["services"]["caller_service_visible"] is True
    assert result["stages"]["operations_for_service"]["names"] == [PLACE_ORDER_EXPECTED]
    assert "could not be queried" in export.empty_result_reason(
        result["stages"], result["failures"]
    )


# --- caps, provenance and classification ------------------------------------


def test_a_full_page_is_recorded_as_capped_rather_than_a_census(export: Any) -> None:
    fake = FakeJaeger(
        traces=[
            place_order_trace(f"t{index}", start_us=HISTORICAL[0] + index * 100)
            for index in range(3)
        ]
    )

    result = probe(export, fake, limit=2)

    stage = result["stages"]["historical"]
    assert stage["count"] == 2
    assert stage["truncated"] is True
    assert len(stage["trace_ids"]) == 2


def test_each_stage_records_the_range_it_actually_observed(export: Any) -> None:
    fake = FakeJaeger(
        traces=[
            place_order_trace("t1", start_us=HISTORICAL[0] + 60 * SECOND),
            place_order_trace("t2", start_us=HISTORICAL[0] + 120 * SECOND),
        ]
    )

    result = probe(export, fake)

    stage = result["stages"]["historical"]
    assert stage["observed_start"] == iso_of(HISTORICAL[0] + 60 * SECOND)
    assert stage["observed_end"] == iso_of(HISTORICAL[0] + 120 * SECOND + 5_000)
    assert stage["requested_window"]["start"] == iso_of(HISTORICAL[0])


def test_a_summarised_trace_keeps_its_identifier_and_finding(export: Any) -> None:
    fake = FakeJaeger(traces=[place_order_trace("t1", start_us=HISTORICAL[0], charge_status="OK")])

    result = probe(export, fake)

    summary = result["traces"][0]
    assert summary["trace_id"] == "t1"
    assert summary["attempt_found"] is True
    # The fixture's own root span records ERROR and its Charge span records OK:
    # both facts are reported, and the finding says so instead of calling the
    # trace successful on the strength of one child span.
    assert summary["finding"] == "edge_span_present_and_ok_with_failures_elsewhere"
    assert summary["has_error_spans"] is True
    assert summary["edge_span"]["relationship"] == "direct_child"
    assert summary["edge_span"]["operation"] == CHARGE


def test_the_unfiltered_replay_only_happens_when_the_filter_found_nothing(
    export: Any,
) -> None:
    empty = FakeJaeger(traces=[place_order_trace("recent-1", start_us=RECENT[0] + 30)])
    full = FakeJaeger(traces=[place_order_trace("t1", start_us=HISTORICAL[0])])

    empty_result = probe(export, empty)
    full_result = probe(export, full)

    assert empty_result["stages"]["historical_unfiltered"]["count"] == 0
    assert "historical_unfiltered" not in full_result["stages"]


# --- choosing the operation, in isolation -----------------------------------


def test_choose_entry_operation_prefers_evidence_over_expectation(export: Any) -> None:
    choose = export.choose_entry_operation

    assert (
        choose(
            expected=PLACE_ORDER_EXPECTED,
            advertised=[],
            observed=[PLACE_ORDER_EXPECTED],
        )[1]
        == "expected operation observed in a returned trace"
    )
    chosen, basis = choose(
        expected=PLACE_ORDER_EXPECTED,
        advertised=[PLACE_ORDER_ALTERNATE],
        observed=[PLACE_ORDER_ALTERNATE],
    )
    assert chosen == PLACE_ORDER_ALTERNATE
    assert "single observed operation" in basis
    assert (
        choose(
            expected=PLACE_ORDER_EXPECTED,
            advertised=[PLACE_ORDER_EXPECTED],
            observed=[],
        )[1]
        == "expected operation advertised for the service, not observed"
    )
    assert choose(expected=PLACE_ORDER_EXPECTED, advertised=[], observed=[])[1].startswith(
        "unverified"
    )


def test_choose_entry_operation_reports_ambiguity_rather_than_guessing(export: Any) -> None:
    chosen, basis = export.choose_entry_operation(
        expected=PLACE_ORDER_EXPECTED,
        advertised=[],
        observed=[
            "POST /oteldemo.CheckoutService/PlaceOrder",
            "oteldemo.CheckoutService/PlaceOrder/retry",
        ],
    )
    assert basis.startswith("ambiguous")
    assert chosen in {
        "POST /oteldemo.CheckoutService/PlaceOrder",
        "oteldemo.CheckoutService/PlaceOrder/retry",
    }


# --- the command's own contract ---------------------------------------------


def test_the_command_refuses_to_start_without_an_endpoint(
    export: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("OPSACCORD_JAEGER_BASE_URL", raising=False)

    assert export.main([]) == 3
    assert "refusing to start" in capsys.readouterr().out


def test_the_command_rejects_an_out_of_range_limit(export: Any) -> None:
    with pytest.raises(SystemExit) as excinfo:
        export.main(["--limit", "0"])
    assert excinfo.value.code == 2


def test_the_extract_is_written_with_its_stages_and_evidence(
    export: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end through ``main``: one file, the stages, and the verdict."""
    inside = iso_to_micros("2026-09-25T02:27:40Z")
    fake = FakeJaeger(traces=[place_order_trace("t1", start_us=inside)])
    out = tmp_path / "extract.json"
    http_client = fake._http_client()
    monkeypatch.setattr(export.httpx, "Client", lambda **_: http_client)
    monkeypatch.setenv("OPSACCORD_JAEGER_BASE_URL", "http://jaeger.test/jaeger/ui")

    status = export.main(
        [
            "--start",
            "2026-09-25T02:26:40Z",
            "--end",
            "2026-09-25T02:36:40Z",
            "--out",
            str(out),
        ]
    )

    assert status == 0
    extract = json.loads(out.read_text())
    assert extract["exit_status"] == 0
    assert extract["failures"] == {}
    assert extract["counts"] == {"traces": 1, "with_error_spans": 1, "without_error_spans": 0}
    assert extract["services_visible"] == sorted(SERVICES)
    assert extract["evidence"]["query_completed"] is True
    assert extract["evidence"]["traces_retrieved"] == 1
    assert extract["evidence"]["request_path_reconstructed"] is True
    assert extract["evidence"]["sufficiency"] == "sufficient_for_path_review"
    # Retrieving a trace never licenses a claim about the dependency or recovery.
    assert extract["evidence"]["sufficient_for"]["dependency_attribution"]["sufficient"] is False
    assert extract["evidence"]["sufficient_for"]["recovery"]["sufficient"] is False
    assert extract["stages"]["services"]["caller_service_visible"] is True


# --- the bounds, the counts and the ladder ----------------------------------


def test_a_stage_reports_the_latest_span_end_its_traces_reached(export: Any) -> None:
    """One span's start plus another span's duration is not an instant in the trace.

    The interval a stage reports has to be built per span: the previous version
    paired the latest *start* with the longest *duration*, which for a trace with a
    long early span and a short late one reported an end well past either.
    """
    start = HISTORICAL[0]
    trace_ = place_order_trace("t1", start_us=start)
    trace_["spans"][0]["duration"] = 600 * SECOND  # ends exactly on the window's end
    trace_["spans"][1]["startTime"] = start + 300 * SECOND
    trace_["spans"][1]["duration"] = 1

    result = probe(export, FakeJaeger(traces=[trace_]))

    stage = result["stages"]["historical"]
    assert stage["observed_start"] == iso_of(start)
    assert stage["observed_end"] == iso_of(start + 600 * SECOND)
    # The mixed-up arithmetic would have reported start + 900 s, outside the window.
    assert stage["observed_end"] != iso_of(start + 900 * SECOND)


def test_a_trace_counts_as_error_bearing_when_any_span_failed(export: Any) -> None:
    trace_ = place_order_trace("t1", start_us=HISTORICAL[0], charge_status="OK", root_status="OK")
    trace_["spans"].append(
        {
            "traceID": "t1",
            "spanID": "t1-lg",
            "operationName": "POST",
            "processID": "load-generator",
            "startTime": HISTORICAL[0] + 10,
            "duration": 20,
            "tags": [{"key": "otel.status_code", "value": "ERROR"}],
        }
    )
    trace_["processes"]["load-generator"] = {"serviceName": "load-generator"}

    result = probe(export, FakeJaeger(traces=[trace_]))

    summary = result["traces"][0]
    assert summary["edge_span"]["status"] == "OK"
    assert summary["has_error_spans"] is True
    assert summary["finding"] == "edge_span_present_and_ok_with_failures_elsewhere"


def test_spans_that_cross_the_restoration_edge_are_positioned_in_the_extract(
    export: Any,
) -> None:
    """The window asked for is the fault interval, so its edges are injection and restoration."""
    start = HISTORICAL[0]
    trace_ = place_order_trace("t1", start_us=start)
    trace_["spans"][0]["duration"] = 900 * SECOND  # runs past the window's end

    result = probe(export, FakeJaeger(traces=[trace_]))

    summary = result["traces"][0]
    assert summary["attempt"]["position"] == "crosses_window_end"
    assert summary["span_position_counts"]["crosses_window_end"] == 1
    described = export.describe(summary, summary["finding"])
    assert "position=crosses_window_end" in described
    assert "(otel.status_code)" in described
    assert "relationship=direct_child" in described


def test_evidence_says_the_path_was_not_reconstructed(export: Any) -> None:
    """A retrieved trace that misses the edge is not a reconstructed request path."""
    fake = FakeJaeger(
        traces=[place_order_trace("t1", start_us=HISTORICAL[0], charge_present=False)]
    )

    result = probe(export, fake)
    evidence = export.evidence_ladder(
        stages=result["stages"],
        failures=result["failures"],
        summaries=result["traces"],
        empty_reason="",
    )

    assert evidence["traces_retrieved"] == 1
    assert evidence["request_path_reconstructed"] is False
    assert evidence["sufficiency"] == "not_sufficient"
    assert evidence["sufficient_for"]["request_path_review"]["sufficient"] is False
    assert "not reconstructed" in evidence["sufficient_for"]["request_path_review"]["reason"]


def test_a_capped_page_is_reported_beside_every_claim_it_limits(export: Any) -> None:
    fake = FakeJaeger(
        traces=[
            place_order_trace(f"t{index}", start_us=HISTORICAL[0] + index * 100)
            for index in range(3)
        ]
    )

    result = probe(export, fake, limit=2)
    evidence = export.evidence_ladder(
        stages=result["stages"],
        failures=result["failures"],
        summaries=result["traces"],
        empty_reason="",
    )

    assert evidence["capped"] is True
    assert "capped" in evidence["reason"]
    assert "capped" in evidence["sufficient_for"]["request_path_review"]["reason"]
