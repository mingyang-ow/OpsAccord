"""The vocabulary a diagnostic capture reports in, and the traces it summarizes.

The fixtures here are modelled on what the pinned stack actually emits, and each
one names where it comes from:

* the reading fixtures reproduce the *measured* shapes - the 2026-09-24 run's
  generic caller failure beside a clean Charge edge, and the 2026-09-25 capture's
  ordinary traffic while the flag was armed;
* the trace fixtures reproduce the pinned source's own structure: a checkout
  ``PlaceOrder`` server span whose children are its client calls, one of which is
  ``oteldemo.PaymentService/Charge`` on ``checkout`` (client) and, when it
  completed, a matching server span on ``payment``.

They are labelled rather than guessed: no fixture asserts a shape the stack has
not been observed or read to produce.
"""

from __future__ import annotations

from typing import Any

from opsaccord.tools.fault_evidence import (
    ATTRIBUTABLE_FAILURE,
    EVIDENCE_CLASSES,
    GENERIC_CALLER_FAILURE_ONLY,
    NO_USABLE_OBSERVATION,
    TRAFFIC_WITHOUT_FAILURE,
    classify_reading,
    sanitize_span,
    summarize,
    summarize_trace,
    trace_finding,
)

TRACE_ID = "4f1a9c1b0c6c1d2e"
PLACE_ORDER = "oteldemo.CheckoutService/PlaceOrder"
CHARGE = "oteldemo.PaymentService/Charge"


def reading(calls: float, share: float | None) -> dict[str, Any]:
    return {"calls_per_second": calls, "error_rate": share}


def series(calls: float, share: float) -> dict[str, Any]:
    return {"calls_per_second": calls, "error_share": share}


# --- classification ----------------------------------------------------------


def test_the_attributed_edge_is_the_only_thing_that_arms_a_premise() -> None:
    assert (
        classify_reading(
            client=series(0.02, 1.0),
            parent=series(0.02, 1.0),
            dependency={"attributable": True, **reading(0.02, 1.0)},
        )
        == ATTRIBUTABLE_FAILURE
    )


def test_the_measured_generic_caller_failure_is_not_attribution() -> None:
    """The 2026-09-24 and 2026-09-25 shape: checkout fails, the Charge edge is clean."""
    assert (
        classify_reading(
            client=series(0.0, 0.0),
            parent=series(0.0167, 1.0),
            dependency={"attributable": True, **reading(0.0, None)},
        )
        == GENERIC_CALLER_FAILURE_ONLY
    )


def test_a_failing_client_series_carries_the_reading_when_the_tool_cannot_resolve() -> None:
    """The grouped series is scoped by the labels attribution needs, so it can."""
    assert (
        classify_reading(
            client=series(0.05, 0.8),
            parent=series(0.05, 0.8),
            dependency=None,
        )
        == ATTRIBUTABLE_FAILURE
    )


def test_ordinary_traffic_on_the_edge_is_not_health_and_not_failure() -> None:
    assert (
        classify_reading(
            client=series(0.0333, 0.0),
            parent=series(0.0333, 0.0),
            dependency={"attributable": True, **reading(0.0333, 0.0)},
        )
        == TRAFFIC_WITHOUT_FAILURE
    )


def test_no_traffic_and_unreadable_readings_are_the_same_answer() -> None:
    assert (
        classify_reading(client=series(0.0, 0.0), parent=series(0.0, 0.0), dependency=None)
        == NO_USABLE_OBSERVATION
    )
    assert (
        classify_reading(
            client=series(0.05, 1.0),
            parent=series(0.05, 1.0),
            dependency={"attributable": True, **reading(0.05, 1.0)},
            readable=False,
        )
        == NO_USABLE_OBSERVATION
    )


def test_a_failure_share_without_traffic_is_never_a_fault() -> None:
    """The Calibration-2 rule: a share over zero calls is arithmetic, not evidence."""
    assert (
        classify_reading(
            client=series(0.0, 1.0),
            parent=series(0.0167, 0.0),
            dependency={"attributable": True, **reading(0.0, 1.0)},
        )
        == TRAFFIC_WITHOUT_FAILURE
    )


def test_a_generic_failure_beside_a_failing_edge_is_attributable() -> None:
    assert (
        classify_reading(
            client=series(0.02, 0.9),
            parent=series(0.02, 0.9),
            dependency={"attributable": True, **reading(0.02, 0.9)},
        )
        == ATTRIBUTABLE_FAILURE
    )


# --- aggregation -------------------------------------------------------------


def test_the_commonest_reading_does_not_win_the_verdict() -> None:
    """The measured capture: 12 pre-visibility polls, 9 quiet, 9 generic at the end."""
    readings = (
        [NO_USABLE_OBSERVATION] * 12
        + [TRAFFIC_WITHOUT_FAILURE] * 9
        + [GENERIC_CALLER_FAILURE_ONLY] * 9
    )
    summary = summarize(readings)
    assert summary.strongest == GENERIC_CALLER_FAILURE_ONLY
    assert summary.last == GENERIC_CALLER_FAILURE_ONLY
    assert summary.total == 30
    assert summary.attributable_observed is False
    assert summary.as_dict()["classes"] == {
        ATTRIBUTABLE_FAILURE: 0,
        GENERIC_CALLER_FAILURE_ONLY: 9,
        TRAFFIC_WITHOUT_FAILURE: 9,
        NO_USABLE_OBSERVATION: 12,
    }


def test_an_attributable_reading_anywhere_is_visible_as_one_reading() -> None:
    summary = summarize([NO_USABLE_OBSERVATION] * 20 + [ATTRIBUTABLE_FAILURE])
    assert summary.strongest == ATTRIBUTABLE_FAILURE
    assert summary.attributable_observed is True
    assert summary.counts[ATTRIBUTABLE_FAILURE] == 1


def test_an_empty_capture_is_no_usable_observation() -> None:
    summary = summarize([])
    assert summary.strongest == NO_USABLE_OBSERVATION
    assert summary.last == NO_USABLE_OBSERVATION
    assert summary.total == 0
    assert summary.as_dict()["classes"] == {name: 0 for name in EVIDENCE_CLASSES}


# --- retained traces ---------------------------------------------------------


def span(
    span_id: str,
    operation: str,
    process: str,
    *,
    parent: str | None = None,
    kind: str = "SPAN_KIND_INTERNAL",
    status: str = "UNSET",
    duration: int = 1000,
    description: str | None = None,
    tags: tuple[tuple[str, str], ...] = (),
) -> dict[str, Any]:
    attributes: list[dict[str, str]] = [{"key": "span.kind", "value": kind}]
    if status != "UNSET":
        attributes.append({"key": "otel.status_code", "value": status})
    if description is not None:
        attributes.append({"key": "otel.status_description", "value": description})
    attributes.extend({"key": key, "value": value} for key, value in tags)
    return {
        "traceID": TRACE_ID,
        "spanID": span_id,
        "operationName": operation,
        "references": (
            [{"refType": "CHILD_OF", "traceID": TRACE_ID, "spanID": parent}] if parent else []
        ),
        "startTime": 1758762271000000,
        "duration": duration,
        "tags": attributes,
        "logs": [],
        "processID": process,
    }


def trace(*spans: dict[str, Any]) -> dict[str, Any]:
    return {
        "traceID": TRACE_ID,
        "spans": list(spans),
        "processes": {
            "checkout": {"serviceName": "checkout"},
            "payment": {"serviceName": "payment"},
        },
    }


def test_a_failure_with_no_edge_span_says_which_error_it_recorded() -> None:
    """Hypothesis under test: the Charge attempt failed before a span was created."""
    message = (
        "could not charge the card: rpc error: code = Unavailable desc = name resolver "
        "error: produced zero addresses"
    )
    summary = summarize_trace(
        trace(
            span(
                "root",
                PLACE_ORDER,
                "checkout",
                kind="SPAN_KIND_SERVER",
                status="ERROR",
                duration=4_000,
                description=message,
            ),
            span("cart", "oteldemo.CartService/GetCart", "checkout", parent="root", kind="CLIENT"),
            span(
                "convert",
                "oteldemo.CurrencyService/Convert",
                "checkout",
                parent="root",
                kind="CLIENT",
            ),
        ),
        root_operation=PLACE_ORDER,
        edge_operation=CHARGE,
    )
    assert summary["edge_span"] is None
    assert summary["failure_message"] == message
    assert trace_finding(summary) == "edge_span_absent_with_a_failure_recorded"
    assert [child["operation"] for child in summary["child_spans"]] == [
        "oteldemo.CartService/GetCart",
        "oteldemo.CurrencyService/Convert",
    ]


def test_a_failing_edge_span_names_the_service_that_emitted_it() -> None:
    summary = summarize_trace(
        trace(
            span("root", PLACE_ORDER, "checkout", kind="SPAN_KIND_SERVER", status="ERROR"),
            span(
                "charge",
                CHARGE,
                "checkout",
                parent="root",
                kind="SPAN_KIND_CLIENT",
                status="ERROR",
                description="rpc error: code = Unavailable desc = connection refused",
            ),
        ),
        root_operation=PLACE_ORDER,
        edge_operation=CHARGE,
    )
    assert summary["edge_span"]["service"] == "checkout"
    assert summary["edge_span"]["span_kind"] == "SPAN_KIND_CLIENT"
    assert summary["edge_span"]["child_of_root"] is True
    assert trace_finding(summary) == "edge_span_present_and_failing"


def test_a_successful_trace_shows_the_edge_both_sides() -> None:
    summary = summarize_trace(
        trace(
            span("root", PLACE_ORDER, "checkout", kind="SPAN_KIND_SERVER", duration=9_000),
            span(
                "charge-client",
                CHARGE,
                "checkout",
                parent="root",
                kind="SPAN_KIND_CLIENT",
                duration=2_000,
            ),
            span("charge-server", CHARGE, "payment", kind="SPAN_KIND_SERVER", duration=1_500),
        ),
        root_operation=PLACE_ORDER,
        edge_operation=CHARGE,
    )
    assert trace_finding(summary) == "edge_span_present_and_ok"
    assert summary["edge_span"]["service"] == "checkout"
    assert {child["service"] for child in summary["child_spans"]} == {"checkout"}


def test_a_span_is_sanitized_before_it_is_written_down() -> None:
    record = sanitize_span(
        span(
            "charge",
            CHARGE,
            "checkout",
            kind="SPAN_KIND_CLIENT",
            tags=(
                ("user.email", "someone@example.com"),
                ("demo.payment.card_number", "4432 8000 1234 5678"),
                ("rpc.service", "oteldemo.PaymentService"),
                ("url.full", "http://example.test/?token=secret"),
                ("uninteresting", "dropped"),
            ),
            description="declined card 4432800012345678",
        )
    )
    assert record["attributes"]["rpc.service"] == "oteldemo.PaymentService"
    assert record["attributes"]["span.kind"] == "SPAN_KIND_CLIENT"
    assert record["attributes"]["otel.status_description"] == "declined card <redacted>"
    assert "user.email" not in record["attributes"]
    assert "demo.payment.card_number" not in record["attributes"]
    assert "url.full" not in record["attributes"]
    assert "uninteresting" not in record["attributes"]
    assert "4432 8000 1234 5678" not in str(record)


def test_a_span_free_trace_is_reported_rather_than_summarized() -> None:
    summary = summarize_trace(
        {"traceID": TRACE_ID, "spans": []}, root_operation=PLACE_ORDER, edge_operation=CHARGE
    )
    assert summary["root_found"] is False
    assert trace_finding(summary) == "no_root_span"
