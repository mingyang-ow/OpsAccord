#!/usr/bin/env bash
# Validate OpsAccord end to end, in the order the handoff asks for.
#
#   scripts/validate.sh                 offline, lint, types, integration, sensitivity, simulated
#   scripts/validate.sh --live          the above, then the seven live paths
#   scripts/validate.sh --offline-only  offline, lint, types only (no PostgreSQL needed)
#
# Properties this script is responsible for:
#
# * Fail-fast. Every stage runs in a subshell under `set -euo pipefail`, so a
#   failure stops the run and a failing stage is never hidden by the pipe into
#   `tee` (`set -o pipefail` is what keeps that honest).
# * Explicit environment. `uv run` selects this project's environment regardless
#   of which interpreter, venv or `pytest` happens to be active, and the script
#   always runs from the repository root.
# * Evidence. Every stage writes `reports/local/validation-<timestamp>/<stage>.log`
#   and appends to `summary.log`.
# * Scoped configuration. See `LIVE_SESSION_VARS` below: the offline, integration
#   and simulated stages run without the demo-mode and endpoint settings a live
#   session exports, and the live stage is the only one that loads
#   `.local/demo-endpoints.env`, inside a nested subshell.
#
# The pre-fix sensitivity harness represents its expected negative tests with an
# overall successful exit (0); that is handled by running it as a normal stage.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

MODE="full"
case "${1:-}" in
  "" ) MODE="full" ;;
  --live ) MODE="live" ;;
  --offline-only ) MODE="offline" ;;
  -h | --help ) sed -n '2,18p' "$0"; exit 0 ;;
  * ) printf 'usage: scripts/validate.sh [--live|--offline-only]\n' >&2; exit 2 ;;
esac

TS="$(date -u +%Y%m%dT%H%M%S)"
LOGS="reports/local/validation-$TS"
mkdir -p "$LOGS"

say() { printf '\n=== %s ===\n' "$1" | tee -a "$LOGS/summary.log"; }
run() {
  local name="$1"
  shift
  say "$name"
  "$@" 2>&1 | tee "$LOGS/$name.log"
}

# Settings a live session leaves in the shell. `scripts/demo.sh endpoints` writes
# them to .local/demo-endpoints.env and the documented live command exports them
# with `set -a; . .local/demo-endpoints.env`, so they outlive the run that made
# them. Only these are removed, and only for the stages that must not be live:
#
# * OPSACCORD_DEMO_MODE / the three endpoint URLs select the demo adapter;
# * OPSACCORD_LIVE_DEMO is the opt-in switch for tests/integration/test_live_demo.py,
#   which is *deliberately* not marked `integration` -- unset here it cannot turn
#   the offline stage into a live run against the real stack.
#
# Deliberately kept: OPSACCORD_TEST_DATABASE_URL and OPSACCORD_DATABASE_URL
# (connectivity the operator configured), the model and budget settings, and
# anything else the user exported on purpose. Runtime defaults are not modified.
LIVE_SESSION_VARS=(
  OPSACCORD_DEMO_MODE
  OPSACCORD_PROMETHEUS_BASE_URL
  OPSACCORD_JAEGER_BASE_URL
  OPSACCORD_FLAG_CONTROL_BASE_URL
  OPSACCORD_LIVE_DEMO
)

without_live_session() {
  local args=() name
  for name in "${LIVE_SESSION_VARS[@]}"; do
    args+=(-u "$name")
  done
  env "${args[@]}" "$@"
}

say "00-checkout (revision under test)"
git log -1 --format='%h %s' | tee -a "$LOGS/summary.log"
git status --short | tee -a "$LOGS/summary.log"

if [ "$MODE" != "offline" ]; then
  run 01-database scripts/pg.sh start
fi

run 02-offline-suite without_live_session uv run pytest -q -m "not integration"

# Same suite with the live settings force-exported, which is the state that broke
# it once. If this stage ever diverges from 02, the suite has picked up a
# dependency on the operator's shell.
run 03-offline-suite-with-live-settings-without-the-opt-in \
  env -u OPSACCORD_LIVE_DEMO OPSACCORD_DEMO_MODE=live uv run pytest -q -m "not integration"

run 04-lint uv run ruff check .
run 05-format uv run ruff format --check .
run 06-types uv run mypy src/opsaccord

if [ "$MODE" = "full" ] || [ "$MODE" = "live" ]; then
  run 07-integration-suite without_live_session uv run pytest -q -m integration
  run 08-sensitivity-harness scripts/prefix-sensitivity.sh
  run 09-simulated-scenarios without_live_session uv run python -m opsaccord.cli run-scenarios --json "reports/local/scenarios-$TS.json"
fi

if [ "$MODE" = "live" ]; then
  say "10-live-preflight"
  # A competing driver would share the demo stack's feature flags with this run,
  # so this stops rather than continuing with a warning.
  if pgrep -f 'opsaccord.cli run-live-scenarios' >/dev/null 2>&1; then
    printf 'refusing to run: another live scenario driver is active:\n' >&2
    pgrep -af 'opsaccord.cli run-live-scenarios' >&2
    exit 3
  fi
  printf 'no competing live scenario driver\n' | tee -a "$LOGS/summary.log"

  run 11-demo-stack bash -c 'scripts/demo.sh health >/dev/null 2>&1 || scripts/demo.sh start'
  run 12-demo-endpoints scripts/demo.sh endpoints

  # The endpoint file is loaded here and nowhere else, so no earlier stage can
  # inherit live mode from it.
  say "13-live-paths (seven paths against the pinned stack)"
  (
    set -a
    # shellcheck disable=SC1091
    . .local/demo-endpoints.env
    set +a
    uv run python -m opsaccord.cli run-live-scenarios --json "reports/local/live-scenarios-$TS.json"
  ) 2>&1 | tee "$LOGS/13-live-paths.log"
fi

say "done"
printf 'logs: %s\n' "$LOGS" | tee -a "$LOGS/summary.log"
