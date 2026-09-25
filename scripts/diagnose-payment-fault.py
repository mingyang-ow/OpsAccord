#!/usr/bin/env python
"""Bounded, read-only-until-armed capture of the paymentUnreachable fault path.

DIAGNOSTIC CAPTURE, NOT SCENARIO ACCEPTANCE. This script never runs a scenario,
never writes an incident and never scores an outcome. It answers one question the
2026-09-24 run left open:

    while ``paymentUnreachable`` is armed, what does the caller's *own client call*
    to payment look like, and is that the same thing as the caller's generic
    parent-span failure?

Those two readings were measured together on 2026-09-24 and disagreed: checkout's
PlaceOrder spans showed a 17.66% failure share while the caller's own Charge client
share read 0.0, so the scenario armed a premise its own controller could not
attribute. This capture records both, side by side, with the flag position and
timestamps, so the next step is chosen from measurement rather than from inference.

It is bounded and it cleans up:

* it refuses to start beside a running scenario driver, or on a demo that already
  has a verified fault flag outside its safe position;
* the only write it ever makes is ``paymentUnreachable`` (through the harness's own
  arm/restore helpers, so ownership and read-back semantics are the harness's, not
  a second implementation), recorded with its UTC timestamps;
* every request is bounded, and the whole capture is bounded by ``--seconds``;
* the flag is restored in a ``finally`` block, including after Ctrl+C or a failed
  read, and the read-back is printed and recorded either way.

What it reports, and what it deliberately does not:

* every poll is classified into one of four evidence classes (see
  ``opsaccord.tools.fault_evidence``): an attributable failure on the Charge
  edge, a generic caller failure without it, traffic with no failure in it, or no
  usable observation. The headline ``verdict`` is the *strongest* class observed
  while armed, and the per-class counts are reported beside it, because the first
  polls of any capture read a window that predates the injection;
* ``recovery`` is ``assessed: false``. The post-restore sample averages the window
  that contains the armed period, so it cannot show that the service recovered;
* exit status is ``0`` when the capture completed and the flag was restored (the
  verdict is data, not a pass/fail), ``1`` when the capture itself failed, ``3``
  when it refused to start, and ``4`` when the flag could not be restored.

Usage (the validated path is the copy-paste block in
``docs/reports/phase1-implementation.md``; run it from the repository root after
``scripts/demo.sh endpoints`` has written ``.local/demo-endpoints.env``):

    set -a; . .local/demo-endpoints.env; set +a
    .venv/bin/python scripts/diagnose-payment-fault.py --seconds 600
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

import httpx  # noqa: E402

from opsaccord.scenarios.live import (  # noqa: E402
    DEPENDENCY_PREMISE_WINDOW_SECONDS,
    FAULT_VARIANTS,
    _ScenarioFault,  # the harness's own ownership record, reused rather than copied
    arm_fault,
    detect_residual_faults,
    live_settings,
    restore_owned_fault,
)
from opsaccord.tools.fault_evidence import (  # noqa: E402
    EVIDENCE_CLASSES,
    classify_reading,
    summarize,
)
from opsaccord.tools.live_environment import (  # noqa: E402
    CALLS_METRIC_CANDIDATES,
    CLIENT_SPAN_KIND_TOKEN,
    SERVICE_LABEL_CANDIDATES,
    SPAN_KIND_LABEL_CANDIDATES,
    LiveEnvironment,
)

SCENARIO_ID = "P1-01"
CALLER = "checkout-service"
CALLEE = "payment-service"
CHARGE_SPAN = "oteldemo.PaymentService/Charge"
PLACE_ORDER_SPAN = "oteldemo.CheckoutService/PlaceOrder"
REQUEST_TIMEOUT_SECONDS = 8.0
#: Never write more than the one flag this diagnosis is about.
FLAG_ID = "paymentUnreachable"
#: One poll cannot outlive the capture, and a capture cannot outlive a scenario run.
MAX_SECONDS = 900


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def log(message: str) -> None:
    print(f"{now()} {message}", flush=True)


def competing_drivers() -> list[str]:
    """Processes that would share the demo's fault flags with this capture."""
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


def grouped_series(client: httpx.Client, metric: str, span: str, window: int) -> Any:
    """One span's current series, split by emitting service, kind and status.

    This is the measurement, not a summary of one: the whole question is whether the
    caller's client series and the callee's server series carry the *same* failure
    share, and a single summed number cannot say that.
    """
    query = (
        f"sum by (service_name, span_kind, status_code) "
        f"(rate({metric}{{span_name='{span}'}}[{window}s]))"
    )
    try:
        payload = client.get("/api/v1/query", params={"query": query}).json()
    except Exception as exc:
        return {"query": query, "error": f"{type(exc).__name__}: {exc}"}
    result = payload.get("data", {}).get("result") or []
    return {
        "query": query,
        "series": sorted(
            (
                {
                    "labels": sample.get("metric"),
                    "rate_per_second": float(sample["value"][1]),
                }
                for sample in result
                if sample.get("value")
            ),
            key=lambda item: repr(item["labels"]),
        ),
    }


def shares(grouped: Any, *, service: str, kind_token: str | None = None) -> dict[str, float]:
    """The failure share of one emitter, from a grouped series payload."""
    calls = errors = 0.0
    for sample in grouped.get("series", []):
        labels = sample.get("labels") or {}
        if labels.get(SERVICE_LABEL_CANDIDATES[0]) != service:
            continue
        if (
            kind_token
            and kind_token not in str(labels.get(SPAN_KIND_LABEL_CANDIDATES[0], "")).lower()
        ):
            continue
        rate = float(sample["rate_per_second"])
        if labels.get("status_code") == "STATUS_CODE_ERROR":
            errors += rate
        else:
            calls += rate
    total = calls + errors
    return {
        "calls_per_second": round(total, 6),
        "errors_per_second": round(errors, 6),
        "error_share": round(errors / total, 6) if total > 0 else 0.0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "exit status: 0 capture completed and the flag was restored, 1 the capture "
            "failed, 3 it refused to start, 4 the flag could not be restored"
        ),
    )
    parser.add_argument("--seconds", type=float, default=600.0)
    parser.add_argument("--poll", type=float, default=20.0)
    parser.add_argument("--window", type=int, default=DEPENDENCY_PREMISE_WINDOW_SECONDS)
    parser.add_argument(
        "--out", type=Path, default=None, help="JSON capture path (default: reports/local)"
    )
    args = parser.parse_args(argv)

    if not 0 < args.seconds <= MAX_SECONDS:
        parser.error(f"--seconds must be within (0, {MAX_SECONDS}]")
    if os.environ.get("OPSACCORD_PROMETHEUS_BASE_URL") is None:
        parser.error(
            "no live endpoints in the environment; source .local/demo-endpoints.env first "
            "(scripts/demo.sh endpoints writes it)"
        )
    drivers = competing_drivers()
    if drivers:
        print("refusing to run: another live scenario driver is active:", file=sys.stderr)
        for line in drivers:
            print(f"  {line}", file=sys.stderr)
        return 3

    settings = live_settings("scenario")
    environment = LiveEnvironment(settings=settings)
    out_path = args.out or (
        Path("reports/local") / f"payment-fault-diagnostic-{int(time.time())}.json"
    )
    capture: dict[str, Any] = {
        "kind": "opsaccord.payment-fault-diagnostic",
        "note": (
            "diagnostic capture, not scenario acceptance: no scenario ran, no incident was "
            "written, no outcome was scored"
        ),
        "started_at": now(),
        "scenario_id": SCENARIO_ID,
        "pinned_revision": environment.revision,
        "revision_source": environment.revision_source,
        "endpoints": {
            "prometheus": settings.prometheus_base_url,
            "jaeger": settings.jaeger_base_url,
            "flag_control": settings.flag_control_base_url,
        },
        "window_seconds": args.window,
        "budget_seconds": args.seconds,
        "baseline": {},
        "armed_at": None,
        "observations": [],
        "cleanup": None,
        "restored_at": None,
        "verdict": None,
    }
    deadline = time.monotonic() + args.seconds
    fault = _ScenarioFault(flag_id=FLAG_ID, armed_variant=FAULT_VARIANTS[FLAG_ID])
    client = httpx.Client(
        base_url=settings.prometheus_base_url.rstrip("/"),
        timeout=REQUEST_TIMEOUT_SECONDS,
        trust_env=False,
    )
    try:
        log("DIAGNOSTIC CAPTURE, NOT SCENARIO ACCEPTANCE")
        log(f"revision {environment.revision} ({environment.revision_source})")
        residual = detect_residual_faults(environment, timeout_seconds=REQUEST_TIMEOUT_SECONDS)
        capture["baseline"]["residual_faults"] = residual
        if residual:
            log(f"refusing to run: the demo is not at its baseline: {residual}")
            return 3
        document = environment.read_flag_document(timeout_seconds=REQUEST_TIMEOUT_SECONDS)
        capture["baseline"]["flag_document"] = {
            flag_id: body.get("defaultVariant") for flag_id, body in document["flags"].items()
        }
        metric = environment.prometheus.resolve_metric(
            CALLS_METRIC_CANDIDATES, REQUEST_TIMEOUT_SECONDS
        )
        capture["baseline"]["span_metric_series"] = metric
        if metric is None:
            log("refusing to run: the span-metrics series is not exported")
            return 3
        capture["baseline"]["prometheus_series_count"] = len(
            environment.prometheus.series_names(REQUEST_TIMEOUT_SECONDS)
        )
        capture["baseline"]["charge_label_names"] = sorted(
            environment.prometheus.series_labels(
                f"{{span_name='{CHARGE_SPAN}'}}", REQUEST_TIMEOUT_SECONDS
            )
        )

        def snapshot(tag: str) -> dict[str, Any]:
            health = environment.read_service_health(
                target=CALLEE, timeout_seconds=REQUEST_TIMEOUT_SECONDS
            )
            caller_health = environment.read_service_health(
                target=CALLER, timeout_seconds=REQUEST_TIMEOUT_SECONDS
            )
            dependency = environment.read_dependency(
                target=CALLER,
                dependency=CALLEE,
                window_seconds=args.window,
                timeout_seconds=REQUEST_TIMEOUT_SECONDS,
            )
            traces = environment.read_traces(
                target=CALLEE,
                operation="Charge",
                window_seconds=args.window,
                timeout_seconds=REQUEST_TIMEOUT_SECONDS,
            )
            charge = grouped_series(client, metric, CHARGE_SPAN, args.window)
            place_order = grouped_series(client, metric, PLACE_ORDER_SPAN, args.window)
            entry = {
                "at": now(),
                "tag": tag,
                "flag_variant": environment.read_flag_document(
                    timeout_seconds=REQUEST_TIMEOUT_SECONDS
                )["flags"][FLAG_ID]["defaultVariant"],
                "callee_health": {
                    key: health.value.get(key)
                    for key in ("health", "error_rate", "calls_per_second", "service_scope")
                },
                "caller_health": {
                    key: caller_health.value.get(key)
                    for key in ("health", "error_rate", "calls_per_second", "service_scope")
                },
                "dependency": {
                    key: dependency.value.get(key)
                    for key in (
                        "attributable",
                        "reason",
                        "calls_per_second",
                        "errors_per_second",
                        "error_rate",
                        "queries",
                    )
                },
                "charge_client": shares(
                    charge, service="checkout", kind_token=CLIENT_SPAN_KIND_TOKEN
                ),
                "charge_server": shares(charge, service="payment"),
                "place_order_caller": shares(place_order, service="checkout"),
                "charge_grouped": charge,
                "place_order_grouped": place_order,
                "charge_traces": {
                    "spans": (traces.value.get("spans") or [])[:5],
                    "error_spans": traces.value.get("error_spans"),
                },
            }
            entry["verdict"] = classify_reading(
                client=entry["charge_client"],
                parent=entry["place_order_caller"],
                dependency=dependency.value if dependency.succeeded else None,
                readable=dependency.succeeded,
            )
            log(
                f"[{tag}] flag={entry['flag_variant']} verdict={entry['verdict']} "
                f"charge client={entry['charge_client']} charge server={entry['charge_server']} "
                f"placeOrder caller={entry['place_order_caller']} "
                f"dependency={entry['dependency']}"
            )
            return entry

        capture["baseline"]["reading"] = snapshot("baseline")
        log(f"arming {FLAG_ID}={FAULT_VARIANTS[FLAG_ID]}")
        fault.attempted = True
        arm_fault(
            environment,
            flag_id=FLAG_ID,
            variant=FAULT_VARIANTS[FLAG_ID],
            timeout_seconds=REQUEST_TIMEOUT_SECONDS,
        )
        fault.armed = True
        capture["armed_at"] = now()
        log(f"{FLAG_ID} armed at {capture['armed_at']}")
        while time.monotonic() < deadline:
            capture["observations"].append(snapshot("armed"))
            if time.monotonic() + args.poll >= deadline:
                break
            time.sleep(args.poll)
    except (KeyboardInterrupt, Exception) as exc:
        capture["error"] = f"{type(exc).__name__}: {exc}"
        log(f"capture stopped: {capture['error']}")
    finally:
        log(f"restoring {FLAG_ID}")
        restoration = restore_owned_fault(
            environment, fault, timeout_seconds=REQUEST_TIMEOUT_SECONDS
        )
        capture["cleanup"] = restoration.detail
        capture["restored_at"] = now()
        capture["restored"] = restoration.restored
        log(f"cleanup: {restoration.detail}")
        try:
            if capture["baseline"].get("reading") is not None:
                capture["after_restore"] = snapshot("after-restore")
            else:
                capture["after_restore"] = {
                    "skipped": "the capture stopped before its baseline reading completed"
                }
        except Exception as exc:  # a failed read must not hide the cleanup result
            capture["after_restore"] = {"error": f"{type(exc).__name__}: {exc}"}
        if isinstance(capture["after_restore"], dict):
            # Named for what it is. The sample spans the same window as the last
            # armed reading, so it reports what the window held, not what the
            # restore caused; recovery needs a fresh window after the restore.
            capture["after_restore"]["caveat"] = (
                "post-restore sample, not recovery evidence: it averages the window that "
                "includes the armed period"
            )
        client.close()

    summary = summarize(item["verdict"] for item in capture["observations"])
    capture["verdict"] = summary.strongest
    capture["evidence_summary"] = summary.as_dict()
    capture["recovery"] = {
        "assessed": False,
        "reason": (
            "no reading in this capture can show recovery: the post-restore sample averages the "
            "window that contains the armed period, so a clean reading there is as consistent "
            "with the fault never having been in effect as with the service having recovered. "
            "Recovery needs a fresh window after the restore."
        ),
    }
    if not capture.get("restored"):
        exit_status = 4
    elif capture.get("error") is not None:
        exit_status = 1
    else:
        exit_status = 0
    capture["exit_status"] = exit_status
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(capture, indent=1, sort_keys=True), encoding="utf-8")
    counts = capture["evidence_summary"]["classes"]
    log(
        f"verdict: {capture['verdict']} (strongest of {summary.total} armed readings; "
        + ", ".join(f"{name}={counts[name]}" for name in EVIDENCE_CLASSES)
        + f"; last={summary.last})"
    )
    log(f"attributable failure observed while armed: {summary.attributable_observed}")
    log(f"recovery assessed: False - {capture['recovery']['reason']}")
    log(f"capture written to {out_path}")
    log(
        "reading: 'attributable_failure' means the caller's own client share for the Charge "
        "edge rose above the telemetry bound, which is the evidence P1-01's premise and "
        "diagnosis both require. 'generic_caller_failure_only' is the 2026-09-24 signature: "
        "the demo degraded without that evidence, so this revision cannot demonstrate P1-01 "
        "as written. 'traffic_without_failure' is traffic on the path with nothing above the "
        "bound, which is not health. 'no_usable_observation' is no traffic or a reading that "
        "could not be taken, which is not health either."
    )
    log(f"exit status: {exit_status}")
    return exit_status


if __name__ == "__main__":
    raise SystemExit(main())
