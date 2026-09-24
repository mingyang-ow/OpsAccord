"""Command-line entry points for local development and acceptance runs."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from opsaccord.config import REPO_ROOT, Settings, get_settings
from opsaccord.domain.enums import ScenarioOutcome
from opsaccord.persistence.db import Database
from opsaccord.reasoning.deterministic import DeterministicAdapter
from opsaccord.scenarios.fixtures import SCENARIOS
from opsaccord.scenarios.live import (
    LIVE_PLANS,
    LIVE_SETTLE_ATTEMPTS,
    LIVE_SETTLE_SECONDS,
    LIVE_VERIFICATION_WINDOW_SECONDS,
    LiveProgress,
    live_settings,
    preflight,
    run_live_suite,
)
from opsaccord.scenarios.runner import evaluate, run_scenario
from opsaccord.tools.live_environment import LiveEnvironment
from opsaccord.workflow.checkpoints import ensure_checkpoint_schema


def _db(settings: Settings) -> Database:
    return Database(settings.state_database_url)


def _migrate(url: str) -> None:
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT,
        check=True,
        env={**os.environ, "OPSACCORD_DATABASE_URL": url},
    )
    ensure_checkpoint_schema(url)
    print(f"schema up to date for {url}")


def cmd_init_db(args: argparse.Namespace) -> int:
    settings = get_settings()
    if args.all:
        # The three local databases are deliberately separate: the pytest session
        # rebuilds the *_test* schema, scenario runs own their own database, and
        # the interactive demo/live environment owns the third.
        for url in (
            settings.database_url,
            settings.scenario_database_url,
            settings.test_database_url,
        ):
            _migrate(url)
        return 0
    _migrate(args.database_url or Settings(app_env=args.env).state_database_url)
    return 0


def cmd_checkpoints(args: argparse.Namespace) -> int:
    settings = get_settings()
    ensure_checkpoint_schema(settings.checkpoint_url)
    print(f"langgraph checkpoint schema ready at {settings.checkpoint_url}")
    return 0


def cmd_serve_api(args: argparse.Namespace) -> int:
    import uvicorn

    from opsaccord.api.app import create_app

    settings = get_settings()
    app = create_app(settings=settings)
    uvicorn.run(app, host=settings.api_host, port=settings.api_port)
    return 0


def cmd_serve_ui(args: argparse.Namespace) -> int:
    settings = get_settings()
    ui = REPO_ROOT / "src" / "opsaccord" / "ui" / "app.py"
    return subprocess.call(
        [
            sys.executable,
            "-m",
            "streamlit",
            "run",
            str(ui),
            "--server.address",
            settings.api_host,
            "--server.port",
            str(settings.ui_port),
            "--browser.gatherUsageStats",
            "false",
        ]
    )


def cmd_run_scenarios(args: argparse.Namespace) -> int:
    settings = Settings(app_env=args.env)
    print(f"# scenario state database: {settings.state_database_url}")
    db = _db(settings)
    selected = [args.scenario] if args.scenario else sorted(SCENARIOS)
    records = []
    failures = 0
    print(f"{'id':<7} {'outcome':<19} {'status':<20} actions details")
    for scenario_id in selected:
        spec = SCENARIOS[scenario_id]
        trace = run_scenario(spec, db=db, settings=settings, provider=DeterministicAdapter())
        outcome, reasons = evaluate(trace)
        if outcome is not ScenarioOutcome.PASS:
            failures += 1
        records.append(
            {
                "scenario_id": scenario_id,
                "title": spec.title,
                "outcome": str(outcome),
                "incident_id": trace.incident.id,
                "final_status": str(trace.incident.status),
                "executed_actions": len(trace.executed_actions),
                "duplicate_side_effects": trace.duplicate_side_effects,
                "forbidden_actions": len(trace.forbidden_actions),
                "resolved_without_verification": trace.resolved_without_verification,
                "evidence": len(trace.evidence),
                "approvals": len(trace.approvals),
                "verifications": [str(v.outcome) for v in trace.verifications],
                "reasons": reasons,
                "simulated": True,
            }
        )
        print(
            f"{scenario_id:<7} {outcome!s:<19} {trace.incident.status!s:<20} "
            f"{len(trace.executed_actions):<7} {'; '.join(reasons) or 'ok'}"
        )
    if args.json:
        path = Path(args.json)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(records, indent=2), encoding="utf-8")
        print(f"wrote {path}")
    passed = len(records) - failures
    print(f"\n{passed}/{len(records)} scenarios passed")
    db.dispose()
    return 1 if failures else 0


def cmd_run_live_scenarios(args: argparse.Namespace) -> int:
    """Execute the Phase 1 paths against the real pinned Astronomy Shop stack.

    This command never invents evidence. If the demo stack does not answer, it
    fails before running anything: a live run that quietly fell back to the
    simulator would be worse than no run at all.
    """
    settings = live_settings(args.env)
    print(
        f"# verification settling: {LIVE_SETTLE_ATTEMPTS} attempt(s) x "
        f"{LIVE_SETTLE_SECONDS}s over a {LIVE_VERIFICATION_WINDOW_SECONDS}s window "
        "(evidence refresh only, never a side effect)"
    )
    print(f"# live scenario state database: {settings.state_database_url}")
    try:
        # Preflight always probes a plain surface: a plan that deliberately points
        # at an unroutable endpoint would fail the very check that proves the
        # stack is up.
        probe = preflight(LiveEnvironment(settings=settings))
    except Exception as exc:
        print(f"live preflight FAILED against the demo stack: {exc}")
        print("start it with scripts/demo.sh start, then retry")
        return 2
    print(
        f"# live preflight ok: {probe['flag_count']} flags, "
        f"{probe['prometheus_series']} prometheus series"
    )
    if probe["missing_verified_flags"]:
        print(
            f"# WARNING: verified flags absent from the live document: "
            f"{probe['missing_verified_flags']}"
        )

    db = _db(settings)
    try:
        run = run_live_suite(
            db=db,
            settings=settings,
            scenario_ids=args.scenario,
            # Progress goes to stdout and is flushed per line: the live stage is
            # normally piped through `tee`, and a buffered run looks hung.
            progress=LiveProgress(stream=sys.stdout),
            evidence_path=args.json,
        )
    except KeyboardInterrupt:
        print(
            "\nlive run interrupted: completed scenarios are recorded in the evidence and the "
            "active scenario is recorded as interrupted, not as a result"
        )
        if args.json:
            print(f"  evidence: {args.json}")
        print(
            "  check the demo's fault flags before the next run: an interrupted scenario "
            "still restores what it armed, and the evidence notes say whether it could"
        )
        db.dispose()
        return 130

    print(f"{'id':<7} {'outcome':<19} {'status':<20} actions setup")
    for result in run.results:
        trace = result.trace
        if trace is None:
            print(f"{result.scenario_id:<7} {'NOT MEASURED':<19} {'-':<20} {'-':<7} {result.setup}")
        else:
            print(
                f"{result.scenario_id:<7} {result.outcome!s:<19} {trace.incident.status!s:<20} "
                f"{len(trace.executed_actions):<7} {result.setup}"
            )
        for reason in result.reasons:
            print(f"        cause: {reason}")
    if run.evidence_path is not None:
        print(
            f"wrote {run.evidence_path} "
            f"({len(run.measured)}/{len(run.selected)} scenarios measured, run {run.status})"
        )
    for message in run.evidence_errors:
        print(f"  evidence error: {message}")
    simulated = [result for result in run.results if result.simulated]
    if simulated:
        # A live path that collected simulator evidence is a defect, not a live
        # result, and the command must not exit 0 on it.
        print(
            f"\n{len(simulated)} live result(s) carry simulator evidence: "
            f"{[result.scenario_id for result in simulated]}"
        )
    passed = len(run.results) - len(run.failures)
    print(f"\n{passed}/{len(run.results)} live scenarios passed")
    if not run.finished:
        print(f"run status: {run.status}")
        for note in run.notes:
            print(f"  note: {note}")
    db.dispose()
    return 1 if run.failures or not run.finished or run.evidence_errors or simulated else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="opsaccord", description="OpsAccord local tooling")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init-db", help="apply migrations and create checkpoint tables")
    p.add_argument("--database-url", default=None)
    p.add_argument("--env", default="dev", choices=["dev", "test", "ci", "scenario"])
    p.add_argument(
        "--all",
        action="store_true",
        help="migrate the dev, scenario, and test databases (isolated from each other)",
    )
    p.set_defaults(func=cmd_init_db)

    p = sub.add_parser("checkpoints", help="ensure the LangGraph checkpoint schema exists")
    p.set_defaults(func=cmd_checkpoints)

    p = sub.add_parser("serve-api", help="run the FastAPI service on localhost")
    p.set_defaults(func=cmd_serve_api)

    p = sub.add_parser("serve-ui", help="run the Streamlit UI on localhost")
    p.set_defaults(func=cmd_serve_ui)

    p = sub.add_parser(
        "run-live-scenarios",
        help="execute the Phase 1 paths against the real pinned Astronomy Shop demo stack",
    )
    p.add_argument(
        "--scenario",
        action="append",
        default=None,
        choices=sorted(LIVE_PLANS),
        help="repeat to select several; defaults to every live plan",
    )
    p.add_argument("--json", default=None, help="write a JSON evidence record to this path")
    p.add_argument("--env", default="dev", choices=["dev", "test", "ci", "scenario"])
    p.set_defaults(func=cmd_run_live_scenarios)

    p = sub.add_parser("run-scenarios", help="execute the Phase 1 acceptance scenarios")
    p.add_argument("--scenario", default=None, choices=sorted(SCENARIOS))
    p.add_argument("--json", default=None, help="write a JSON evidence record to this path")
    p.add_argument("--env", default="scenario", choices=["dev", "test", "ci", "scenario"])
    p.set_defaults(func=cmd_run_scenarios)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
