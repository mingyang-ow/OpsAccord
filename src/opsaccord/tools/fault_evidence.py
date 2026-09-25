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


def _parent_span_id(span: Mapping[str, Any]) -> str | None:
    for reference in span.get("references") or []:
        if str(reference.get("refType", "")).upper() == "CHILD_OF":
            parent = reference.get("spanID")
            return str(parent) if parent else None
    return None


def _status(attributes: Mapping[str, Any]) -> str:
    code = str(attributes.get("otel.status_code", "")).upper()
    errored = str(attributes.get("error", "")).lower() in {"true", "1"}
    return "ERROR" if code in {"ERROR", "STATUS_CODE_ERROR"} or errored else "OK"


def _message(attributes: Mapping[str, Any]) -> str | None:
    for key in ("otel.status_description", "error.message", "exception.message", "message"):
        value = attributes.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def summarize_trace(
    trace: Mapping[str, Any], *, root_operation: str, edge_operation: str
) -> dict[str, Any]:
    """What one retained trace shows about the operation that failed.

    The question a failing trace has to settle is which child of the failing
    operation failed, and whether the measured edge appears under it at all. A
    failure the edge series cannot see is either a failure that happened before
    the edge was called, or one whose call produced no span; the error text tells
    those apart, so it is kept verbatim.

    Each span is reported with the service that emitted it, because for this
    question the interesting fact is precisely that the caller's *client* span for
    the edge belongs to ``checkout`` and the callee's *server* span to ``payment``.
    """
    spans = [span for span in (trace.get("spans") or []) if isinstance(span, Mapping)]
    sanitized = {str(span.get("spanID")): sanitize_span(span) for span in spans}
    processes = trace.get("processes") or {}
    services = {
        span_id: (processes.get(str(span.get("processID"))) or {}).get("serviceName")
        for span_id, span in ((str(item.get("spanID")), item) for item in spans)
    }
    for span_id, record in sanitized.items():
        record["service"] = services.get(span_id)
    roots = [span for span in spans if span.get("operationName") == root_operation]
    if not roots:
        return {
            "trace_id": trace.get("traceID"),
            "root_found": False,
            "root_operation": root_operation,
        }
    root = max(roots, key=lambda span: float(span.get("duration") or 0.0))
    root_id = str(root.get("spanID"))
    root_record = sanitized[root_id]
    children = [span for span in spans if _parent_span_id(span) == root_id]
    edge_spans = [span for span in spans if span.get("operationName") == edge_operation]
    failed_spans = [
        {
            "operation": record["operation"],
            "service": record["service"],
            "message": _message(record["attributes"]),
            "span_kind": record["attributes"].get("span.kind"),
        }
        for record in sanitized.values()
        if _status(record["attributes"]) == "ERROR"
    ]
    edge = None
    if edge_spans:
        edge_span = max(edge_spans, key=lambda span: float(span.get("duration") or 0.0))
        record = sanitized[str(edge_span.get("spanID"))]
        edge = {
            "operation": record["operation"],
            "service": record["service"],
            "span_kind": record["attributes"].get("span.kind"),
            "status": _status(record["attributes"]),
            "message": _message(record["attributes"]),
            "duration_us": record["duration_us"],
            "child_of_root": _parent_span_id(edge_span) == root_id,
        }
    failure_message = _message(root_record["attributes"])
    if failure_message is None and failed_spans:
        failure_message = next((span["message"] for span in failed_spans if span["message"]), None)
    return {
        "trace_id": trace.get("traceID"),
        "root_found": True,
        "root_operation": root_operation,
        "root": {
            "operation": root_record["operation"],
            "service": root_record["service"],
            "status": _status(root_record["attributes"]),
            "message": _message(root_record["attributes"]),
            "duration_us": root_record["duration_us"],
            "started_at_us": root.get("startTime"),
        },
        "child_spans": [
            {
                "operation": sanitized[str(span.get("spanID"))]["operation"],
                "service": sanitized[str(span.get("spanID"))]["service"],
                "span_kind": sanitized[str(span.get("spanID"))]["attributes"].get("span.kind"),
                "status": _status(sanitized[str(span.get("spanID"))]["attributes"]),
                "message": _message(sanitized[str(span.get("spanID"))]["attributes"]),
            }
            for span in children
        ],
        "failed_spans": failed_spans,
        "edge_span": edge,
        "failure_message": failure_message,
    }


def trace_finding(summary: Mapping[str, Any]) -> str:
    """Name what one trace says about the measured edge, in words a reader can use."""
    if not summary.get("root_found"):
        return "no_root_span"
    edge = summary.get("edge_span")
    if edge is None:
        if (summary.get("root") or {}).get("status") == "ERROR" or summary.get("failed_spans"):
            return "edge_span_absent_with_a_failure_recorded"
        return "edge_span_absent_no_failure_recorded"
    if edge.get("status") == "ERROR":
        return "edge_span_present_and_failing"
    return "edge_span_present_and_ok"


__all__ = [
    "ATTRIBUTABLE_FAILURE",
    "EVIDENCE_CLASSES",
    "GENERIC_CALLER_FAILURE_ONLY",
    "NO_USABLE_OBSERVATION",
    "TRAFFIC_WITHOUT_FAILURE",
    "EvidenceSummary",
    "classify_reading",
    "sanitize_span",
    "summarize",
    "summarize_trace",
    "trace_finding",
]
