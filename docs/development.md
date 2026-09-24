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

`scripts/prefix-sensitivity.sh` is separate from the suite: it runs a revision-portable harness
against both the pre-fix revision (`5cbcce2`) and the current tip, to show that the two safety
defects closed in `b508a0f` fail there and pass here. Its expected results, exit codes, and the
portability concessions it makes are documented in `scripts/prefix_sensitivity/README.md`.

## Live Astronomy Shop stack

The live paths run against the pinned upstream demo, not a mock. The checkout
lives outside this repository (for example
`/mnt/c/Users/admin/opsaccord-demo/opentelemetry-demo` at tag `3.1.0`); point
`OPSACCORD_DEMO_CHECKOUT` at it if it is somewhere else. All lifecycle commands
are scoped to this project's containers.

```
scripts/demo.sh pull        # pull the pinned 3.1.0 images
scripts/demo.sh start       # core + observability layers, with the local override
scripts/demo.sh health      # probe every published port and API
scripts/demo.sh endpoints   # resolve the ephemeral ports into .local/demo-endpoints.env
scripts/demo.sh status
scripts/demo.sh logs checkout
scripts/demo.sh stop
```

Only `frontend-proxy` (8080) and `prometheus` (9090) are stable host ports; every
other port is ephemeral, so always load the resolved endpoints before a live
command:

```
set -a; . .local/demo-endpoints.env; set +a
uv run python -m opsaccord.cli run-live-scenarios --json reports/local/live-scenarios.json
OPSACCORD_LIVE_DEMO=1 uv run pytest -q -m live_demo
```

`scripts/run_live_scenarios.sh` wraps the whole sequence. Live runs take tens of
minutes because they wait for real telemetry: see the calibrated windows in
`docs/astronomy-shop.md`. Live tests are opt-in and skipped unless
`OPSACCORD_LIVE_DEMO=1`, so the default suite stays offline and fast.

Tests are split by what they need. Unit tests are offline and never touch PostgreSQL; acceptance and integration tests are marked `integration` and require the local cluster (`scripts/pg.sh start`) or a CI service container.

```
uv run pytest -m "not integration"   # offline
uv run pytest -m integration         # requires PostgreSQL
uv run pytest                        # everything
uv run ruff check . && uv run ruff format --check . && uv run mypy src/opsaccord
```

`scripts/validate.sh` runs the whole sequence in one pass, with per-stage logs under
`reports/local/validation-<timestamp>/` and a fail-fast subshell: offline suite, the same suite with
the live settings exported, lint, formatting and types, then — unless `--offline-only` is passed —
the PostgreSQL integration suite, the pre-fix sensitivity harness and the nine simulated scenarios.
`--live` adds the seven live paths, and that is the only stage that loads
`.local/demo-endpoints.env`, inside a nested subshell so no earlier stage can inherit live mode. It
also refuses to start the live stage while another live scenario driver is running.

### Configuration isolation in tests

`Settings` reads `OPSACCORD_*` variables and the `.env` file beside the working directory, which is
the runtime override mechanism. A shell that still exports `.local/demo-endpoints.env` (`scripts/demo.sh
endpoints` writes `OPSACCORD_DEMO_MODE=live` into it) therefore changes what a *test* observes: a
default-value assertion fails while everything is working.

A test that asserts a default must request the `isolated_config` fixture. It clears the inherited
`OPSACCORD_*` variables and moves the working directory to an empty temporary one, for that test
only. It is never autouse, so override tests and the integration fixtures still see the real
environment and `OPSACCORD_TEST_DATABASE_URL` keeps working. `tests/unit/test_config_isolation.py`
pins both halves — the override mechanism still works (the process environment beats `.env`, a
temporary `.env` is read, and live mode never falls back to the simulator), and the default
assertions hold while live mode is exported. `scripts/validate.sh` also removes those variables for
the offline, integration and simulated stages; the list lives in `LIVE_SESSION_VARS` in the script
and in `tests/conftest.py`, and a test keeps it in step with what `scripts/demo.sh` writes.

## Independent review

External review is not a completion gate and no reviewer has to be registered before work starts. The DeepSeek-backed Codex agent is the primary development tool; the authenticated, subscription-backed Claude Code harness is authorized for bounded implementation help or an optional second review, and this authorization is about the development workflow only, never the application's runtime models. Record the tool and model that actually ran, and never present a fallback as if the requested reviewer had run.
