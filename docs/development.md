# WSL development

Use a checkout on the Linux filesystem, for example under ~/projects/OpsAccord, rather than under /mnt/c. Reuse an existing checkout only after checking its remote and uncommitted work. Keep credentials in the user's existing local authentication and environment, never repository files.

Phase 1 targets Python 3.12 or later and uses uv with a committed `uv.lock`. PostgreSQL is provisioned project-locally; there is no Docker dependency and no system service.

## Project-local PostgreSQL

`scripts/pg.sh` extracts the official Ubuntu PostgreSQL packages under `.local/pg/root` and runs a cluster as the current user, so no root access and no system service are required.

```
scripts/pg.sh provision   # one-off: download and unpack the packages
scripts/pg.sh init        # one-off: initdb with a project-scoped socket dir
scripts/pg.sh start       # start on 127.0.0.1:55432, create opsaccord and opsaccord_test
scripts/pg.sh status
scripts/pg.sh stop
scripts/pg.sh reset       # drop the cluster and rebuild it
scripts/pg.sh url [db]
```

`.local/` is gitignored and holds the cluster, the socket directory, and logs. The default URLs are `postgresql+psycopg://opsaccord@127.0.0.1:55432/opsaccord` and `.../opsaccord_test`; override them with `OPSACCORD_DATABASE_URL` and `OPSACCORD_TEST_DATABASE_URL`.

## Migrations, API, UI, and tests

```
uv sync --all-groups
scripts/pg.sh start
uv run python -m opsaccord.cli init-db        # Alembic upgrade plus LangGraph checkpoint tables
uv run python -m opsaccord.cli serve-api      # http://127.0.0.1:8410
uv run python -m opsaccord.cli serve-ui       # http://127.0.0.1:8511
scripts/run_scenarios.sh                      # all P1 scenarios, JSON evidence in reports/local/
```

Tests are split by what they need. Unit tests are offline and never touch PostgreSQL; acceptance and integration tests are marked `integration` and require the local cluster (`scripts/pg.sh start`) or a CI service container.

```
uv run pytest -m "not integration"   # offline
uv run pytest -m integration         # requires PostgreSQL
uv run pytest                        # everything
uv run ruff check . && uv run ruff format --check . && uv run mypy src/opsaccord
```

## Independent review

External review is not a completion gate and no reviewer has to be registered before work starts. The DeepSeek-backed Codex agent is the primary development tool; the authenticated, subscription-backed Claude Code harness is authorized for bounded implementation help or an optional second review, and this authorization is about the development workflow only, never the application's runtime models. Record the tool and model that actually ran, and never present a fallback as if the requested reviewer had run.
