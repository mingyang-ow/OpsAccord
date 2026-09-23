#!/usr/bin/env bash
# Execute the Phase 1 paths against the real pinned Astronomy Shop stack.
#
# Requires Docker Desktop with WSL integration (the compose project is the
# pinned upstream checkout) and the project-local PostgreSQL cluster. The demo
# stack is left running; stop it with scripts/demo.sh stop.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

scripts/pg.sh start >/dev/null
uv run python -m opsaccord.cli init-db --all

if ! scripts/demo.sh health >/dev/null 2>&1; then
  scripts/demo.sh start
fi
scripts/demo.sh endpoints

set -a
# shellcheck disable=SC1091
. .local/demo-endpoints.env
set +a

uv run python -m opsaccord.cli run-live-scenarios "$@" --json reports/local/live-scenarios.json
