"""The target policy evaluates is the target the executor acts on.

Found while putting a hosted model in the loop (Phase 2): policy, approval binding and the
audit trail used ``proposal.target``, while dispatch used ``arguments["target"]``. The
deterministic adapter always kept the two equal, so Phase 1 never exercised the gap.
"""

from __future__ import annotations

import pytest

from opsaccord.config import Settings
from opsaccord.domain.models import ActionProposal, DiagnosisProposal
from opsaccord.persistence.db import Database
from opsaccord.reasoning.base import Proposal, ProposalRequest, ProposalUsage
from opsaccord.scenarios.fixtures import SCENARIOS
from opsaccord.scenarios.runner import run_scenario

pytestmark = pytest.mark.integration


class Scripted:
    """A provider that always proposes one fixed action."""

    provider = "scripted"

    def __init__(self, model_id: str, action: ActionProposal) -> None:
        self.model_id = model_id
        self.action = action

    def propose(self, request: ProposalRequest) -> Proposal:
        return Proposal(
            diagnosis=DiagnosisProposal(
                summary=f"scripted diagnosis for {request.service}",
                confidence=0.7,
                escalate=False,
                proposed_action=self.action,
            ),
            provider=self.provider,
            model_id=self.model_id,
            usage=ProposalUsage(),
        )


def test_a_proposal_cannot_name_one_target_and_act_on_another(
    db: Database, settings: Settings
) -> None:
    """Policy evaluated ``proposal.target`` while dispatch used ``arguments["target"]``.

    In the Ad incident, ad-service is allow-listed for an unapproved fix. A proposal that
    names ad-service but carries payment-service in its arguments would pass validation
    (the arguments are valid for payment), pass policy (ad-service needs no approval),
    and then change payment's flag without any human approval.
    """
    mismatched = Scripted(
        "mismatched",
        ActionProposal(
            tool_name="apply_remediation",
            target="ad-service",
            arguments={
                "target": "payment-service",
                "remediation_id": "disable_feature_flag",
                "flag_id": "paymentUnreachable",
            },
            rationale="looks like the ad fix",
        ),
    )
    trace = run_scenario(SCENARIOS["P1-02"], db=db, settings=settings, provider=mismatched)

    assert trace.executed_actions == []
    assert trace.approvals == []
    assert any("does not match" in note for note in trace.run_notes)


def test_a_missing_argument_target_is_taken_from_the_proposal(
    db: Database, settings: Settings
) -> None:
    """A model that names the target once is not refused for it, and nothing else is filled."""
    provider = Scripted(
        "terse",
        ActionProposal(
            tool_name="apply_remediation",
            target="ad-service",
            arguments={"remediation_id": "disable_feature_flag", "flag_id": "adHighCpu"},
            rationale="ad CPU is saturated by the adHighCpu flag",
        ),
    )
    trace = run_scenario(SCENARIOS["P1-02"], db=db, settings=settings, provider=provider)

    assert len(trace.executed_actions) == 1
    assert trace.executed_actions[0].arguments["target"] == "ad-service"


def test_a_read_tool_is_never_accepted_as_an_action(db: Database, settings: Settings) -> None:
    """deepseek-flash, P1-02, 2026-10-17: it proposed ``read_recent_changes`` as its action.

    The action path only checked that the tool was registered, so the read was claimed
    and handed to the write dispatcher, which called ``apply_remediation`` with a read's
    arguments. It failed before any side effect and was recorded UNCERTAIN, but a read
    must never reach the dispatcher at all: investigation reads are the controller's.
    """
    reader = Scripted(
        "reader",
        ActionProposal(
            tool_name="read_recent_changes",
            target="ad-service",
            arguments={},
            rationale="check for a recent deploy before remediating",
        ),
    )
    trace = run_scenario(SCENARIOS["P1-02"], db=db, settings=settings, provider=reader)

    assert trace.actions == []
    assert trace.executed_actions == []
    assert any("not a write tool" in note for note in trace.run_notes)
    assert str(trace.incident.status) == "READY_FOR_HUMAN"
