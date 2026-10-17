"""The hosted-model guard rails that need no database: the run budget, the prompt's
contract and size cap, the tool contracts a model may see, and refusal attribution."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from opsaccord.domain.models import DiagnosisProposal
from opsaccord.reasoning.base import Proposal, ProposalRequest, ProposalUsage
from opsaccord.reasoning.budget import (
    BudgetedProvider,
    ModelBudgetExceeded,
    SpendLedger,
    TokenRates,
)
from opsaccord.reasoning.openai_compatible import OpenAICompatibleAdapter, PromptTooLarge
from opsaccord.scenarios.model_eval import _refusal
from opsaccord.tools.registry import ToolRegistry

RATES = TokenRates(input_per_1k_usd=0.001, output_per_1k_usd=0.002)


class _Stub:
    provider = "stub"
    model_id = "stub-1"

    def __init__(self, usage: ProposalUsage | None = None, fail: bool = False) -> None:
        self.usage = usage or ProposalUsage(input_tokens=1000, output_tokens=500)
        self.fail = fail
        self.calls = 0

    def propose(self, request: ProposalRequest) -> Proposal:
        self.calls += 1
        if self.fail:
            raise RuntimeError("provider down")
        return Proposal(
            diagnosis=DiagnosisProposal(summary="stub diagnosis", confidence=0.5, escalate=True),
            provider=self.provider,
            model_id=self.model_id,
            usage=self.usage,
        )


def _request(**overrides: Any) -> ProposalRequest:
    values: dict[str, Any] = {
        "incident_id": "inc_1",
        "incident_title": "Payment unreachable",
        "service": "payment-service",
        "severity": "SEV1",
        "status": "INVESTIGATING",
        "evidence": (),
        "runbook_texts": (),
        "allowed_tools": ToolRegistry.default().names(),
        "steps_remaining": 5,
        "allowed_targets": ("payment-service", "cart-service"),
        "tool_contracts": ToolRegistry.default().contracts(),
    }
    values.update(overrides)
    return ProposalRequest(**values)


def _budgeted(inner: Any, ledger: SpendLedger) -> BudgetedProvider:
    return BudgetedProvider(
        inner, ledger=ledger, rates=RATES, max_input_tokens=10_000, max_output_tokens=1_000
    )


# -- the run budget --------------------------------------------------------------------


def test_a_call_is_charged_its_actual_cost_after_reserving_its_worst_case() -> None:
    ledger = SpendLedger(budget_usd=1.0, max_calls=10)
    provider = _budgeted(_Stub(), ledger)
    assert provider.worst_case_usd == pytest.approx(0.012)

    provider.propose(_request())

    assert ledger.calls == 1
    assert ledger.spent_usd == pytest.approx(0.002)  # 1000 in + 500 out at the rates
    assert ledger.charged_at_worst_case == 0


def test_a_call_that_could_exceed_the_budget_is_refused_before_it_is_made() -> None:
    inner = _Stub()
    ledger = SpendLedger(budget_usd=0.015, max_calls=10)
    provider = _budgeted(inner, ledger)

    provider.propose(_request())  # spends 0.002, leaves 0.013 >= 0.012
    provider.propose(_request())  # spends 0.002, leaves 0.011 <  0.012
    with pytest.raises(ModelBudgetExceeded):
        provider.propose(_request())
    assert inner.calls == 2
    assert ledger.refused == 1


def test_the_call_cap_holds() -> None:
    inner = _Stub()
    ledger = SpendLedger(budget_usd=100.0, max_calls=2)
    provider = _budgeted(inner, ledger)
    provider.propose(_request())
    provider.propose(_request())
    with pytest.raises(ModelBudgetExceeded, match="call cap"):
        provider.propose(_request())
    assert inner.calls == 2


def test_a_failed_or_unmetered_call_stays_charged_at_its_worst_case() -> None:
    ledger = SpendLedger(budget_usd=1.0, max_calls=10)
    with pytest.raises(RuntimeError):
        _budgeted(_Stub(fail=True), ledger).propose(_request())
    _budgeted(_Stub(usage=ProposalUsage()), ledger).propose(_request())

    assert ledger.spent_usd == pytest.approx(0.024)
    assert ledger.charged_at_worst_case == 2


def test_a_provider_without_rates_cannot_be_budgeted() -> None:
    with pytest.raises(ModelBudgetExceeded, match="rates"):
        BudgetedProvider(
            _Stub(),
            ledger=SpendLedger(budget_usd=1.0, max_calls=1),
            rates=None,
            max_input_tokens=1,
            max_output_tokens=1,
        )


# -- what the model is told ------------------------------------------------------------


def test_the_contracts_show_the_catalog_but_not_per_target_policy() -> None:
    contracts = {c.name: c for c in ToolRegistry.default().contracts()}
    remediation = contracts["apply_remediation"]

    assert remediation.requires_approval
    assert remediation.arguments == ("target", "remediation_id", "flag_id")
    assert "disable_feature_flag" in remediation.allowed_values["remediation_id"]
    assert "cartFailure" in remediation.allowed_values["flag_id"]
    # The per-target allow-list is policy, not documentation: it is not shown.
    assert not any("cart-service" in str(v) for v in remediation.allowed_values.values())


def _capture() -> tuple[list[dict[str, Any]], httpx.Client]:
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        content = json.dumps({"summary": "short summary", "confidence": 0.4, "escalate": True})
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": content}}],
                "usage": {"prompt_tokens": 900, "completion_tokens": 40},
            },
        )

    return sent, httpx.Client(transport=httpx.MockTransport(handler))


def _adapter(client: httpx.Client, **kwargs: Any) -> OpenAICompatibleAdapter:
    return OpenAICompatibleAdapter(
        base_url="https://model.test/v1",
        model_id="m-1",
        api_key_env="OPSACCORD_TEST_MODEL_KEY",
        allow_paid_calls=True,
        input_cost_per_1k_usd=0.001,
        output_cost_per_1k_usd=0.002,
        client=client,
        **kwargs,
    )


def test_the_prompt_carries_the_contracts_targets_and_output_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPSACCORD_TEST_MODEL_KEY", "test-key")
    sent, client = _capture()

    proposal = _adapter(client).propose(_request())

    prompt = sent[0]["messages"][1]["content"]
    assert "apply_remediation (write, requires human approval)" in prompt
    assert "remediation_id: one of ['disable_feature_flag', 'restart_workload']" in prompt
    assert "Services in scope: payment-service, cart-service" in prompt
    assert "escalate_reason" in prompt and "evidence_ids" in prompt
    assert proposal.usage.cost_usd == pytest.approx(0.00098)


def test_an_oversized_prompt_is_refused_before_any_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPSACCORD_TEST_MODEL_KEY", "test-key")
    sent, client = _capture()

    with pytest.raises(PromptTooLarge):
        _adapter(client, max_input_bytes=200).propose(_request())
    assert sent == []


# -- refusal attribution ---------------------------------------------------------------


def test_refusals_are_attributed_to_the_layer_that_wrote_them() -> None:
    assert _refusal(["diagnosis: x", "blocked: unknown tool 'shell_exec'"])[0] == "validation"
    assert _refusal(["policy blocked apply_remediation on payment-service: frozen"])[0] == (
        "policy"
    )
    assert _refusal(["policy blocked read: budget"]) == (None, None)
    assert _refusal(["execute: ok"]) == (None, None)
