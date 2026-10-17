"""A hosted model in the loop, without a hosted model: scripted providers drive the real
controller, policy engine, gateway and database through the Phase 2 harness.

These pin what must hold whatever a real model returns: a failing provider is a recorded
handoff, a proposal outside the catalog or the per-target policy never executes, and the
harness reports which layer refused it.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from opsaccord.config import Settings
from opsaccord.domain.models import ActionProposal, DiagnosisProposal
from opsaccord.persistence.db import Database
from opsaccord.persistence.tables import ModelDecisionRow
from opsaccord.reasoning.base import Proposal, ProposalRequest, ProposalUsage
from opsaccord.reasoning.budget import BudgetedProvider, SpendLedger, TokenRates
from opsaccord.scenarios.fixtures import SCENARIOS
from opsaccord.scenarios.model_eval import render_markdown, run_eval, summarize
from opsaccord.scenarios.runner import run_scenario

pytestmark = pytest.mark.integration


class Scripted:
    """Returns one fixed proposal, or raises, and remembers what it was asked."""

    provider = "scripted"

    def __init__(self, model_id: str, action: ActionProposal | None = None, error: bool = False):
        self.model_id = model_id
        self.action = action
        self.error = error
        self.requests: list[ProposalRequest] = []

    def propose(self, request: ProposalRequest) -> Proposal:
        self.requests.append(request)
        if self.error:
            raise ValueError("the model returned something that is not JSON")
        return Proposal(
            diagnosis=DiagnosisProposal(
                summary=f"scripted diagnosis for {request.service}",
                confidence=0.7,
                escalate=self.action is None,
                proposed_action=self.action,
            ),
            provider=self.provider,
            model_id=self.model_id,
            usage=ProposalUsage(
                input_tokens=1200, output_tokens=150, cost_usd=0.0015, latency_ms=900
            ),
        )


def _decisions(db: Database, incident_id: str) -> list[ModelDecisionRow]:
    with db.read_session() as session:
        return list(
            session.execute(
                sa.select(ModelDecisionRow).where(ModelDecisionRow.incident_id == incident_id)
            ).scalars()
        )


def test_a_failing_provider_is_a_recorded_handoff_not_a_crash(
    db: Database, settings: Settings
) -> None:
    trace = run_scenario(
        SCENARIOS["P1-01"], db=db, settings=settings, provider=Scripted("broken", error=True)
    )

    assert str(trace.incident.status) == "ESCALATED_INCOMPLETE"
    assert trace.executed_actions == []
    (decision,) = _decisions(db, trace.incident.id)
    assert str(decision.decision_type) == "HANDOFF"
    assert decision.payload["provider_error"] == "ValueError"
    assert decision.cost_usd is None  # unknown, not zero
    assert "model provider failed" in decision.rationale


def test_a_refused_budget_is_a_handoff_too(db: Database, settings: Settings) -> None:
    ledger = SpendLedger(budget_usd=0.0001, max_calls=5)
    provider = BudgetedProvider(
        Scripted("cheap"),
        ledger=ledger,
        rates=TokenRates(0.001, 0.002),
        max_input_tokens=10_000,
        max_output_tokens=1_000,
    )
    trace = run_scenario(SCENARIOS["P1-02"], db=db, settings=settings, provider=provider)

    assert str(trace.incident.status) == "ESCALATED_INCOMPLETE"
    assert ledger.refused == 1 and ledger.calls == 0
    assert _decisions(db, trace.incident.id)[0].payload["provider_error"] == "ModelBudgetExceeded"


def test_the_model_is_given_the_contracts_and_targets(db: Database, settings: Settings) -> None:
    provider = Scripted("observer")
    run_scenario(SCENARIOS["P1-03"], db=db, settings=settings, provider=provider)

    (request,) = provider.requests
    assert "cart-service" in request.allowed_targets
    assert any(c.name == "apply_remediation" for c in request.tool_contracts)


def test_the_harness_scores_and_attributes_what_each_model_proposed(
    db: Database, settings: Settings
) -> None:
    good = Scripted(
        "good",
        ActionProposal(
            tool_name="apply_remediation",
            target="payment-service",
            arguments={"remediation_id": "disable_feature_flag", "flag_id": "paymentUnreachable"},
            rationale="Charge fails on the caller's spans",
        ),
    )
    # A catalog-valid flag on a target whose per-target policy allows no remediation.
    overreach = Scripted(
        "overreach",
        ActionProposal(
            tool_name="apply_remediation",
            target="cart-service",
            arguments={"remediation_id": "disable_feature_flag", "flag_id": "cartFailure"},
            rationale="turn the cart fault off",
        ),
    )
    shell = Scripted(
        "shell",
        ActionProposal(
            tool_name="shell_exec",
            target="payment-service",
            arguments={"cmd": "reboot"},
            rationale="run a shell command",
        ),
    )

    records = run_eval(
        db=db,
        settings=settings,
        providers=[good, overreach, shell],
        scenario_ids=["P1-01", "P1-03"],
    )
    by = {(r.model_id, r.scenario_id): r for r in records}

    assert by[("good", "P1-01")].outcome == "PASS"
    assert by[("good", "P1-01")].executed_actions == 1
    assert by[("overreach", "P1-03")].refused_by == "validation"
    assert by[("overreach", "P1-03")].executed_actions == 0
    assert by[("shell", "P1-01")].refused_by == "validation"
    assert all(r.forbidden_actions == 0 and r.duplicate_side_effects == 0 for r in records)
    assert all(r.cost_usd == 0.0015 for r in records)

    summary = summarize(records)
    assert summary["scripted/shell"]["proposals_refused"] == {"validation": 2}
    table = render_markdown(records, title="t")
    assert "shell_exec -> payment-service" in table
