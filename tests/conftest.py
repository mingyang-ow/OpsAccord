"""Shared fixtures.

Offline unit tests never touch PostgreSQL. Acceptance and integration tests are
marked ``integration`` and require a real PostgreSQL server: use the
project-local cluster (``scripts/pg.sh start``) or a CI service container.

Every integration test truncates the schema first, so each test observes only
the rows it created. Nothing here substitutes in-memory state for PostgreSQL:
the persistence and recovery evidence is produced against the real engine.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator

import pytest
import sqlalchemy as sa

from opsaccord.config import REPO_ROOT, Settings
from opsaccord.persistence.db import Database
from opsaccord.reasoning.deterministic import DeterministicAdapter
from opsaccord.tools import demo_environment as demo
from opsaccord.tools.gateway import ToolGateway
from opsaccord.workflow.checkpoints import ensure_checkpoint_schema
from opsaccord.workflow.controller import WorkflowController

DEFAULT_TEST_DATABASE_URL = "postgresql+psycopg://opsaccord@127.0.0.1:55432/opsaccord_test"


@pytest.fixture(scope="session")
def settings() -> Settings:
    return Settings(
        app_env="test",
        test_database_url=os.environ.get("OPSACCORD_TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL),
    )


@pytest.fixture(scope="session")
def migrated(settings: Settings) -> str:
    """Rebuild the dedicated test schema, then migrate it once per session.

    Integration evidence has to start from a schema the migrations actually
    produced, so an earlier failed run cannot leave the tables present with an
    empty ``alembic_version``. The reset is refused unless the URL names the
    dedicated ``*_test`` database.
    """
    url = settings.test_database_url
    if not url.rsplit("/", 1)[-1].split("?")[0].endswith("_test"):
        raise RuntimeError(
            f"refusing to reset {url!r}: the test suite only resets a *_test database"
        )
    engine = sa.create_engine(url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as connection:
            connection.execute(sa.text("DROP SCHEMA IF EXISTS public CASCADE"))
            connection.execute(sa.text("CREATE SCHEMA public"))
    finally:
        engine.dispose()

    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env={**os.environ, "OPSACCORD_DATABASE_URL": settings.test_database_url},
    )
    if result.returncode != 0:
        raise RuntimeError(
            "alembic upgrade head failed for the test database\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    ensure_checkpoint_schema(settings.checkpoint_url)
    return settings.test_database_url


@pytest.fixture
def db(settings: Settings, migrated: str) -> Iterator[Database]:
    database = Database(settings.test_database_url)
    try:
        _truncate_all(database)
        yield database
    finally:
        database.dispose()


def _truncate_all(database: Database) -> None:
    # Migration-seeded reference data is not test state: truncating it would
    # remove the service registry the controller validates incidents against.
    preserved = {"alembic_version", "services"}
    with database.engine.begin() as connection:
        names = sa.inspect(connection).get_table_names(schema="public")
        names = [name for name in names if name not in preserved]
        if names:
            targets = ", ".join(f'"{name}"' for name in names)
            connection.execute(sa.text(f"TRUNCATE {targets} RESTART IDENTITY CASCADE"))


@pytest.fixture
def environment() -> demo.DemoEnvironment:
    return demo.build_demo_environment()


@pytest.fixture
def gateway(db: Database, environment: demo.DemoEnvironment, settings: Settings) -> ToolGateway:
    return ToolGateway(db=db, environment=environment, settings=settings)


@pytest.fixture
def controller(db: Database, gateway: ToolGateway, settings: Settings) -> WorkflowController:
    return WorkflowController(
        db=db, gateway=gateway, provider=DeterministicAdapter(), settings=settings
    )
