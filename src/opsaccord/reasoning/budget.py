"""A hard, run-level spending cap around a hosted reasoning provider.

``allow_paid_model_calls`` only switches paid calls on; on its own it bounds nothing. A
model evaluation makes dozens of calls, so it needs a cap that holds across the whole run:
a maximum number of calls and a maximum spend in USD, checked *before* each call against
that call's worst-case cost. A call that could take the run over budget is refused, never
made and then regretted.

The worst case is computed from configured per-1k-token rates, so a provider without
rates cannot be budgeted and is refused at construction. A call whose response reports no
usage is charged at its worst case, because unknown is not zero.

A refusal is raised as :class:`ModelBudgetExceeded`. The controller treats it like any
other provider failure: a recorded handoff, never a crash and never a guessed decision.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from opsaccord.reasoning.base import Proposal, ProposalRequest, ReasoningProvider


class ModelBudgetExceeded(RuntimeError):
    pass


@dataclass(frozen=True)
class TokenRates:
    input_per_1k_usd: float
    output_per_1k_usd: float

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        return (
            input_tokens / 1000 * self.input_per_1k_usd
            + output_tokens / 1000 * self.output_per_1k_usd
        )


@dataclass
class SpendLedger:
    """What a run has spent. Shared by every wrapped call in the run."""

    budget_usd: float
    max_calls: int
    calls: int = 0
    refused: int = 0
    spent_usd: float = 0.0
    charged_at_worst_case: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def snapshot(self) -> dict[str, float | int]:
        with self._lock:
            return {
                "budget_usd": self.budget_usd,
                "max_calls": self.max_calls,
                "calls": self.calls,
                "refused": self.refused,
                "spent_usd": round(self.spent_usd, 6),
                "charged_at_worst_case": self.charged_at_worst_case,
            }


class BudgetedProvider:
    """Wrap a provider so no call can take the run past its cap."""

    def __init__(
        self,
        inner: ReasoningProvider,
        *,
        ledger: SpendLedger,
        rates: TokenRates | None,
        max_input_tokens: int,
        max_output_tokens: int,
    ) -> None:
        if rates is None:
            raise ModelBudgetExceeded(
                "a hosted provider cannot be budgeted without per-1k-token input and output "
                "rates; set OPSACCORD_MODEL_INPUT_COST_PER_1K_USD and "
                "OPSACCORD_MODEL_OUTPUT_COST_PER_1K_USD"
            )
        if ledger.budget_usd <= 0 or ledger.max_calls < 1:
            raise ModelBudgetExceeded("the run budget must allow at least one call")
        self.inner = inner
        self.provider = inner.provider
        self.model_id = inner.model_id
        self.ledger = ledger
        self.rates = rates
        self.worst_case_usd = rates.cost(max_input_tokens, max_output_tokens)

    def propose(self, request: ProposalRequest) -> Proposal:
        ledger = self.ledger
        with ledger._lock:
            if ledger.calls >= ledger.max_calls:
                ledger.refused += 1
                raise ModelBudgetExceeded(f"the run's call cap of {ledger.max_calls} is reached")
            if ledger.spent_usd + self.worst_case_usd > ledger.budget_usd:
                ledger.refused += 1
                raise ModelBudgetExceeded(
                    f"a call could cost up to ${self.worst_case_usd:.4f}, and the run has "
                    f"${ledger.budget_usd - ledger.spent_usd:.4f} of ${ledger.budget_usd:.2f} left"
                )
            # Reserve the worst case before calling, so a concurrent caller cannot spend it.
            ledger.calls += 1
            ledger.spent_usd += self.worst_case_usd
        actual: float | None = None
        try:
            proposal = self.inner.propose(request)
            usage = proposal.usage
            if usage.input_tokens is not None and usage.output_tokens is not None:
                actual = self.rates.cost(usage.input_tokens, usage.output_tokens)
            return proposal
        finally:
            with ledger._lock:
                if actual is None:
                    # A failed call or one without usage stays charged at its worst case.
                    ledger.charged_at_worst_case += 1
                else:
                    ledger.spent_usd += actual - self.worst_case_usd


__all__ = ["BudgetedProvider", "ModelBudgetExceeded", "SpendLedger", "TokenRates"]
