"""The API never builds a hosted model provider without a hard spending cap."""

from __future__ import annotations

import pytest

from opsaccord.api.app import build_provider
from opsaccord.config import Settings
from opsaccord.reasoning.budget import BudgetedProvider, ModelBudgetExceeded
from opsaccord.reasoning.deterministic import DeterministicAdapter
from opsaccord.reasoning.openai_compatible import PaidModelCallsDisabled


def _hosted(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "model_provider": "openai_compatible",
        "model_id": "some-model",
        "allow_paid_model_calls": True,
        "model_budget_usd": 1.0,
        "model_max_calls": 10,
        "model_input_cost_per_1k_usd": 0.001,
        "model_output_cost_per_1k_usd": 0.002,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def test_the_default_provider_is_deterministic_and_free(isolated_config: object) -> None:
    assert isinstance(build_provider(Settings()), DeterministicAdapter)


def test_a_hosted_provider_is_always_wrapped_in_the_cap() -> None:
    provider = build_provider(_hosted())

    assert isinstance(provider, BudgetedProvider)
    assert provider.ledger.budget_usd == 1.0 and provider.ledger.max_calls == 10
    assert provider.model_id == "some-model"


@pytest.mark.parametrize(
    ("overrides", "error", "message"),
    [
        ({"allow_paid_model_calls": False}, PaidModelCallsDisabled, "ALLOW_PAID"),
        ({"model_budget_usd": None}, ModelBudgetExceeded, "MODEL_BUDGET_USD"),
        ({"model_input_cost_per_1k_usd": None}, ModelBudgetExceeded, "rates"),
    ],
)
def test_the_server_refuses_to_start_an_uncapped_hosted_provider(
    overrides: dict[str, object], error: type[Exception], message: str
) -> None:
    with pytest.raises(error, match=message):
        build_provider(_hosted(**overrides))
