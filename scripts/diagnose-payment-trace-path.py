#!/usr/bin/env python
"""Read-only inspection of retained traces for the Payment fault path.

This answers, from Jaeger alone, the question the metric capture could not settle:
when checkout's ``PlaceOrder`` failed while ``paymentUnreachable`` was armed, did
the request reach the measured ``Charge`` edge at all, and what did the failing
span actually record? A metric series can only say that no Charge span was
counted; a trace says whether a Charge span exists, which service emitted it, and
what the error text was.

It asks that question the way the demo's own telemetry tests do, in stages, each
recorded separately:

1. the services the query API advertises, to check the service name is real
   rather than a registry alias;
2. the operation names it advertises *for that service* - the authoritative
   answer to "what does Jaeger call this span", which is not answerable from
   another service's naming convention;
3. a bounded, recent query for the service with **no** operation filter, which
   distinguishes "nothing is being retained" from "the filter matched nothing",
   and which names the operations a returned trace actually contains;
4. the same query filtered by an operation that was actually returned;
5. a *bounded retrieval check*: a window built from a returned trace's own
   timestamps, re-queried for that trace's operation, which is what proves the
   query path can retrieve a trace it has just been handed;
6. the historical interval asked for on the command line.

Every request is a read-only HTTP GET against Jaeger's query API with explicit
``start``/``end`` bounds. No flag is armed, cleared or changed, no scenario runs,
nothing is written to the demo, and there is therefore nothing to restore.

``--trace-id`` narrows the run to traces the caller already has. The interval is
still queried with explicit bounds - the store is in memory and the query API has
no "fetch this id" route this project uses - but the historical query is then made
*without* an operation filter, because a named trace is what the caller wants and
an unverified operation name could only exclude it. Every returned trace that is
not one of the named ids is dropped from the counts and the report, an id the page
does not hold is listed as missing rather than passed off as absent, and the run
exits ``1`` in that case so a partial hand-off cannot read as a complete one.

What this script does *not* claim. An empty answer is not eviction: an in-memory
store that no longer holds an interval, an operation filter that does not match
the stored name, and a collector that dropped the traces are different facts, and
the run reports which of them it ruled out and which remain open. A successful
HTTP response does not make the query correct, and no returned error span does not
mean the dependency was healthy - when the callee is unreachable its server span
is never created, so its absence is expected. A capped page is a lower bound on
matching traffic, never a census.

The ``evidence`` block separates five things that are not the same: the query
completed, traces came back, the relevant request path was reconstructed from the
payload's own parent references, the evidence is sufficient for a particular
diagnosis, and the evidence is sufficient for recovery. Retrieving two traces
answers the first two and never, by itself, the last one.

Three things are read from the payload's references instead of assumed, because
assuming them is how a run comes to describe a trace by the wrong span: the
**root** is a span whose parent is not in the payload (a load generator's HTTP
span, not the frontend's client span the operation name also matches), the
**attempt** is the measured operation's own server span, and the edge's
**relationship** to the attempt is a walk over parent references - direct child,
descendant, ancestor, unrelated branch, or unresolved because a named parent is
missing. Each reported span carries both timestamps, its duration, its status
*and the signal that decided that status*, and its position relative to the
window asked for, so a call that ended minutes after the window is visible as
such rather than as a duration to interpret.

Usage (after ``scripts/demo.sh endpoints`` has written the endpoint file):

    set -a; . .local/demo-endpoints.env; set +a
    .venv/bin/python scripts/diagnose-payment-trace-path.py --lookback-minutes 240
    .venv/bin/python scripts/diagnose-payment-trace-path.py \
        --start 2026-09-25T00:56:00Z --end 2026-09-25T01:06:00Z

Exit status: ``0`` every query completed (whatever they found), ``1`` at least one
query failed to complete or a named trace was not returned, ``2`` bad arguments,
``3`` it refused to start.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

import httpx  # noqa: E402

from opsaccord.tools.fault_evidence import (  # noqa: E402
    SPAN_STATUS_ERROR,
    summarize_trace,
    trace_finding,
)
from opsaccord.tools.live_environment import (  # noqa: E402
    LIVE_TARGETS,
    JaegerClient,
    to_micros,
)

# The caller/callee pair the Payment scenario measures, taken from the same
# registry the runtime adapter uses so the two cannot drift: ``service_name`` is
# the name the store actually holds (it appears in the advertised service list),
# and ``operation`` is the span name the pinned revision exports.
CALLER_TARGET = "checkout-service"
CALLEE_TARGET = "payment-service"
CALLER_SERVICE = LIVE_TARGETS[CALLER_TARGET].service_name
ENTRY_OPERATION = LIVE_TARGETS[CALLER_TARGET].operation
ENTRY_ALIAS = LIVE_TARGETS[CALLER_TARGET].operation_alias
EDGE_OPERATION = LIVE_TARGETS[CALLEE_TARGET].operation
REQUEST_TIMEOUT_SECONDS = 8.0
DEFAULT_LIMIT = 40
MAX_LIMIT = 200
DEFAULT_MAX_TRACES = 5
DEFAULT_MAX_SUCCESS = 2
DEFAULT_RECENT_MINUTES = 60.0
#: Padding for the retrieval check's window, to absorb rounding and clock skew
#: between the trace's own timestamps and the query API's comparison.
RECHECK_PADDING_SECONDS = 2.0


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def log(message: str) -> None:
    print(f"{now()} {message}", flush=True)


def iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, tz=UTC).isoformat(timespec="seconds")


def iso_to_micros(value: str) -> int:
    return to_micros(datetime.fromisoformat(value.replace("Z", "+00:00")))


def trace_bounds(trace: dict[str, Any]) -> tuple[float, float]:
    """The earliest span start and the latest span end in one trace, in seconds.

    Each end is that span's own ``start + duration``. The previous version took
    the latest *start* and added the longest *duration* in the trace, which can
    pair one span's start with another span's duration and report an instant at
    which nothing in the trace happened; both are microsecond fields of the same
    payload, so the arithmetic is only meaningful per span.
    """
    starts = [float(span.get("startTime") or 0) for span in trace.get("spans") or []]
    ends = [
        float(span.get("startTime") or 0) + float(span.get("duration") or 0)
        for span in trace.get("spans") or []
    ]
    if not starts:
        return 0.0, 0.0
    return min(starts) / 1e6, max(ends) / 1e6


def trace_has_error_spans(summary: dict[str, Any]) -> bool:
    """Whether *any* span in the trace was reported failing.

    Asking only about the direct children of one chosen span reported a trace as
    successful while nine spans in it recorded errors, which is how a retained
    trace came to be logged as a "successful trace" beside its own list of failing
    spans.
    """
    return bool((summary.get("span_status_counts") or {}).get(SPAN_STATUS_ERROR))


def trace_ids(traces: list[dict[str, Any]]) -> list[str]:
    return [str(trace["traceID"]) for trace in traces if trace.get("traceID")]


def service_operations(traces: list[dict[str, Any]], service: str) -> list[str]:
    """The operation names the traces show *that service's own* spans emitting.

    A trace returned for the caller service still contains other services' spans,
    so the process map decides which operation names belong to the caller. This is
    what makes an operation filter checkable against observed reality instead of
    against the name another service happens to use.
    """
    seen: set[str] = set()
    for trace in traces:
        processes = trace.get("processes") or {}
        for span in trace.get("spans") or []:
            process = processes.get(str(span.get("processID"))) or {}
            if process.get("serviceName") != service:
                continue
            name = span.get("operationName")
            if name:
                seen.add(str(name))
    return sorted(seen)


def observed_range(traces: list[dict[str, Any]]) -> dict[str, str | None]:
    bounds = [bounds for bounds in (trace_bounds(trace) for trace in traces) if bounds[0]]
    if not bounds:
        return {"observed_start": None, "observed_end": None}
    return {
        "observed_start": iso(min(start for start, _ in bounds)),
        "observed_end": iso(max(end for _, end in bounds)),
    }


def trace_stage(
    traces: list[dict[str, Any]],
    *,
    window: dict[str, str],
    service: str,
    operation: str | None,
    limit: int,
) -> dict[str, Any]:
    """What one trace query asked for and what it returned, recorded together.

    ``truncated`` is the honest cap: the API stops at ``limit`` traces, so a full
    page is a lower bound on matching traffic and never a count of it.
    """
    record: dict[str, Any] = {
        "requested_window": window,
        "service": service,
        "operation": operation,
        "count": len(traces),
        "trace_ids": trace_ids(traces),
        "truncated": len(traces) >= limit,
    }
    record.update(observed_range(traces))
    return record


def select_requested(
    summaries: list[dict[str, Any]], requested: list[str]
) -> tuple[list[dict[str, Any]], list[str]]:
    """The named traces this page held, and the names it did not.

    A page answers "which traces *start* inside this interval", not "where is this
    request". A named trace the page does not hold is therefore reported as *not
    returned by this query* - weaker, and true - rather than as "not retained": the
    store may have rotated it out, the interval may not cover its start, or the
    collector may never have kept it. The reply keeps the caller's order so two
    runs of the same command read the same way.
    """
    wanted = list(dict.fromkeys(str(item) for item in requested))
    by_id = {str(item.get("trace_id")): item for item in summaries}
    return [by_id[item] for item in wanted if item in by_id], [
        item for item in wanted if item not in by_id
    ]


def choose_entry_operation(
    *, expected: str, advertised: list[str], observed: list[str]
) -> tuple[str, str]:
    """Pick the operation to analyse, and say what the choice rests on.

    The store matches ``operation`` exactly, so this choice decides what the run
    can see: naming the wrong one turns a populated window into an empty answer.
    An operation seen in a returned trace is the strongest evidence, because the
    trace proves the store can retrieve it; an advertised name is next; the
    registry's expectation is last and is recorded as unverified.
    """
    if expected in observed:
        return expected, "expected operation observed in a returned trace"
    matches = sorted(name for name in observed if ENTRY_ALIAS in name)
    if len(matches) == 1:
        return matches[0], "single observed operation containing the alias"
    if matches:
        return matches[0], f"ambiguous: {len(matches)} observed operations contain the alias"
    if expected in advertised:
        return expected, "expected operation advertised for the service, not observed"
    return expected, "unverified: neither observed nor advertised"


def empty_result_reason(stages: dict[str, Any], failures: dict[str, str]) -> str:
    """Why the requested interval returned nothing, as far as this run measured.

    Deliberately not "evicted": eviction is one of several causes, and only a
    demonstrated retrieval of some other trace separates "the store no longer has
    this interval" from "the query cannot return what it asks for".
    """
    recent = stages.get("recent_unfiltered") or {}
    check = stages.get("bounded_retrieval_check") or {}
    operations = stages.get("operations_for_service") or {}
    entry = stages.get("entry_operation") or {}
    if "historical" in failures:
        return (
            f"the requested interval could not be queried ({failures['historical']}), so this "
            "run establishes nothing about it"
        )
    if not recent.get("count"):
        return (
            "no recent trace at all came back for the caller service, so the store may hold "
            "nothing for it or may rotate faster than the query interval"
        )
    if check.get("retrieved") is True:
        return (
            "the query path did return a trace it had just been handed "
            f"(trace {check.get('anchor_trace_id')}), so the requested interval holds nothing "
            "the store still returns; *why* it does not (retention by count, coverage or "
            "collector loss) is not measured here and is not claimed"
        )
    if not operations.get("names"):
        return (
            "the operation names for the service could not be listed, so an empty interval and "
            "a wrong operation filter cannot be told apart"
        )
    return (
        f"the operation filter {entry.get('operation')!r} returned nothing and no retrieval was "
        "demonstrated, so neither a wrong operation name nor an empty interval is ruled out"
    )


@dataclass(frozen=True)
class ProbeRequest:
    """What the caller asked for, in the units the query API wants."""

    window_start_us: int
    window_end_us: int
    recent_start_us: int
    recent_end_us: int
    limit: int
    #: Traces the caller named. Empty means "report whatever the interval holds".
    trace_ids: tuple[str, ...] = ()

    def window(self) -> dict[str, str]:
        return {"start": iso(self.window_start_us / 1e6), "end": iso(self.window_end_us / 1e6)}

    def recent_window(self) -> dict[str, str]:
        return {"start": iso(self.recent_start_us / 1e6), "end": iso(self.recent_end_us / 1e6)}


def span_row(record: Mapping[str, Any]) -> str:
    """One span as one row: identity, ancestry, timing, status and position.

    Both timestamps are printed as UTC instants *and* as the raw microsecond
    fields they came from, so the units can be checked rather than trusted: the
    query API reports ``startTime`` and ``duration`` in microseconds, and a row
    whose end lands minutes after the interval's end is a fact about the trace,
    not a formatting artefact.
    """
    started = record.get("started_at_us")
    ended = record.get("ends_at_us")
    started_at = int(started) if started is not None else None
    ends_at = int(ended) if ended is not None else None
    row = (
        f"{record.get('span_id')} <- {record.get('parent_span_id') or '(none)'} "
        f"{record.get('service')}/{record.get('operation')} "
        f"kind={record.get('span_kind')} status={record.get('status')}"
        f"({record.get('status_source')}) "
        f"start={iso(started_at / 1e6) if started_at else '?'}@{started_at} "
        f"end={iso(ends_at / 1e6) if ends_at else '?'}@{ends_at} "
        f"duration_ms={(record.get('duration_us') or 0) / 1000.0:.1f} "
        f"position={record.get('position')}"
    )
    if record.get("relationship"):
        row += (
            f" relationship={record['relationship']}"
            f" hops_from_attempt={record.get('hops_from_attempt')}"
        )
    if record.get("message"):
        row += f" message={record['message']!r}"
    return row


def describe(summary: dict[str, Any], finding: str) -> str:
    """The spans that answer the question, as a table, plus what was unresolved."""
    attempt = summary.get("attempt") or {}
    root = summary.get("trace_root") or {}
    edge = summary.get("edge_span")
    lines = [
        f"  spans in the payload: {summary.get('spans_seen')}",
        f"  attempt ({summary.get('attempt_basis')}): {span_row(attempt) if attempt else '(none)'}",
        f"  trace root ({summary.get('root_basis')}): "
        f"{span_row(root) if root else '(unresolved)'} "
        f"root candidates={summary.get('root_count')}",
        "  ancestors of the attempt, nearest first:",
        *[f"    {span_row(item)}" for item in summary.get("ancestors_of_attempt") or []],
        "  children of the attempt:",
        *[f"    {span_row(item)}" for item in summary.get("child_spans") or []],
        "  edge: " + (span_row(edge) if edge else "absent from the whole trace"),
    ]
    if summary.get("other_edge_spans"):
        lines.append("  other spans carrying the edge operation:")
        lines.extend(f"    {span_row(item)}" for item in summary["other_edge_spans"])
    lines.append("  every span in the payload, earliest start first:")
    lines.extend(f"    {span_row(item)}" for item in summary.get("spans") or [])
    lines.append("  spans reported failing:")
    lines.extend(f"    {span_row(item)}" for item in summary.get("failed_spans") or [])
    if not summary.get("failed_spans"):
        lines.append("    (none)")
    lines.append(
        f"  status counts: {summary.get('span_status_counts')} "
        f"positions: {summary.get('span_position_counts')}"
    )
    if summary.get("missing_parent_ids"):
        lines.append(
            f"  parents named but absent from the payload: {summary['missing_parent_ids']}"
        )
    if summary.get("ancestry_truncated"):
        lines.append("  the attempt's own ancestry is unresolved: a named parent is missing")
    lines.append(f"  finding: {finding}")
    return "\n".join(lines)


def summarize_traces(
    traces: list[dict[str, Any]],
    *,
    root_operation: str,
    window: tuple[int, int] | None = None,
) -> list[dict[str, Any]]:
    """One reportable summary per retained trace, earliest first."""
    summaries: list[dict[str, Any]] = []
    for trace in traces:
        summary = summarize_trace(
            trace,
            root_operation=root_operation,
            edge_operation=EDGE_OPERATION,
            window_start_us=window[0] if window else None,
            window_end_us=window[1] if window else None,
        )
        summary["has_error_spans"] = trace_has_error_spans(summary)
        summary["finding"] = trace_finding(summary)
        bounds = trace_bounds(trace)
        summary["started_at"] = iso(bounds[0]) if bounds[0] else None
        summary["ended_at"] = iso(bounds[1]) if bounds[1] else None
        summaries.append(summary)
    summaries.sort(key=lambda item: item.get("started_at") or "")
    return summaries


def run_probe(client: JaegerClient, request: ProbeRequest) -> dict[str, Any]:
    """Run the progressive query sequence and return the recorded stages.

    Each stage records either a result or a failure, never both: a stage that
    raises leaves an entry in ``failures`` and *no* result entry, so a run cannot
    report "the API did not answer" as "there was nothing to find".
    """
    stages: dict[str, Any] = {}
    failures: dict[str, str] = {}

    def stage(name: str, thunk: Callable[[], Any]) -> tuple[bool, Any]:
        try:
            return True, thunk()
        except Exception as exc:
            failures[name] = f"{type(exc).__name__}: {exc}"
            log(f"stage {name} failed: {failures[name]}")
            return False, None

    ok, services = stage("services", lambda: client.services(REQUEST_TIMEOUT_SECONDS))
    if ok:
        stages["services"] = {
            "count": len(services),
            "caller_service_visible": CALLER_SERVICE in services,
        }
        log(f"services visible in Jaeger: {len(services)}")
    else:
        services = []

    ok, advertised = stage(
        "operations_for_service",
        lambda: client.operations(CALLER_SERVICE, REQUEST_TIMEOUT_SECONDS),
    )
    if ok:
        stages["operations_for_service"] = {
            "service": CALLER_SERVICE,
            "names": advertised,
            "advertises_expected": ENTRY_OPERATION in advertised,
            "names_containing_alias": sorted(n for n in advertised if ENTRY_ALIAS in n),
        }
        log(f"operations advertised for {CALLER_SERVICE}: {len(advertised)}")
        # The names themselves, not only the count. On 2026-09-26 the store
        # advertised exactly one operation for `checkout` and it was not the one
        # this script looks for, so the interval query returned nothing while a
        # recent unfiltered trace existed - and the log line that would have named
        # the difference printed a count. An entry filter that does not match is
        # not an absent path, so the vocabulary belongs in the log.
        for name in advertised:
            log(f"  advertised: {name!r}")
        if advertised and ENTRY_OPERATION not in advertised:
            log(
                f"  none of them is {ENTRY_OPERATION!r}; an interval query under an "
                f"unmatched filter cannot distinguish a wrong name from an empty window"
            )
    else:
        advertised = []

    ok, recent = stage(
        "recent_unfiltered",
        lambda: client.traces(
            service=CALLER_SERVICE,
            start_us=request.recent_start_us,
            end_us=request.recent_end_us,
            timeout_seconds=REQUEST_TIMEOUT_SECONDS,
            limit=request.limit,
        ),
    )
    recent = recent if ok else []
    observed = service_operations(recent, CALLER_SERVICE)
    if ok:
        stages["recent_unfiltered"] = trace_stage(
            recent,
            window=request.recent_window(),
            service=CALLER_SERVICE,
            operation=None,
            limit=request.limit,
        ) | {"caller_operations_seen": observed}
        log(
            f"recent unfiltered {CALLER_SERVICE} traces in {request.recent_window()}: {len(recent)}"
        )

    chosen, basis = choose_entry_operation(
        expected=ENTRY_OPERATION, advertised=advertised, observed=observed
    )
    stages["entry_operation"] = {"operation": chosen, "basis": basis, "expected": ENTRY_OPERATION}
    log(f"analysing operation {chosen!r} ({basis})")

    if recent:
        # Positive control: hand the query path a trace it has already returned and
        # ask for that trace's own window, so an empty historical answer is
        # interpretable as "the store no longer has it" rather than "the query
        # cannot retrieve what it asks for".
        anchor = next(
            (
                trace
                for trace in recent
                if any(span.get("operationName") == chosen for span in trace.get("spans") or [])
            ),
            max(recent, key=lambda trace: trace_bounds(trace)[0]),
        )
        anchor_id = str(anchor.get("traceID") or "")
        start_s, end_s = trace_bounds(anchor)
        check_window = (
            int((start_s - RECHECK_PADDING_SECONDS) * 1e6),
            int((end_s + RECHECK_PADDING_SECONDS) * 1e6),
        )
        check_window_iso = {
            "start": iso(check_window[0] / 1e6),
            "end": iso(check_window[1] / 1e6),
        }
        ok, retrieved = stage(
            "bounded_retrieval_check",
            lambda: client.traces(
                service=CALLER_SERVICE,
                operation=chosen,
                start_us=check_window[0],
                end_us=check_window[1],
                timeout_seconds=REQUEST_TIMEOUT_SECONDS,
                limit=request.limit,
            ),
        )
        if ok:
            retrieved_ids = trace_ids(retrieved)
            stages["bounded_retrieval_check"] = trace_stage(
                retrieved,
                window=check_window_iso,
                service=CALLER_SERVICE,
                operation=chosen,
                limit=request.limit,
            ) | {
                "anchor_trace_id": anchor_id,
                "anchor_started_at": iso(start_s) if start_s else None,
                "retrieved": bool(anchor_id) and anchor_id in retrieved_ids,
            }
            log(
                f"bounded retrieval check around {anchor_id or '<no trace id>'} in "
                f"{check_window_iso}: retrieved={stages['bounded_retrieval_check']['retrieved']}"
            )

    # A named trace is what the caller wants, so the operation filter is dropped for
    # it: the filter's only work would be to exclude the very payload asked for, and
    # the entry-operation choice it depends on is itself unverified on this stack.
    historical_operation = None if request.trace_ids else chosen
    ok, historical = stage(
        "historical",
        lambda: client.traces(
            service=CALLER_SERVICE,
            operation=historical_operation,
            start_us=request.window_start_us,
            end_us=request.window_end_us,
            timeout_seconds=REQUEST_TIMEOUT_SECONDS,
            limit=request.limit,
        ),
    )
    historical = historical if ok else []
    if ok:
        stages["historical"] = trace_stage(
            historical,
            window=request.window(),
            service=CALLER_SERVICE,
            operation=historical_operation,
            limit=request.limit,
        )
        if request.trace_ids:
            stages["historical"]["requested_trace_ids"] = list(request.trace_ids)
            stages["historical"]["operation_filter_skipped"] = (
                "the caller named the traces it wants, so the interval was queried without an "
                "operation filter: an unverified name could only exclude them"
            )
        filter_note = (
            "no operation filter, the caller named the traces"
            if request.trace_ids
            else f"operation={chosen!r}"
        )
        log(
            f"interval query {request.window()}: {len(historical)} traces"
            f"{' (capped)' if stages['historical']['truncated'] else ''}; {filter_note}"
        )
    if ok and not historical and not request.trace_ids:
        # The same interval without an operation filter: an empty filtered answer
        # is only interpretable beside an unfiltered one.
        unfiltered_ok, unfiltered = stage(
            "historical_unfiltered",
            lambda: client.traces(
                service=CALLER_SERVICE,
                start_us=request.window_start_us,
                end_us=request.window_end_us,
                timeout_seconds=REQUEST_TIMEOUT_SECONDS,
                limit=request.limit,
            ),
        )
        if unfiltered_ok:
            stages["historical_unfiltered"] = trace_stage(
                unfiltered,
                window=request.window(),
                service=CALLER_SERVICE,
                operation=None,
                limit=request.limit,
            ) | {"caller_operations_seen": service_operations(unfiltered, CALLER_SERVICE)}
            log(
                f"unfiltered {CALLER_SERVICE} traces in the same interval: "
                f"{stages['historical_unfiltered']['count']}"
            )

    summaries = summarize_traces(
        historical,
        root_operation=chosen,
        window=(request.window_start_us, request.window_end_us),
    )
    missing: list[str] = []
    if request.trace_ids:
        summaries, missing = select_requested(summaries, list(request.trace_ids))
    return {
        "stages": stages,
        "failures": failures,
        "services_visible": sorted(str(name) for name in services),
        "operation_analysed": chosen,
        "traces": summaries,
        "missing_trace_ids": missing,
    }


def evidence_ladder(
    *,
    stages: dict[str, Any],
    failures: dict[str, str],
    summaries: list[dict[str, Any]],
    empty_reason: str,
) -> dict[str, Any]:
    """What this run established, level by level, and what it did not.

    Two retained traces are enough to look at a request path and nowhere near
    enough to conclude that a dependency served requests or that a service
    recovered. Collapsing those into one word - "sufficient" - is what let a run
    that had merely retrieved two traces be read as a statement about the fault.
    The levels are therefore separate, and the two a trace cannot reach say why.
    """
    retrieved = len(summaries)
    capped = bool((stages.get("historical") or {}).get("truncated"))
    reconstructed = any(
        item.get("attempt_found")
        and (item.get("edge_span") or {}).get("relationship")
        in {"direct_child", "descendant", "same_span", "ancestor"}
        for item in summaries
    )
    cap_note = "; the page was capped, so the counts are a lower bound" if capped else ""
    claims = {
        "request_path_review": {
            "sufficient": bool(summaries) and reconstructed,
            "reason": (
                f"{retrieved} retained trace(s) were reconstructed from their own parent "
                "references, so which span is which and which span hangs from which can be "
                "read off the extract" + cap_note
                if reconstructed
                else "a trace carries the measured operation, but no edge span could be tied "
                "to the measured attempt through parent references, so the relevant request "
                "path was not reconstructed" + cap_note
            ),
        },
        "dependency_attribution": {
            "sufficient": False,
            "reason": (
                "a retained span says what its own service recorded and nothing more: a client "
                "span with no error is not proof that the dependency served the call (when the "
                "callee is unreachable its own server span is never created), and no span here "
                "measures the dependency's side of the edge"
            ),
        },
        "recovery": {
            "sufficient": False,
            "reason": (
                "recovery is a fresh post-restoration health question; a retained trace "
                "describes what happened inside an interval and cannot show that a service is "
                "serving now"
            ),
        },
    }
    if summaries:
        reason = f"{retrieved} retained trace(s) were summarized for the requested interval"
        if capped:
            reason += "; the page was capped, so the counts are a lower bound"
    else:
        reason = empty_reason
    return {
        "query_completed": not failures,
        "traces_retrieved": retrieved,
        "request_path_reconstructed": reconstructed,
        "sufficient_for": claims,
        "capped": capped,
        "sufficiency": (
            "sufficient_for_path_review"
            if claims["request_path_review"]["sufficient"]
            else "not_sufficient"
        ),
        "reason": reason,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "exit status: 0 every query completed (whatever they found), 1 at least one query "
            "failed to complete or a named trace was not returned, 2 bad arguments, 3 it "
            "refused to start"
        ),
    )
    parser.add_argument("--start", default=None, help="UTC ISO start, e.g. 2026-09-25T00:56:00Z")
    parser.add_argument("--end", default=None, help="UTC ISO end (default: now)")
    parser.add_argument("--lookback-minutes", type=float, default=180.0)
    parser.add_argument(
        "--recent-minutes",
        type=float,
        default=DEFAULT_RECENT_MINUTES,
        help="length of the bounded recent window used to establish that queries work",
    )
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument(
        "--trace-id",
        dest="trace_ids",
        action="append",
        default=[],
        metavar="ID",
        help=(
            "report only this trace id (repeatable). The interval is queried without an "
            "operation filter, ids the page did not hold are listed as missing, and the run "
            "exits 1 if any named trace was not returned"
        ),
    )
    parser.add_argument("--max-traces", type=int, default=DEFAULT_MAX_TRACES)
    parser.add_argument(
        "--max-success",
        "--max-no-error-traces",
        dest="max_success",
        type=int,
        default=DEFAULT_MAX_SUCCESS,
        help="how many traces that recorded no error span to describe (default: 2)",
    )
    parser.add_argument("--out", type=Path, default=None)
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

    end = iso_to_micros(args.end) if args.end else int(time.time() * 1_000_000)
    start = iso_to_micros(args.start) if args.start else end - int(args.lookback_minutes * 60 * 1e6)
    if start >= end:
        parser.error("--start must be before --end")
    recent_end = int(time.time() * 1_000_000)
    recent_start = recent_end - int(max(args.recent_minutes, 0.0) * 60 * 1e6)

    request = ProbeRequest(
        window_start_us=start,
        window_end_us=end,
        recent_start_us=recent_start,
        recent_end_us=recent_end,
        limit=args.limit,
        trace_ids=tuple(str(item) for item in args.trace_ids),
    )
    out_path = args.out or (Path("reports/local") / f"payment-trace-path-{int(time.time())}.json")
    extract: dict[str, Any] = {
        "kind": "opsaccord.payment-trace-path",
        "note": (
            "read-only trace extract: no flag was armed or cleared, no scenario ran, nothing "
            "was written to the demo"
        ),
        "queried_at": now(),
        "jaeger_base_url": base,
        "window": request.window(),
        "service": CALLER_SERVICE,
        "operation": ENTRY_OPERATION,
        "operation_analysed": None,
        "edge_operation": EDGE_OPERATION,
        "requested_trace_ids": list(dict.fromkeys(str(item) for item in args.trace_ids)),
        "missing_trace_ids": [],
        "stages": {},
        "failures": {},
        "services_visible": [],
        "traces": [],
        "counts": {"traces": 0, "with_error_spans": 0, "without_error_spans": 0},
        "finding_counts": {},
        "evidence": {},
        "exit_status": 0,
    }
    exit_status = 0
    summaries: list[dict[str, Any]] = []
    missing: list[str] = []
    client = httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS, trust_env=False)
    try:
        probe = run_probe(JaegerClient(base, client=client), request)
        extract["stages"] = probe["stages"]
        extract["failures"] = probe["failures"]
        extract["services_visible"] = probe["services_visible"]
        extract["operation_analysed"] = probe["operation_analysed"]
        summaries = probe["traces"]
        missing = probe["missing_trace_ids"]
        # A partial hand-off must not read as a complete one, so a named trace the
        # interval did not return carries the same exit status as a failed query.
        exit_status = 1 if probe["failures"] or missing else 0
    except Exception as exc:
        extract["error"] = f"{type(exc).__name__}: {exc}"
        log(f"trace probe failed: {extract['error']}")
        exit_status = 1
    finally:
        client.close()

    requested = list(dict.fromkeys(str(item) for item in args.trace_ids))
    extract["missing_trace_ids"] = missing
    if requested:
        log(
            f"named traces: {len(requested)} requested, {len(summaries)} returned by this "
            f"interval's query, {len(missing)} not returned"
        )
        for trace_id in missing:
            log(
                f"  not returned by this query: {trace_id} - the store may have rotated it out, "
                "the interval may not cover its start, or the collector may never have kept it; "
                "this run does not tell those apart"
            )

    failing = [item for item in summaries if item.get("has_error_spans")]
    without_errors = [item for item in summaries if not item.get("has_error_spans")]
    extract["counts"] = {
        "traces": len(summaries),
        "with_error_spans": len(failing),
        "without_error_spans": len(without_errors),
    }
    extract["finding_counts"] = dict(Counter(item["finding"] for item in summaries))
    extract["traces"] = summaries
    if summaries:
        extract["retained_window_seen"] = {
            "earliest": summaries[0].get("started_at"),
            "latest": summaries[-1].get("started_at"),
        }
        log(
            f"retained window actually seen: {extract['retained_window_seen']['earliest']} .. "
            f"{extract['retained_window_seen']['latest']}"
        )
    if requested:
        # Every named trace is described: the caller asked for these, not for a sample.
        to_describe = [
            (f"named trace {index + 1}/{len(summaries)}", item)
            for index, item in enumerate(summaries)
        ]
    else:
        to_describe = [("trace with error spans", item) for item in failing[: args.max_traces]] + [
            ("trace without error spans", item)
            for item in without_errors[: max(args.max_success, 0)]
        ]
    for label, item in to_describe:
        log(
            f"[{label}] id={item.get('trace_id')} started={item.get('started_at')} "
            f"ended={item.get('ended_at')} finding={item['finding']}"
        )
        for line in describe(item, item["finding"]).splitlines():
            log(line)
    log(f"trace counts: {extract['counts']} findings: {extract['finding_counts']}")

    evidence = evidence_ladder(
        stages=extract["stages"],
        failures=extract["failures"],
        summaries=summaries,
        empty_reason=empty_result_reason(extract["stages"], extract["failures"]),
    )
    extract["evidence"] = evidence
    log(
        f"evidence: query_completed={evidence['query_completed']} "
        f"traces_retrieved={evidence['traces_retrieved']} "
        f"request_path_reconstructed={evidence['request_path_reconstructed']}"
    )
    for name, claim in sorted(evidence["sufficient_for"].items()):
        log(f"  sufficient for {name}: {claim['sufficient']} - {claim['reason']}")
    log(f"evidence sufficiency: {evidence['sufficiency']} - {evidence['reason']}")
    log(
        f"query completion: exit status {exit_status}"
        + (f"; failed stages: {sorted(extract['failures'])}" if extract["failures"] else "")
    )
    if not summaries and exit_status == 0:
        log("an empty answer is not eviction, not a statement about the fault, and not recovery")

    extract["exit_status"] = exit_status
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(extract, indent=1, sort_keys=True), encoding="utf-8")
    log(f"extract written to {out_path}")
    log(f"exit status: {exit_status}")
    return exit_status


if __name__ == "__main__":
    raise SystemExit(main())
