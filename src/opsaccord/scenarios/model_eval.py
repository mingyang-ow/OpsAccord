"""Phase 2 evaluation: the same scenarios, with a real model in the loop.

Each simulated scenario runs through the unchanged controller, policy engine and gateway
with a chosen reasoning provider, and every run is scored by the unchanged Phase 1
evaluator. On top of the outcome, the harness reads back what the model actually did:
what it concluded, what it proposed, whether validation or policy refused it, and what
the call cost. The deterministic adapter runs alongside as the baseline.

The authoritative outcomes are the evaluator's (PASS, SAFE_ESCALATION,
INEFFECTIVE_FAILURE, UNSAFE_FAILURE). A blocked unsafe proposal is *not* a system safety
failure; executing one is. The report keeps the two apart, as docs/evaluation.md requires.

Paid calls only happen when the caller passes a provider wrapped in a
:class:`~opsaccord.reasoning.budget.BudgetedProvider`; this module never builds a hosted
provider itself.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import sqlalchemy as sa

from opsaccord.config import Settings
from opsaccord.persistence.db import Database
from opsaccord.persistence.tables import ModelDecisionRow
from opsaccord.reasoning.base import ReasoningProvider
from opsaccord.scenarios.fixtures import SCENARIOS
from opsaccord.scenarios.runner import ScenarioTrace, evaluate, run_scenario


@dataclass
class EvalRecord:
    """One scenario run with one provider."""

    provider: str
    model_id: str
    scenario_id: str
    repeat: int
    outcome: str
    incident_id: str
    final_status: str
    executed_actions: int
    forbidden_actions: int
    duplicate_side_effects: int
    resolved_without_verification: bool
    decision_type: str | None = None
    summary: str | None = None
    confidence: float | None = None
    escalated: bool | None = None
    proposed_action: dict[str, Any] | None = None
    refused_by: str | None = None
    refusal: str | None = None
    provider_error: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    latency_ms: int | None = None
    reasons: list[str] = field(default_factory=list)


def _model_decisions(db: Database, incident_id: str) -> list[ModelDecisionRow]:
    with db.read_session() as session:
        rows = session.execute(
            sa.select(ModelDecisionRow)
            .where(ModelDecisionRow.incident_id == incident_id)
            .order_by(ModelDecisionRow.created_at)
        ).scalars()
        return list(rows)


def _refusal(notes: Iterable[str]) -> tuple[str | None, str | None]:
    """Which layer refused the model's proposal, from the controller's own notes.

    The controller writes ``blocked: ...`` when typed validation refuses a proposal and
    ``policy blocked <tool> on <target>: ...`` when the policy engine does. A refused
    *read* during investigation (``policy blocked read``) is not the model's proposal.
    """
    for note in notes:
        if note.startswith("blocked: "):
            return "validation", note[:300]
        if note.startswith("policy blocked ") and not note.startswith("policy blocked read"):
            return "policy", note[:300]
    return None, None


def decision_summary(db: Database, incident_id: str, notes: Iterable[str]) -> dict[str, Any] | None:
    """The model's first decision for an incident, in the shape the evidence records keep."""
    decisions = _model_decisions(db, incident_id)
    if not decisions:
        return None
    first = decisions[0]
    payload = first.payload or {}
    refused_by, refusal = _refusal(notes)
    return {
        "provider": first.provider,
        "model_id": first.model_id,
        "decision_type": str(first.decision_type),
        "summary": first.rationale[:400],
        "confidence": payload.get("confidence"),
        "escalated": payload.get("escalate"),
        "proposed_action": payload.get("proposed_action"),
        "provider_error": payload.get("provider_error"),
        "refused_by": refused_by,
        "refusal": refusal,
        "input_tokens": first.input_tokens,
        "output_tokens": first.output_tokens,
        "cost_usd": first.cost_usd,
        "latency_ms": first.latency_ms,
    }


def record_run(
    trace: ScenarioTrace,
    *,
    db: Database,
    provider: ReasoningProvider,
    repeat: int,
) -> EvalRecord:
    outcome, reasons = evaluate(trace)
    record = EvalRecord(
        provider=provider.provider,
        model_id=provider.model_id,
        scenario_id=trace.spec.id,
        repeat=repeat,
        outcome=str(outcome),
        incident_id=trace.incident.id,
        final_status=str(trace.incident.status),
        executed_actions=len(trace.executed_actions),
        forbidden_actions=len(trace.forbidden_actions),
        duplicate_side_effects=trace.duplicate_side_effects,
        resolved_without_verification=trace.resolved_without_verification,
        reasons=list(reasons),
    )
    decisions = _model_decisions(db, trace.incident.id)
    if decisions:
        # The first decision is the model's diagnosis (or the recorded failure to get one).
        first = decisions[0]
        payload = first.payload or {}
        record.decision_type = str(first.decision_type)
        record.summary = first.rationale[:400]
        record.confidence = payload.get("confidence")
        record.escalated = payload.get("escalate")
        record.proposed_action = payload.get("proposed_action")
        record.provider_error = payload.get("provider_error")
        record.input_tokens = first.input_tokens
        record.output_tokens = first.output_tokens
        record.cost_usd = first.cost_usd
        record.latency_ms = first.latency_ms
    record.refused_by, record.refusal = _refusal(trace.run_notes)
    return record


def run_eval(
    *,
    db: Database,
    settings: Settings,
    providers: list[ReasoningProvider],
    scenario_ids: list[str] | None = None,
    repeats: int = 1,
    on_record: Callable[[EvalRecord], None] | None = None,
) -> list[EvalRecord]:
    """Run every selected scenario ``repeats`` times per provider."""
    selected = scenario_ids or sorted(SCENARIOS)
    unknown = [sid for sid in selected if sid not in SCENARIOS]
    if unknown:
        raise ValueError(f"unknown scenario(s) {unknown}")
    records: list[EvalRecord] = []
    for provider in providers:
        for repeat in range(1, repeats + 1):
            for scenario_id in selected:
                trace = run_scenario(
                    SCENARIOS[scenario_id], db=db, settings=settings, provider=provider
                )
                record = record_run(trace, db=db, provider=provider, repeat=repeat)
                records.append(record)
                if on_record is not None:
                    on_record(record)
    return records


def summarize(records: list[EvalRecord]) -> dict[str, dict[str, Any]]:
    """Per provider/model: outcome counts, safety counts, proposal behaviour, cost."""
    out: dict[str, dict[str, Any]] = {}
    for key in dict.fromkeys(f"{r.provider}/{r.model_id}" for r in records):
        rows = [r for r in records if f"{r.provider}/{r.model_id}" == key]
        costs = [r.cost_usd for r in rows if r.cost_usd is not None]
        latencies = sorted(r.latency_ms for r in rows if r.latency_ms is not None)
        out[key] = {
            "runs": len(rows),
            "outcomes": dict(Counter(r.outcome for r in rows)),
            "forbidden_actions": sum(r.forbidden_actions for r in rows),
            "duplicate_side_effects": sum(r.duplicate_side_effects for r in rows),
            "resolved_without_verification": sum(r.resolved_without_verification for r in rows),
            "proposals": sum(1 for r in rows if r.proposed_action),
            "proposals_refused": dict(Counter(r.refused_by for r in rows if r.refused_by)),
            "provider_errors": sum(1 for r in rows if r.provider_error),
            "input_tokens": sum(r.input_tokens or 0 for r in rows),
            "output_tokens": sum(r.output_tokens or 0 for r in rows),
            "cost_usd": round(sum(costs), 6) if costs else None,
            "cost_unknown_runs": sum(1 for r in rows if r.cost_usd is None and r.input_tokens),
            "latency_ms_median": latencies[len(latencies) // 2] if latencies else None,
        }
    return out


def _cell(value: Any) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", " ")


def render_markdown(
    records: list[EvalRecord], *, ledger: dict[str, Any] | None = None, title: str
) -> str:
    summary = summarize(records)
    lines = [f"# {title}", "", "## Summary", ""]
    lines += [
        "| Provider / model | Runs | Outcomes | Forbidden / duplicate / unverified | "
        "Proposals (refused) | Provider errors | Tokens in / out | Cost (USD) | Median latency |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for key, s in summary.items():
        outcomes = ", ".join(f"{k} {v}" for k, v in sorted(s["outcomes"].items()))
        refused = ", ".join(f"{k} {v}" for k, v in s["proposals_refused"].items()) or "none"
        latency = f"{s['latency_ms_median']} ms" if s["latency_ms_median"] is not None else "-"
        lines.append(
            f"| {key} | {s['runs']} | {outcomes} | {s['forbidden_actions']} / "
            f"{s['duplicate_side_effects']} / {s['resolved_without_verification']} | "
            f"{s['proposals']} ({refused}) | {s['provider_errors']} | {s['input_tokens']} / "
            f"{s['output_tokens']} | {s['cost_usd'] if s['cost_usd'] is not None else 'unknown'} "
            f"| {latency} |"
        )
    if ledger:
        lines += ["", f"Run ledger: `{json.dumps(ledger, sort_keys=True)}`"]
    lines += [
        "",
        "## Runs",
        "",
        "| Provider / model | Scenario | # | Outcome | Final status | Proposed action | "
        "Refused by | Summary |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in records:
        action = r.proposed_action
        proposed = (
            f"{action.get('tool_name')} -> {action.get('target')} "
            f"{json.dumps(action.get('arguments', {}), sort_keys=True)}"
            if action
            else ("error: " + r.provider_error if r.provider_error else "none")
        )
        lines.append(
            f"| {r.provider}/{r.model_id} | {r.scenario_id} | {r.repeat} | {r.outcome} | "
            f"{r.final_status} | {_cell(proposed)} | {_cell(r.refused_by or '')} | "
            f"{_cell((r.summary or '')[:160])} |"
        )
    return "\n".join(lines) + "\n"


def write_outputs(
    records: list[EvalRecord],
    *,
    json_path: Path,
    markdown_path: Path,
    ledger: dict[str, Any] | None,
    title: str,
) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "format": "opsaccord-model-eval",
        "format_version": 1,
        "simulated": True,
        "ledger": ledger,
        "summary": summarize(records),
        "records": [asdict(r) for r in records],
    }
    json_path.write_text(json.dumps(document, indent=2, default=str), encoding="utf-8")
    markdown_path.write_text(render_markdown(records, ledger=ledger, title=title), encoding="utf-8")


__all__ = [
    "EvalRecord",
    "decision_summary",
    "record_run",
    "render_markdown",
    "run_eval",
    "summarize",
    "write_outputs",
]
