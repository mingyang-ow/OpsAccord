"""The fault-free baseline capture: what it accepts, and what it refuses.

The gate under test is the whole point of the script, so the fixtures are built to
be *differently wrong* one at a time: a trace that never reached the measured edge,
one whose Payment span hangs off a branch the attempt never called, one whose
Payment span wrote no status at all, one where the catalog call exists only as the
caller's own client span. Each of those must fail, must name itself, and must never
be reported as a verified baseline.

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
#: One fixed instant, inside the window the end-to-end tests query.
WINDOW_START = "2026-10-15T09:29:59Z"
WINDOW_END = "2026-10-15T09:31:00Z"


def micros(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1_000_000)


BASE_US = micros("2026-10-15T09:30:00Z")
SECOND_US = 1_000_000


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
    """
    spans = [
        span(
            "lg-root", "user_checkout_multi", "load-generator", start_us=start_us, duration=90_000
        ),
        span("fpx", "POST", "frontend-proxy", parent="lg-root", kind="server", duration=80_000),
        span(
            "fe-server",
            "POST /api/checkout",
            "frontend",
            parent="fpx",
            kind="server",
            duration=70_000,
        ),
        span(
            "fe-client",
            PLACE_ORDER,
            "frontend",
            parent="fe-server",
            kind="client",
            status="OK",
            duration=60_000,
        ),
        span(
            "ck-server",
            PLACE_ORDER,
            "checkout",
            parent="fe-client",
            kind="server",
            status=checkout_status,
            duration=50_000,
        ),
        span("ck-cart", CART_GET, "checkout", parent="ck-server", kind="client", status="OK"),
        span("cart-server", CART_GET, "cart", parent="ck-cart", kind="server", status="OK"),
        span("ck-catalog", CATALOG_GET, "checkout", parent="ck-server", kind="client", status="OK"),
    ]
    if catalog_server:
        spans.append(
            span(
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
            span("ck-charge", CHARGE, "checkout", parent="ck-server", kind="client", status="OK")
        )
    if payment_server:
        spans.append(
            span(
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
    assert any("outside the requested interval" in caveat for caveat in verdict["caveats"])


# --- the flag document is part of the premise -------------------------------


def test_an_armed_flag_is_not_a_baseline(capture: Any) -> None:
    check = capture.flag_check(
        {
            "read": True,
            "variants": {"paymentUnreachable": "on", "cartFailure": "off"},
            "unsafe": ["paymentUnreachable"],
        }
    )

    assert check["ok"] is False
    assert "paymentUnreachable='on'" in check["detail"]


def test_an_unread_flag_document_is_unverified_rather_than_safe(capture: Any) -> None:
    check = capture.flag_check({"read": False, "reason": "no flag endpoint in the environment"})

    assert check["ok"] is False
    assert "unverified rather than true" in check["detail"]


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
    ) -> None:
        self.traces = list(traces or [])
        self.traces_status = traces_status
        self.flags = flags if flags is not None else flag_document()
        self.flags_status = flags_status

    def client(self) -> httpx.Client:
        return httpx.Client(
            transport=httpx.MockTransport(self.handler),
            base_url="http://jaeger.test/jaeger/ui",
        )

    def matching(self, params: dict[str, str]) -> list[dict[str, Any]]:
        """``/api/traces`` as the API documents it: service, and the window."""
        start, end = int(params["start"]), int(params["end"])
        limit = int(params["limit"])
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


def run_capture(
    capture: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    stack: FakeStack,
    extra: list[str] | None = None,
) -> tuple[int, Path, Path]:
    out = tmp_path / "capture.json"
    log = tmp_path / "capture.log"
    # Built before the patch: the script reaches httpx through the same module
    # object the patch replaces, so constructing it afterwards would recurse.
    client = stack.client()
    monkeypatch.setattr(capture.httpx, "Client", lambda **_: client)
    monkeypatch.setenv("OPSACCORD_JAEGER_BASE_URL", "http://jaeger.test/jaeger/ui")
    monkeypatch.setenv("OPSACCORD_FLAG_CONTROL_BASE_URL", "http://flags.test/feature")
    argv = [
        "--start",
        WINDOW_START,
        "--end",
        WINDOW_END,
        "--out",
        str(out),
        "--log",
        str(log),
        *(extra or []),
    ]
    return capture.main(argv), out, log


def test_the_command_refuses_to_start_without_an_endpoint(
    capture: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("OPSACCORD_JAEGER_BASE_URL", raising=False)

    assert capture.main([]) == 3
    assert "refusing to start" in capsys.readouterr().out


def test_the_command_rejects_an_out_of_range_limit(capture: Any) -> None:
    with pytest.raises(SystemExit) as excinfo:
        capture.main(["--limit", "0"])

    assert excinfo.value.code == 2


def test_a_verified_baseline_is_written_with_its_raw_span_evidence(
    capture: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stack = FakeStack(traces=[checkout_trace()])

    status, out, log = run_capture(capture, monkeypatch, tmp_path, stack=stack)

    assert status == 0
    extract = json.loads(out.read_text())
    assert extract["kind"] == "opsaccord.baseline-checkout-capture"
    assert extract["verdict"] == "baseline_verified"
    assert extract["exit_status"] == 0
    assert extract["failures"] == {}
    assert extract["blocking"] == []
    assert extract["chosen_trace_id"] == TRACE_ID
    # The raw span fields, not only a verdict: ids, parents, services and timing.
    spans = extract["evidence"]["spans"]
    assert [item["span_id"] for item in spans][:2] == ["lg-root", "fpx"]
    charge = next(item for item in spans if item["span_id"] == "pm-server")
    assert charge["parent_span_id"] == "ck-charge"
    assert charge["service"] == "payment"
    assert charge["status"] == "OK"
    assert charge["status_source"] == "otel.status_code"
    assert charge["relationship"] == "descendant"
    assert charge["hops_from_attempt"] == 2
    assert isinstance(charge["started_at_us"], int)
    assert charge["ends_at_us"] > charge["started_at_us"]
    assert extract["flags"]["read"] is True

    text = log.read_text()
    assert "verdict: baseline_verified" in text
    assert "pm-server <- ck-charge payment/" + CHARGE in text


def test_an_empty_interval_is_not_health(
    capture: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    status, out, _ = run_capture(capture, monkeypatch, tmp_path, stack=FakeStack(traces=[]))

    assert status == 1
    extract = json.loads(out.read_text())
    assert extract["verdict"] == "not_verified_no_trace_in_interval"
    assert extract["evidence"] is None
    assert extract["candidates"] == []
    assert any("an empty query is not health" in item for item in extract["limits"])


def test_an_unreachable_edge_is_named_in_the_extract(
    capture: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stack = FakeStack(traces=[checkout_trace(charge_client=False, payment_server=False)])

    status, out, _ = run_capture(capture, monkeypatch, tmp_path, stack=stack)

    assert status == 1
    extract = json.loads(out.read_text())
    assert extract["verdict"] == "not_verified"
    assert extract["blocking"][0] == "payment_charge_on_path"
    assert extract["evidence"] is not None


def test_an_armed_flag_blocks_the_verdict(
    capture: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stack = FakeStack(traces=[checkout_trace()], flags=flag_document(paymentUnreachable="on"))

    status, out, _ = run_capture(capture, monkeypatch, tmp_path, stack=stack)

    assert status == 1
    extract = json.loads(out.read_text())
    assert extract["verdict"] == "not_verified"
    assert extract["blocking"] == ["fault_flags_safe"]


def test_a_failed_query_is_recorded_rather_than_reported_as_absent(
    capture: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    status, out, _ = run_capture(capture, monkeypatch, tmp_path, stack=FakeStack(traces_status=500))

    assert status == 1
    extract = json.loads(out.read_text())
    assert extract["verdict"] == "not_verified_query_failed"
    assert "traces_for_checkout" in extract["failures"]
    assert extract["traces_retrieved"] == 0


def test_the_window_is_respected(
    capture: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A trace from another interval is not evidence about this one."""
    outside = checkout_trace(trace_id="outside", start_us=BASE_US - 3600 * SECOND_US)
    inside = checkout_trace(trace_id="inside")
    stack = FakeStack(traces=[outside, inside])

    status, out, _ = run_capture(capture, monkeypatch, tmp_path, stack=stack)

    assert status == 0
    extract = json.loads(out.read_text())
    # Both traces are healthy; only the one inside the queried window came back.
    assert extract["traces_retrieved"] == 1
    assert extract["chosen_trace_id"] == "inside"
    assert extract["window"]["start"].startswith("2026-10-15T09:29:59")
