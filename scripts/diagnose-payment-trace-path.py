#!/usr/bin/env python
"""Read-only inspection of retained traces for the Payment fault path.

This answers, from Jaeger alone, the question the metric capture could not settle:
when checkout's ``PlaceOrder`` failed while ``paymentUnreachable`` was armed, did
the request reach the measured ``Charge`` edge at all, and what did the failing
span actually record? A metric series can only say that no Charge span was
counted; a trace says whether a Charge span exists, which service emitted it, and
what the error text was.

Nothing here writes anything. Every request is an HTTP GET against Jaeger's query
API, the time range is explicit and bounded, no flag is armed, cleared or changed,
and no scenario runs. There is therefore nothing to restore and no competing
driver to guard against: this reads evidence that was already collected.

Usage (after ``scripts/demo.sh endpoints`` has written the endpoint file):

    set -a; . .local/demo-endpoints.env; set +a
    .venv/bin/python scripts/diagnose-payment-trace-path.py --lookback-minutes 240
    .venv/bin/python scripts/diagnose-payment-trace-path.py \
        --start 2026-09-25T00:56:00Z --end 2026-09-25T01:06:00Z

The pinned revision runs Jaeger with an in-memory store (``max_traces`` 25000), so
retention is by count rather than by age. When the requested interval has already
been evicted the script says so and reports the window it could actually see
instead of reporting an absence of failures as a result.

Exit status: ``0`` the query completed (whatever it found), ``1`` a query failed,
``3`` it refused to start.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

import httpx  # noqa: E402

from opsaccord.tools.fault_evidence import (  # noqa: E402
    summarize_trace,
    trace_finding,
)

CALLER_SERVICE = "checkout"
PLACE_ORDER_SPAN = "oteldemo.CheckoutService/PlaceOrder"
CHARGE_SPAN = "oteldemo.PaymentService/Charge"
REQUEST_TIMEOUT_SECONDS = 8.0
DEFAULT_LIMIT = 40
DEFAULT_MAX_TRACES = 5
DEFAULT_MAX_SUCCESS = 2
MAX_LIMIT = 200


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def log(message: str) -> None:
    print(f"{now()} {message}", flush=True)


def iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, tz=UTC).isoformat(timespec="seconds")


def iso_to_micros(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1_000_000)


def trace_bounds(trace: dict[str, Any]) -> tuple[float, float]:
    starts = [float(span.get("startTime") or 0) for span in trace.get("spans") or []]
    durs = [float(span.get("duration") or 0) for span in trace.get("spans") or []]
    if not starts:
        return 0.0, 0.0
    return min(starts) / 1e6, (max(starts) + max(durs)) / 1e6


def trace_failed(trace: dict[str, Any], summary: dict[str, Any]) -> bool:
    if (summary.get("root") or {}).get("status") == "ERROR":
        return True
    return any(span.get("status") == "ERROR" for span in summary.get("child_spans") or [])


def load_traces(
    client: httpx.Client, *, service: str, operation: str, start_us: int, end_us: int, limit: int
) -> list[dict[str, Any]]:
    response = client.get(
        "/api/traces",
        params={
            "service": service,
            "operation": operation,
            "start": start_us,
            "end": end_us,
            "limit": limit,
        },
    )
    response.raise_for_status()
    return list(response.json().get("data") or [])


def describe(summary: dict[str, Any], finding: str) -> str:
    root = summary.get("root") or {}
    lines = [
        f"  root: {root.get('operation')} service={root.get('service')} "
        f"status={root.get('status')} duration_ms="
        f"{(root.get('duration_us') or 0) / 1000.0:.1f} message={root.get('message')!r}",
        "  children: "
        + (
            " | ".join(
                f"{child['operation']}({child.get('service')},{child.get('span_kind')},"
                f"{child['status']})"
                for child in summary.get("child_spans") or []
            )
            or "(none)"
        ),
    ]
    edge = summary.get("edge_span")
    lines.append(
        "  edge: "
        + (
            f"{edge['operation']} service={edge.get('service')} kind={edge.get('span_kind')} "
            f"status={edge['status']} child_of_root={edge.get('child_of_root')} "
            f"message={edge.get('message')!r}"
            if edge
            else "absent from the whole trace"
        )
    )
    failed = summary.get("failed_spans") or []
    lines.append(
        "  failing spans: "
        + (
            " | ".join(
                f"{span['operation']}({span.get('service')},{span.get('span_kind')}) "
                f"message={span.get('message')!r}"
                for span in failed
            )
            or "(none)"
        )
    )
    lines.append(f"  finding: {finding}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "exit status: 0 the query completed (whatever it found), 1 a query failed, "
            "3 it refused to start"
        ),
    )
    parser.add_argument("--start", default=None, help="UTC ISO start, e.g. 2026-09-25T00:56:00Z")
    parser.add_argument("--end", default=None, help="UTC ISO end (default: now)")
    parser.add_argument("--lookback-minutes", type=float, default=180.0)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--max-traces", type=int, default=DEFAULT_MAX_TRACES)
    parser.add_argument("--max-success", type=int, default=DEFAULT_MAX_SUCCESS)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    if not 0 < args.limit <= MAX_LIMIT:
        parser.error(f"--limit must be within (0, {MAX_LIMIT}]")
    base = os.environ.get("OPSACCORD_JAEGER_BASE_URL")
    if base is None:
        parser.error(
            "no Jaeger endpoint in the environment; source .local/demo-endpoints.env first "
            "(scripts/demo.sh endpoints writes it)"
        )

    end = iso_to_micros(args.end) if args.end else int(time.time() * 1_000_000)
    start = iso_to_micros(args.start) if args.start else end - int(args.lookback_minutes * 60 * 1e6)
    if start >= end:
        parser.error("--start must be before --end")

    out_path = args.out or (Path("reports/local") / f"payment-trace-path-{int(time.time())}.json")
    extract: dict[str, Any] = {
        "kind": "opsaccord.payment-trace-path",
        "note": (
            "read-only trace extract: no flag was armed or cleared, no scenario ran, nothing "
            "was written to the demo"
        ),
        "queried_at": now(),
        "jaeger_base_url": base,
        "window": {"start": iso(start / 1e6), "end": iso(end / 1e6)},
        "service": CALLER_SERVICE,
        "operation": PLACE_ORDER_SPAN,
        "edge_operation": CHARGE_SPAN,
        "traces": [],
    }
    client = httpx.Client(base_url=base.rstrip("/"), timeout=REQUEST_TIMEOUT_SECONDS)
    exit_status = 0
    try:
        try:
            services = client.get("/api/services").json().get("data") or []
            extract["services_visible"] = sorted(str(name) for name in services)
            log(f"services visible in Jaeger: {len(services)}")
        except Exception as exc:
            extract["services_visible"] = {"error": f"{type(exc).__name__}: {exc}"}
            log(f"could not list services: {type(exc).__name__}: {exc}")
        traces = load_traces(
            client,
            service=CALLER_SERVICE,
            operation=PLACE_ORDER_SPAN,
            start_us=start,
            end_us=end,
            limit=args.limit,
        )
        log(
            f"{PLACE_ORDER_SPAN} traces in {extract['window']['start']}.."
            f"{extract['window']['end']}: {len(traces)}"
        )
        summaries: list[dict[str, Any]] = []
        for trace in traces:
            summary = summarize_trace(
                trace, root_operation=PLACE_ORDER_SPAN, edge_operation=CHARGE_SPAN
            )
            summary["failed"] = trace_failed(trace, summary)
            summary["finding"] = trace_finding(summary)
            bounds = trace_bounds(trace)
            summary["started_at"] = iso(bounds[0]) if bounds[0] else None
            summaries.append(summary)
        summaries.sort(key=lambda item: item.get("started_at") or "")
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
        failing = [item for item in summaries if item["failed"]]
        successful = [item for item in summaries if not item["failed"]]
        extract["counts"] = {
            "traces": len(summaries),
            "failed": len(failing),
            "ok": len(successful),
        }
        extract["finding_counts"] = dict(Counter(item["finding"] for item in summaries))
        for label, chosen in (
            ("failing", failing[: args.max_traces]),
            ("successful", successful[: max(args.max_success, 0)]),
        ):
            for item in chosen:
                log(f"[{label} trace] id={item.get('trace_id')} started={item.get('started_at')}")
                for line in describe(item, item["finding"]).splitlines():
                    log(line)
        log(f"trace counts: {extract['counts']} findings: {extract['finding_counts']}")
        if not summaries:
            log(
                "no PlaceOrder trace was retained for this window; the pinned stack keeps traces "
                "in memory with a 25000-trace cap, so the interval may already have been evicted. "
                "That is a retention fact, not a statement about the fault."
            )
        elif not failing:
            log(
                "every retained PlaceOrder trace in the window succeeded; a capture that armed the "
                "fault would have to be re-run and this script pointed at its window"
            )
    except Exception as exc:
        extract["error"] = f"{type(exc).__name__}: {exc}"
        log(f"trace query failed: {extract['error']}")
        exit_status = 1
    finally:
        client.close()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    extract["exit_status"] = exit_status
    out_path.write_text(json.dumps(extract, indent=1, sort_keys=True), encoding="utf-8")
    log(f"extract written to {out_path}")
    log(f"exit status: {exit_status}")
    return exit_status


if __name__ == "__main__":
    raise SystemExit(main())
