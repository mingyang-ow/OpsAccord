"""The advisory-lock registry and the statement that takes the control gate.

The control gate is what makes a global human control ordered against a dispatch
authorization, and PostgreSQL advisory locks are a cluster-wide namespace keyed by
two integers, so the two properties worth pinning without a database are that the
keys cannot collide with each other and that the gate is taken in *transaction*
scope. A session-scoped ``pg_advisory_lock`` would outlive the transaction and
block whichever request borrowed the pooled connection next.
"""

from __future__ import annotations

from typing import Any

from opsaccord.persistence.locks import (
    CONTROL_GATE_LOCK,
    LOCK_NAMESPACE,
    TEST_SCHEMA_RESET_LOCK,
    lock_control_gate,
)


class RecordingSession:
    """A session stand-in that records what a lock helper issued."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    def execute(self, statement: Any, parameters: dict[str, Any] | None = None) -> None:
        self.calls.append((str(statement), parameters))


def test_every_registered_key_is_a_distinct_pair_of_int4_values() -> None:
    for key in (CONTROL_GATE_LOCK, TEST_SCHEMA_RESET_LOCK):
        assert len(key) == 2
        assert key[0] == LOCK_NAMESPACE
        assert all(isinstance(part, int) and 0 <= part <= 2**31 - 1 for part in key)
    assert CONTROL_GATE_LOCK != TEST_SCHEMA_RESET_LOCK


def test_the_gate_is_transaction_scoped_and_uses_its_registered_key() -> None:
    session = RecordingSession()

    lock_control_gate(session)

    assert len(session.calls) == 1
    statement, parameters = session.calls[0]
    assert "pg_advisory_xact_lock" in statement
    assert "pg_advisory_lock(" not in statement
    assert parameters == {"classid": CONTROL_GATE_LOCK[0], "objid": CONTROL_GATE_LOCK[1]}
