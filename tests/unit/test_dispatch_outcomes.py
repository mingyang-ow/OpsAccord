"""The outcome contract for a dispatch that raised.

This is the one part of the dispatch path that is fully testable offline, and it
is the part that decides what a *real* side effect is recorded as, so it is pinned
here rather than only through the database-backed tests.

The defect these tests exist for: the general handler in ``execute_action`` caught
a bare ``Exception`` and recorded ``FAILED`` / ``CONFIRMED_NOT_APPLIED``. An
adapter can apply a change and then fail while reading, parsing, constructing or
recording its acknowledgement, so that classification asserted a non-execution the
code had no evidence for.
"""

from __future__ import annotations

import pytest

from opsaccord.domain.enums import ActionOutcome, ActionStatus
from opsaccord.tools.contracts import (
    DispatchRefused,
    DispatchTimeout,
    classify_dispatch_failure,
)


def test_a_proved_refusal_before_dispatch_is_a_definite_non_execution() -> None:
    """The adapter decided locally that no request was sent, so the ledger may say so."""
    failure = classify_dispatch_failure(
        DispatchRefused("demo adapter has no mapping for target 'unknown-service'")
    )

    assert failure.status is ActionStatus.FAILED
    assert failure.outcome is ActionOutcome.CONFIRMED_NOT_APPLIED
    assert "refused before dispatch" in failure.error


def test_a_dispatch_timeout_is_unknown_and_never_a_failure() -> None:
    failure = classify_dispatch_failure(DispatchTimeout("dispatch exceeded 3.0s"))

    assert failure.status is ActionStatus.UNCERTAIN
    assert failure.outcome is ActionOutcome.UNKNOWN
    assert failure.error == "dispatch exceeded 3.0s"


def test_a_timeout_without_a_message_still_records_something_usable() -> None:
    failure = classify_dispatch_failure(DispatchTimeout())

    assert failure.outcome is ActionOutcome.UNKNOWN
    assert failure.error.strip() != ""


@pytest.mark.parametrize(
    "error",
    [
        ValueError("malformed acknowledgement: the response body could not be parsed"),
        KeyError("paymentUnreachable"),
        RuntimeError("the connection was closed while recording the result"),
        TimeoutError("read timed out"),
        OSError("broken pipe"),
        LookupError("unexpected"),
    ],
)
def test_an_unclassifiable_error_never_claims_non_execution(error: BaseException) -> None:
    """Anything the gateway cannot classify stays ambiguous and escalates."""
    failure = classify_dispatch_failure(error)

    assert failure.status is ActionStatus.UNCERTAIN
    assert failure.outcome is ActionOutcome.UNKNOWN
    assert failure.outcome is not ActionOutcome.CONFIRMED_NOT_APPLIED
    assert failure.status is not ActionStatus.FAILED
    assert type(error).__name__ in failure.error
    assert "must be reconciled" in failure.error


def test_the_two_typed_outcomes_are_distinct_signals() -> None:
    """A refusal is not a timeout: the classification order depends on this."""
    assert not issubclass(DispatchRefused, DispatchTimeout)
    assert not issubclass(DispatchTimeout, DispatchRefused)
