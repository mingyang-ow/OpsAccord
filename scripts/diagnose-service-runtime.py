#!/usr/bin/env python
"""Read one service's own runtime metrics, to separate "starved" from "blocked".

Three services in this stack can present the same symptom - a gRPC health probe
that times out while the container stays ``Up`` - for two different reasons:

* the process is *starved*: its Go runtime cannot get enough CPU to answer,
  because the configured ceiling is far below what the workload needs and the
  garbage collector runs back-to-back to stay underneath it;
* the process is *blocked*: it is waiting on something else - a dependency, its
  own telemetry export path, a lock - and no amount of CPU would help.

``docker stats`` cannot tell those apart. It reports CPU percent and one memory
sample, and a GC-limited process looks exactly like a busy one: high CPU and a
resident set that can sit *well below* the configured limit, because the limit is
what forces the collector to keep returning memory to the operating system. The
numbers that separate the two cases are the runtime's own: how much of the CPU
time the collector is spending, how fast the heap is growing, how many goroutines
are parked. This reads those from Prometheus, where the demo's OTLP pipeline
publishes them.

It is discovery-driven on purpose. The metric name a service emits depends on the
exporter shape its SDK uses (``go_memstats_*`` for the classic Prometheus
instrumentation, ``go_memory_*`` from the OTLP runtime instrumentation the pinned
revision enables with ``OTEL_GO_X_OBSERVABILITY=true``), and no run has yet
established which names this stack's store actually holds. So the script first
asks the server which names it advertises, keeps the runtime-looking ones, and
queries only those. A name that is absent is reported as absent rather than
guessed at, and the ``service_name`` label values are printed so that "this
service reports nothing at all" is visible as such.

That guess is then checked against the store, because a guessed name list cannot
distinguish a service whose export path is broken from one whose metrics are named
something this script never asked about - on 2026-09-26 it reported "0 of 24 runtime
metrics have a value" for a service the store advertised 347 names for, and both
causes read the same. For each service the script first runs
``count by (__name__) ({service_name="..."})``, which lists the metric names the
store really holds for that service. An empty answer is then a statement about the
service, and any name in that list which the discovery never asked about is printed
as ``held but never asked about`` - the vocabulary to build the next discovery from.

Read-only: every request is an HTTP GET against the Prometheus query API with an
explicit timeout. No flag is armed, nothing is written to the demo, no service is
started, stopped or restarted.

What this does *not* claim. A high collector fraction is consistent with a too
small ceiling, but it is not proof that raising it will help, and a low one does
not clear the ceiling of blame - it moves the question to what the process is
waiting for. Absent metrics are not evidence that a service is healthy or dead:
the demo forwards its metrics through the collector, and a service whose export
path is timing out is exactly the service whose runtime numbers will be missing,
which is itself the finding.

Usage (after ``scripts/demo.sh endpoints`` has written the endpoint file):

    set -a; . .local/demo-endpoints.env; set +a
    .venv/bin/python scripts/diagnose-service-runtime.py
    .venv/bin/python scripts/diagnose-service-runtime.py --services product-catalog

Exit status: ``0`` every query completed (whatever they found), ``1`` at least one
query failed, ``2`` bad arguments, ``3`` no Prometheus setting to read.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from opsaccord.tools.live_environment import (  # noqa: E402
    LiveEnvironmentUnavailable,
    PrometheusClient,
    ReadTimeout,
)

DEFAULT_SERVICES = ("product-catalog", "checkout", "payment")
DEFAULT_TIMEOUT_SECONDS = 10.0
MAX_METRICS = 40
# How many names of an unguessed family to print before summarizing the rest. The
# count is printed either way; this only keeps one service's vocabulary from
# burying the next service's readings.
MAX_UNREAD_NAMES = 30

# What "resource-bound" looks like in a metric name: a runtime or process family
# crossed with one of the quantities that decides it. Substring matching, because
# the exact names differ between instrumentation shapes.
FAMILY_PREFIXES = ("go_", "process_", "runtime_", "container_")
FAMILY_TOKENS = (
    "gc",
    "memory",
    "goroutine",
    "threads",
    "cpu",
    "resident",
    "virtual",
)

# A label value goes inside a PromQL selector, so it is checked to be a plain
# service name before it is interpolated; a name with a brace or a quote in it
# would otherwise turn a read into a parse error, which is the failure that cost
# an earlier pass a live run.
SAFE_LABEL_VALUE = re.compile(r"[A-Za-z0-9_.-]+\Z")


def runtime_metric_names(advertised: set[str], *, limit: int = MAX_METRICS) -> list[str]:
    """The runtime-looking subset of the names the server advertises."""
    picked = []
    for name in sorted(advertised):
        lowered = name.lower()
        if not lowered.startswith(FAMILY_PREFIXES):
            continue
        if not any(token in lowered for token in FAMILY_TOKENS):
            continue
        picked.append(name)
    return picked[:limit]


def read_service_runtime(
    client: PrometheusClient,
    service: str,
    metrics: list[str],
    timeout_seconds: float,
) -> dict[str, Any]:
    """Every discovered metric for one service, with its value or why it is absent."""
    if not SAFE_LABEL_VALUE.fullmatch(service):
        raise ValueError(f"{service!r} is not a plain service name")
    readings: dict[str, float | None] = {}
    failures: list[str] = []
    # What the store actually holds for this service, before any guess about what
    # the names should be. Discovery by substring can only ever answer "is one of
    # my guesses present"; on 2026-09-26 that produced "0 of 24 runtime metrics
    # have a value" for a service the store advertised 347 names for, and the two
    # causes of that - a broken export path, or names this script never thought to
    # ask about - look identical from there. One probe separates them, and an
    # empty answer then means the service publishes nothing rather than that the
    # guess was wrong.
    scope = f'{{service_name="{service}"}}'
    try:
        held = client.metric_names_for(scope, timeout_seconds)
    except (LiveEnvironmentUnavailable, ReadTimeout) as exc:
        held = set()
        failures.append(f"count by (__name__) ({scope}): {exc}")
    for name in metrics:
        selector = f'{name}{{service_name="{service}"}}'
        try:
            # Summed, not sampled: one service can publish several series under
            # one name (per method, per status), and the first of those is an
            # artefact of the store's ordering rather than a reading of the
            # process. A sum over an absent name is still empty, so "the service
            # does not report this" stays visible.
            readings[name] = client.scalar(f"sum({selector})", timeout_seconds)
        except (LiveEnvironmentUnavailable, ReadTimeout) as exc:
            readings[name] = None
            failures.append(f"{selector}: {exc}")
    found = {name: value for name, value in readings.items() if value is not None}
    return {
        "service": service,
        "metrics": readings,
        "values": found,
        # Names the service really publishes that this script never asked about.
        # A non-empty list is the finding that the guessed families were wrong,
        # and it is the list to build the next discovery from.
        "names_held_but_not_read": sorted(held.difference(metrics)),
        "held_metric_names": sorted(held),
        "failures": failures,
    }


def collect(
    client: PrometheusClient,
    services: tuple[str, ...],
    timeout_seconds: float,
) -> dict[str, Any]:
    """Discovery plus one bounded read per discovered metric, per service."""
    advertised = client.series_names(timeout_seconds)
    metrics = runtime_metric_names(advertised)
    try:
        reporting = sorted(client.label_values("service_name", timeout_seconds))
    except (LiveEnvironmentUnavailable, ReadTimeout) as exc:
        reporting = []
        label_failure: str | None = str(exc)
    else:
        label_failure = None
    return {
        "advertised_metric_names": len(advertised),
        "runtime_metric_names": metrics,
        "service_name_label_values": reporting,
        "service_name_label_failure": label_failure,
        "services": [read_service_runtime(client, s, metrics, timeout_seconds) for s in services],
    }


def report(document: dict[str, Any]) -> None:
    print(f"prometheus advertised {document['advertised_metric_names']} metric names")
    print(f"runtime-looking names kept: {len(document['runtime_metric_names'])}")
    for name in document["runtime_metric_names"]:
        print(f"  {name}")
    if document["service_name_label_failure"] is not None:
        print(f"service_name label values unreadable: {document['service_name_label_failure']}")
    else:
        values = document["service_name_label_values"]
        print(f"services reporting metrics ({len(values)}): {' '.join(values) or '<none>'}")
    for reading in document["services"]:
        service = reading["service"]
        found = reading["values"]
        total = len(reading["metrics"])
        print(f"\n{service}: {len(found)} of {total} runtime metrics have a value")
        held = reading["held_metric_names"]
        if not held:
            print(f'  the store holds no series at all under service_name="{service}"')
        else:
            print(f"  the store holds {len(held)} metric names for this service")
            unread = reading["names_held_but_not_read"]
            if unread:
                shown = unread[:MAX_UNREAD_NAMES]
                more = f" (+{len(unread) - len(shown)} more)" if len(unread) > len(shown) else ""
                print(f"  held but never asked about ({len(unread)}): {' '.join(shown)}{more}")
        for name, value in sorted(found.items()):
            print(f"  {name} = {value:g}")
        absent = [n for n, v in reading["metrics"].items() if v is None]
        if absent:
            print(f"  no value from: {' '.join(absent)}")
        for failure in reading["failures"]:
            print(f"  query failed: {failure}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--services",
        nargs="+",
        default=list(DEFAULT_SERVICES),
        help=f"service_name values to read (default: {' '.join(DEFAULT_SERVICES)})",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"per-request timeout in seconds (default: {DEFAULT_TIMEOUT_SECONDS:g})",
    )
    parser.add_argument("--out", help="write the same document as JSON to this path")
    parser.add_argument(
        "--prometheus",
        default=os.environ.get("OPSACCORD_PROMETHEUS_BASE_URL", ""),
        help="Prometheus base URL (default: $OPSACCORD_PROMETHEUS_BASE_URL)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.prometheus:
        print(
            "no Prometheus base URL: set OPSACCORD_PROMETHEUS_BASE_URL or pass --prometheus",
            file=sys.stderr,
        )
        return 3
    if args.timeout <= 0:
        print("--timeout must be positive", file=sys.stderr)
        return 2

    client = PrometheusClient(args.prometheus)
    try:
        document = collect(client, tuple(args.services), args.timeout)
    except (LiveEnvironmentUnavailable, ReadTimeout) as exc:
        print(f"prometheus unreachable at {args.prometheus}: {exc}", file=sys.stderr)
        return 3

    print(f"prometheus: {args.prometheus}")
    print(f"services asked for: {' '.join(args.services)}")
    report(document)
    if args.out:
        destination = Path(args.out)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(document, indent=2, sort_keys=True), encoding="utf-8")
        print(f"\nwritten to {destination}")
    return 1 if any(reading["failures"] for reading in document["services"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
