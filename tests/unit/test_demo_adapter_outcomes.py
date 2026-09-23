"""What the simulated adapter can honestly say about its own side effects.

The gateway records whatever the adapter reports as the outcome of a real
dispatch, so these tests pin the adapter's half of that contract: which failures
prove that nothing happened, which ones prove the change landed, and which ones it
genuinely cannot tell apart. All of it is in-process and needs no database.
"""

from __future__ import annotations

from typing import Any

import pytest

from opsaccord.domain.enums import ActionOutcome
from opsaccord.tools import demo_environment as demo
from opsaccord.tools.contracts import DispatchRefused, DispatchTimeout

REMEDIATION = {
    "target": "payment-service",
    "remediation_id": "disable_feature_flag",
    "flag_id": "paymentUnreachable",
    "timeout_seconds": 1.0,
}


def unreachable_payment(**faults: Any) -> demo.DemoEnvironment:
    """P1-01's starting state: payment is down, and the flag can bring it back."""
    return demo.DemoEnvironment(
        services={
            "payment-service": demo.ServiceState(
                reachable=False,
                health="NOT_SERVING",
                error_rate=1.0,
                latency_p95_ms=1500.0,
                request_rate=0.0,
            )
        },
        faults=demo.FaultInjection(**faults),
    )


def test_a_change_that_landed_then_failed_while_acknowledging_proves_it_landed() -> None:
    """The applied-then-exception case: the adapter knows the write happened."""
    environment = unreachable_payment(action_response_error=True)

    with pytest.raises(ValueError, match="malformed acknowledgement"):
        environment.apply_remediation(idempotency_key="act-response-error", **REMEDIATION)

    service = environment.services["payment-service"]
    assert service.reachable is True, "the change really was applied"
    assert service.health == "SERVING"
    assert "act-response-error" in environment.applied
    assert environment.action_status("act-response-error") is ActionOutcome.CONFIRMED_APPLIED


def test_a_lost_acknowledgement_is_a_timeout_and_still_reconcilable() -> None:
    environment = unreachable_payment(action_dispatch_timeout=True)

    with pytest.raises(DispatchTimeout):
        environment.apply_remediation(idempotency_key="act-timeout", **REMEDIATION)

    assert environment.action_status("act-timeout") is ActionOutcome.CONFIRMED_APPLIED


def test_an_unknown_target_is_refused_before_anything_is_attempted() -> None:
    environment = unreachable_payment()

    with pytest.raises(DispatchRefused):
        environment.apply_remediation(
            idempotency_key="act-unknown-target",
            target="not-a-demo-service",
            remediation_id="disable_feature_flag",
            flag_id="paymentUnreachable",
            timeout_seconds=1.0,
        )

    assert environment.attempted == {}
    assert environment.applied == {}
    assert environment.action_status("act-unknown-target") is ActionOutcome.UNKNOWN


def test_an_unsupported_remediation_is_refused_without_an_attempt_or_a_record() -> None:
    """A refusal that proves nothing happened must not be remembered as an attempt."""
    environment = unreachable_payment()

    result = environment.apply_remediation(
        idempotency_key="act-unsupported",
        target="payment-service",
        remediation_id="restart_the_database",
        flag_id="paymentUnreachable",
        timeout_seconds=1.0,
    )

    assert result.succeeded is False
    assert "unsupported remediation" in (result.error or "")
    assert environment.attempted == {}
    assert environment.action_status("act-unsupported") is ActionOutcome.UNKNOWN


def test_a_successful_dispatch_that_did_not_help_is_still_an_applied_change() -> None:
    """Reconciliation reports what the external system did, not whether it worked."""
    environment = unreachable_payment(remediation_ineffective=True)

    result = environment.apply_remediation(idempotency_key="act-ineffective", **REMEDIATION)

    assert result.succeeded is True
    assert environment.action_status("act-ineffective") is ActionOutcome.CONFIRMED_APPLIED
    assert environment.services["payment-service"].reachable is False


def test_reconciliation_reports_unknown_when_the_status_query_is_unavailable() -> None:
    environment = unreachable_payment(status_query_available=False)

    environment.apply_remediation(idempotency_key="act-no-status", **REMEDIATION)

    assert environment.action_status("act-no-status") is ActionOutcome.UNKNOWN
