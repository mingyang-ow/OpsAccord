#!/usr/bin/env bash
# Execute the Phase 1 acceptance scenarios against the project-local PostgreSQL.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
scripts/pg.sh start >/dev/null
uv run python -m opsaccord.cli init-db
uv run python -m opsaccord.cli run-scenarios "${@:-}" --json reports/local/scenarios.json
