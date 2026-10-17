"""Command-line entry points for local development and acceptance runs."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

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
    checkout_driver_factory,
    live_settings,
    preflight,
    run_live_suite,
    storefront_preflight,
)
from opsaccord.scenarios.runner import evaluate, run_scenario
from opsaccord.scenarios.traffic import storefront_origin
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
    try:
        storefront = storefront_preflight(
            LiveEnvironment(settings=settings), storefront_url=storefront_origin(settings)
        )
    except Exception as exc:
        print(f"live preflight FAILED reading the storefront's catalog calls: {exc}")
        return 2
    print(f"# storefront preflight: {storefront['detail']}")
    if storefront["healthy"] is False:
        print(
            "live preflight REFUSED: the storefront cannot reach product-catalog, so every "
            "order fails before it reaches payment and the Payment paths would have nothing "
            "to measure"
        )
        print(
            "recover it with: bash scripts/demo.sh restart product-catalog checkout && "
            "bash scripts/demo.sh verify-recovery product-catalog checkout"
        )
        return 2
    driver_factory = None if args.no_checkout_driver else checkout_driver_factory(settings)
    print(
        "# synthetic checkout traffic: "
        + ("off (--no-checkout-driver)" if driver_factory is None else "on for the Payment plans")
    )

    provider: Any = None
    ledger: Any = None
    if args.model is not None or args.budget_usd is not None:
        built = _hosted_provider(settings, args)
        if isinstance(built, str):
            print(f"live run REFUSED: {built}")
            return 2
        provider, ledger = built
    else:
        print("# reasoning: deterministic adapter (pass --model and a budget for a hosted model)")

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
            checkout_driver=driver_factory,
            provider=provider,
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
    for result in run.results:
        decision = result.model_decision or {}
        if decision.get("provider") not in (None, "deterministic"):
            action = decision.get("proposed_action") or {}
            print(
                f"model {result.scenario_id}: {decision.get('model_id')} proposed "
                f"{action.get('tool_name', '-')}->{action.get('target', '-')}"
                + (f", refused by {decision['refused_by']}" if decision.get("refused_by") else "")
                + (
                    f", error {decision['provider_error']}"
                    if decision.get("provider_error")
                    else ""
                )
            )
    if ledger is not None:
        print(f"# ledger: {ledger.snapshot()}")
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
    p.add_argument(
        "--no-checkout-driver",
        action="store_true",
        help="rely on the load generator alone instead of placing bounded synthetic orders",
    )
    _add_model_arguments(p, max_calls=20)
    p.set_defaults(func=cmd_run_live_scenarios)

    p = sub.add_parser(
        "eval-model",
        help="run the simulated scenarios with a hosted model in the loop, under a hard budget",
    )
    p.add_argument("--scenario", action="append", default=None, choices=sorted(SCENARIOS))
    p.add_argument("--repeats", type=int, default=1)
    _add_model_arguments(p, max_calls=50)
    p.add_argument("--baseline-only", action="store_true", help="the deterministic adapter only")
    p.add_argument("--out-dir", default="reports/local")
    p.add_argument("--env", default="scenario", choices=["dev", "test", "ci", "scenario"])
    p.set_defaults(func=cmd_eval_model)

    p = sub.add_parser("run-scenarios", help="execute the Phase 1 acceptance scenarios")
    p.add_argument("--scenario", default=None, choices=sorted(SCENARIOS))
    p.add_argument("--json", default=None, help="write a JSON evidence record to this path")
    p.add_argument("--env", default="scenario", choices=["dev", "test", "ci", "scenario"])
    p.set_defaults(func=cmd_run_scenarios)
    return parser


#: Worst-case prompt size per call. Bytes bound tokens from above (see
#: OpenAICompatibleAdapter.max_input_bytes), so this is also the input-token ceiling the
#: run budget reserves against.
EVAL_MAX_INPUT_BYTES = 32_000


def _hosted_provider(settings: Settings, args: argparse.Namespace) -> tuple[Any, Any] | str:
    """A budgeted hosted provider from explicit flags, or the reason one cannot be built.

    Paid calls need three explicit things: permission (``--allow-paid-calls``, or
    ``OPSACCORD_ALLOW_PAID_MODEL_CALLS`` for the evaluation command), a budget
    (``--budget-usd``), and per-1k-token rates (flags, or the ``OPSACCORD_MODEL_*_COST``
    settings). The flags exist because ``scripts/validate.sh`` runs the offline and API
    stages in the same shell as the live stage: provider settings exported to the
    environment would reach the API's own provider there.
    """
    from opsaccord.reasoning.budget import BudgetedProvider, SpendLedger, TokenRates
    from opsaccord.reasoning.openai_compatible import OpenAICompatibleAdapter

    if not (getattr(args, "allow_paid_calls", False) or settings.allow_paid_model_calls):
        return "paid model calls are not allowed; pass --allow-paid-calls"
    if args.budget_usd is None:
        return "a hosted model run needs an explicit --budget-usd"
    input_rate = (
        args.input_cost_per_1k
        if args.input_cost_per_1k is not None
        else settings.model_input_cost_per_1k_usd
    )
    output_rate = (
        args.output_cost_per_1k
        if args.output_cost_per_1k is not None
        else settings.model_output_cost_per_1k_usd
    )
    if input_rate is None or output_rate is None:
        return (
            "the budget cannot be enforced without rates; pass --input-cost-per-1k and "
            "--output-cost-per-1k"
        )
    rates = TokenRates(input_rate, output_rate)
    max_output = args.max_output_tokens or settings.model_max_output_tokens
    model_id = args.model or settings.model_id
    adapter = OpenAICompatibleAdapter(
        base_url=settings.model_base_url,
        model_id=model_id,
        api_key_env=settings.model_api_key_env,
        timeout_seconds=settings.model_timeout_seconds,
        max_output_tokens=max_output,
        allow_paid_calls=True,
        input_cost_per_1k_usd=rates.input_per_1k_usd,
        output_cost_per_1k_usd=rates.output_per_1k_usd,
        max_input_bytes=EVAL_MAX_INPUT_BYTES,
    )
    ledger = SpendLedger(budget_usd=args.budget_usd, max_calls=args.max_calls)
    budgeted = BudgetedProvider(
        adapter,
        ledger=ledger,
        rates=rates,
        max_input_tokens=EVAL_MAX_INPUT_BYTES,
        max_output_tokens=max_output,
    )
    print(
        f"# model {model_id} at {settings.model_base_url}; budget ${args.budget_usd:.2f}, "
        f"at most {args.max_calls} calls, {max_output} output tokens, "
        f"worst case ${budgeted.worst_case_usd:.4f} per call"
    )
    return budgeted, ledger


def _add_model_arguments(p: argparse.ArgumentParser, *, max_calls: int) -> None:
    p.add_argument("--model", default=None, help="hosted model id; defaults to OPSACCORD_MODEL_ID")
    p.add_argument("--budget-usd", type=float, default=None)
    p.add_argument("--max-calls", type=int, default=max_calls)
    p.add_argument("--allow-paid-calls", action="store_true")
    p.add_argument("--input-cost-per-1k", type=float, default=None)
    p.add_argument("--output-cost-per-1k", type=float, default=None)
    p.add_argument("--max-output-tokens", type=int, default=None)


def cmd_eval_model(args: argparse.Namespace) -> int:
    """Run the simulated scenarios with a hosted model in the loop, under a hard budget."""
    from datetime import UTC, datetime

    from opsaccord.reasoning.deterministic import DeterministicAdapter
    from opsaccord.scenarios.model_eval import run_eval, write_outputs

    settings = Settings(app_env=args.env)
    providers: list[Any] = [DeterministicAdapter()]
    ledger: Any = None
    if not args.baseline_only:
        built = _hosted_provider(settings, args)
        if isinstance(built, str):
            print(f"refusing: {built}; --baseline-only runs without a hosted model")
            return 2
        budgeted, ledger = built
        providers.append(budgeted)
    print(f"# scenario state database: {settings.state_database_url}")
    db = _db(settings)

    def show(record: Any) -> None:
        action = record.proposed_action
        proposed = f"{action['tool_name']}->{action['target']}" if action else "-"
        print(
            f"{record.model_id:<22} {record.scenario_id:<6} #{record.repeat} {record.outcome:<20} "
            f"{record.final_status:<21} {proposed:<36} {record.refused_by or ''}"
            + (f" error={record.provider_error}" if record.provider_error else "")
        )

    try:
        records = run_eval(
            db=db,
            settings=settings,
            providers=providers,
            scenario_ids=args.scenario,
            repeats=args.repeats,
            on_record=show,
        )
    finally:
        db.dispose()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    base = Path(args.out_dir) / f"model-eval-{stamp}"
    snapshot = ledger.snapshot() if ledger else None
    write_outputs(
        records,
        json_path=base.with_suffix(".json"),
        markdown_path=base.with_suffix(".md"),
        ledger=snapshot,
        title=f"Model evaluation {stamp} (simulated demo)",
    )
    if snapshot:
        print(f"# ledger: {snapshot}")
    print(f"wrote {base.with_suffix('.json')} and {base.with_suffix('.md')}")
    unsafe = sum(1 for r in records if r.outcome == "UNSAFE_FAILURE")
    return 1 if unsafe else 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
