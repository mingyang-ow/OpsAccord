"""Command-line entry points for local development and acceptance runs."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from opsaccord.config import REPO_ROOT, Settings, get_settings
from opsaccord.domain.enums import ScenarioOutcome
from opsaccord.persistence.db import Database
from opsaccord.reasoning.deterministic import DeterministicAdapter
from opsaccord.scenarios.fixtures import SCENARIOS
from opsaccord.scenarios.runner import evaluate, run_scenario
from opsaccord.workflow.checkpoints import ensure_checkpoint_schema


def _db(settings: Settings) -> Database:
    return Database(settings.state_database_url)


def cmd_init_db(args: argparse.Namespace) -> int:
    settings = get_settings()
    url = args.database_url or settings.database_url
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT,
        check=True,
        env={**__import__("os").environ, "OPSACCORD_DATABASE_URL": url},
    )
    ensure_checkpoint_schema(url)
    print(f"schema up to date for {url}")
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="opsaccord", description="OpsAccord local tooling")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init-db", help="apply migrations and create checkpoint tables")
    p.add_argument("--database-url", default=None)
    p.set_defaults(func=cmd_init_db)

    p = sub.add_parser("checkpoints", help="ensure the LangGraph checkpoint schema exists")
    p.set_defaults(func=cmd_checkpoints)

    p = sub.add_parser("serve-api", help="run the FastAPI service on localhost")
    p.set_defaults(func=cmd_serve_api)

    p = sub.add_parser("serve-ui", help="run the Streamlit UI on localhost")
    p.set_defaults(func=cmd_serve_ui)

    p = sub.add_parser("run-scenarios", help="execute the Phase 1 acceptance scenarios")
    p.add_argument("--scenario", default=None, choices=sorted(SCENARIOS))
    p.add_argument("--json", default=None, help="write a JSON evidence record to this path")
    p.add_argument("--env", default="test", choices=["dev", "test", "ci"])
    p.set_defaults(func=cmd_run_scenarios)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
