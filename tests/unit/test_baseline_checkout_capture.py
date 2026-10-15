"""The fault-free baseline capture: what it accepts, and what it refuses.

The gate under test is the whole point of the script, so the fixtures are built to
be *differently wrong* one at a time: a trace that never reached the measured edge,
one whose Payment span hangs off a branch the attempt never called, one whose
Payment span wrote no status at all, one where the catalog call exists only as the
caller's own client span. Each of those must fail, must name itself, and must never
be reported as a verified baseline.

Since 2026-10-15 the gate has a second half, and it is tested the same way: the
capture no longer queries a historical interval, so a trace is only evidence about
the flags if its measured *attempt began* after the observation-start boundary. A
healthy request that was already in flight is refused, a request that appears only
after a later poll is accepted, and the flag premise is the pair of readings that
brackets the observation rather than a single present-day one.

The fixture *shape* follows ``test_fault_evidence``: a Jaeger v1-compatible payload
whose spans carry ``traceID``/``spanID``/``operationName``/``processID``/
``startTime``/``duration`` with ``processes[processID].serviceName``. Service and
operation names are the pinned registry's own (``live_environment.LIVE_TARGETS``
and the demo's compose service names).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from opsaccord.config import REPO_ROOT
from opsaccord.tools.live_environment import FLAG_SAFE_VARIANT

MODULE_PATH = REPO_ROOT / "scripts" / "capture-baseline-checkout.py"
TRACE_ID = "1a2b3c4d5e6f7081"
PLACE_ORDER = "oteldemo.CheckoutService/PlaceOrder"
CHARGE = "oteldemo.PaymentService/Charge"
CART_GET = "oteldemo.CartService/GetCart"
CATALOG_GET = "oteldemo.ProductCatalogService/GetProduct"
SERVICES = ["cart", "checkout", "frontend", "frontend-proxy", "payment", "product-catalog"]
PROCESSES = {
    name: {"serviceName": name}
    for name in (
        "load-generator",
        "frontend-proxy",
        "frontend",
        "checkout",
        "cart",
        "product-catalog",
        "payment",
    )
}

#: The instant the injected clock starts at, and therefore the capture's
#: observation-start boundary. Spans are placed relative to it, never in real time:
#: a fixture that used "now" would pass or fail depending on when the suite ran.
BASE_US = int(datetime.fromisoformat("2026-10-15T09:30:00+00:00").timestamp() * 1_000_000)
SECOND_US = 1_000_000
#: The measured attempt of a fresh fixture begins five seconds after the boundary,
#: so it cannot be answered in the first poll's zero-width window and the test
#: exercises the polling loop rather than bypassing it.
FRESH_US = BASE_US + 5 * SECOND_US
HISTORICAL_US = BASE_US - 30 * SECOND_US
DEADLINE_SECONDS = 30.0
POLL_SECONDS = 10.0


def load_module() -> Any:
    """Import the hyphenated script by path, the way an operator runs it."""
    spec = importlib.util.spec_from_file_location("opsaccord_baseline_checkout", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_MODULE: Any = None


def module() -> Any:
    """The loaded script, once per session: the gate is patched on this object."""
    global _MODULE
    if _MODULE is None:
        _MODULE = load_module()
    return _MODULE


@pytest.fixture(name="capture")
def capture_fixture() -> Any:
    return module()


class Clock:
    """A controllable clock: the deadline is reached without waiting for it."""

    def __init__(self, start_us: int = BASE_US) -> None:
        self.us = start_us

    def now(self) -> int:
        return self.us

    def sleep(self, seconds: float) -> None:
        self.us += int(seconds * 1_000_000)


class CountingClock(Clock):
    """Counts how often the loop looked at the time, so a poll count is checkable."""

    def __init__(self, start_us: int = BASE_US) -> None:
        super().__init__(start_us)
        self.reads = 0

    def now(self) -> int:
        self.reads += 1
        return super().now()


def span(
    span_id: str,
    operation: str,
    process: str,
    *,
    parent: str | None = None,
    kind: str = "internal",
    status: str = "UNSET",
    duration: int = 1000,
    start_us: int = BASE_US,
) -> dict[str, Any]:
    tags: list[dict[str, str]] = [{"key": "span.kind", "value": kind}]
    if status != "UNSET":
        tags.append({"key": "otel.status_code", "value": status})
    return {
        "traceID": TRACE_ID,
        "spanID": span_id,
        "operationName": operation,
        "references": (
            [{"refType": "CHILD_OF", "traceID": TRACE_ID, "spanID": parent}] if parent else []
        ),
        "startTime": start_us,
        "duration": duration,
        "tags": tags,
        "logs": [],
        "processID": process,
    }


def checkout_trace(
    *,
    trace_id: str = TRACE_ID,
    start_us: int = BASE_US,
    checkout_status: str = "OK",
    charge_client: bool = True,
    payment_server: bool = True,
    payment_status: str = "OK",
    catalog_server: bool = True,
) -> dict[str, Any]:
    """One complete, successful checkout: load generator -> frontend -> checkout.

    Every span is a child of the one that called it, which is what makes the
    reconstruction a walk over the payload's own references rather than a guess.
    ``start_us`` moves the *whole* trace, so the measured attempt's own start - the
    one the freshness gate reads - is ``start_us`` too.
    """

    def at(span_id: str, operation: str, process: str, **kwargs: Any) -> dict[str, Any]:
        kwargs.setdefault("duration", 1000)
        return span(span_id, operation, process, start_us=start_us, **kwargs)

    spans = [
        at("lg-root", "user_checkout_multi", "load-generator", duration=90_000),
        at("fpx", "POST", "frontend-proxy", parent="lg-root", kind="server", duration=80_000),
        at(
            "fe-server",
            "POST /api/checkout",
            "frontend",
            parent="fpx",
            kind="server",
            duration=70_000,
        ),
        at(
            "fe-client",
            PLACE_ORDER,
            "frontend",
            parent="fe-server",
            kind="client",
            status="OK",
            duration=60_000,
        ),
        at(
            "ck-server",
            PLACE_ORDER,
            "checkout",
            parent="fe-client",
            kind="server",
            status=checkout_status,
            duration=50_000,
        ),
        at("ck-cart", CART_GET, "checkout", parent="ck-server", kind="client", status="OK"),
        at("cart-server", CART_GET, "cart", parent="ck-cart", kind="server", status="OK"),
        at("ck-catalog", CATALOG_GET, "checkout", parent="ck-server", kind="client", status="OK"),
    ]
    if catalog_server:
        spans.append(
            at(
                "catalog-server",
                CATALOG_GET,
                "product-catalog",
                parent="ck-catalog",
                kind="server",
                status="OK",
            )
        )
    if charge_client:
        spans.append(
            at("ck-charge", CHARGE, "checkout", parent="ck-server", kind="client", status="OK")
        )
    if payment_server:
        spans.append(
            at(
                "pm-server",
                CHARGE,
                "payment",
                parent="ck-charge" if charge_client else "ck-server",
                kind="server",
                status=payment_status,
            )
        )
    return {"traceID": trace_id, "spans": spans, "processes": dict(PROCESSES)}


def reparent(payload: dict[str, Any], span_id: str, parent: str | None) -> None:
    """Move one span onto another parent, as a branch the attempt never called."""
    target = next(item for item in payload["spans"] if item["spanID"] == span_id)
    target["references"] = (
        [{"refType": "CHILD_OF", "traceID": payload["traceID"], "spanID": parent}] if parent else []
    )


def flag_document(**variants: str) -> dict[str, Any]:
    """A safe document, keyed by the pinned revision's own flag list."""
    document = {flag: {"defaultVariant": safe} for flag, safe in FLAG_SAFE_VARIANT.items()}
    for flag, variant in variants.items():
        assert flag in document, f"{flag} is not a flag of the pinned revision"
        document[flag] = {"defaultVariant": variant}
    return {"flags": document}


def flag_state(read: bool = True, reason: str | None = None, **variants: str) -> dict[str, Any]:
    """One flag reading, in the shape ``read_flag_variants`` returns it."""
    values = dict(FLAG_SAFE_VARIANT)
    values.update(variants)
    return {
        "base_url": "http://flags.test/feature",
        "read": read,
        "reason": None if read else (reason or "httpx.ConnectError: unreachable"),
        "variants": values if read else {},
        "safe_variant": dict(FLAG_SAFE_VARIANT),
        "unsafe": (
            [flag for flag, value in values.items() if value != FLAG_SAFE_VARIANT[flag]]
            if read
            else []
        ),
        "observed_at": "2026-10-15T09:30:00+00:00",
    }


def flag_reader(*states: dict[str, Any]) -> Any:
    """A `read_flags` callable: the states in order, the last one repeating."""
    remaining = [dict(state) for state in states]
    assert remaining, "at least one flag reading is needed"

    def read() -> dict[str, Any]:
        return dict(remaining.pop(0)) if len(remaining) > 1 else dict(remaining[0])

    return read


def evaluate(payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    capture = module()
    summary = capture.summarize_trace(
        payload,
        root_operation=PLACE_ORDER,
        edge_operation=CHARGE,
        window_start_us=BASE_US - SECOND_US,
        window_end_us=BASE_US + 60 * SECOND_US,
    )
    return capture.evaluate_summary(summary, **kwargs)


def checks_of(verdict: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {check["name"]: check for check in verdict["checks"]}


# --- the gate, one property at a time ---------------------------------------


def test_a_clean_checkout_passes_every_property() -> None:
    verdict = evaluate(checkout_trace())

    assert verdict["verified"] is True
    assert verdict["blocking"] == []
    assert set(checks_of(verdict)) == {
        "checkout_entry_span",
        "product_catalog_reached",
        "payment_charge_on_path",
        "explicit_success",
    }
    assert "descendant" in checks_of(verdict)["payment_charge_on_path"]["detail"]


def test_a_trace_that_never_reached_the_edge_names_the_missing_edge() -> None:
    verdict = evaluate(checkout_trace(charge_client=False, payment_server=False))

    assert verdict["verified"] is False
    # Two consequences of the one missing span, named separately: the call is not
    # shown to be in the ancestry, and no success signal exists for it.
    assert verdict["blocking"] == ["payment_charge_on_path", "explicit_success"]
    detail = checks_of(verdict)["payment_charge_on_path"]["detail"]
    assert "no span in this payload carries" in detail
    assert "no Charge status can be read" in checks_of(verdict)["explicit_success"]["detail"]
    # The rest of the request still happened; the edge is what is missing.
    assert checks_of(verdict)["checkout_entry_span"]["ok"] is True
    assert checks_of(verdict)["product_catalog_reached"]["ok"] is True


def test_a_charge_server_span_in_an_unrelated_branch_is_not_in_ancestry() -> None:
    payload = checkout_trace()
    payload["spans"].append(span("other-root", "retry_timer", "load-generator", kind="internal"))
    reparent(payload, "pm-server", "other-root")

    verdict = evaluate(payload)

    assert verdict["verified"] is False
    assert verdict["blocking"][0] == "payment_charge_on_path"
    assert "unrelated_branch" in checks_of(verdict)["payment_charge_on_path"]["detail"]


def test_the_gate_would_catch_a_relationship_blind_variant(
    capture: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Falsification: without the relationship requirement the bad trace passes."""
    payload = checkout_trace()
    payload["spans"].append(span("other-root", "retry_timer", "load-generator", kind="internal"))
    reparent(payload, "pm-server", "other-root")

    assert "payment_charge_on_path" in evaluate(payload)["blocking"]

    monkeypatch.setattr(
        capture,
        "IN_PATH_RELATIONSHIPS",
        ("same_span", "direct_child", "descendant", "unrelated_branch"),
    )
    assert evaluate(payload)["verified"] is True


def test_a_span_that_wrote_no_status_is_not_success() -> None:
    verdict = evaluate(checkout_trace(payment_status="UNSET"))

    assert verdict["verified"] is False
    assert verdict["blocking"] == ["explicit_success"]
    detail = checks_of(verdict)["explicit_success"]["detail"]
    assert "status is UNSET" in detail
    # The entry span is OK, so the failure is about the edge and not the request.
    assert checks_of(verdict)["checkout_entry_span"]["ok"] is True
    assert checks_of(verdict)["payment_charge_on_path"]["ok"] is True


def test_an_error_span_on_the_path_is_not_success() -> None:
    payload = checkout_trace()
    cart = next(item for item in payload["spans"] if item["spanID"] == "cart-server")
    cart["tags"].append({"key": "otel.status_code", "value": "ERROR"})

    verdict = evaluate(payload)

    assert verdict["verified"] is False
    assert verdict["blocking"] == ["explicit_success"]
    assert "recorded ERROR" in checks_of(verdict)["explicit_success"]["detail"]


def test_a_caller_client_span_is_not_the_catalog_answering() -> None:
    verdict = evaluate(checkout_trace(catalog_server=False))

    assert verdict["verified"] is False
    assert verdict["blocking"] == ["product_catalog_reached"]
    detail = checks_of(verdict)["product_catalog_reached"]["detail"]
    assert "not that the catalog answered it" in detail


def test_a_client_only_entry_span_is_not_the_callee_completing_the_request() -> None:
    payload = checkout_trace()
    entry = next(item for item in payload["spans"] if item["spanID"] == "ck-server")
    entry["tags"] = [
        {"key": "span.kind", "value": "client"},
        {"key": "otel.status_code", "value": "OK"},
    ]

    verdict = evaluate(payload)

    assert verdict["verified"] is False
    assert verdict["blocking"][0] == "checkout_entry_span"


def test_the_chosen_trace_reports_spans_outside_the_window_as_a_caveat(capture: Any) -> None:
    payload = checkout_trace()
    stale = next(item for item in payload["spans"] if item["spanID"] == "lg-root")
    stale["startTime"] = BASE_US - 30 * SECOND_US

    summary = capture.summarize_trace(
        payload,
        root_operation=PLACE_ORDER,
        edge_operation=CHARGE,
        window_start_us=BASE_US - SECOND_US,
        window_end_us=BASE_US + 60 * SECOND_US,
    )
    verdict = capture.evaluate_summary(summary)

    assert verdict["verified"] is True
    assert any("outside the observed interval" in caveat for caveat in verdict["caveats"])


# --- freshness: an observation is a request that began after the boundary ----


def test_a_fresh_attempt_is_accepted_and_a_historical_one_is_refused(capture: Any) -> None:
    fresh = capture.summarize_trace(
        checkout_trace(start_us=FRESH_US),
        root_operation=PLACE_ORDER,
        edge_operation=CHARGE,
        window_start_us=BASE_US,
        window_end_us=FRESH_US + SECOND_US,
    )
    historical = capture.summarize_trace(
        checkout_trace(start_us=HISTORICAL_US),
        root_operation=PLACE_ORDER,
        edge_operation=CHARGE,
        window_start_us=BASE_US,
        window_end_us=FRESH_US + SECOND_US,
    )

    accepted = capture.freshness_check(fresh, boundary_us=BASE_US)
    refused = capture.freshness_check(historical, boundary_us=BASE_US)

    assert accepted["ok"] is True
    assert "at or after the observation boundary" in accepted["detail"]
    assert refused["ok"] is False
    assert "already in flight when the flags were read" in refused["detail"]
    assert refused["name"] == "attempt_after_boundary"


def test_an_attempt_with_no_start_time_cannot_be_shown_to_be_fresh(capture: Any) -> None:
    """An untimed attempt is unverifiable, which is not the same as fresh."""
    summary = capture.summarize_trace(
        checkout_trace(start_us=FRESH_US),
        root_operation=PLACE_ORDER,
        edge_operation=CHARGE,
    )
    summary["attempt"] = {**summary["attempt"], "started_at_us": None}

    check = capture.freshness_check(summary, boundary_us=BASE_US)

    assert check["ok"] is False
    assert "carries no start time" in check["detail"]


# --- the flag premise: two readings, not continuous observation --------------


def test_an_armed_flag_is_not_a_baseline(capture: Any) -> None:
    check = capture.flag_check(flag_state(paymentUnreachable="on"))

    assert check["ok"] is False
    assert "paymentUnreachable='on'" in check["detail"]


def test_an_unread_flag_document_is_unverified_rather_than_safe(capture: Any) -> None:
    check = capture.flag_check(flag_state(read=False, reason="no flag endpoint in the environment"))

    assert check["ok"] is False
    assert "unverified rather than true" in check["detail"]


def test_a_flag_that_moved_during_the_observation_blocks_the_verdict(capture: Any) -> None:
    check = capture.flag_change_check(flag_state(), flag_state(paymentUnreachable="on"))

    assert check["ok"] is False
    assert check["name"] == "fault_flags_unchanged"
    assert "changed during the observation" in check["detail"]
    assert "paymentUnreachable: 'off' -> 'on'" in check["detail"]


def test_a_flag_document_that_cannot_be_re_read_blocks_the_verdict(capture: Any) -> None:
    check = capture.flag_change_check(flag_state(), flag_state(read=False))

    assert check["ok"] is False
    assert "could not be re-read" in check["detail"]


def test_a_flag_document_that_could_not_be_read_first_blocks_the_verdict(capture: Any) -> None:
    """ "Unchanged" is unanswerable without the earlier reading, so it is not claimed."""
    check = capture.flag_change_check(flag_state(read=False), flag_state())

    assert check["ok"] is False
    assert "nothing to compare" in check["detail"]


def test_unchanged_safe_readings_pass_and_say_only_that(capture: Any) -> None:
    check = capture.flag_change_check(flag_state(), flag_state())

    assert check["ok"] is True
    assert "unchanged" in check["detail"]
    assert "all 6 fault flags" in check["detail"]


def test_the_flag_guarantee_disclaims_continuous_observation(capture: Any) -> None:
    """The wording is load-bearing: a pair of snapshots is not an interval guarantee."""
    guarantee = capture.FLAG_GUARANTEE

    assert "cannot exclude a change that began and ended inside the interval" in guarantee
    assert "not continuous observation" in guarantee
    assert "competing scenario driver" in guarantee


def test_a_competing_driver_blocks_because_it_shares_the_flags(capture: Any) -> None:
    check = capture.driver_check(
        ["4242: python -m opsaccord.cli run-live-scenarios"], observed_at="t"
    )

    assert check["ok"] is False
    assert check["name"] == "no_competing_driver"
    assert "share this demo's feature flags" in check["detail"]

    quiet = capture.driver_check([], observed_at="t")

    assert quiet["ok"] is True
    assert capture.COMPETING_DRIVER_METHOD in quiet["detail"]


# --- the command's own contract ---------------------------------------------


class FakeStack:
    """The three Jaeger routes the capture uses, plus the flagd-ui read."""

    def __init__(
        self,
        *,
        traces: list[dict[str, Any]] | None = None,
        traces_status: int = 200,
        flags: dict[str, Any] | None = None,
        flags_status: int = 200,
        ignore_window: bool = False,
    ) -> None:
        self.traces = list(traces or [])
        self.traces_status = traces_status
        self.flags = flags if flags is not None else flag_document()
        self.flags_status = flags_status
        #: A store that answers with what it holds rather than with what falls in the
        #: window. The real one is bounded and ingest-lagged, and it is precisely a
        #: payload that *arrives during* the observation but whose request began
        #: before it that the freshness gate has to catch, so that case is modelled
        #: directly instead of being excluded by the fake's own filter.
        self.ignore_window = ignore_window

    def client(self) -> httpx.Client:
        return httpx.Client(
            transport=httpx.MockTransport(self.handler),
            base_url="http://jaeger.test/jaeger/ui",
        )

    def matching(self, params: dict[str, str]) -> list[dict[str, Any]]:
        """``/api/traces`` as the API documents it: service, and the window."""
        limit = int(params["limit"])
        if self.ignore_window:
            return self.traces[:limit]
        start, end = int(params["start"]), int(params["end"])
        matched = [
            trace
            for trace in self.traces
            if any(
                (trace.get("processes") or {})
                .get(str(item.get("processID")), {})
                .get("serviceName")
                == params["service"]
                for item in trace.get("spans") or []
            )
            and start <= min(int(item["startTime"]) for item in trace["spans"]) <= end
        ]
        return matched[:limit]

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/api/services"):
            return httpx.Response(200, json={"data": SERVICES})
        if path.endswith("/api/operations"):
            return httpx.Response(200, json={"data": [{"name": PLACE_ORDER, "spanKind": "server"}]})
        if path.endswith("/api/traces"):
            if self.traces_status != 200:
                return httpx.Response(self.traces_status, json={"data": None})
            return httpx.Response(200, json={"data": self.matching(dict(request.url.params))})
        if path.endswith("/api/read"):
            if self.flags_status != 200:
                return httpx.Response(self.flags_status, json={"flags": None})
            return httpx.Response(200, json=self.flags)
        raise AssertionError(f"unexpected request to {path}")


def observe(
    capture: Any,
    *,
    stack: FakeStack,
    reader: Any = None,
    drivers: list[str] | None = None,
    clock: Clock | None = None,
    deadline_seconds: float = DEADLINE_SECONDS,
    poll_interval_seconds: float = POLL_SECONDS,
) -> tuple[dict[str, Any], Clock]:
    """Run `capture.capture` against a fake store and a controllable clock."""
    clock = clock or Clock()
    client = stack.client()
    if reader is None:
        # No injected reader means the flag document comes over HTTP from the fake
        # flagd-ui, exactly as `main` reads it - so `FakeStack(flags=...)` decides the
        # premise in these tests rather than a Python literal standing in for it.
        def reader() -> dict[str, Any]:
            return capture.read_flag_variants(
                "http://flags.test/feature", client=client, timeout_seconds=1.0
            )

    document = capture.capture(
        capture.JaegerClient("http://jaeger.test/jaeger/ui", client=client),
        limit=20,
        read_flags=reader,
        competing_driver=lambda: list(drivers or []),
        deadline_seconds=deadline_seconds,
        poll_interval_seconds=poll_interval_seconds,
        now_us=clock.now,
        sleep=clock.sleep,
    )
    return document, clock


def test_a_fresh_linked_checkout_is_accepted_and_recorded_with_its_raw_spans(
    capture: Any,
) -> None:
    document, clock = observe(capture, stack=FakeStack(traces=[checkout_trace(start_us=FRESH_US)]))

    assert document["verdict"] == "baseline_verified"
    assert document["exit_status"] == 0
    assert document["blocking"] == []
    assert document["chosen_trace_id"] == TRACE_ID
    assert document["observation_start_us"] == BASE_US
    # It took a poll to see it: the first query's window ended before the attempt.
    assert document["polling"]["requests_completed"] >= 2
    assert document["polling"]["deadline_exceeded"] is False
    assert clock.us >= FRESH_US

    chosen = next(item for item in document["candidates"] if item["trace_id"] == TRACE_ID)
    assert chosen["fresh"] is True
    assert chosen["started_at_us"] == FRESH_US
    assert [check["name"] for check in chosen["checks"]] == [
        "attempt_after_boundary",
        "fault_flags_safe_before",
        "fault_flags_unchanged",
        "no_competing_driver",
        "checkout_entry_span",
        "product_catalog_reached",
        "payment_charge_on_path",
        "explicit_success",
    ]

    # The raw span fields, not only a verdict: ids, parents, services and timing.
    spans = document["evidence"]["spans"]
    charge = next(item for item in spans if item["span_id"] == "pm-server")
    assert charge["parent_span_id"] == "ck-charge"
    assert charge["service"] == "payment"
    assert charge["status"] == "OK"
    assert charge["status_source"] == "otel.status_code"
    assert charge["relationship"] == "descendant"
    assert charge["hops_from_attempt"] == 2
    assert isinstance(charge["started_at_us"], int)
    assert charge["ends_at_us"] > charge["started_at_us"]
    # Both readings are kept, each with its own instant, and the guarantee is stated.
    assert document["flags_before"]["read"] is True
    assert document["flags_after"]["read"] is True
    assert document["flag_guarantee"] == capture.FLAG_GUARANTEE


def test_a_healthy_request_that_began_before_the_observation_is_refused(capture: Any) -> None:
    """The whole point of the boundary: an old, healthy trace is not fresh evidence.

    The store is answering with what it *holds* here, which is how a pre-boundary
    request arrives during an observation at all; every other property of the trace
    is perfect, so freshness is the only thing that can reject it.
    """
    stack = FakeStack(traces=[checkout_trace(start_us=HISTORICAL_US)], ignore_window=True)

    document, _ = observe(capture, stack=stack)

    assert document["verdict"] == "not_verified_no_fresh_attempt"
    assert document["exit_status"] == 1
    assert document["chosen_trace_id"] == TRACE_ID
    assert "attempt_after_boundary" in document["blocking"]
    assert "attempt_after_boundary" not in [
        check["name"] for check in document["candidates"][0]["checks"] if check["ok"]
    ]
    # Every other property really did hold; only the boundary refused it.
    assert document["blocking"] == ["attempt_after_boundary"]
    assert document["polling"]["deadline_exceeded"] is True


def test_no_traffic_at_all_ends_at_the_deadline_with_an_honest_result(capture: Any) -> None:
    clock = CountingClock()

    document, _ = observe(capture, stack=FakeStack(traces=[]), clock=clock)

    assert document["verdict"] == "not_verified_no_trace_in_interval"
    assert document["exit_status"] == 1
    assert document["evidence"] is None
    assert document["candidates"] == []
    assert document["deadline_exceeded"] is True
    assert document["polling"]["requests_completed"] >= 3
    assert document["polling"]["payloads_observed"] == 0
    # It waited rather than concluding from the first empty answer.
    assert clock.us >= BASE_US + int(DEADLINE_SECONDS * SECOND_US)
    assert any("an empty query is not health" in item for item in document["limits"])


def test_traffic_that_appears_only_on_a_later_poll_is_accepted(capture: Any) -> None:
    """Ingestion lags the request: "not yet" must not be read as "never"."""
    stack = FakeStack(traces=[checkout_trace(start_us=FRESH_US + 8 * SECOND_US)])

    document, _ = observe(capture, stack=stack, poll_interval_seconds=POLL_SECONDS)

    assert document["verdict"] == "baseline_verified"
    assert document["polling"]["requests_completed"] >= 2


def test_an_unreachable_edge_is_named_in_the_extract(capture: Any) -> None:
    stack = FakeStack(
        traces=[checkout_trace(start_us=FRESH_US, charge_client=False, payment_server=False)]
    )

    document, _ = observe(capture, stack=stack)

    assert document["verdict"] == "not_verified"
    assert document["exit_status"] == 1
    assert document["blocking"][0] == "payment_charge_on_path"
    assert document["evidence"] is not None


def test_an_ambiguous_status_cannot_become_success(capture: Any) -> None:
    stack = FakeStack(traces=[checkout_trace(start_us=FRESH_US, payment_status="UNSET")])

    document, _ = observe(capture, stack=stack)

    assert document["verdict"] == "not_verified"
    assert document["blocking"] == ["explicit_success"]


def test_an_armed_flag_blocks_the_verdict(capture: Any) -> None:
    stack = FakeStack(
        traces=[checkout_trace(start_us=FRESH_US)], flags=flag_document(paymentUnreachable="on")
    )

    document, _ = observe(capture, stack=stack)

    assert document["verdict"] == "not_verified"
    # Armed before the boundary *and* still armed after it: each reading is reported,
    # because "it was armed" and "it was still armed at the end" are both true.
    assert document["blocking"] == ["fault_flags_safe_before", "fault_flags_unchanged"]


def test_a_flag_that_moves_while_the_capture_waits_blocks_the_verdict(capture: Any) -> None:
    """The second reading is what catches a flag armed between the two."""
    stack = FakeStack(traces=[checkout_trace(start_us=FRESH_US)])
    reader = flag_reader(flag_state(), flag_state(paymentUnreachable="on"))

    document, _ = observe(capture, stack=stack, reader=reader)

    assert document["verdict"] == "not_verified"
    assert document["blocking"] == ["fault_flags_unchanged"]


def test_a_competing_driver_blocks_the_verdict(capture: Any) -> None:
    stack = FakeStack(traces=[checkout_trace(start_us=FRESH_US)])

    document, _ = observe(
        capture, stack=stack, drivers=["7331: python -m opsaccord.cli run-live-scenarios"]
    )

    assert document["verdict"] == "not_verified"
    assert document["blocking"] == ["no_competing_driver"]
    assert document["competing_driver"]["found"] == [
        "7331: python -m opsaccord.cli run-live-scenarios"
    ]


def test_a_failed_query_is_recorded_rather_than_reported_as_absent(capture: Any) -> None:
    document, _ = observe(capture, stack=FakeStack(traces_status=500))

    assert document["verdict"] == "not_verified_query_failed"
    assert document["exit_status"] == 1
    assert "traces_for_checkout" in document["failures"]
    assert document["traces_retrieved"] == 0
    assert document["polling"]["requests_failed"] >= 1
    assert document["polling"]["requests_completed"] == 0


def test_the_extract_is_written_before_the_rendered_log(
    capture: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The sanitized spans survive even if everything after them does."""
    stack = FakeStack(traces=[checkout_trace(start_us=FRESH_US)])
    out = tmp_path / "capture.json"
    log = tmp_path / "capture.log"
    client = stack.client()
    # One clock, shared by both seams: a sleeping clock that is not the reading clock
    # would never reach the deadline, and the test would hang rather than fail.
    clock = Clock()
    monkeypatch.setattr(capture.httpx, "Client", lambda **_: client)
    monkeypatch.setattr(capture, "_epoch_micros", clock.now)
    monkeypatch.setattr(capture, "_sleep", clock.sleep)
    monkeypatch.setattr(capture, "competing_drivers", list)
    monkeypatch.setenv("OPSACCORD_JAEGER_BASE_URL", "http://jaeger.test/jaeger/ui")
    monkeypatch.setenv("OPSACCORD_FLAG_CONTROL_BASE_URL", "http://flags.test/feature")

    status = capture.main(["--out", str(out), "--log", str(log)])

    assert status == 0
    extract = json.loads(out.read_text())
    assert extract["kind"] == "opsaccord.baseline-checkout-capture"
    assert extract["verdict"] == "baseline_verified"
    assert extract["evidence"]["spans"]
    assert extract["observation_start_us"] == BASE_US
    assert "verdict: baseline_verified" in log.read_text()
    assert "observation start:" in log.read_text()


def test_the_command_refuses_to_start_without_an_endpoint(
    capture: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("OPSACCORD_JAEGER_BASE_URL", raising=False)

    assert capture.main([]) == 3
    assert "refusing to start" in capsys.readouterr().out


@pytest.mark.parametrize(
    "argv",
    [
        ["--limit", "0"],
        ["--limit", "1000"],
        ["--deadline-seconds", "0"],
        ["--poll-interval-seconds", "0"],
    ],
)
def test_the_command_rejects_arguments_that_would_unbound_the_observation(
    capture: Any, argv: list[str]
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        capture.main(argv)

    assert excinfo.value.code == 2
