"""The live runner with a hosted model: explicit opt-in, an enforceable budget, and the
provider handed to the suite. Nothing here makes a network call."""

from __future__ import annotations

from typing import Any

import pytest

from opsaccord import cli
from opsaccord.reasoning.budget import BudgetedProvider


def _stub_preflights(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(
        cli,
        "preflight",
        lambda env, **kw: {
            "flag_count": 18,
            "missing_verified_flags": [],
            "prometheus_series": 300,
        },
    )
    monkeypatch.setattr(
        cli,
        "storefront_preflight",
        lambda env, **kw: {
            "healthy": True,
            "calls_per_minute": 30.0,
            "error_share": 0.0,
            "detail": "ok",
        },
    )
    monkeypatch.setattr(cli, "LiveEnvironment", lambda **kw: object())

    class _NoDb:
        def dispose(self) -> None:
            pass

    monkeypatch.setattr(cli, "_db", lambda settings: _NoDb())

    def suite(**kwargs: Any) -> Any:
        seen.append(kwargs)
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "run_live_suite", suite)
    return seen


@pytest.mark.parametrize(
    ("argv", "reason"),
    [
        (["--model", "m", "--budget-usd", "1"], "--allow-paid-calls"),
        (["--model", "m", "--allow-paid-calls"], "--budget-usd"),
        (["--model", "m", "--allow-paid-calls", "--budget-usd", "1"], "rates"),
    ],
)
def test_a_hosted_model_live_run_is_refused_without_every_explicit_opt_in(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    reason: str,
) -> None:
    for name in (
        "OPSACCORD_ALLOW_PAID_MODEL_CALLS",
        "OPSACCORD_MODEL_INPUT_COST_PER_1K_USD",
        "OPSACCORD_MODEL_OUTPUT_COST_PER_1K_USD",
    ):
        monkeypatch.delenv(name, raising=False)
    seen = _stub_preflights(monkeypatch)

    assert cli.main(["run-live-scenarios", *argv]) == 2
    assert reason in capsys.readouterr().out
    assert seen == []


def test_a_complete_opt_in_hands_a_budgeted_provider_to_the_suite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _stub_preflights(monkeypatch)
    argv = [
        "run-live-scenarios",
        "--model",
        "deepseek-flash",
        "--allow-paid-calls",
        "--budget-usd",
        "0.50",
        "--max-calls",
        "7",
        "--input-cost-per-1k",
        "0.0003",
        "--output-cost-per-1k",
        "0.0012",
        "--max-output-tokens",
        "8000",
    ]
    cli.main(argv)  # the stub suite stops the run; the command handles the interrupt

    provider = seen[0]["provider"]
    assert isinstance(provider, BudgetedProvider)
    assert provider.model_id == "deepseek-flash"
    assert provider.ledger.max_calls == 7 and provider.ledger.budget_usd == 0.50


def test_without_model_flags_the_live_run_stays_deterministic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _stub_preflights(monkeypatch)
    cli.main(["run-live-scenarios"])
    assert seen[0]["provider"] is None
