"""The advisory-lock registry, the statement that takes the control gate, and the
order in which the gate and the per-incident row lock are requested.

The control gate is what makes a global human control ordered against a dispatch
authorization, and PostgreSQL advisory locks are a cluster-wide namespace keyed by
two integers, so the two properties worth pinning without a database are that the
keys cannot collide with each other and that the gate is taken in *transaction*
scope. A session-scoped ``pg_advisory_lock`` would outlive the transaction and
block whichever request borrowed the pooled connection next.

A third property needs no database either, and pins the correction of a real
deadlock: ``repositories.lock_incident`` asks for the gate *before* it asks for the
incident row. A caller that composes the two helpers by hand in the other order
inverts the documented order (gate -> incident row -> workflow row) and PostgreSQL
answers with a deadlock, so the order belongs to the helper rather than to a
comment asking callers to be careful.
"""

from __future__ import annotations

from typing import Any

import pytest

from opsaccord.persistence import repositories as repo
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


class OrderRecordingSession(RecordingSession):
    """A session stand-in that records the gate and the row lock in one sequence.

    ``RecordingSession`` keeps the two kinds of call in separate lists, which cannot
    express an order between them: "the gate was taken" and "the row was locked" are
    each true of the inverted composition too. This one merges them into a single
    log, because the order is the only thing the guard is for.
    """

    def __init__(self, row: Any) -> None:
        super().__init__()
        self.row = row
        self.sequence: list[str] = []

    def execute(self, statement: Any, parameters: dict[str, Any] | None = None) -> None:
        self.sequence.append(str(statement))
        super().execute(statement, parameters)

    def get(self, entity: Any, ident: Any, with_for_update: bool = False) -> Any:
        self.sequence.append(f"row lock {ident} with_for_update={with_for_update}")
        return self.row


def test_the_incident_row_lock_is_requested_only_after_the_control_gate() -> None:
    """The order is a property of ``lock_incident``, not of its callers.

    A regression here is a real deadlock rather than a failed assertion: a control
    writer that holds the incident row and then asks for the gate waits on a
    dispatch that holds the gate and waits for the row. The guard is therefore the
    merged sequence and not the two statements separately - swapping the two lines
    of ``lock_incident`` leaves both statements present, and only their order
    changes.
    """
    row = object()
    session = OrderRecordingSession(row)

    returned = repo.lock_incident(session, "inc-1")

    assert returned is row
    assert len(session.sequence) == 2, "one gate statement and one row-lock request"
    gate_statement, row_request = session.sequence
    assert "pg_advisory_xact_lock" in gate_statement
    assert row_request == "row lock inc-1 with_for_update=True"
    assert session.calls[0][1] == {
        "classid": CONTROL_GATE_LOCK[0],
        "objid": CONTROL_GATE_LOCK[1],
    }


def test_an_unknown_incident_fails_after_the_gate_is_taken() -> None:
    session = OrderRecordingSession(None)

    with pytest.raises(repo.RepositoryError):
        repo.lock_incident(session, "inc-missing")

    assert "pg_advisory_xact_lock" in session.sequence[0]
    assert session.sequence[1] == "row lock inc-missing with_for_update=True"
