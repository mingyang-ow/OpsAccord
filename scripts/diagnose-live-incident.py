#!/usr/bin/env python
"""Read-only export of one live Payment incident, and of the current edge series.

Two questions this answers that no file on disk can:

* what each *verification attempt* actually read, and why it stopped - the
  evidence JSON carries aggregates and one final verdict string, so the per-round
  tool calls and verification rows only exist in the operational database;
* what the *caller-side* Charge series really looks like right now - which label
  values exist on the stack, and which of them a recovery verdict would rest on.

Nothing here writes anything. The database session is opened read-only, the only
HTTP calls are Prometheus queries, and the flag document is never touched, so
running this cannot arm, clear or change a fault.

Usage:

    .venv/bin/python scripts/diagnose-live-incident.py [incident-id ...]

With no argument it exports the most recent Payment incident
(``scenario_id = 'P1-01'``) and prints its id so it can be re-run by id. Every
read is bounded; a read that fails is reported as a collection failure and the
remaining sections still run.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import httpx  # noqa: E402
import psycopg  # noqa: E402

from opsaccord.config import Settings, to_libpq_dsn  # noqa: E402
from opsaccord.tools.live_environment import (  # noqa: E402
    CALLS_METRIC_CANDIDATES,
    CLIENT_SPAN_KIND_TOKEN,
    SERVICE_LABEL_CANDIDATES,
    SPAN_KIND_LABEL_CANDIDATES,
    PrometheusClient,
)

CHARGE_SPAN = "oteldemo.PaymentService/Charge"
PLACE_ORDER_SPAN = "oteldemo.CheckoutService/PlaceOrder"
CALLS_METRIC_FALLBACK = CALLS_METRIC_CANDIDATES[0]
WINDOW_SECONDS = 120
TIMEOUT = 10.0
VALUE_LIMIT = 400

#: Tool arguments that say which series the call meant to read.
INTERESTING_ARGUMENTS = (
    "query",
    "metric",
    "window_seconds",
    "operation",
    "dependency",
    "caller",
    "destination",
    "flag_id",
    "variant",
)


def bounded(value: Any, limit: int = VALUE_LIMIT) -> str:
    text = " ".join(value.split()) if isinstance(value, str) else repr(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def arguments_summary(arguments: dict[str, Any]) -> str:
    if not isinstance(arguments, dict):
        return bounded(arguments)
    picked = {key: arguments[key] for key in INTERESTING_ARGUMENTS if key in arguments}
    return bounded(picked or arguments)


def rows(cur: psycopg.Cursor, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    cur.execute(sql, params)
    return list(cur.fetchall())


def export_incident(conn: psycopg.Connection, incident_id: str) -> None:
    with conn.cursor() as cur:
        found = rows(
            cur,
            "select id, scenario_id, status, service, severity, summary,"
            " to_char(created_at, 'YYYY-MM-DD HH24:MI:SS') from incidents where id = %s",
            (incident_id,),
        )
        if not found:
            print(f"\n=== {incident_id}: no such incident")
            return
        inc_id, scenario_id, status, service, severity, summary, created = found[0]
        print(f"\n=== incident {inc_id}")
        print(f"    scenario={scenario_id} status={status} service={service}")
        print(f"    severity={severity} created={created}")
        print(f"    summary: {bounded(summary)}")

        for workflow in rows(
            cur,
            "select id, status, outcome, steps_used, soft_step_budget, hard_step_cap,"
            " model_calls, tool_calls, extensions_granted,"
            " coalesce(terminal_reason, ''), to_char(created_at, 'HH24:MI:SS')"
            " from workflows where incident_id = %s",
            (incident_id,),
        ):
            print(
                "    workflow: "
                f"{workflow[0]} {workflow[1]}/{workflow[2]} steps={workflow[3]}"
                f"/soft {workflow[4]}/hard {workflow[5]} model_calls={workflow[6]}"
                f" tool_calls={workflow[7]} extensions={workflow[8]}"
            )
            print(f"      terminal_reason: {bounded(workflow[9])}")

        print("    approvals:")
        for approval in rows(
            cur,
            "select id, status, decision, coalesce(approver, ''), target,"
            " to_char(created_at, 'HH24:MI:SS'),"
            " coalesce(to_char(decided_at, 'HH24:MI:SS'), '-'),"
            " coalesce(to_char(consumed_at, 'HH24:MI:SS'), '-')"
            " from approvals where incident_id = %s order by created_at",
            (incident_id,),
        ):
            print(
                f"      {approval[5]} {approval[0]} {approval[1]}/{approval[2]}"
                f" approver={approval[3] or '-'} target={approval[4]}"
                f" decided={approval[6]} consumed={approval[7]}"
            )

        print("    actions:")
        for action in rows(
            cur,
            "select id, tool_name, target, status, attempt,"
            " coalesce(external_outcome::text, '-'),"
            " to_char(created_at, 'HH24:MI:SS'),"
            " coalesce(to_char(dispatch_started_at, 'HH24:MI:SS'), '-'),"
            " coalesce(to_char(completed_at, 'HH24:MI:SS'), '-'), is_simulated,"
            " coalesce(error, '')"
            " from actions where incident_id = %s order by created_at",
            (incident_id,),
        ):
            print(
                f"      {action[6]} {action[0]} {action[1]}({action[2]})"
                f" status={action[3]} attempt={action[4]} outcome={action[5]}"
                f" dispatch={action[7]} completed={action[8]}"
                f" simulated={action[9]}"
            )
            if action[10]:
                print(f"        error: {bounded(action[10])}")

        print("    tool calls:")
        for call in rows(
            cur,
            "select id, tool_name, target, side_effect::text, attempt, succeeded,"
            " to_char(started_at, 'HH24:MI:SS'),"
            " to_char(finished_at, 'HH24:MI:SS'), arguments, coalesce(error, ''),"
            " is_simulated"
            " from tool_calls where incident_id = %s order by started_at, id",
            (incident_id,),
        ):
            print(
                f"      {call[6]}->{call[7]} {call[0]} {call[1]}({call[2]})"
                f" effect={call[3]} attempt={call[4]}"
                f" {'ok' if call[5] else 'FAILED'} simulated={call[10]}"
            )
            print(f"        args: {arguments_summary(call[8])}")
            if call[9]:
                print(f"        error: {bounded(call[9])}")

        print("    verifications:")
        for verification in rows(
            cur,
            "select id, outcome::text, checked_at, detail, evidence_ids,"
            " coalesce(action_id, '-')"
            " from verifications where incident_id = %s order by checked_at",
            (incident_id,),
        ):
            checked = verification[2].strftime("%Y-%m-%d %H:%M:%S")
            print(
                f"      {checked} {verification[1]} action={verification[5]}"
                f" evidence={len(verification[4] or [])}"
            )
            print(f"        detail: {bounded(verification[3], 600)}")

        print("    evidence (grouped by kind, then oldest first):")
        for item in rows(
            cur,
            "select kind::text, source, service, summary, value,"
            " to_char(collected_at, 'HH24:MI:SS'),"
            " coalesce(to_char(observation_window_start, 'HH24:MI:SS'), '-'),"
            " coalesce(to_char(observation_window_end, 'HH24:MI:SS'), '-'),"
            " coalesce(tool_call_id, '-'), is_simulated,"
            " coalesce(value->>'error_rate', '-'),"
            " coalesce(value->>'calls_per_second', '-'),"
            " coalesce(value->>'errors_per_second', '-'),"
            " coalesce(value->>'attributable', '-'),"
            " coalesce(value->>'caller', value->>'service_name', '-'),"
            " coalesce(value->>'destination', value->>'span_kind', '-'),"
            " coalesce(value->>'reason', '')"
            " from evidence where incident_id = %s"
            " order by kind, collected_at, id",
            (incident_id,),
        ):
            print(
                f"      [{item[0]}] {item[5]} {item[1]}/{item[2]} tool_call={item[8]}"
                f" window={item[6]}..{item[7]} simulated={item[9]}"
            )
            print(f"        summary: {bounded(item[3], 300)}")
            print(
                "        value: "
                f"error_rate={item[10]} calls_per_second={item[11]}"
                f" errors_per_second={item[12]} attributable={item[13]}"
                f" caller/service={item[14]} destination/kind={item[15]}"
            )
            if item[16]:
                print(f"        reason: {bounded(item[16], 300)}")

        counts = rows(
            cur,
            "select kind::text, count(*) from evidence where incident_id = %s"
            " group by kind order by kind",
            (incident_id,),
        )
        print(f"    evidence by kind: { {kind: count for kind, count in counts} }")

        failures = rows(
            cur,
            "select count(*) from tool_calls where incident_id = %s and not succeeded",
            (incident_id,),
        )
        simulated = rows(
            cur,
            "select count(*) from tool_calls where incident_id = %s and is_simulated",
            (incident_id,),
        )
        total = rows(
            cur,
            "select count(*) from tool_calls where incident_id = %s",
            (incident_id,),
        )
        print(
            f"    tool calls: {total[0][0]} total, {failures[0][0]} failed,"
            f" {simulated[0][0]} simulated"
        )


def read_current_series(settings: Settings) -> bool:
    """What the caller-side Charge series contains *now*: current, not the incident."""
    print("\n=== current Prometheus readings (not part of the persisted incident)")
    prometheus = PrometheusClient(settings.prometheus_base_url)
    try:
        metric = prometheus.resolve_metric(CALLS_METRIC_CANDIDATES, TIMEOUT)
        print(f"    span-metrics series name: {metric or 'no candidate series is exported'}")
        labels = prometheus.series_labels(f"{{span_name='{CHARGE_SPAN}'}}", TIMEOUT)
        print(f"    label names on {CHARGE_SPAN!r}: {sorted(labels) or 'none'}")
        for candidate in (*SERVICE_LABEL_CANDIDATES, *SPAN_KIND_LABEL_CANDIDATES):
            values = sorted(prometheus.label_values(candidate, TIMEOUT))
            print(f"    {candidate} values: {values[:20]}{' ...' if len(values) > 20 else ''}")
        client_values = sorted(
            value
            for name in SPAN_KIND_LABEL_CANDIDATES
            for value in prometheus.label_values(name, TIMEOUT)
            if CLIENT_SPAN_KIND_TOKEN in value.lower()
        )
        print(f"    client-side span-kind values: {client_values or 'none'}")
    except Exception as exc:  # a failed read is the answer, not a crash
        print(f"    collection failure: {type(exc).__name__}: {exc}")
        return False

    name = metric or CALLS_METRIC_FALLBACK
    for span in (CHARGE_SPAN, PLACE_ORDER_SPAN):
        query = (
            f"sum by (service_name, span_kind, status_code) "
            f"(rate({name}{{span_name='{span}'}}[{WINDOW_SECONDS}s]))"
        )
        print(f"    {span} over {WINDOW_SECONDS}s, grouped:")
        print(f"      {query}")
        try:
            payload = httpx.get(
                f"{settings.prometheus_base_url.rstrip('/')}/api/v1/query",
                params={"query": query},
                timeout=TIMEOUT,
                trust_env=False,
            ).json()
        except Exception as exc:
            print(f"      collection failure: {type(exc).__name__}: {exc}")
            continue
        series = payload.get("data", {}).get("result") or []
        if not series:
            print("      no series: nothing is calling this span in the window")
            continue
        for sample in sorted(series, key=lambda item: repr(item.get("metric"))):
            print(f"      {bounded(sample.get('metric'), 200)} -> {sample.get('value')}")
    return True


def read_flag_positions(settings: Settings) -> None:
    """Which variant each verified fault flag is in *now*, through the flagd-ui read.

    Read-only, and deliberately through the harness's own channel: this is the
    state a scenario's baseline check sees, so if a fault is still armed here the
    demo is not in the baseline the records claim it was. A gap between this
    document and what the services actually do is a separate question, and it is
    named rather than answered.
    """
    print("\n=== current flag document (read-only, not part of the persisted incident)")
    try:
        from opsaccord.tools.live_environment import FLAG_SAFE_VARIANT, LiveEnvironment

        document = LiveEnvironment(settings=settings).read_flag_document(timeout_seconds=TIMEOUT)
    except Exception as exc:
        print(f"    collection failure: {type(exc).__name__}: {exc}")
        return
    flags = document.get("flags", {})
    for flag_id, safe_variant in sorted(FLAG_SAFE_VARIANT.items()):
        body = flags.get(flag_id)
        if not isinstance(body, dict):
            print(f"    {flag_id}: absent from the document")
            continue
        current = body.get("defaultVariant")
        position = "safe" if current == safe_variant else "FAULT POSITION"
        print(f"    {flag_id}: {current!r} (safe variant {safe_variant!r}) -> {position}")


def main(argv: list[str]) -> int:
    settings = Settings()
    print("live incident diagnosis (read-only)")
    print(f"  project        : {ROOT}")
    print(f"  database       : {to_libpq_dsn(settings.database_url)}")
    print(f"  prometheus     : {settings.prometheus_base_url}")

    exported = 0
    try:
        conn = psycopg.connect(to_libpq_dsn(settings.database_url), connect_timeout=5)
    except Exception as exc:
        print(f"\ndatabase unavailable: {type(exc).__name__}: {exc}")
        print("  (no persisted per-attempt records can be exported from here)")
        conn = None
    if conn is not None:
        conn.read_only = True
        try:
            with conn.cursor() as cur:
                selected = list(argv)
                if not selected:
                    selected = [
                        row[0]
                        for row in rows(
                            cur,
                            "select id from incidents where scenario_id = 'P1-01'"
                            " order by created_at desc limit 1",
                        )
                    ]
                    print(f"  incident       : {selected or 'no P1-01 incident is recorded'}")
                for incident_id in selected:
                    export_incident(conn, incident_id)
                    exported += 1
        except Exception as exc:
            print(f"\ndatabase export failed: {type(exc).__name__}: {exc}")
        finally:
            conn.close()

    current = read_current_series(settings)
    read_flag_positions(settings)
    if exported or current:
        print("\nRESULT: export complete. Current readings are a measurement taken now;")
        print("the incident sections are the persisted record of one past run.")
        return 0
    print("\nRESULT: nothing was exported and no series answered. That is a collection")
    print("failure, not an empty result: start PostgreSQL and the demo stack, then rerun.")
    return 3


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
