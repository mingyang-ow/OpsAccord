"""Isolation guards for destructive test setup.

The pytest session rebuilds the public schema of its database once per session.
That is how the suite gets migration-accurate evidence, and it is also a loaded
gun: it must never point at a database that holds demo or scenario records, and
two sessions must never do it at the same time.

Three guards live here. ``tests/conftest.py`` uses them for every integration
run, and ``tests/integration/test_isolation.py`` asserts their behaviour:

* ``assert_dedicated_test_database`` refuses any URL that is not the dedicated
  ``*_test`` database;
* ``assert_checkpoints_are_local`` refuses a session whose LangGraph checkpoint
  URL points outside that same database, so recovery evidence cannot be split
  across two databases;
* ``dedicated_test_session`` holds a database-scoped advisory lock for the whole
  session, so a second concurrent run fails loudly instead of dropping the schema
  out from under the first one.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import sqlalchemy as sa

from opsaccord.config import Settings, to_libpq_dsn

TEST_DATABASE_SUFFIX = "_test"

# Database-scoped advisory lock key: (classid, objid). Advisory locks are a
# cluster-wide resource, so the two-argument form is used to scope the lock to
# the database the session actually resets.
ADVISORY_LOCK_KEY = (11296, 1)


def database_name(url: str) -> str:
    return url.rsplit("/", 1)[-1].split("?")[0]


def assert_dedicated_test_database(url: str) -> str:
    """Return ``url`` if it is the dedicated test database, else refuse."""
    name = database_name(url)
    if not name.endswith(TEST_DATABASE_SUFFIX):
        raise RuntimeError(
            f"refusing destructive schema setup for database {name!r}: the suite only "
            f"rebuilds a database whose name ends with {TEST_DATABASE_SUFFIX!r}"
        )
    return url


def assert_checkpoints_are_local(settings: Settings) -> None:
    """Checkpoints must live in the same database as the state they describe."""
    checkpoint = settings.checkpoint_url
    if to_libpq_dsn(checkpoint) != to_libpq_dsn(settings.state_database_url):
        raise RuntimeError(
            "refusing to run: checkpoint_url "
            f"({database_name(checkpoint)}) must be the state database "
            f"({database_name(settings.state_database_url)}) for the selected app_env"
        )


@contextmanager
def dedicated_test_session(url: str) -> Iterator[None]:
    """Hold the session lock, or refuse to run concurrently with another session."""
    assert_dedicated_test_database(url)
    # AUTOCOMMIT matters: this connection lives for the whole session, and an
    # open idle transaction would block LangGraph's CREATE INDEX CONCURRENTLY
    # (and any other concurrent DDL) forever.
    engine = sa.create_engine(url, pool_size=1, max_overflow=0, isolation_level="AUTOCOMMIT")
    connection = engine.connect()
    try:
        acquired = connection.execute(
            sa.text("SELECT pg_try_advisory_lock(:classid, :objid)"),
            {"classid": ADVISORY_LOCK_KEY[0], "objid": ADVISORY_LOCK_KEY[1]},
        ).scalar()
        if not acquired:
            raise RuntimeError(
                "another test session already holds the schema-reset lock on "
                f"{database_name(url)}; concurrent runs would truncate each other's schema"
            )
        try:
            yield
        finally:
            connection.execute(
                sa.text("SELECT pg_advisory_unlock(:classid, :objid)"),
                {"classid": ADVISORY_LOCK_KEY[0], "objid": ADVISORY_LOCK_KEY[1]},
            )
    finally:
        connection.close()
        engine.dispose()


def reset_public_schema(url: str) -> None:
    """Drop and recreate ``public`` in the dedicated test database only."""
    assert_dedicated_test_database(url)
    engine = sa.create_engine(url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as connection:
            connection.execute(sa.text("DROP SCHEMA IF EXISTS public CASCADE"))
            connection.execute(sa.text("CREATE SCHEMA public"))
    finally:
        engine.dispose()


__all__ = [
    "ADVISORY_LOCK_KEY",
    "TEST_DATABASE_SUFFIX",
    "assert_checkpoints_are_local",
    "assert_dedicated_test_database",
    "database_name",
    "dedicated_test_session",
    "reset_public_schema",
]
