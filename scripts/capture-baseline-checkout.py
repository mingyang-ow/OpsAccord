#!/usr/bin/env python
"""Capture one fault-free Checkout -> Payment baseline from Jaeger and flagd.

The pass this serves has to demonstrate that the *unfaulted* local demo still
completes a checkout that reaches Payment - without arming, clearing or otherwise
touching anything. Every live entry point already in the tree injects a fault
(``scripts/validate.sh --live`` arms one for each of the seven scenarios), so the
question had no way to be asked with the tools that existed.

What it does, and what each step is allowed to mean:

* it queries Jaeger for recent ``checkout`` traces over an explicit interval - the
  store is bounded and in memory, so a query without bounds is a statement about
  recent traffic rather than about an interval;
* for each trace it reconstructs the request path from the payload's *own* parent
  references (``opsaccord.tools.fault_evidence.summarize_trace``): the root is a
  span whose ``CHILD_OF`` parent is absent, the attempt is the measured
  operation's own *server* span, and every other span's relationship to the
  attempt is a walk over those references - direct child, descendant, ancestor,
  unrelated branch, or unresolved;
* it reads the flag document from flagd-ui to confirm the fault flags sit at their
  safe variants, because a checkout that "succeeds" while a fault is armed is not
  a baseline;
* it names *which* property failed, because a trace that never reached the
  catalog, one whose Charge span is only the caller's client span, one whose
  Charge span sits in an unrelated branch, one whose spans wrote no status, and an
  empty query are five different findings - and none of them is "healthy".

Nothing here writes to the demo: no flag is set, no scenario runs, no container is
touched and no service is called directly. The only files written are the extract
in ``--out`` and the human-readable log in ``--log``.

An empty answer is not health, ``UNSET`` is not success, and one verified trace is
a statement about the interval it came from: not about sustained reliability, and
not a P1-01 result.

exit status: 0 a fault-free baseline was verified, 1 it was not (whatever the
reason), 2 bad arguments, 3 it refused to start.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from opsaccord.tools.fault_evidence import (
    POSITION_AFTER,
    POSITION_BEFORE,
    POSITION_STARTS_BEFORE,
    POSITION_UNKNOWN,
    SPAN_STATUS_ERROR,
    SPAN_STATUS_OK,
    summarize_trace,
)
from opsaccord.tools.live_environment import FLAG_SAFE_VARIANT, JaegerClient

CHECKOUT_SERVICE = "checkout"
CHECKOUT_OPERATION = "oteldemo.CheckoutService/PlaceOrder"
PAYMENT_SERVICE = "payment"
PAYMENT_OPERATION = "oteldemo.PaymentService/Charge"
CATALOG_SERVICE = "product-catalog"
#: The token the demo's span names carry for the catalog's RPCs, independent of
#: whether the stored name is the semconv form or the ``POST /`` form.
CATALOG_OPERATION_TOKEN = "ProductCatalog"

#: ``fault_evidence.span_kind_of`` normalises both spellings the payload can carry
#: (``server`` and ``SPAN_KIND_SERVER``) to the semantic-convention word, so the
#: comparison is made on that word rather than on either spelling.
SERVER_KIND = "server"

#: Relationships that put a span *below* the measured attempt: the attempt itself,
#: its direct children, and anything further down its own subtree. ``ancestor``,
#: ``unrelated_branch`` and ``missing_parent`` are deliberately absent, because a
#: span the attempt did not reach is not evidence about the call it made.
IN_PATH_RELATIONSHIPS = ("same_span", "direct_child", "descendant")

#: Span positions that mean the span's own start fell outside the queried window.
OUTSIDE_WINDOW_POSITIONS = (
    POSITION_BEFORE,
    POSITION_STARTS_BEFORE,
    POSITION_AFTER,
    POSITION_UNKNOWN,
)

DEFAULT_LOOKBACK_MINUTES = 30.0
DEFAULT_LIMIT = 20
MAX_LIMIT = 200
REQUEST_TIMEOUT_SECONDS = 10.0


def log(message: str) -> None:
    """One timestamped line, the way the sibling diagnostics print theirs."""
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    print(f"{stamp} {message}", flush=True)


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, tz=UTC).isoformat(timespec="milliseconds")


def iso_to_micros(value: str) -> int:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1_000_000)


def span_line(span: Mapping[str, Any]) -> str:
    """One span as one line: identity, ancestry, service, timing, status, position.

    The sibling ``diagnose-payment-trace-path.py`` prints the same fields in
    ``span_row``. Both timestamps are written as UTC instants *and* as the raw
    microsecond fields they came from, so the units can be checked rather than
    trusted: the query API reports ``startTime`` and ``duration`` in microseconds,
    and a row whose end lands after the interval's end is a fact about the trace.
    """
    started = span.get("started_at_us")
    ended = span.get("ends_at_us")
    line = (
        f"{span.get('span_id')} <- {span.get('parent_span_id') or '(none)'} "
        f"{span.get('service')}/{span.get('operation')} kind={span.get('span_kind')} "
        f"status={span.get('status')}({span.get('status_source')}) "
        f"start={iso(int(started) / 1e6) if started else '?'}@{started} "
        f"end={iso(int(ended) / 1e6) if ended else '?'}@{ended} "
        f"duration_ms={(span.get('duration_us') or 0) / 1000.0:.1f} "
        f"position={span.get('position')} relationship={span.get('relationship')}"
    )
    if span.get("hops_from_attempt") is not None:
        line += f" hops_from_attempt={span['hops_from_attempt']}"
    if span.get("message"):
        line += f" message={span['message']!r}"
    return line


def brief_spans(items: Sequence[Mapping[str, Any]]) -> str:
    """Name a few spans compactly: which side emitted each, and where it sits."""
    return ", ".join(
        f"{item.get('service')}/{item.get('span_kind')}[{item.get('relationship')}]"
        for item in items
    )


def trace_bounds(summary: Mapping[str, Any]) -> tuple[int | None, int | None]:
    """The earliest span start and the latest span end in one payload."""
    starts = [
        int(span["started_at_us"])
        for span in summary.get("spans") or []
        if span.get("started_at_us") is not None
    ]
    ends = [
        int(span["ends_at_us"])
        for span in summary.get("spans") or []
        if span.get("ends_at_us") is not None
    ]
    return (min(starts) if starts else None, max(ends) if ends else None)


def read_flag_variants(
    base_url: str | None, *, client: httpx.Client, timeout_seconds: float
) -> dict[str, Any]:
    """Every fault flag's stored variant, and whether it is the safe one.

    A flag that cannot be read is not a safe flag, and a document that could not be
    fetched leaves the question unverified rather than answered: the two are
    recorded differently, because "the faults are off" is exactly the premise a
    baseline capture must not assume.
    """
    state: dict[str, Any] = {
        "base_url": base_url,
        "read": False,
        "reason": None,
        "variants": {},
        "safe_variant": dict(FLAG_SAFE_VARIANT),
        "unsafe": [],
    }
    if not base_url:
        state["reason"] = (
            "no flag endpoint in the environment (OPSACCORD_FLAG_CONTROL_BASE_URL unset); "
            "source .local/demo-endpoints.env first"
        )
        return state
    url = f"{base_url.rstrip('/')}/api/read"
    try:
        response = client.get(url, timeout=timeout_seconds)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        # A document that could not be fetched is a *finding* of this capture, so
        # the request is caught broadly: an unreadable flag endpoint must not take
        # the trace evidence down with it.
        state["reason"] = f"{type(exc).__name__}: {exc}"
        return state
    flags = payload.get("flags") if isinstance(payload, Mapping) else None
    if not isinstance(flags, Mapping):
        state["reason"] = f"flagd-ui read at {url} did not return a flag document"
        return state
    variants: dict[str, Any] = {}
    for flag_id in sorted(FLAG_SAFE_VARIANT):
        entry = flags.get(flag_id)
        variants[flag_id] = entry.get("defaultVariant") if isinstance(entry, Mapping) else None
    state["read"] = True
    state["variants"] = variants
    state["unsafe"] = [flag for flag, value in variants.items() if value != FLAG_SAFE_VARIANT[flag]]
    return state


def flag_check(state: Mapping[str, Any]) -> dict[str, Any]:
    """The flag document as one gate, without pretending an unread one is safe."""
    if not state.get("read"):
        return {
            "name": "fault_flags_safe",
            "ok": False,
            "detail": (
                f"the flag document could not be read ({state.get('reason')}), so 'the faults "
                "are off' is unverified rather than true"
            ),
        }
    variants = state.get("variants") or {}
    unsafe = list(state.get("unsafe") or [])
    if unsafe:
        armed = ", ".join(f"{flag}={variants.get(flag)!r}" for flag in unsafe)
        return {
            "name": "fault_flags_safe",
            "ok": False,
            "detail": f"these flags are not at their safe variant: {armed}",
        }
    reads = ", ".join(f"{flag}={variants[flag]!r}" for flag in sorted(variants))
    return {
        "name": "fault_flags_safe",
        "ok": True,
        "detail": f"every fault flag in the pinned document reads its safe variant: {reads}",
    }


def evaluate_summary(
    summary: Mapping[str, Any],
    *,
    payment_service: str = PAYMENT_SERVICE,
    catalog_service: str = CATALOG_SERVICE,
) -> dict[str, Any]:
    """Which of the four trace properties this one reconstructed payload shows.

    Each property is recorded separately, with the evidence that decided it, so a
    failed capture reports what was missing instead of one undifferentiated "no".
    """
    spans = [span for span in (summary.get("spans") or []) if isinstance(span, Mapping)]
    attempt = summary.get("attempt") or None
    ancestors = list(summary.get("ancestors_of_attempt") or [])
    on_path = [span for span in spans if span.get("relationship") in IN_PATH_RELATIONSHIPS]
    path_ids = {span.get("span_id") for span in on_path + ancestors}
    if attempt is not None:
        path_ids.add(attempt.get("span_id"))

    checks: list[dict[str, Any]] = []

    def record(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    # --- the entry operation, and the callee's own side of it -----------------
    if attempt is None:
        record(
            "checkout_entry_span",
            False,
            f"no span in this payload carries {CHECKOUT_OPERATION!r}, so no request path can be "
            "reconstructed from it",
        )
    elif attempt.get("span_kind") != SERVER_KIND:
        record(
            "checkout_entry_span",
            False,
            f"the entry operation resolved to a {attempt.get('span_kind')!r} span on "
            f"{attempt.get('service')!r} ({summary.get('attempt_basis')}); a caller's own span "
            "is not the callee completing the request",
        )
    else:
        record(
            "checkout_entry_span",
            True,
            f"{attempt.get('service')}/{attempt.get('operation')} span {attempt.get('span_id')} "
            f"({summary.get('attempt_basis')}), status {attempt.get('status')}"
            f"({attempt.get('status_source')})",
        )

    # --- the catalog, on that same reconstructed path ------------------------
    catalog_servers = [
        span
        for span in on_path
        if span.get("service") == catalog_service and span.get("span_kind") == SERVER_KIND
    ]
    catalog_calls = [
        span for span in spans if CATALOG_OPERATION_TOKEN in str(span.get("operation"))
    ]
    if catalog_servers:
        record(
            "product_catalog_reached",
            True,
            f"{catalog_service} served {catalog_servers[0].get('operation')!r} on the attempt's "
            f"path ({brief_spans(catalog_servers[:3])})",
        )
    elif catalog_calls:
        record(
            "product_catalog_reached",
            False,
            f"{len(catalog_calls)} span(s) carry a {CATALOG_OPERATION_TOKEN} operation and none "
            f"is a {catalog_service} server span on the attempt's path: the caller's own client "
            "span records that the call was made, not that the catalog answered it "
            f"({brief_spans(catalog_calls[:3])})",
        )
    else:
        record(
            "product_catalog_reached",
            False,
            f"no span in this payload names a {CATALOG_OPERATION_TOKEN} operation at all - an "
            "empty cart produces none - so 'reached the catalog' is not demonstrated by this "
            "trace",
        )

    # --- the measured edge, as the callee's own server span, on that path -----
    charge_spans = [span for span in spans if span.get("operation") == PAYMENT_OPERATION]
    charge_served = [
        span
        for span in charge_spans
        if span.get("service") == payment_service
        and span.get("span_kind") == SERVER_KIND
        and span.get("relationship") in IN_PATH_RELATIONSHIPS
    ]
    if charge_served:
        first = charge_served[0]
        record(
            "payment_charge_on_path",
            True,
            f"{payment_service} served {PAYMENT_OPERATION!r} as span {first.get('span_id')} "
            f"({first.get('relationship')} of the attempt, {first.get('hops_from_attempt')} "
            f"hop(s)), status {first.get('status')}({first.get('status_source')})",
        )
    elif charge_spans:
        record(
            "payment_charge_on_path",
            False,
            f"{len(charge_spans)} span(s) carry {PAYMENT_OPERATION!r} and none is a "
            f"{payment_service} server span on the attempt's path: {brief_spans(charge_spans)}",
        )
    else:
        record(
            "payment_charge_on_path",
            False,
            f"no span in this payload carries {PAYMENT_OPERATION!r} at all, so this attempt is "
            "not shown to have reached the measured edge",
        )

    # --- explicit success, on the path rather than in the trace at large -----
    failing_on_path = [
        span
        for span in spans
        if span.get("span_id") in path_ids and span.get("status") == SPAN_STATUS_ERROR
    ]
    attempt_status = attempt.get("status") if attempt else None
    attempt_source = attempt.get("status_source") if attempt else None
    charge_status = charge_served[0].get("status") if charge_served else None
    charge_source = charge_served[0].get("status_source") if charge_served else None
    if attempt_status != SPAN_STATUS_OK:
        record(
            "explicit_success",
            False,
            f"the entry span's own status is {attempt_status}"
            f"({attempt_source or 'no attempt'}), which is not an "
            "affirmative success",
        )
    elif not charge_served:
        record(
            "explicit_success",
            False,
            "the entry span is OK, but no Charge status can be read: the measured edge's own "
            "server span is not on the attempt's path, so this attempt carries no affirmative "
            "success signal for it",
        )
    elif charge_status != SPAN_STATUS_OK:
        record(
            "explicit_success",
            False,
            f"the entry span is OK but the {payment_service} Charge span's status is "
            f"{charge_status}, so the measured edge carries no success signal",
        )
    elif failing_on_path:
        record(
            "explicit_success",
            False,
            f"{len(failing_on_path)} span(s) on the attempt's path recorded ERROR: "
            f"{brief_spans(failing_on_path[:3])}",
        )
    else:
        record(
            "explicit_success",
            True,
            "the entry span and the "
            f"{payment_service} Charge span both record OK with a named status source "
            f"({attempt_source} / {charge_source}), and no "
            "span on the reconstructed path recorded ERROR",
        )

    caveats: list[str] = []
    if summary.get("ancestry_truncated"):
        caveats.append(
            "a parent named by the attempt's ancestry is missing from the payload, so the path "
            "above the attempt is partial"
        )
    if summary.get("missing_parent_ids"):
        caveats.append(f"parents named but absent: {summary['missing_parent_ids']}")
    if summary.get("other_edge_spans"):
        caveats.append(
            f"{len(summary['other_edge_spans'])} further span(s) carry {PAYMENT_OPERATION!r} "
            "beside the one used here (a retry, or another branch of the same trace)"
        )
    positions = summary.get("span_position_counts") or {}
    outside = sum(int(positions.get(name) or 0) for name in OUTSIDE_WINDOW_POSITIONS)
    if outside:
        caveats.append(
            f"{outside} span(s) in the payload fall outside the requested interval; they are "
            "reported here, and the gate rests on the attempt and the edge span only"
        )

    return {
        "checks": checks,
        "verified": all(check["ok"] for check in checks),
        "blocking": [check["name"] for check in checks if not check["ok"]],
        "caveats": caveats,
    }


def capture(
    jaeger: JaegerClient,
    *,
    start_us: int,
    end_us: int,
    limit: int,
    flag_state: Mapping[str, Any],
    payment_service: str = PAYMENT_SERVICE,
    catalog_service: str = CATALOG_SERVICE,
) -> dict[str, Any]:
    """Query, reconstruct, gate, and choose the trace the verdict rests on."""
    stages: dict[str, Any] = {}
    failures: dict[str, str] = {}

    def stage(name: str, thunk: Callable[[], Any]) -> tuple[bool, Any]:
        try:
            return True, thunk()
        except Exception as exc:
            failures[name] = f"{type(exc).__name__}: {exc}"
            log(f"stage {name} failed: {failures[name]}")
            return False, None

    ok, services = stage("services", lambda: jaeger.services(REQUEST_TIMEOUT_SECONDS))
    services = list(services) if ok else []
    if ok:
        stages["services"] = {
            "count": len(services),
            "checkout_visible": CHECKOUT_SERVICE in services,
            "payment_visible": payment_service in services,
            "catalog_visible": catalog_service in services,
            "services_named_like_the_catalog": sorted(
                name for name in services if "catalog" in name.lower()
            ),
        }
        log(
            f"services visible in Jaeger: {len(services)}; checkout={CHECKOUT_SERVICE in services} "
            f"payment={payment_service in services} {catalog_service}={catalog_service in services}"
        )

    ok, advertised = stage(
        "operations_for_checkout",
        lambda: jaeger.operations(CHECKOUT_SERVICE, REQUEST_TIMEOUT_SECONDS),
    )
    advertised = list(advertised) if ok else []
    if ok:
        stages["operations_for_checkout"] = {
            "names": advertised,
            "advertises_entry_operation": CHECKOUT_OPERATION in advertised,
        }
        log(f"operations advertised for {CHECKOUT_SERVICE}: {advertised}")

    ok, traces = stage(
        "traces_for_checkout",
        lambda: jaeger.traces(
            service=CHECKOUT_SERVICE,
            start_us=start_us,
            end_us=end_us,
            timeout_seconds=REQUEST_TIMEOUT_SECONDS,
            limit=limit,
        ),
    )
    traces = list(traces) if ok else []
    if ok:
        stages["traces_for_checkout"] = {"retrieved": len(traces), "limit": limit}
        log(f"traces for {CHECKOUT_SERVICE} in the interval: {len(traces)} (limit {limit})")

    flags = flag_check(flag_state)
    candidates: list[dict[str, Any]] = []
    for trace in traces:
        summary = summarize_trace(
            trace,
            root_operation=CHECKOUT_OPERATION,
            edge_operation=PAYMENT_OPERATION,
            window_start_us=start_us,
            window_end_us=end_us,
        )
        verdict = evaluate_summary(
            summary, payment_service=payment_service, catalog_service=catalog_service
        )
        checks = [*verdict["checks"], flags]
        started, ended = trace_bounds(summary)
        candidates.append(
            {
                "trace_id": summary.get("trace_id"),
                "started_at": iso(started / 1e6) if started else None,
                "started_at_us": started,
                "ended_at": iso(ended / 1e6) if ended else None,
                "ended_at_us": ended,
                "spans_seen": summary.get("spans_seen"),
                "verified": all(check["ok"] for check in checks),
                "blocking": [check["name"] for check in checks if not check["ok"]],
                "checks": checks,
                "caveats": verdict["caveats"],
                "summary": summary,
            }
        )
    candidates.sort(key=lambda item: item["started_at"] or "", reverse=True)

    chosen = next((item for item in candidates if item["verified"]), None)
    if chosen is None and candidates:
        # Newest first, so the first of the equally-strong candidates is the newest.
        chosen = max(candidates, key=lambda item: sum(1 for c in item["checks"] if c["ok"]))

    if chosen is not None and chosen["verified"]:
        verdict_name = "baseline_verified"
    elif failures:
        verdict_name = "not_verified_query_failed"
    elif not candidates:
        verdict_name = "not_verified_no_trace_in_interval"
    else:
        verdict_name = "not_verified"
    exit_status = 0 if verdict_name == "baseline_verified" else 1

    return {
        "kind": "opsaccord.baseline-checkout-capture",
        "note": (
            "read-only: no flag was armed or cleared, no scenario ran, no container was touched "
            "and no service was called directly; the only writes are this extract and its log"
        ),
        "captured_at": now(),
        "jaeger_base_url": jaeger.base_url,
        "flag_control_base_url": flag_state.get("base_url"),
        "service": CHECKOUT_SERVICE,
        "entry_operation": CHECKOUT_OPERATION,
        "edge_operation": PAYMENT_OPERATION,
        "catalog_service": catalog_service,
        "payment_service": payment_service,
        "window": {"start": iso(start_us / 1e6), "end": iso(end_us / 1e6)},
        "flags": {key: value for key, value in flag_state.items() if key != "base_url"},
        "stages": stages,
        "failures": failures,
        "traces_retrieved": len(traces),
        "candidates": [
            {key: value for key, value in item.items() if key != "summary"} for item in candidates
        ],
        "chosen_trace_id": chosen["trace_id"] if chosen else None,
        "verdict": verdict_name,
        "blocking": chosen["blocking"] if chosen else [],
        "evidence": chosen["summary"] if chosen else None,
        "limits": [
            "an empty query is not health: no trace back means the store holds nothing for that "
            "service in that interval, or rotated it out, or the collector never kept it",
            "one verified trace describes the interval it was captured in; it is not sustained "
            "reliability, and it is not a P1-01 result",
            "UNSET is reported as UNSET: a span that wrote no status is not a success",
            "ingestion delay means the newest traffic may still be missing from a query made "
            "immediately after it",
        ],
        "exit_status": exit_status,
    }


def render(document: Mapping[str, Any]) -> str:
    """The same document as a human-readable log: verdict first, then the evidence."""
    lines = [
        f"verdict: {document['verdict']}",
        f"captured: {document['captured_at']}",
        f"jaeger: {document['jaeger_base_url']}",
        "flagd: "
        f"{document.get('flag_control_base_url') or '(no flag endpoint in the environment)'}",
        f"window: {document['window']['start']} -> {document['window']['end']}",
        f"looked for: {document['service']} {document['entry_operation']!r} reaching "
        f"{document['payment_service']} {document['edge_operation']!r} through "
        f"{document['catalog_service']}",
        f"traces retrieved: {document['traces_retrieved']}",
        f"chosen trace: {document.get('chosen_trace_id') or '(none)'}",
    ]
    if document.get("blocking"):
        lines.append(f"blocking: {', '.join(str(name) for name in document['blocking'])}")

    flags = document.get("flags") or {}
    lines.append(f"flag document read: {bool(flags.get('read'))}")
    if not flags.get("read"):
        lines.append(f"  {flags.get('reason')}")
    for flag_id, variant in sorted((flags.get("variants") or {}).items()):
        safe = (flags.get("safe_variant") or {}).get(flag_id)
        armed = "" if variant == safe else " ARMED"
        lines.append(f"  {flag_id}: {variant!r} (safe {safe!r}){armed}")

    for candidate in document.get("candidates") or []:
        lines.append(
            f"trace {candidate['trace_id']} started {candidate['started_at']} "
            f"ended {candidate['ended_at']} spans={candidate['spans_seen']} "
            f"verified={candidate['verified']}"
        )
        for check in candidate["checks"]:
            mark = "PASS" if check["ok"] else "FAIL"
            lines.append(f"  [{mark}] {check['name']}: {check['detail']}")
        for caveat in candidate["caveats"]:
            lines.append(f"  [note] {caveat}")

    evidence = document.get("evidence")
    if evidence:
        root = evidence.get("trace_root") or {}
        attempt = evidence.get("attempt") or {}
        lines.append(f"chosen trace root: {span_line(root) if root else '(unresolved)'}")
        lines.append(f"chosen trace attempt: {span_line(attempt) if attempt else '(unresolved)'}")
        lines.append("ancestors of the attempt, nearest first:")
        lines.extend(f"  {span_line(item)}" for item in evidence.get("ancestors_of_attempt") or [])
        lines.append("children of the attempt:")
        lines.extend(f"  {span_line(item)}" for item in evidence.get("child_spans") or [])
        lines.append(
            f"status counts: {evidence.get('span_status_counts')} "
            f"positions: {evidence.get('span_position_counts')}"
        )
        lines.append("every span in the chosen payload, earliest start first (raw fields):")
        ordered = sorted(
            evidence.get("spans") or [],
            key=lambda span: (
                span.get("started_at_us") is None,
                span.get("started_at_us") or 0,
            ),
        )
        lines.extend(f"  {span_line(span)}" for span in ordered)

    for name, reason in sorted((document.get("failures") or {}).items()):
        lines.append(f"query failure {name}: {reason}")

    lines.append("limits:")
    lines.extend(f"  - {item}" for item in document["limits"])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "exit status: 0 a fault-free baseline was verified, 1 it was not (whatever the "
            "reason), 2 bad arguments, 3 it refused to start"
        ),
    )
    parser.add_argument("--start", default=None, help="UTC ISO start, e.g. 2026-10-15T09:00:00Z")
    parser.add_argument("--end", default=None, help="UTC ISO end (default: now)")
    parser.add_argument("--lookback-minutes", type=float, default=DEFAULT_LOOKBACK_MINUTES)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--payment-service", default=PAYMENT_SERVICE)
    parser.add_argument("--catalog-service", default=CATALOG_SERVICE)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--log", type=Path, default=None)
    args = parser.parse_args(argv)

    if not 0 < args.limit <= MAX_LIMIT:
        parser.error(f"--limit must be within (0, {MAX_LIMIT}]")
    base = os.environ.get("OPSACCORD_JAEGER_BASE_URL")
    if base is None:
        log(
            "refusing to start: no Jaeger endpoint in the environment; source "
            ".local/demo-endpoints.env first (scripts/demo.sh endpoints writes it)"
        )
        return 3

    end_us = iso_to_micros(args.end) if args.end else int(time.time() * 1_000_000)
    start_us = (
        iso_to_micros(args.start) if args.start else end_us - int(args.lookback_minutes * 60 * 1e6)
    )
    if start_us >= end_us:
        parser.error("--start must be before --end")

    stamp = int(time.time())
    out_path = args.out or (Path("reports/local") / f"baseline-checkout-{stamp}.json")
    log_path = args.log or out_path.with_suffix(".log")

    client = httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS, trust_env=False)
    try:
        flag_state = read_flag_variants(
            os.environ.get("OPSACCORD_FLAG_CONTROL_BASE_URL"),
            client=client,
            timeout_seconds=REQUEST_TIMEOUT_SECONDS,
        )
        if flag_state["read"]:
            log(
                f"flag document read: {len(flag_state['variants'])} fault flag(s), "
                f"{len(flag_state['unsafe'])} not at their safe variant"
            )
        else:
            log(f"flag document unreadable: {flag_state['reason']}")
        document = capture(
            JaegerClient(base, client=client),
            start_us=start_us,
            end_us=end_us,
            limit=args.limit,
            flag_state=flag_state,
            payment_service=args.payment_service,
            catalog_service=args.catalog_service,
        )
    finally:
        client.close()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    text = render(document)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(text + "\n", encoding="utf-8")
    print(text, flush=True)
    log(f"verdict: {document['verdict']}")
    log(f"extract written to {out_path}")
    log(f"log written to {log_path}")
    log(f"exit status: {document['exit_status']}")
    return int(document["exit_status"])


if __name__ == "__main__":
    raise SystemExit(main())
