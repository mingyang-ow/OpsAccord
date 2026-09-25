"""The vocabulary a fault diagnostic uses to say what it actually saw.

A capture that polls a demo while a fault is armed accumulates readings, and
those readings are not the same kind of thing: an *attributable* failure on the
path the scenario measures, a generic failure somewhere in the caller, ordinary
traffic with no failure in it, or no usable reading at all. Collapsing them into
one verdict loses the distinction the diagnosis exists to report, and taking the
*commonest* reading over a capture is worse than losing it: the first polls of
every capture read a window that predates the injection, so the least informative
reading wins by construction and the latest measurement is hidden.

The four classes, ordered by how much they license:

* ``ATTRIBUTABLE_FAILURE`` - the caller's own client call to the managed
  dependency is failing above the telemetry bound. This is the only reading a
  Payment premise or a recovery verdict may rest on;
* ``GENERIC_CALLER_FAILURE_ONLY`` - the caller is failing and its own call to the
  dependency is not. Context, never attribution;
* ``TRAFFIC_WITHOUT_FAILURE`` - traffic was observed on the path with nothing
  above the bound, so the reading is "no failure seen", not "no data seen";
* ``NO_USABLE_OBSERVATION`` - no traffic, or a reading that could not be taken.
  Never health.

Cleanup and recovery stay separate questions here too. A sample taken immediately
after a flag is restored averages the same window as the last armed reading, so it
cannot show that a service recovered; nothing in this module has a "recovered"
answer, and a capture records its post-restore sample as a sample.

The trace helpers describe one retained trace in the same spirit: which spans
exist under the failing operation, whether the measured edge is among them, and
what the error text actually says - bounded, and stripped of anything that can
carry a person's data.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from opsaccord.tools.contracts import TELEMETRY_ERROR_BOUND

#: The caller's own client call to the managed dependency is failing.
ATTRIBUTABLE_FAILURE = "attributable_failure"
#: The caller is failing; its own call to the dependency is not.
GENERIC_CALLER_FAILURE_ONLY = "generic_caller_failure_only"
#: Traffic on the path, nothing above the telemetry bound.
TRAFFIC_WITHOUT_FAILURE = "traffic_without_failure"
#: No traffic, or a reading that could not be taken.
NO_USABLE_OBSERVATION = "no_usable_observation"

#: Strongest first. A summary's headline is the strongest class observed, and the
#: counts are reported beside it so one reading is never the whole story.
EVIDENCE_CLASSES = (
    ATTRIBUTABLE_FAILURE,
    GENERIC_CALLER_FAILURE_ONLY,
    TRAFFIC_WITHOUT_FAILURE,
    NO_USABLE_OBSERVATION,
)


def _rate(reading: Mapping[str, Any] | None, key: str) -> float | None:
    """One numeric field of a reading, or ``None`` when it is absent or unusable."""
    if not reading:
        return None
    value = reading.get(key)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _calls(reading: Mapping[str, Any] | None) -> float:
    return _rate(reading, "calls_per_second") or 0.0


def _share(reading: Mapping[str, Any] | None) -> float | None:
    share = _rate(reading, "error_rate")
    return share if share is not None else _rate(reading, "error_share")


def _failing(reading: Mapping[str, Any] | None) -> bool:
    """Traffic above zero *and* a failure share above the telemetry bound."""
    share = _share(reading)
    return _calls(reading) > 0.0 and share is not None and share > TELEMETRY_ERROR_BOUND


def classify_reading(
    *,
    client: Mapping[str, Any] | None,
    parent: Mapping[str, Any] | None,
    dependency: Mapping[str, Any] | None,
    readable: bool = True,
) -> str:
    """Name one poll's reading in the four-class vocabulary.

    ``client`` is the caller's own client spans for the measured edge (the
    ``service_name='checkout', span_kind='SPAN_KIND_CLIENT'`` series for
    ``Charge``), ``parent`` is the caller's own operation that reaches it, and
    ``dependency`` is the same edge read through ``read_dependency``.

    The edge counts as attributable when *either* reading shows it failing: the
    dependency read and the grouped client series are the same query, and the
    grouped one is scoped by the very labels attribution needs, so it can carry
    the reading when the tool's schema lookup could not resolve. A generic
    parent-span failure is never enough on its own: that is the inference the
    controller's read plan forbids, and it is the shape that armed P1-01's premise
    on a fault its own diagnosis could not reproduce.
    """
    if not readable:
        return NO_USABLE_OBSERVATION
    attributable = dependency is not None and dependency.get("attributable") is True
    if (attributable and _failing(dependency)) or _failing(client):
        return ATTRIBUTABLE_FAILURE
    if _failing(parent) and not _failing(client):
        return GENERIC_CALLER_FAILURE_ONLY
    if _calls(client) > 0.0 or _calls(parent) > 0.0 or _calls(dependency) > 0.0:
        return TRAFFIC_WITHOUT_FAILURE
    return NO_USABLE_OBSERVATION


@dataclass(frozen=True)
class EvidenceSummary:
    """What a whole capture of readings adds up to, and nothing more."""

    counts: dict[str, int]
    strongest: str
    last: str
    total: int

    @property
    def attributable_observed(self) -> bool:
        return self.counts.get(ATTRIBUTABLE_FAILURE, 0) > 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "classes": {name: self.counts.get(name, 0) for name in EVIDENCE_CLASSES},
            "strongest": self.strongest,
            "last": self.last,
            "total": self.total,
            "attributable_observed": self.attributable_observed,
        }


def summarize(readings: Iterable[str]) -> EvidenceSummary:
    """Aggregate readings without letting the least informative one win.

    ``strongest`` is the highest class observed (see ``EVIDENCE_CLASSES``) and
    ``last`` is the final reading; both are reported beside the counts, because a
    single attributable reading inside a capture that mostly saw ordinary traffic
    must be visible as one rather than as a headline with no denominator.
    """
    ordered = list(readings)
    counts: dict[str, int] = {}
    for reading in ordered:
        counts[reading] = counts.get(reading, 0) + 1
    strongest = next((name for name in EVIDENCE_CLASSES if counts.get(name, 0) > 0), None)
    return EvidenceSummary(
        counts=counts,
        strongest=strongest or NO_USABLE_OBSERVATION,
        last=ordered[-1] if ordered else NO_USABLE_OBSERVATION,
        total=len(ordered),
    )


# --- retained traces ---------------------------------------------------------

#: Attribute and event keys worth keeping, because they say which dependency a
#: failure belongs to. Everything else is dropped rather than copied into a local
#: file: a trace carries whatever its service annotated it with, and this
#: repository keeps no raw payloads.
INTERESTING_KEYS = frozenset(
    {
        "span.kind",
        "rpc.system",
        "rpc.service",
        "rpc.method",
        "rpc.grpc.status_code",
        "network.peer.address",
        "network.peer.port",
        "net.peer.name",
        "net.peer.port",
        "server.address",
        "server.port",
        "http.request.method",
        "http.response.status_code",
        "status.code",
        "otel.status_code",
        "otel.status_description",
        "error",
        "error.type",
        "error.message",
        "exception.type",
        "exception.message",
        "message",
    }
)

#: Keys that are never copied, whatever their value.
REDACTED_KEYS = frozenset(
    {
        "user.email",
        "user.id",
        "email",
        "demo.payment.card_number",
        "demo.payment.card_cvv",
        "demo.payment.card_type",
        "url.full",
        "url.query",
    }
)

_CARD_LIKE = re.compile(r"\b\d{13,19}\b")
_VALUE_LIMIT = 300


def _sanitize_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    if len(value) > _VALUE_LIMIT:
        value = value[:_VALUE_LIMIT] + "..."
    return _CARD_LIKE.sub("<redacted>", value)


def sanitize_span(span: Mapping[str, Any]) -> dict[str, Any]:
    """One span, bounded: identity, timing, status and dependency attributes.

    Only ``INTERESTING_KEYS`` survive, and anything in ``REDACTED_KEYS`` or shaped
    like a card number is dropped, so an extract can sit beside the report without
    carrying the demo's own PII.
    """
    attributes: dict[str, Any] = {}
    for tag in span.get("tags") or []:
        key = str(tag.get("key", ""))
        if not key or key in REDACTED_KEYS or key not in INTERESTING_KEYS:
            continue
        attributes[key] = _sanitize_value(tag.get("value"))
    events: list[dict[str, Any]] = []
    for event in span.get("logs") or []:
        fields: dict[str, Any] = {}
        for field in event.get("fields") or []:
            key = str(field.get("key", ""))
            if not key or key in REDACTED_KEYS or key not in INTERESTING_KEYS:
                continue
            fields[key] = _sanitize_value(field.get("value"))
        if fields:
            events.append({"timestamp": event.get("timestamp"), "fields": fields})
    return {
        "span_id": span.get("spanID"),
        "operation": span.get("operationName"),
        "started_at_us": span.get("startTime"),
        "duration_us": span.get("duration"),
        "attributes": attributes,
        "events": events,
    }


#: One span's status. Three values, because a span that recorded nothing is not a
#: success: ``UNSET`` is the honest name for "no status was written down", and
#: reading it as ``OK`` is how an unrecorded failure becomes a healthy dependency.
SPAN_STATUS_ERROR = "ERROR"
SPAN_STATUS_OK = "OK"
SPAN_STATUS_UNSET = "UNSET"

#: Where one span sits relative to the interval a query asked for. When that
#: interval is a fault run (armed -> restored) these are the positions a reader
#: has to tell apart: a call that ran inside the fault, one that crossed the
#: restoration edge, one that started afterwards, and one that has nothing to do
#: with either.
POSITION_BEFORE = "before_window"
POSITION_STARTS_BEFORE = "starts_before_window"
POSITION_DURING = "during_window"
POSITION_CROSSES_END = "crosses_window_end"
POSITION_AFTER = "after_window"
POSITION_UNKNOWN = "unpositionable"
SPAN_POSITIONS = (
    POSITION_BEFORE,
    POSITION_STARTS_BEFORE,
    POSITION_DURING,
    POSITION_CROSSES_END,
    POSITION_AFTER,
    POSITION_UNKNOWN,
)

_ERROR_STATUS_CODES = frozenset({"ERROR", "STATUS_CODE_ERROR", "2"})
_OK_STATUS_CODES = frozenset({"OK", "STATUS_CODE_OK", "1"})
_UNSET_STATUS_CODES = frozenset({"UNSET", "STATUS_CODE_UNSET", "0"})
_TRUE_VALUES = frozenset({"true", "1", "yes"})
_EXCEPTION_EVENT_KEYS = ("exception.type", "exception.message", "error.object")


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _child_of_reference(span: Mapping[str, Any]) -> tuple[bool, str | None]:
    """``(names_a_parent, parent_span_id)`` for one span's ``CHILD_OF`` link.

    The pair rather than a bare id, because "this span names no parent" and "this
    span names a parent the payload does not contain" are different facts: the
    first makes it a root candidate and the second leaves its ancestry unresolved,
    which has to be reported as unresolved rather than quietly read as a root.
    """
    for reference in span.get("references") or []:
        if str(reference.get("refType", "")).upper() == "CHILD_OF":
            parent = reference.get("spanID")
            return True, (str(parent) if parent else None)
    return False, None


def span_status(
    attributes: Mapping[str, Any], events: Iterable[Mapping[str, Any]] = ()
) -> tuple[str, str]:
    """One span's status, and the signal that decided it.

    Every signal the pinned revision can carry is consulted in a fixed order and
    the second element names the one that decided: an explicit ``otel.status_code``
    or legacy ``status.code``, an ``error`` tag, an ``error.type`` or
    ``exception.type`` attribute, a gRPC or HTTP protocol status, or an exception
    event. A status that reads only ``otel.status_code`` calls a span whose
    framework wrote ``rpc.grpc.status_code = 14`` and nothing else ``OK``, which
    hides exactly the failure a dependency diagnosis is looking for.
    An explicit status code is taken at its word even beside an exception event;
    the signals below it are consulted only when no status code was written.
    """
    for key in ("otel.status_code", "status.code"):
        value = str(attributes.get(key, "")).strip().upper()
        if value in _ERROR_STATUS_CODES:
            return SPAN_STATUS_ERROR, key
        if value in _OK_STATUS_CODES:
            return SPAN_STATUS_OK, key
        if value in _UNSET_STATUS_CODES:
            return SPAN_STATUS_UNSET, key
    if str(attributes.get("error", "")).strip().lower() in _TRUE_VALUES:
        return SPAN_STATUS_ERROR, "error tag"
    for key in ("error.type", "exception.type"):
        if str(attributes.get(key, "")).strip():
            return SPAN_STATUS_ERROR, key
    grpc_code = _as_int(attributes.get("rpc.grpc.status_code"))
    if grpc_code is not None:
        source = "rpc.grpc.status_code"
        return (SPAN_STATUS_ERROR, source) if grpc_code else (SPAN_STATUS_OK, source)
    http_code = _as_int(attributes.get("http.response.status_code"))
    if http_code is not None:
        if http_code >= 500:
            return SPAN_STATUS_ERROR, "http.response.status_code"
        if http_code < 400:
            return SPAN_STATUS_OK, "http.response.status_code"
        # A 4xx is the request's own problem, not evidence about the dependency.
        return SPAN_STATUS_UNSET, "http.response.status_code (4xx)"
    for event in events:
        fields = event.get("fields") or {}
        if any(key in fields for key in _EXCEPTION_EVENT_KEYS):
            return SPAN_STATUS_ERROR, "exception event"
    return SPAN_STATUS_UNSET, "no status signal"


def span_status_of(span: Mapping[str, Any]) -> tuple[str, str]:
    """``(status, source)`` for one raw Jaeger span, sanitized as a summary is."""
    record = sanitize_span(span)
    return span_status(record["attributes"], record["events"])


def position_in_window(
    started_us: Any, duration_us: Any, *, window_start_us: int | None, window_end_us: int | None
) -> str:
    """Where one span sits relative to the interval the query asked for.

    A span with no start or no duration is ``unpositionable`` rather than guessed
    at, and a span that ends exactly on the window's edge counts as inside it.
    """
    if window_start_us is None or window_end_us is None:
        return POSITION_UNKNOWN
    started = _as_float(started_us)
    duration = _as_float(duration_us)
    if started is None or duration is None:
        return POSITION_UNKNOWN
    end = started + duration
    if end <= window_start_us:
        return POSITION_BEFORE
    if started < window_start_us:
        return POSITION_STARTS_BEFORE
    if started >= window_end_us:
        return POSITION_AFTER
    if end > window_end_us:
        return POSITION_CROSSES_END
    return POSITION_DURING


def _message(attributes: Mapping[str, Any]) -> str | None:
    for key in ("otel.status_description", "error.message", "exception.message", "message"):
        value = attributes.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def summarize_trace(
    trace: Mapping[str, Any],
    *,
    root_operation: str,
    edge_operation: str,
    window_start_us: int | None = None,
    window_end_us: int | None = None,
) -> dict[str, Any]:
    """What one retained trace shows about the operation that failed.

    The question a failing trace has to settle is which call under the failing
    operation failed, and whether the measured edge appears there at all. A
    failure the edge series cannot see is either a failure that happened before
    the edge was called, or one whose call produced no span; the error text tells
    those apart, so it is kept verbatim.

    Three things are resolved from the payload's own references instead of being
    assumed, because assuming them is how such a summary comes to describe a trace
    by the wrong span:

    * the **root** is a span whose ``CHILD_OF`` parent is not in the payload. A
      caller's *client* span and a callee's *server* span for one operation carry
      the same name, and the HTTP span above both of them is the real root;
    * the **attempt** is the measured operation's own *server* span, so the
      caller's identically named client span above it is not mistaken for it;
    * the **relationship** between the edge and the attempt is a walk over parent
      references - direct child, descendant at *n* hops, ancestor, unrelated
      branch, or unresolved because a named parent is missing - rather than one
      "is it a child of the span that was picked" boolean.

    Each span is reported with the service that emitted it, because for this
    question the interesting fact is precisely that the caller's *client* span for
    the edge belongs to ``checkout`` and the callee's *server* span to ``payment``.
    """
    spans = [span for span in (trace.get("spans") or []) if isinstance(span, Mapping)]
    processes = trace.get("processes") or {}
    records: dict[str, dict[str, Any]] = {}
    parents: dict[str, str | None] = {}
    order: list[str] = []
    for span in spans:
        span_id = str(span.get("spanID"))
        record = sanitize_span(span)
        process = processes.get(str(span.get("processID"))) or {}
        record["service"] = process.get("serviceName")
        status, source = span_status(record["attributes"], record["events"])
        record["status"] = status
        record["status_source"] = source
        record["message"] = _message(record["attributes"])
        record["position"] = position_in_window(
            record["started_at_us"],
            record["duration_us"],
            window_start_us=window_start_us,
            window_end_us=window_end_us,
        )
        started, duration = _as_float(record["started_at_us"]), _as_float(record["duration_us"])
        record["ends_at_us"] = None if started is None or duration is None else started + duration
        records[span_id] = record
        parents[span_id] = _child_of_reference(span)[1]
        order.append(span_id)

    present = set(records)

    def parent_in_payload(span_id: str) -> bool:
        parent = parents.get(span_id)
        return parent is not None and parent in present

    def ancestry(span_id: str) -> tuple[list[str], bool]:
        """Ancestors of one span, nearest first, and whether the walk was cut short."""
        chain: list[str] = []
        seen = {span_id}
        current = parents.get(span_id)
        while current is not None and current not in seen:
            if current not in present:
                return chain, True
            chain.append(current)
            seen.add(current)
            current = parents.get(current)
        return chain, False

    def brief(span_id: str) -> dict[str, Any]:
        record = records[span_id]
        return {
            "span_id": span_id,
            "parent_span_id": parents.get(span_id),
            "operation": record["operation"],
            "service": record["service"],
            "span_kind": record["attributes"].get("span.kind"),
            "status": record["status"],
            "status_source": record["status_source"],
            "message": record["message"],
            "started_at_us": record["started_at_us"],
            "duration_us": record["duration_us"],
            "ends_at_us": record["ends_at_us"],
            "position": record["position"],
        }

    root_ids = [span_id for span_id in order if not parent_in_payload(span_id)]
    missing_parent_ids = sorted(
        {parent for parent in parents.values() if parent is not None and parent not in present}
    )
    failed_spans = [
        brief(span_id) for span_id in order if records[span_id]["status"] == SPAN_STATUS_ERROR
    ]
    status_counts = {name: 0 for name in (SPAN_STATUS_ERROR, SPAN_STATUS_OK, SPAN_STATUS_UNSET)}
    position_counts = {name: 0 for name in SPAN_POSITIONS}
    for record in records.values():
        status_counts[record["status"]] += 1
        position_counts[record["position"]] += 1
    empty_result = {
        "trace_id": trace.get("traceID"),
        "spans_seen": len(spans),
        "attempt_found": False,
        "attempt": None,
        "attempt_operation": root_operation,
        "attempt_basis": "the measured operation does not appear in this payload",
        "ancestors_of_attempt": [],
        "ancestry_truncated": False,
        "child_spans": [],
        "trace_root": brief(root_ids[0]) if root_ids else None,
        "root_count": len(root_ids),
        "missing_parent_ids": missing_parent_ids,
        "span_status_counts": status_counts,
        "span_position_counts": position_counts,
        "failed_spans": failed_spans,
        "edge_span": None,
        "other_edge_spans": [],
        "failure_message": None,
    }
    candidates = [span_id for span_id in order if records[span_id]["operation"] == root_operation]
    if not candidates:
        return empty_result

    servers = [
        span_id
        for span_id in candidates
        if records[span_id]["attributes"].get("span.kind") == "SPAN_KIND_SERVER"
    ]
    if servers:
        attempt_id = max(
            servers, key=lambda span_id: _as_float(records[span_id]["duration_us"]) or 0.0
        )
        attempt_basis = "the measured operation's own server span"
    else:
        below_root = [span_id for span_id in candidates if parent_in_payload(span_id)]
        attempt_id = max(
            below_root or candidates,
            key=lambda span_id: _as_float(records[span_id]["duration_us"]) or 0.0,
        )
        if below_root:
            attempt_basis = (
                "the longest span carrying the measured operation below a parentless span"
            )
        elif attempt_id in root_ids:
            attempt_basis = "the measured operation's span, itself parentless in this payload"
        else:
            attempt_basis = (
                "the longest span carrying the measured operation; no server span and no ancestry"
            )
    attempt_chain, attempt_truncated = ancestry(attempt_id)
    root_id = next((span_id for span_id in attempt_chain if span_id in root_ids), None)
    if root_id is not None:
        root_basis = "the parentless span the measured attempt descends from"
    elif attempt_id in root_ids:
        root_id = attempt_id
        root_basis = "the measured attempt itself: it names no parent in this payload"
    elif root_ids:
        root_id = root_ids[0]
        root_basis = "the first parentless span; the measured attempt's own ancestry is unresolved"
    else:
        root_basis = "none: every span in this payload names a parent the payload contains"

    def relationship(edge_id: str) -> tuple[str, int | None]:
        """How the edge span sits relative to the measured attempt."""
        if edge_id == attempt_id:
            return "same_span", 0
        chain, cut_short = ancestry(edge_id)
        if attempt_id in chain:
            hops = chain.index(attempt_id) + 1
            return ("direct_child" if hops == 1 else "descendant"), hops
        if edge_id in attempt_chain:
            return "ancestor", attempt_chain.index(edge_id) + 1
        if cut_short:
            return "missing_parent", None
        return "unrelated_branch", None

    edge_spans = [span_id for span_id in order if records[span_id]["operation"] == edge_operation]
    ranked = sorted(
        ((span_id, relationship(span_id)) for span_id in edge_spans),
        key=lambda item: (
            0
            if item[1][0] in {"direct_child", "descendant"}
            else 1
            if item[1][0] == "same_span"
            else 2,
            item[1][1] if item[1][1] is not None else 0,
            -(_as_float(records[item[0]]["duration_us"]) or 0.0),
        ),
    )
    edge = None
    other_edges: list[dict[str, Any]] = []
    for span_id, (name, hops) in ranked:
        entry = brief(span_id)
        entry["relationship"] = name
        entry["hops_from_attempt"] = hops
        entry["direct_child_of_attempt"] = hops == 1
        if edge is None:
            edge = entry
        else:
            other_edges.append(entry)

    failure_message = records[attempt_id]["message"]
    if failure_message is None:
        failure_message = next((span["message"] for span in failed_spans if span["message"]), None)
    end_times = [record["ends_at_us"] for record in records.values() if record["ends_at_us"]]
    return {
        "trace_id": trace.get("traceID"),
        "spans_seen": len(spans),
        "attempt_found": True,
        "attempt": brief(attempt_id),
        "attempt_operation": root_operation,
        "attempt_basis": attempt_basis,
        "ancestors_of_attempt": [brief(span_id) for span_id in attempt_chain],
        "ancestry_truncated": attempt_truncated,
        "child_spans": [brief(span_id) for span_id in order if parents.get(span_id) == attempt_id],
        "trace_root": brief(root_id) if root_id else None,
        "root_basis": root_basis,
        "root_count": len(root_ids),
        "missing_parent_ids": missing_parent_ids,
        "edge_span": edge,
        "other_edge_spans": other_edges,
        "failed_spans": failed_spans,
        "failure_message": failure_message,
        "span_status_counts": status_counts,
        "span_position_counts": position_counts,
        "latest_span_end_us": max(end_times) if end_times else None,
    }


def trace_finding(summary: Mapping[str, Any]) -> str:
    """Name what one trace says about the measured edge, in words a reader can use.

    ``edge_span_present_and_ok`` is the only finding that says the measured call
    itself recorded success - and it is a finding about that call, not about the
    request: a trace can hold a successful edge span beside failing spans
    elsewhere, which ``edge_span_present_and_ok_with_failures_elsewhere`` names
    rather than hides. A span that recorded no status at all is
    ``edge_span_present_without_a_status`` and never a success.
    """
    if not summary.get("attempt_found"):
        return "no_attempt_span"
    edge = summary.get("edge_span")
    errors = (summary.get("span_status_counts") or {}).get(SPAN_STATUS_ERROR, 0)
    if edge is None:
        if errors or summary.get("failed_spans"):
            return "edge_span_absent_with_a_failure_recorded"
        return "edge_span_absent_no_failure_recorded"
    if edge.get("relationship") == "unrelated_branch":
        return "edge_span_present_on_an_unrelated_branch"
    if edge.get("relationship") == "missing_parent":
        return "edge_span_present_with_its_ancestry_unresolved"
    if edge.get("status") == SPAN_STATUS_ERROR:
        return "edge_span_present_and_failing"
    if edge.get("status") == SPAN_STATUS_UNSET:
        return "edge_span_present_without_a_status"
    if errors:
        return "edge_span_present_and_ok_with_failures_elsewhere"
    return "edge_span_present_and_ok"


__all__ = [
    "ATTRIBUTABLE_FAILURE",
    "EVIDENCE_CLASSES",
    "GENERIC_CALLER_FAILURE_ONLY",
    "NO_USABLE_OBSERVATION",
    "POSITION_AFTER",
    "POSITION_BEFORE",
    "POSITION_CROSSES_END",
    "POSITION_DURING",
    "POSITION_STARTS_BEFORE",
    "POSITION_UNKNOWN",
    "SPAN_POSITIONS",
    "SPAN_STATUS_ERROR",
    "SPAN_STATUS_OK",
    "SPAN_STATUS_UNSET",
    "TRAFFIC_WITHOUT_FAILURE",
    "EvidenceSummary",
    "classify_reading",
    "position_in_window",
    "sanitize_span",
    "span_status",
    "span_status_of",
    "summarize",
    "summarize_trace",
    "trace_finding",
]
