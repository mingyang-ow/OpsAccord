"""The advisory-lock registry and the global control gate.

PostgreSQL advisory locks are a cluster-wide resource keyed by a pair of 32-bit
integers, so every use in this project - application and tests - has to draw from
one registry. Two features that pick the same pair serialise each other, and a
session-scoped test lock held for a whole pytest session would block a dispatch
forever. The keys are therefore declared here, once, with the reason each exists.

The control gate is the mechanism behind the ordering guarantee documented in
``docs/architecture.md`` and D17: every side-effect authorization and every human
control write takes it as the *first* lock of its transaction, so no control can
commit inside the window between an authorization reading the control set and
that authorization's transaction committing.

Two properties of ``pg_advisory_xact_lock`` are load-bearing:

* it is *transaction*-scoped, so the server releases it at commit, at rollback,
  and when a crashed backend dies. A worker that dies holding the gate cannot
  leave every future control write waiting forever;
* because it is not session-scoped, it cannot leak into the next transaction
  that borrows the same pooled connection, which ``pg_advisory_lock`` would.

No lock here is ever held across an external call. The gate covers a short
in-database transaction; the dispatch itself runs after that commit and under no
lock, so a hung adapter cannot freeze human controls.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import Session

#: Project namespace shared by every advisory lock this codebase takes.
LOCK_NAMESPACE = 11296

#: The control gate: held by every side-effect authorization
#: (``ToolGateway.execute_action``) and by every human control write
#: (``repositories.add_constraint``, ``repositories.set_takeover``).
CONTROL_GATE_LOCK = (LOCK_NAMESPACE, 1)

#: Held for a whole pytest session by ``tests.support.isolation.dedicated_test_session``
#: while it rebuilds the dedicated test schema. Deliberately a different key from
#: the control gate: the drop of ``public`` and every dispatch inside the suite
#: run against the same database, and a collision would deadlock the suite.
TEST_SCHEMA_RESET_LOCK = (LOCK_NAMESPACE, 2)


def lock_control_gate(session: Session) -> None:
    """Acquire the control gate for the remainder of this transaction.

    Call this before any other lock in the transaction. The documented order is
    control gate -> incident row -> workflow row; taking the gate afterwards is
    what would create a cycle, because a control writer that already holds the
    incident row lock would wait on the gate while the gate holder waits on the
    incident row.
    """
    session.execute(
        sa.text("SELECT pg_advisory_xact_lock(:classid, :objid)"),
        {"classid": CONTROL_GATE_LOCK[0], "objid": CONTROL_GATE_LOCK[1]},
    )


__all__ = [
    "CONTROL_GATE_LOCK",
    "LOCK_NAMESPACE",
    "TEST_SCHEMA_RESET_LOCK",
    "lock_control_gate",
]
