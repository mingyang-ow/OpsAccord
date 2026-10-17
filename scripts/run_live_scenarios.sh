#!/usr/bin/env bash
# Execute the Phase 1 paths against the real pinned Astronomy Shop stack.
#
# Requires Docker Desktop with WSL integration (the compose project is the
# pinned upstream checkout) and the project-local PostgreSQL cluster. The demo
# stack is left running; stop it with scripts/demo.sh stop.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

# A second driver would share the demo's fault flags with this one: it would arm a
# fault this run then restores, or restore one this run is measuring. Refused up
# front and again immediately before the driver, because the setup in between takes
# minutes. Same check as scripts/validate.sh, for the same reason; a bare
# `uv run python -m opsaccord.cli run-live-scenarios` bypasses both.
assert_no_competing_driver() {
  if pgrep -f 'opsaccord\.cli run-live-scenarios' >/dev/null 2>&1; then
    printf 'refusing to run: another live scenario driver is active:\n' >&2
    pgrep -af 'opsaccord\.cli run-live-scenarios' >&2
    exit 3
  fi
}
assert_no_competing_driver

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

assert_no_competing_driver

# Progress is unbuffered: the driver prints one flushed line per event, and a
# buffered stream would hide it until the process exits.
PYTHONUNBUFFERED=1 uv run python -u -m opsaccord.cli run-live-scenarios "$@" --json reports/local/live-scenarios.json
