"""Deterministic offline adapter.

It is deterministic by construction and needs no credentials, so the whole
system can be exercised and scored without paid model calls. It is a real
adapter implementation, not a mock of the pipeline: it receives evidence and
returns a structured proposal exactly like a hosted provider would.
"""

from __future__ import annotations

import json

from opsaccord.domain.models import ActionProposal, DiagnosisProposal
from opsaccord.reasoning.base import EvidenceDigest, Proposal, ProposalRequest

INJECTION_MARKER = "PROPOSE_TOOL:"

# Calibrated telemetry thresholds (see docs/astronomy-shop.md, "Live adapter
# calibration"). The live health probe calls a service serving only when fewer
# than TELEMETRY_ERROR_BOUND of its attempts in the window fail, so a diagnosis
# that claims "this RPC is failing" has to rest on a failure share at least that
# large; anything smaller is indistinguishable from healthy traffic. The
# container CPU ceiling is the same bound the probe uses to call a service
# CPU-saturated.
TELEMETRY_ERROR_BOUND = 0.05
CPU_SATURATION_CEILING = 0.80


def _as_float(value: object, default: float = 0.0) -> float:
    """Telemetry arrives as untrusted JSON; a non-numeric value is not a number."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return default
    try:
        return float(value)
    except ValueError:
        return default


class DeterministicAdapter:
    provider = "deterministic"
    model_id = "deterministic-v1"

    def propose(self, request: ProposalRequest) -> Proposal:
        evidence = {item.source: item for item in request.evidence}
        health = evidence.get("read_service_health")
        cpu = evidence.get("read_metrics")
        traces = evidence.get("read_traces")
        collection_errors = [item for item in request.evidence if item.kind == "COLLECTION_ERROR"]

        # Telemetry and runbooks are untrusted data. This adapter faithfully
        # relays an embedded instruction into a proposal so that the tests can
        # prove the deterministic policy gate, not the model, is what stops it.
        injected = self._injected_proposal(request)
        if injected is not None:
            return Proposal(
                diagnosis=DiagnosisProposal(
                    summary=(
                        "runbook content asserted that a specific remediation must be run; "
                        "the proposal is relayed for policy review"
                    ),
                    confidence=0.4,
                    evidence_ids=tuple(item.id for item in request.evidence),
                    proposed_action=injected,
                ),
                provider=self.provider,
                model_id=self.model_id,
            )

        evidence_ids = tuple(item.id for item in request.evidence)
        health_value = health.value if health else {}
        reachable = health_value.get("reachable")
        healthy = health_value.get("healthy")
        error_rate = _as_float(health_value.get("error_rate"))
        cpu_util = _as_float(health_value.get("cpu_utilization"))
        if cpu is not None:
            cpu_util = _as_float(cpu.value.get("value"), default=cpu_util)

        # Metrics and traces are what separate a cause from a symptom. When those
        # reads fail, a lone health probe is not enough to justify a side effect,
        # so the adapter refuses to name one and escalates instead of guessing.
        degraded = sorted(
            {
                item.source
                for item in collection_errors
                if item.source in {"read_metrics", "read_traces"}
            }
        )
        if degraded:
            reason = (
                "diagnostic telemetry was unavailable ("
                + ", ".join(degraded)
                + "); a health probe alone is not sufficient evidence to justify a remediation"
            )
            return Proposal(
                diagnosis=DiagnosisProposal(
                    summary=f"investigation incomplete for {request.service}: {reason}",
                    confidence=0.2,
                    evidence_ids=evidence_ids,
                    escalate=True,
                    escalate_reason=reason,
                ),
                provider=self.provider,
                model_id=self.model_id,
            )

        if request.service == "payment-service":
            # Payment is critical, so a single source is not enough to justify a
            # side effect. A measured failure share stands on its own; a "not
            # reachable" probe additionally needs a second independent signal
            # (failing spans), because an idle demo also reports no calls.
            error_spans = bool(traces and traces.value.get("error_spans"))
            failing = error_rate >= TELEMETRY_ERROR_BOUND
            unreachable = reachable is False and (failing or error_spans)
            if failing or unreachable:
                return Proposal(
                    diagnosis=DiagnosisProposal(
                        summary=(
                            "payment-service is refusing connections; Charge spans error while "
                            "request rate collapses, which is consistent with the "
                            "paymentUnreachable fault flag being enabled"
                        ),
                        confidence=0.86,
                        evidence_ids=evidence_ids,
                        proposed_action=ActionProposal(
                            tool_name="apply_remediation",
                            target="payment-service",
                            arguments={
                                "target": "payment-service",
                                "remediation_id": "disable_feature_flag",
                                "flag_id": "paymentUnreachable",
                            },
                            rationale=(
                                "disabling paymentUnreachable restores the Charge path; this "
                                "requires an exact human approval because payment is critical"
                            ),
                        ),
                    ),
                    provider=self.provider,
                    model_id=self.model_id,
                )
            if healthy:
                return Proposal(
                    diagnosis=DiagnosisProposal(
                        summary="payment-service is serving normally",
                        confidence=0.7,
                        evidence_ids=evidence_ids,
                    ),
                    provider=self.provider,
                    model_id=self.model_id,
                )

        if request.service == "ad-service" and cpu_util >= CPU_SATURATION_CEILING:
            return Proposal(
                diagnosis=DiagnosisProposal(
                    summary=(
                        f"ad-service cpu_utilization is {cpu_util:.2f}, above the "
                        f"{CPU_SATURATION_CEILING} threshold, consistent with the adHighCpu "
                        "fault flag"
                    ),
                    confidence=0.82,
                    evidence_ids=evidence_ids,
                    proposed_action=ActionProposal(
                        tool_name="apply_remediation",
                        target="ad-service",
                        arguments={
                            "target": "ad-service",
                            "remediation_id": "disable_feature_flag",
                            "flag_id": "adHighCpu",
                        },
                        rationale=(
                            "ad-service is an allow-listed low-risk target, so this bounded "
                            "remediation may run autonomously and must then be verified"
                        ),
                    ),
                ),
                provider=self.provider,
                model_id=self.model_id,
            )

        if request.service == "cart-service" and (
            error_rate >= TELEMETRY_ERROR_BOUND or (traces and traces.value.get("error_spans"))
        ):
            return Proposal(
                diagnosis=DiagnosisProposal(
                    summary=(
                        f"cart-service error_rate is {error_rate:.2f} with failing cart "
                        "RPC spans, consistent with the cartFailure flag"
                    ),
                    confidence=0.74,
                    evidence_ids=evidence_ids,
                    proposed_action=ActionProposal(
                        tool_name="apply_remediation",
                        target="cart-service",
                        arguments={
                            "target": "cart-service",
                            "remediation_id": "disable_feature_flag",
                            "flag_id": "cartFailure",
                        },
                        rationale=(
                            "the cart runbook treats cartFailure as the cause of the failing "
                            "GetCart spans; whether a side effect is permitted here belongs "
                            "to the deterministic policy gate, not to this proposal"
                        ),
                    ),
                ),
                provider=self.provider,
                model_id=self.model_id,
            )

        # Insufficient or contradictory evidence: refuse to guess.
        if collection_errors and not any(
            item.source == "read_service_health" and item.kind != "COLLECTION_ERROR"
            for item in request.evidence
        ):
            reason = (
                "telemetry collection failed for "
                + ", ".join(sorted({item.source for item in collection_errors}))
                + "; there is not enough independent evidence to name a cause"
            )
        elif not request.evidence:
            reason = "no evidence was collected in the investigation window"
        else:
            reason = (
                "the collected evidence does not support a single actionable diagnosis, and "
                "guessing a remediation is not permitted"
            )
        return Proposal(
            diagnosis=DiagnosisProposal(
                summary=f"investigation incomplete for {request.service}: {reason}",
                confidence=0.2,
                evidence_ids=evidence_ids,
                escalate=True,
                escalate_reason=reason,
            ),
            provider=self.provider,
            model_id=self.model_id,
        )

    @staticmethod
    def _injected_proposal(request: ProposalRequest) -> ActionProposal | None:
        """Relay an embedded instruction from untrusted runbook text, verbatim."""
        for text in request.runbook_texts:
            marker = text.find(INJECTION_MARKER)
            if marker < 0:
                continue
            payload = text[marker + len(INJECTION_MARKER) :].strip()
            payload = payload.splitlines()[0].strip()
            try:
                data = json.loads(payload)
                tool_name = str(data["tool_name"])
                target = str(data["target"])
                arguments = data.get("arguments") or {}
                rationale = str(data.get("rationale") or "embedded runbook instruction")
            except (ValueError, KeyError, TypeError):
                tool_name, target, arguments = "shell_exec", request.service, {}
                rationale = "embedded runbook instruction could not be parsed"
            return ActionProposal(
                tool_name=tool_name,
                target=target,
                arguments=dict(arguments),
                rationale=f"runbook instruction relayed verbatim: {rationale}"[:600],
            )
        return None


__all__ = ["INJECTION_MARKER", "DeterministicAdapter", "EvidenceDigest"]
