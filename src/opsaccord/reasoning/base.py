"""Provider-neutral reasoning interface.

The application runtime model is chosen by explicit configuration. The coding
agent's own model/provider is never consulted, and a provider can only return a
:class:`DiagnosisProposal`; it has no access to the database, the tool gateway,
or the demo environment.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from opsaccord.domain.models import DiagnosisProposal


@dataclass(frozen=True)
class EvidenceDigest:
    id: str
    kind: str
    source: str
    service: str
    summary: str
    value: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ProposalRequest:
    incident_id: str
    incident_title: str
    service: str
    severity: str
    status: str
    evidence: tuple[EvidenceDigest, ...]
    runbook_texts: tuple[str, ...]
    allowed_tools: tuple[str, ...]
    steps_remaining: int


@dataclass(frozen=True)
class ProposalUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    latency_ms: int | None = None


@dataclass(frozen=True)
class Proposal:
    diagnosis: DiagnosisProposal
    provider: str
    model_id: str
    usage: ProposalUsage = ProposalUsage()


class ReasoningProvider(Protocol):
    provider: str
    model_id: str

    def propose(self, request: ProposalRequest) -> Proposal: ...
