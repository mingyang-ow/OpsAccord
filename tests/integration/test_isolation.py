"""Regression evidence that test setup cannot reach demo or scenario records.

The suite intentionally rebuilds the schema of its database once per session.
These tests pin the three properties that make that safe:

* the three environments resolve to three different databases;
* the destructive reset refuses any database that is not the dedicated ``*_test``
  one, and checkpoint tables must live in the same database as the state they
  describe;
* a real session reset leaves demo records untouched, and a second concurrent
  session is refused instead of truncating the schema mid-run.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from collections.abc import Iterator

import pytest
import sqlalchemy as sa

from opsaccord.config import REPO_ROOT, Settings
from opsaccord.persistence.db import Database
from opsaccord.workflow.checkpoints import ensure_checkpoint_schema, reset_setup_cache
from support.isolation import (
    assert_checkpoints_are_local,
    assert_dedicated_test_database,
    database_name,
    dedicated_test_session,
    reset_public_schema,
)

pytestmark = pytest.mark.integration


def _migrate(url: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env={**os.environ, "OPSACCORD_DATABASE_URL": url},
    )
    if result.returncode != 0:
        raise RuntimeError(f"alembic upgrade head failed\n{result.stdout}\n{result.stderr}")


def _sentinel_count(db: Database, sentinel_id: str) -> int:
    with db.read_session() as session:
        return int(
            session.execute(
                sa.text("SELECT count(*) FROM incidents WHERE id = :id"),
                {"id": sentinel_id},
            ).scalar_one()
        )


@pytest.fixture(scope="module")
def demo_sentinel(settings: Settings) -> Iterator[tuple[Database, str]]:
    """Migrate the demo database and leave one incident row in it.

    The row is a real demo record of the kind the live environment writes, so
    "the test suite ran and the row survived" is meaningful evidence. It is
    removed again on teardown; the append-only tables are deliberately not used
    here because they cannot be cleaned up.
    """
    url = settings.database_url
    if database_name(url).endswith("_test"):
        raise RuntimeError("the demo database must not be the test database")
    _migrate(url)
    sentinel_id = f"inc-isolation-{uuid.uuid4().hex[:12]}"
    db = Database(url)
    with db.session() as session:
        session.execute(
            sa.text(
                "INSERT INTO incidents "
                "(id, title, service, severity, status, summary, scenario_id, "
                " created_at, updated_at) "
                "VALUES (:id, 'isolation probe', 'payment-service', 'SEV3', 'NEW', "
                " NULL, NULL, now(), now())"
            ),
            {"id": sentinel_id},
        )
    try:
        yield db, sentinel_id
    finally:
        with db.session() as session:
            session.execute(sa.text("DELETE FROM incidents WHERE id = :id"), {"id": sentinel_id})
        db.dispose()


def test_environments_resolve_to_three_separate_databases() -> None:
    envs = {
        name: database_name(Settings(app_env=name).state_database_url)
        for name in ("dev", "scenario", "test", "ci")
    }
    assert envs["dev"] == "opsaccord"
    assert envs["scenario"] == "opsaccord_scenarios"
    assert envs["test"] == envs["ci"] == "opsaccord_test"
    assert len(set(envs.values())) == 3


def test_schema_reset_refuses_every_non_test_database() -> None:
    for app_env in ("dev", "scenario"):
        url = Settings(app_env=app_env).state_database_url
        with pytest.raises(RuntimeError, match="refusing"):
            assert_dedicated_test_database(url)
        with pytest.raises(RuntimeError, match="refusing"):
            reset_public_schema(url)


def test_checkpoints_must_share_the_state_database() -> None:
    settings = Settings(app_env="test")
    assert_checkpoints_are_local(settings)
    split = Settings(
        app_env="test",
        graph_checkpoint_url="postgresql+psycopg://opsaccord@127.0.0.1:55432/opsaccord",
    )
    with pytest.raises(RuntimeError, match="checkpoint_url"):
        assert_checkpoints_are_local(split)


def test_a_second_concurrent_test_session_is_refused(settings: Settings, migrated: str) -> None:
    # ``migrated`` holds the session lock for this session, so a second session
    # must be refused rather than dropping the schema underneath this one.
    with (
        pytest.raises(RuntimeError, match="another test session"),
        dedicated_test_session(settings.test_database_url),
    ):
        pass


def test_session_reset_leaves_demo_records_intact(
    demo_sentinel: tuple[Database, str], settings: Settings, migrated: str
) -> None:
    """Run the suite's own reset and prove demo rows are untouched.

    The reset is executed exactly as the session fixture runs it, against the
    test database, and the schema is restored in the same test so the rest of
    the session is unaffected.
    """
    db, sentinel_id = demo_sentinel
    assert _sentinel_count(db, sentinel_id) == 1

    reset_public_schema(settings.test_database_url)
    _migrate(settings.test_database_url)
    reset_setup_cache()
    ensure_checkpoint_schema(settings.checkpoint_url)

    assert _sentinel_count(db, sentinel_id) == 1, "the test reset reached the demo database"

    sentinel_in_test_db = Database(settings.test_database_url)
    try:
        with sentinel_in_test_db.read_session() as session:
            assert (
                session.execute(
                    sa.text("SELECT count(*) FROM incidents WHERE id = :id"),
                    {"id": sentinel_id},
                ).scalar_one()
                == 0
            )
    finally:
        sentinel_in_test_db.dispose()
