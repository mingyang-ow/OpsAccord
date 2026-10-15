#!/usr/bin/env python
"""Capture one *fresh* fault-free Checkout -> Payment baseline.

The pass this serves has to demonstrate that the *unfaulted* local demo still
completes a checkout that reaches Payment - without arming, clearing or otherwise
touching anything. Every live entry point already in the tree injects a fault
(``scripts/validate.sh --live`` arms one for each of the seven scenarios), so the
question had no way to be asked with the tools that existed.

What it does, and what each step is allowed to mean:

* it reads the flag document from flagd-ui *first*, and records it with the instant
  it was taken, because the flag state is a premise of the measurement rather than
  a conclusion drawn from it;
* it then takes a **UTC observation-start boundary**, and from that point on looks
  only forwards: a request was observed under these flags only if the measured
  Checkout attempt *began* at or after the boundary. A trace whose request was
  already in flight when the observation started is rejected and named, however
  healthy it is and whenever its payload arrives;
* it polls the store, with bounded requests and a documented overall deadline,
  for a request that qualifies - because the store is bounded and in memory and
  ingestion lags the request it carries;
* for each retained trace it reconstructs the request path from the payload's *own*
  parent references (``opsaccord.tools.fault_evidence.summarize_trace``): the root
  is a span whose ``CHILD_OF`` parent is absent, the attempt is the measured
  operation's own *server* span, and every other span's relationship to the attempt
  is a walk over those references - direct child, descendant, ancestor, unrelated
  branch, or unresolved;
* it re-reads the flag document once a candidate appears (or at the end, if none
  did) and refuses the verdict if a relevant flag moved, became unsafe, or could not
  be read either time;
* it checks for a competing scenario driver, because another writer would share the
  demo's flags, and records how that check was made.

The flag guarantee is **two readings, not continuous observation**. A pair of
snapshots can show that the flags were safe at both instants and that nothing in
the pinned document changed between them; it cannot rule out a change that began and
ended inside the interval, and it is not a licence to call the interval continuously
safe. The extract says so in ``flag_guarantee``, and ``no_competing_driver`` is the
separate, weaker check that no other live driver was running at the moment it was
made.

Nothing here writes to the demo: no flag is set, no scenario runs, no container is
touched and no service is called directly. The only files written are the extract
in ``--out`` and the human-readable log in ``--log``, and the extract - which carries
every observed payload's sanitized spans and timestamps, not a summary of them - is
written before the log is rendered.

An empty answer is not health, ``UNSET`` is not success, a pre-boundary request is
not a fresh observation, and one verified trace is a statement about the request it
came from: not about sustained reliability, and not a P1-01 result.

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

DEFAULT_LIMIT = 20
MAX_LIMIT = 200
REQUEST_TIMEOUT_SECONDS = 10.0

#: How long the capture waits for a qualifying *new* request to appear in the store,
#: and how often it asks. Both are bounded on purpose: the store holds recent traces
#: in memory and ingestion lags the request, so "nothing yet" has to be able to end,
#: and it ends as an honest *not verified* rather than as a wider window.
DEFAULT_DEADLINE_SECONDS = 180.0
DEFAULT_POLL_INTERVAL_SECONDS = 10.0

#: What the two flag readings do and do not establish. Written into the extract so a
#: reader cannot mistake a before/after pair for continuous observation.
FLAG_GUARANTEE = (
    "Two readings bracket the observation: one taken before the observation-start "
    "boundary and one taken when a candidate appeared (or at the end, if none did). "
    "They show that every fault flag in the pinned document held its safe variant at "
    "both instants and that no variant changed between them. They cannot exclude a "
    "change that began and ended inside the interval, they are not continuous "
    "observation, and they say nothing about any other writer - which is why a "
    "competing scenario driver, which would share these flags, is checked separately."
)

#: How the competing-driver check is made, recorded with its result. This is the same
#: guard ``scripts/validate.sh`` and ``scripts/run_live_scenarios.sh`` apply before
#: they arm anything, expressed here as a read-only scan.
COMPETING_DRIVER_METHOD = (
    "scan of /proc/<pid>/cmdline for a process whose command line names both "
    "'opsaccord.cli' and 'run-live-scenarios' (the driver that shares this demo's "
    "feature flags); a point-in-time check, not a lease"
)

#: A candidate is only evidence about the flags if the measured attempt began after
#: the observation started. A request that was already in flight is rejected however
#: healthy it is: its flags were set before the reading that is supposed to bracket it.
FRESHNESS_CHECK = "attempt_after_boundary"


def log(message: str) -> None:
    """One timestamped line, the way the sibling diagnostics print theirs."""
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    print(f"{stamp} {message}", flush=True)


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, tz=UTC).isoformat(timespec="milliseconds")


def iso_us(micros: int) -> str:
    """A UTC instant at the resolution the query API reports spans in."""
    return datetime.fromtimestamp(micros / 1_000_000, tz=UTC).isoformat(timespec="microseconds")


def _epoch_micros() -> int:
    return int(time.time() * 1_000_000)


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


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
        # Stamped before the request, so a reading that *failed* still carries the
        # instant it was attempted - the pair is described by its two instants, and
        # "unreadable" with no time would leave the bracket unstated.
        "observed_at": now(),
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


def flag_summary(state: Mapping[str, Any]) -> str:
    """One word for a log line: the reading exists and holds safe variants, or not."""
    if state.get("read") and not state.get("unsafe"):
        return "safe"
    return "NOT safe or unreadable"


def flag_check(state: Mapping[str, Any], *, name: str = "fault_flags_safe") -> dict[str, Any]:
    """One flag reading as a gate, without pretending an unread one is safe."""
    if not state.get("read"):
        return {
            "name": name,
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
            "name": name,
            "ok": False,
            "detail": f"these flags are not at their safe variant: {armed}",
        }
    reads = ", ".join(f"{flag}={variants[flag]!r}" for flag in sorted(variants))
    return {
        "name": name,
        "ok": True,
        "detail": (
            f"read at {state.get('observed_at')}: every fault flag in the pinned document "
            f"holds its safe variant: {reads}"
        ),
    }


def flag_change_check(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, Any]:
    """The pair: both readings exist, nothing moved, and the later one is still safe.

    This is the *only* claim the two snapshots support. It is deliberately not
    called "the flags were safe throughout": a change that began and ended between
    the readings is invisible to it, and the extract's ``flag_guarantee`` says so.
    """
    name = "fault_flags_unchanged"
    if not before.get("read"):
        return {
            "name": name,
            "ok": False,
            "detail": (
                "the flag document could not be read before the observation "
                f"({before.get('reason')}), so there is nothing to compare the later reading "
                "against and 'unchanged' is unanswerable"
            ),
        }
    if not after.get("read"):
        return {
            "name": name,
            "ok": False,
            "detail": (
                "the flag document could not be re-read at the end of the observation "
                f"({after.get('reason')}), so a change during it cannot be ruled out"
            ),
        }
    before_variants = dict(before.get("variants") or {})
    after_variants = dict(after.get("variants") or {})
    changed = sorted(
        flag
        for flag in set(before_variants) | set(after_variants)
        if before_variants.get(flag) != after_variants.get(flag)
    )
    if changed:
        movements = ", ".join(
            f"{flag}: {before_variants.get(flag)!r} -> {after_variants.get(flag)!r}"
            for flag in changed
        )
        return {
            "name": name,
            "ok": False,
            "detail": f"these relevant flags changed during the observation: {movements}",
        }
    unsafe = list(after.get("unsafe") or [])
    if unsafe:
        armed = ", ".join(f"{flag}={after_variants.get(flag)!r}" for flag in unsafe)
        return {
            "name": name,
            "ok": False,
            "detail": f"these flags are not at their safe variant after the observation: {armed}",
        }
    return {
        "name": name,
        "ok": True,
        "detail": (
            f"read at {before.get('observed_at')} and again at {after.get('observed_at')}: "
            f"all {len(after_variants)} fault flags in the pinned document are unchanged and "
            "hold their safe variants"
        ),
    }


def driver_check(drivers: Sequence[str], *, observed_at: str) -> dict[str, Any]:
    """The competing-driver read, as a gate rather than a footnote."""
    name = "no_competing_driver"
    if drivers:
        named = "; ".join(drivers[:3])
        more = "" if len(drivers) <= 3 else f" (and {len(drivers) - 3} more)"
        return {
            "name": name,
            "ok": False,
            "detail": (
                f"{len(drivers)} competing live-scenario driver(s) are running and share this "
                f"demo's feature flags, so the flag premise cannot be held: {named}{more}"
            ),
        }
    return {
        "name": name,
        "ok": True,
        "detail": (f"none present at {observed_at}; the check is a {COMPETING_DRIVER_METHOD}"),
    }


def freshness_check(summary: Mapping[str, Any], *, boundary_us: int) -> dict[str, Any]:
    """Was the measured attempt *begun* after the observation started?

    The store answers questions about a window, and a trace that was already in
    flight when the observation started can still be *returned* by such a query.
    Asking when the attempt itself began is what separates "a request observed under
    these flags" from "a request that happened to arrive in the store afterwards".
    """
    name = FRESHNESS_CHECK
    attempt = summary.get("attempt") or None
    if attempt is None:
        return {
            "name": name,
            "ok": False,
            "detail": (
                f"no span in this payload carries {CHECKOUT_OPERATION!r}, so there is no "
                "attempt to place in time against the observation boundary"
            ),
        }
    started = attempt.get("started_at_us")
    if started is None:
        return {
            "name": name,
            "ok": False,
            "detail": (
                f"the entry attempt (span {attempt.get('span_id')}) carries no start time, so "
                "it cannot be shown to have begun after the observation boundary"
            ),
        }
    started = int(started)
    if started < boundary_us:
        return {
            "name": name,
            "ok": False,
            "detail": (
                f"the entry attempt began {iso_us(started)}, before the observation boundary "
                f"{iso_us(boundary_us)}: it was already in flight when the flags were read, so "
                "it is a historical request and not an observation under these flags"
            ),
        }
    return {
        "name": name,
        "ok": True,
        "detail": (
            f"the entry attempt began {iso_us(started)}, at or after the observation boundary "
            f"{iso_us(boundary_us)} ({started - boundary_us}us later)"
        ),
    }


def competing_drivers() -> list[str]:
    """Processes that would share the demo's fault flags with this capture.

    The same predicate ``scripts/diagnose-payment-fault.py`` uses, and the same one
    ``scripts/validate.sh`` refuses to run under: a second live scenario driver sets
    and clears the flags this capture is trying to hold still.
    """
    found: list[str] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes().replace(b"\x00", b" ").decode(errors="replace")
        except OSError:
            continue
        if "opsaccord.cli" in raw and "run-live-scenarios" in raw:
            found.append(f"{entry.name}: {raw.strip()[:120]}")
    return found


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
            f"{outside} span(s) in the payload fall outside the observed interval; they are "
            "reported here, and the gate rests on the attempt and the edge span only"
        )

    return {
        "checks": checks,
        "verified": all(check["ok"] for check in checks),
        "blocking": [check["name"] for check in checks if not check["ok"]],
        "caveats": caveats,
    }


def candidate(
    trace: Mapping[str, Any],
    *,
    query_end_us: int,
    boundary_us: int,
    flags_before: Mapping[str, Any],
    flags_after: Mapping[str, Any],
    drivers: Sequence[str],
    payment_service: str,
    catalog_service: str,
) -> dict[str, Any]:
    """One retrieved payload, gated on freshness, the premise, and the request path.

    The checks are reported in the order a reader has to settle them: when the
    attempt began, whether the flags held, whether anyone else could move them, and
    then the four properties of the request itself.
    """
    summary = summarize_trace(
        trace,
        root_operation=CHECKOUT_OPERATION,
        edge_operation=PAYMENT_OPERATION,
        window_start_us=boundary_us,
        window_end_us=query_end_us,
    )
    verdict = evaluate_summary(
        summary, payment_service=payment_service, catalog_service=catalog_service
    )
    freshness = freshness_check(summary, boundary_us=boundary_us)
    checks = [
        freshness,
        flag_check(flags_before, name="fault_flags_safe_before"),
        flag_change_check(flags_before, flags_after),
        driver_check(drivers, observed_at=str(flags_after.get("observed_at") or now())),
        *verdict["checks"],
    ]
    started, ended = trace_bounds(summary)
    return {
        "trace_id": summary.get("trace_id"),
        "started_at": iso(started / 1e6) if started else None,
        "started_at_us": started,
        "ended_at": iso(ended / 1e6) if ended else None,
        "ended_at_us": ended,
        "spans_seen": summary.get("spans_seen"),
        "fresh": bool(freshness["ok"]),
        "verified": all(check["ok"] for check in checks),
        "blocking": [check["name"] for check in checks if not check["ok"]],
        "checks": checks,
        "caveats": verdict["caveats"],
        "summary": summary,
    }


def capture(
    jaeger: JaegerClient,
    *,
    limit: int,
    read_flags: Callable[[], Mapping[str, Any]],
    competing_driver: Callable[[], list[str]],
    deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    now_us: Callable[[], int] | None = None,
    sleep: Callable[[float], None] | None = None,
    payment_service: str = PAYMENT_SERVICE,
    catalog_service: str = CATALOG_SERVICE,
) -> dict[str, Any]:
    """Observe forwards from a UTC boundary until one fresh request qualifies.

    The order is the point. The flag document is read *before* the boundary, so the
    reading cannot have been caused by the request it is meant to bracket; the
    boundary is then fixed; and only requests whose measured attempt starts at or
    after it are considered. A request that was already in flight is rejected by
    name, however healthy it is.
    """
    clock = now_us or _epoch_micros
    nap = sleep or _sleep
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

    flags_before = dict(read_flags())
    boundary_us = clock()
    log(
        f"observation boundary {iso_us(boundary_us)}: only an attempt beginning at or after it "
        "counts as an observation under these flags"
    )
    log(f"flags read before the boundary: {flag_summary(flags_before)}")

    deadline_us = boundary_us + int(deadline_seconds * 1_000_000)
    requests_ok = 0
    requests_failed = 0
    observations: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    flags_after: dict[str, Any] | None = None
    drivers: list[str] = []
    deadline_exceeded = False

    while True:
        query_end_us = clock()
        try:
            traces = jaeger.traces(
                service=CHECKOUT_SERVICE,
                start_us=boundary_us,
                end_us=query_end_us,
                timeout_seconds=REQUEST_TIMEOUT_SECONDS,
                limit=limit,
            )
            requests_ok += 1
        except Exception as exc:
            requests_failed += 1
            failures["traces_for_checkout"] = f"{type(exc).__name__}: {exc}"
            log(
                f"traces query failed ({requests_failed} so far): {failures['traces_for_checkout']}"
            )
            traces = []

        known = {item["trace_id"] for item in observations}
        fresh_payloads = [trace for trace in traces if str(trace.get("traceID")) not in known]
        for trace in fresh_payloads:
            observations.append({"trace_id": str(trace.get("traceID")), "end_us": query_end_us})
        if fresh_payloads:
            log(f"{len(fresh_payloads)} new payload(s); {len(observations)} observed so far")
            if flags_after is None:
                flags_after = dict(read_flags())
                drivers = list(competing_driver())
                log(
                    "flags re-read after the first payload arrived: "
                    f"{flag_summary(flags_after)}"
                    f"; competing drivers: {len(drivers)}"
                )
            for trace in fresh_payloads:
                candidates.append(
                    candidate(
                        trace,
                        query_end_us=query_end_us,
                        boundary_us=boundary_us,
                        flags_before=flags_before,
                        flags_after=flags_after,
                        drivers=drivers,
                        payment_service=payment_service,
                        catalog_service=catalog_service,
                    )
                )
            if any(item["verified"] for item in candidates):
                break

        if clock() >= deadline_us:
            deadline_exceeded = True
            log(
                f"deadline reached after {requests_ok} completed request(s) and "
                f"{requests_failed} failed one(s): {len(observations)} payload(s) observed"
            )
            break
        remaining_s = max((deadline_us - clock()) / 1_000_000, 0.0)
        step = poll_interval_seconds if poll_interval_seconds > 0 else remaining_s
        nap(min(step, remaining_s))

    if flags_after is None:
        # Nothing ever arrived, so the second reading happens here instead - the pair
        # still brackets the interval, and it is still reported as *not* continuous.
        flags_after = dict(read_flags())
        drivers = list(competing_driver())

    candidates.sort(key=lambda item: item["started_at_us"] or 0, reverse=True)
    chosen = next((item for item in candidates if item["verified"]), None)
    if chosen is None and candidates:
        # Newest first, so the first of the equally-strong candidates is the newest.
        chosen = max(candidates, key=lambda item: sum(1 for c in item["checks"] if c["ok"]))

    if chosen is not None and chosen["verified"]:
        verdict_name = "baseline_verified"
    elif requests_ok == 0:
        verdict_name = "not_verified_query_failed"
    elif not candidates:
        verdict_name = "not_verified_no_trace_in_interval"
    elif not any(item["fresh"] for item in candidates):
        verdict_name = "not_verified_no_fresh_attempt"
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
        "flag_control_base_url": flags_before.get("base_url"),
        "service": CHECKOUT_SERVICE,
        "entry_operation": CHECKOUT_OPERATION,
        "edge_operation": PAYMENT_OPERATION,
        "catalog_service": catalog_service,
        "payment_service": payment_service,
        "observation_start": iso_us(boundary_us),
        "observation_start_us": boundary_us,
        "deadline_seconds": deadline_seconds,
        "poll_interval_seconds": poll_interval_seconds,
        "deadline_exceeded": deadline_exceeded,
        "polling": {
            "requests_completed": requests_ok,
            "requests_failed": requests_failed,
            "payloads_observed": len(observations),
            "deadline_exceeded": deadline_exceeded,
        },
        "flags_before": flags_before,
        "flags_after": flags_after,
        "flag_guarantee": FLAG_GUARANTEE,
        "competing_driver": {
            "method": COMPETING_DRIVER_METHOD,
            "observed_at": flags_after.get("observed_at"),
            "found": drivers,
        },
        "stages": stages,
        "failures": failures,
        "traces_retrieved": len(observations),
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
            "one verified trace describes the request it came from; it is not sustained "
            "reliability, and it is not a P1-01 result",
            "UNSET is reported as UNSET: a span that wrote no status is not a success",
            FLAG_GUARANTEE,
            "a request that began before the observation boundary is rejected however healthy it "
            "is, and ingestion delay means the newest traffic may still be missing from a query",
        ],
        "exit_status": exit_status,
    }


def render(document: Mapping[str, Any]) -> str:
    """The same document as a human-readable log: verdict first, then the evidence."""
    flags_before = document.get("flags_before") or {}
    flags_after = document.get("flags_after") or {}
    lines = [
        f"verdict: {document['verdict']}",
        f"captured: {document['captured_at']}",
        f"jaeger: {document['jaeger_base_url']}",
        "flagd: "
        f"{document.get('flag_control_base_url') or '(no flag endpoint in the environment)'}",
        f"observation start: {document['observation_start']}",
        f"polling: {document['polling']} (deadline {document['deadline_seconds']}s, "
        f"interval {document['poll_interval_seconds']}s)",
        f"looked for: {document['service']} {document['entry_operation']!r} reaching "
        f"{document['payment_service']} {document['edge_operation']!r} through "
        f"{document['catalog_service']}",
        f"payloads observed: {document['traces_retrieved']}",
        f"chosen trace: {document.get('chosen_trace_id') or '(none)'}",
    ]
    if document.get("blocking"):
        lines.append(f"blocking: {', '.join(str(name) for name in document['blocking'])}")

    lines.append("flags before the boundary:")
    lines.extend(_flag_lines(flags_before))
    lines.append("flags after the observation:")
    lines.extend(_flag_lines(flags_after))
    lines.append(f"flag guarantee: {document.get('flag_guarantee')}")
    driver = document.get("competing_driver") or {}
    lines.append(
        f"competing drivers: {driver.get('found') or 'none'} (checked at "
        f"{driver.get('observed_at')}; {driver.get('method')})"
    )

    for item in document.get("candidates") or []:
        lines.append(
            f"trace {item['trace_id']} started {item['started_at']} "
            f"ended {item['ended_at']} spans={item['spans_seen']} "
            f"fresh={item['fresh']} verified={item['verified']}"
        )
        for check in item["checks"]:
            mark = "PASS" if check["ok"] else "FAIL"
            lines.append(f"  [{mark}] {check['name']}: {check['detail']}")
        for caveat in item["caveats"]:
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


def _flag_lines(state: Mapping[str, Any]) -> list[str]:
    lines = [f"  read: {bool(state.get('read'))} at {state.get('observed_at')}"]
    if not state.get("read"):
        lines.append(f"  {state.get('reason')}")
    for flag_id, variant in sorted((state.get("variants") or {}).items()):
        safe = (state.get("safe_variant") or {}).get(flag_id)
        armed = "" if variant == safe else " ARMED"
        lines.append(f"  {flag_id}: {variant!r} (safe {safe!r}){armed}")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "exit status: 0 a fault-free baseline was verified, 1 it was not (whatever the "
            "reason), 2 bad arguments, 3 it refused to start"
        ),
    )
    parser.add_argument(
        "--deadline-seconds",
        type=float,
        default=DEFAULT_DEADLINE_SECONDS,
        help="how long to wait for a qualifying new request to reach the store",
    )
    parser.add_argument(
        "--poll-interval-seconds",
        type=float,
        default=DEFAULT_POLL_INTERVAL_SECONDS,
        help="how often to ask the store while waiting",
    )
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--payment-service", default=PAYMENT_SERVICE)
    parser.add_argument("--catalog-service", default=CATALOG_SERVICE)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--log", type=Path, default=None)
    args = parser.parse_args(argv)

    if not 0 < args.limit <= MAX_LIMIT:
        parser.error(f"--limit must be within (0, {MAX_LIMIT}]")
    if args.deadline_seconds <= 0:
        parser.error("--deadline-seconds must be positive")
    if args.poll_interval_seconds <= 0:
        parser.error("--poll-interval-seconds must be positive")
    base = os.environ.get("OPSACCORD_JAEGER_BASE_URL")
    if base is None:
        log(
            "refusing to start: no Jaeger endpoint in the environment; source "
            ".local/demo-endpoints.env first (scripts/demo.sh endpoints writes it)"
        )
        return 3

    stamp = int(time.time())
    out_path = args.out or (Path("reports/local") / f"baseline-checkout-{stamp}.json")
    log_path = args.log or out_path.with_suffix(".log")

    client = httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS, trust_env=False)
    try:

        def read_flags() -> Mapping[str, Any]:
            return read_flag_variants(
                os.environ.get("OPSACCORD_FLAG_CONTROL_BASE_URL"),
                client=client,
                timeout_seconds=REQUEST_TIMEOUT_SECONDS,
            )

        document = capture(
            JaegerClient(base, client=client),
            limit=args.limit,
            read_flags=read_flags,
            competing_driver=competing_drivers,
            deadline_seconds=args.deadline_seconds,
            poll_interval_seconds=args.poll_interval_seconds,
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
