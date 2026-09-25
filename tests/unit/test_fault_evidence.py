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
    position_in_window,
    sanitize_span,
    span_kind_of,
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
    assert summary["edge_span"]["span_kind"] == "client"
    # The failing call is a *direct child of the measured attempt*, which is what
    # the summary has to say: "is it a child of whichever span was picked" was a
    # different, and misleading, question.
    assert summary["attempt"]["span_id"] == "root"
    assert summary["edge_span"]["relationship"] == "direct_child"
    assert summary["edge_span"]["hops_from_attempt"] == 1
    assert summary["edge_span"]["direct_child_of_attempt"] is True
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
                status="OK",
                duration=2_000,
            ),
            span(
                "charge-server",
                CHARGE,
                "payment",
                kind="SPAN_KIND_SERVER",
                status="OK",
                duration=1_500,
            ),
        ),
        root_operation=PLACE_ORDER,
        edge_operation=CHARGE,
    )
    assert trace_finding(summary) == "edge_span_present_and_ok"
    # The attempt is the callee's own server span; the edge that is *tied to it*
    # is the caller's client span, while the callee's separate server span (which
    # names no parent in this payload) is reported as a branch of its own rather
    # than silently dropped or preferred for being longer.
    assert summary["attempt"]["service"] == "checkout"
    assert summary["edge_span"]["span_id"] == "charge-client"
    assert summary["edge_span"]["relationship"] == "direct_child"
    assert [(item["span_id"], item["relationship"]) for item in summary["other_edge_spans"]] == [
        ("charge-server", "unrelated_branch")
    ]
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
    assert summary["attempt_found"] is False
    assert summary["spans_seen"] == 0
    assert summary["trace_root"] is None
    assert summary["root_count"] == 0
    assert trace_finding(summary) == "no_attempt_span"


def test_a_span_that_recorded_no_status_is_not_a_success() -> None:
    """Absent is not healthy: an unset status is reported as unset.

    The live extract called an edge span ``status=OK`` on the strength of there
    being no error field, and a reading like that is how an unrecorded failure
    becomes a healthy dependency.
    """
    summary = summarize_trace(
        trace(
            span("root", PLACE_ORDER, "checkout", kind="SPAN_KIND_SERVER"),
            span("charge", CHARGE, "checkout", parent="root", kind="SPAN_KIND_CLIENT"),
        ),
        root_operation=PLACE_ORDER,
        edge_operation=CHARGE,
    )
    assert summary["edge_span"]["status"] == "UNSET"
    assert summary["edge_span"]["status_source"] == "no status signal"
    assert summary["span_status_counts"]["UNSET"] == 2
    assert trace_finding(summary) == "edge_span_present_without_a_status"


def test_a_protocol_status_decides_the_status_when_no_status_code_was_written() -> None:
    """A gRPC or HTTP status is a failure signal even without ``otel.status_code``."""
    summary = summarize_trace(
        trace(
            span("root", PLACE_ORDER, "checkout", kind="SPAN_KIND_SERVER", status="OK"),
            span(
                "charge",
                CHARGE,
                "checkout",
                parent="root",
                kind="SPAN_KIND_CLIENT",
                tags=(("rpc.grpc.status_code", "14"),),
            ),
        ),
        root_operation=PLACE_ORDER,
        edge_operation=CHARGE,
    )
    assert summary["edge_span"]["status"] == "ERROR"
    assert summary["edge_span"]["status_source"] == "rpc.grpc.status_code"
    assert trace_finding(summary) == "edge_span_present_and_failing"


def test_an_exception_event_is_a_failure_signal() -> None:
    summary = summarize_trace(
        trace(
            span("root", PLACE_ORDER, "checkout", kind="SPAN_KIND_SERVER", status="OK"),
            span(
                "charge",
                CHARGE,
                "checkout",
                parent="root",
                kind="SPAN_KIND_CLIENT",
            )
            | {
                "logs": [
                    {
                        "timestamp": 1758762272000000,
                        "fields": [
                            {"key": "exception.type", "value": "Unavailable"},
                            {"key": "exception.message", "value": "connection refused"},
                        ],
                    }
                ]
            },
        ),
        root_operation=PLACE_ORDER,
        edge_operation=CHARGE,
    )
    assert summary["edge_span"]["status"] == "ERROR"
    assert summary["edge_span"]["status_source"] == "exception event"


def test_the_measured_shape_is_resolved_by_ancestry_not_by_operation_name() -> None:
    """A reconstruction of the shape the 2026-09-25 extract reported.

    ``oteldemo.CheckoutService/PlaceOrder`` is emitted twice with the same name -
    once by ``frontend`` as a client span and once by ``checkout`` as a server
    span - under a load generator's HTTP span that is the real root. The summary
    has to name the load generator's span as the root, the checkout server span as
    the attempt, the frontend client span as its ancestor, and the ``Charge`` call
    as a direct child of the attempt rather than as a child of the frontend span.
    """
    summary = summarize_trace(
        {
            "traceID": TRACE_ID,
            "spans": [
                span("lg", "POST", "load-generator", kind="SPAN_KIND_CLIENT", status="ERROR"),
                span(
                    "fpx",
                    "POST",
                    "frontend-proxy",
                    parent="lg",
                    kind="SPAN_KIND_SERVER",
                    status="ERROR",
                ),
                span(
                    "fe-client",
                    PLACE_ORDER,
                    "frontend",
                    parent="fpx",
                    kind="SPAN_KIND_CLIENT",
                    status="OK",
                ),
                span(
                    "ck-server",
                    PLACE_ORDER,
                    "checkout",
                    parent="fe-client",
                    kind="SPAN_KIND_SERVER",
                    status="OK",
                ),
                span(
                    "ck-charge",
                    CHARGE,
                    "checkout",
                    parent="ck-server",
                    kind="SPAN_KIND_CLIENT",
                    status="OK",
                ),
            ],
            "processes": {
                "load-generator": {"serviceName": "load-generator"},
                "frontend-proxy": {"serviceName": "frontend-proxy"},
                "frontend": {"serviceName": "frontend"},
                "checkout": {"serviceName": "checkout"},
            },
        },
        root_operation=PLACE_ORDER,
        edge_operation=CHARGE,
    )
    assert summary["trace_root"]["span_id"] == "lg"
    assert summary["attempt"]["span_id"] == "ck-server"
    assert summary["attempt_basis"] == "the measured operation's own server span"
    assert [item["span_id"] for item in summary["ancestors_of_attempt"]] == [
        "fe-client",
        "fpx",
        "lg",
    ]
    assert summary["edge_span"]["span_id"] == "ck-charge"
    assert summary["edge_span"]["relationship"] == "direct_child"
    assert trace_finding(summary) == "edge_span_present_and_ok_with_failures_elsewhere"


def test_a_named_parent_missing_from_the_payload_leaves_the_ancestry_unresolved() -> None:
    summary = (
        summarize_trace(
            trace(
                span("root", PLACE_ORDER, "checkout", kind="SPAN_KIND_SERVER", status="OK"),
                span("charge", CHARGE, "checkout", parent="root", kind="SPAN_KIND_CLIENT"),
            ),
            root_operation=PLACE_ORDER,
            edge_operation=CHARGE,
        )
        | {}
    )
    trimmed = summarize_trace(
        {
            "traceID": TRACE_ID,
            "spans": [
                span("charge", CHARGE, "checkout", parent="root", kind="SPAN_KIND_CLIENT"),
            ],
            "processes": {"checkout": {"serviceName": "checkout"}},
        },
        root_operation=PLACE_ORDER,
        edge_operation=CHARGE,
    )
    assert summary["missing_parent_ids"] == []
    assert trimmed["attempt_found"] is False
    assert trimmed["missing_parent_ids"] == ["root"]
    assert trace_finding(trimmed) == "no_attempt_span"

    unresolved = summarize_trace(
        {
            "traceID": TRACE_ID,
            "spans": [
                span(
                    "ck-server",
                    PLACE_ORDER,
                    "checkout",
                    parent="missing",
                    kind="SPAN_KIND_SERVER",
                ),
                span("charge", CHARGE, "checkout", parent="ck-server", kind="SPAN_KIND_CLIENT"),
            ],
            "processes": {"checkout": {"serviceName": "checkout"}},
        },
        root_operation=PLACE_ORDER,
        edge_operation=CHARGE,
    )
    assert unresolved["ancestry_truncated"] is True
    assert unresolved["missing_parent_ids"] == ["missing"]
    assert unresolved["edge_span"]["relationship"] == "direct_child"


def test_an_edge_span_whose_ancestry_is_unresolved_is_reported_as_unresolved() -> None:
    summary = summarize_trace(
        {
            "traceID": TRACE_ID,
            "spans": [
                span("root", PLACE_ORDER, "checkout", kind="SPAN_KIND_SERVER", status="OK"),
                span(
                    "charge",
                    CHARGE,
                    "payment",
                    parent="not-in-this-payload",
                    kind="SPAN_KIND_SERVER",
                ),
            ],
            "processes": {
                "checkout": {"serviceName": "checkout"},
                "payment": {"serviceName": "payment"},
            },
        },
        root_operation=PLACE_ORDER,
        edge_operation=CHARGE,
    )
    assert summary["edge_span"]["relationship"] == "missing_parent"
    assert summary["edge_span"]["hops_from_attempt"] is None
    assert trace_finding(summary) == "edge_span_present_with_its_ancestry_unresolved"


def test_a_span_that_crossed_the_restoration_edge_is_positioned_as_crossing_it() -> None:
    """The window is the interval asked for; its edges are injection and restoration."""
    window_start, window_end = 1_790_303_000_000_000, 1_790_303_600_000_000
    root = span(
        "root",
        PLACE_ORDER,
        "checkout",
        kind="SPAN_KIND_SERVER",
        status="OK",
        duration=600 * 1_000_000,
    )
    root["startTime"] = window_start + 100 * 1_000_000
    summary = summarize_trace(
        {
            "traceID": TRACE_ID,
            "spans": [root],
            "processes": {"checkout": {"serviceName": "checkout"}},
        },
        root_operation=PLACE_ORDER,
        edge_operation=CHARGE,
        window_start_us=window_start,
        window_end_us=window_end,
    )
    assert summary["attempt"]["position"] == "crosses_window_end"
    assert summary["span_position_counts"]["crosses_window_end"] == 1


def test_positions_separate_the_four_sides_of_the_window() -> None:
    window_start, window_end = 1_000_000, 2_000_000
    assert (
        position_in_window(0, 500_000, window_start_us=window_start, window_end_us=window_end)
        == "before_window"
    )
    assert (
        position_in_window(
            window_start - 1, 1_000, window_start_us=window_start, window_end_us=window_end
        )
        == "starts_before_window"
    )
    assert (
        position_in_window(
            window_start, 1_000, window_start_us=window_start, window_end_us=window_end
        )
        == "during_window"
    )
    assert (
        position_in_window(
            window_end - 1, 1, window_start_us=window_start, window_end_us=window_end
        )
        == "during_window"
    )
    assert (
        position_in_window(window_end, 1, window_start_us=window_start, window_end_us=window_end)
        == "after_window"
    )
    assert (
        position_in_window(None, 1, window_start_us=window_start, window_end_us=window_end)
        == "unpositionable"
    )


def test_a_measured_attempt_is_the_operation_s_own_server_span_in_the_live_spelling() -> None:
    """The live payload writes ``span.kind = "server"``, not ``SPAN_KIND_SERVER``.

    Captured structure, from the 2026-09-25 11:28:33 extract of trace
    ``290d12a9429910cab4800d4a44b79e43``: the load generator's scenario span is the
    only parentless span, the frontend's *client* span and checkout's *server* span
    share one operation name, and the measured edge is absent because the request
    failed earlier. Reading only the enum spelling found no server span and fell back
    to the longest span carrying the name - the caller's client span - which reported
    the measured attempt as its own child and the whole ancestry one level too high.
    """
    assert span_kind_of({"span.kind": "server"}) == "server"
    assert span_kind_of({"span.kind": "SPAN_KIND_SERVER"}) == "server"
    assert span_kind_of({"span.kind": "Client"}) == "client"
    assert span_kind_of({"span.kind": "SPAN_KIND_UNSPECIFIED"}) == "unspecified"
    assert span_kind_of({}) is None

    summary = summarize_trace(
        {
            "traceID": TRACE_ID,
            "spans": [
                span("lg-root", "user_checkout_multi", "load-generator", kind="internal"),
                span(
                    "lg-post",
                    "POST",
                    "load-generator",
                    parent="lg-root",
                    kind="client",
                    status="ERROR",
                ),
                span(
                    "fpx-server",
                    "POST",
                    "frontend-proxy",
                    parent="lg-post",
                    kind="server",
                    status="ERROR",
                ),
                span(
                    "fe-server",
                    "POST /api/checkout",
                    "frontend",
                    parent="fpx-server",
                    kind="server",
                    status="ERROR",
                ),
                span(
                    "fe-client",
                    PLACE_ORDER,
                    "frontend",
                    parent="fe-server",
                    kind="client",
                    status="ERROR",
                ),
                span(
                    "ck-server",
                    PLACE_ORDER,
                    "checkout",
                    parent="fe-client",
                    kind="server",
                    status="ERROR",
                    description='failed to prepare order: failed to get product #"L9ECAV7KIM"',
                ),
                span(
                    "ck-getproduct",
                    "oteldemo.ProductCatalogService/GetProduct",
                    "checkout",
                    parent="ck-server",
                    kind="client",
                    status="ERROR",
                ),
            ],
            "processes": {
                "load-generator": {"serviceName": "load-generator"},
                "frontend-proxy": {"serviceName": "frontend-proxy"},
                "frontend": {"serviceName": "frontend"},
                "checkout": {"serviceName": "checkout"},
            },
        },
        root_operation=PLACE_ORDER,
        edge_operation=CHARGE,
    )

    assert summary["attempt"]["span_id"] == "ck-server"
    assert summary["attempt"]["service"] == "checkout"
    assert summary["attempt"]["span_kind"] == "server"
    assert summary["attempt_basis"] == "the measured operation's own server span"
    assert [item["span_id"] for item in summary["ancestors_of_attempt"]] == [
        "fe-client",
        "fe-server",
        "fpx-server",
        "lg-post",
        "lg-root",
    ]
    assert summary["trace_root"]["span_id"] == "lg-root"
    assert [item["span_id"] for item in summary["child_spans"]] == ["ck-getproduct"]
    assert summary["edge_span"] is None
    # The request failed before the measured edge, so the finding says exactly that.
    assert trace_finding(summary) == "edge_span_absent_with_a_failure_recorded"
