#!/usr/bin/env bash
# Validate OpsAccord end to end, in the order the handoff asks for.
#
#   scripts/validate.sh                 offline, lint, types, integration, sensitivity, simulated
#   scripts/validate.sh --live          the above, then the seven live paths
#   scripts/validate.sh --live --scenario P1-01
#                                       the above, but only the named live path(s); repeatable
#   scripts/validate.sh --offline-only  offline, lint, types only (no PostgreSQL needed)
#   scripts/validate.sh --live --model ID --allow-paid-calls --budget-usd N \
#       --input-cost-per-1k X --output-cost-per-1k Y [--max-calls N] [--max-output-tokens N]
#                                       the live paths with a hosted model under a hard budget;
#                                       these flags reach the live stage only, never the
#                                       offline, integration or API stages
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
# * Visible live progress. The live driver writes one flushed, timestamped line per
#   event and runs under `python -u`, so a run piped through `tee` reports what it
#   is doing while it does it instead of appearing to hang.
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
SCENARIO_IDS=()
# Hosted-model flags for the live stage. They are passed as arguments, never exported:
# the API acceptance stage builds its own provider from OPSACCORD_MODEL_* settings, and
# a provider exported for the live run would reach it there.
MODEL_ARGS=()
usage() { printf 'usage: scripts/validate.sh [--live|--offline-only] [--scenario ID]... [model flags with --live]\n' >&2; exit 2; }

while [ $# -gt 0 ]; do
  case "$1" in
    --live ) MODE="live" ;;
    --offline-only ) MODE="offline" ;;
    --scenario )
      shift
      [ $# -gt 0 ] || usage
      SCENARIO_IDS+=("$1")
      ;;
    --allow-paid-calls ) MODEL_ARGS+=("$1") ;;
    --model | --budget-usd | --max-calls | --input-cost-per-1k | --output-cost-per-1k | --max-output-tokens )
      [ $# -gt 1 ] || usage
      MODEL_ARGS+=("$1" "$2")
      shift
      ;;
    -h | --help ) sed -n '2,34p' "$0"; exit 0 ;;
    * ) usage ;;
  esac
  shift
done

# A scenario selection only means something for the live stage; refusing here is
# better than silently running all seven paths.
if [ "${#SCENARIO_IDS[@]}" -gt 0 ] && [ "$MODE" != "live" ]; then
  printf 'refusing to run: --scenario needs --live\n' >&2
  usage
fi
if [ "${#MODEL_ARGS[@]}" -gt 0 ] && [ "$MODE" != "live" ]; then
  printf 'refusing to run: hosted-model flags need --live\n' >&2
  usage
fi

TS="$(date -u +%Y%m%dT%H%M%S)"
LOGS="reports/local/validation-$TS"
mkdir -p "$LOGS"
# Printed before the first stage, not only at the end: a stage that aborts the run
# must still leave the operator knowing which directory holds the logs.
printf 'logs: %s\n' "$LOGS" | tee -a "$LOGS/summary.log"

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
    # `Settings` matches environment names case-insensitively (pydantic-settings
    # defaults to case_sensitive=False), so `opsaccord_demo_mode=live` selects
    # live mode exactly like the upper-case spelling. `env -u` cannot match
    # case-insensitively, so both spellings are removed. A mixed-case spelling of
    # the same name (`Opsaccord_Demo_Mode`) is the recorded residual; nothing in
    # this repository writes one, and the test fixture compares case-insensitively
    # so the *suite* is not exposed by it.
    args+=(-u "$name" -u "${name,,}")
  done
  env "${args[@]}" "$@"
}

# A competing driver shares the demo stack's feature flags with this run, so the
# live stage refuses to start rather than warning and continuing. Called twice:
# once as a preflight, and once immediately before the driver, because the demo
# stack and endpoint stages between them take minutes.
assert_no_competing_driver() {
  if pgrep -f 'opsaccord\.cli run-live-scenarios' >/dev/null 2>&1; then
    printf 'refusing to run: another live scenario driver is active:\n' >&2
    pgrep -af 'opsaccord\.cli run-live-scenarios' >&2
    exit 3
  fi
  printf 'no competing live scenario driver\n' | tee -a "$LOGS/summary.log"
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
  env -u OPSACCORD_LIVE_DEMO -u opsaccord_live_demo OPSACCORD_DEMO_MODE=live \
  uv run pytest -q -m "not integration"

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
  assert_no_competing_driver

  run 11-demo-stack bash -c 'scripts/demo.sh health >/dev/null 2>&1 || scripts/demo.sh start'
  run 12-demo-endpoints scripts/demo.sh endpoints

  # Re-checked here, not only in the preflight: stages 11 and 12 take minutes, and
  # a driver started during them would otherwise share the demo fault flags.
  say "12b-live-driver-guard"
  assert_no_competing_driver

  # The endpoint file is loaded here and nowhere else, so no earlier stage can
  # inherit live mode from it.
  if [ "${#SCENARIO_IDS[@]}" -gt 0 ]; then
    LIVE_SELECTION="focused run: ${SCENARIO_IDS[*]}"
  else
    LIVE_SELECTION="seven paths against the pinned stack"
  fi
  say "13-live-paths ($LIVE_SELECTION)"
  SCENARIO_ARGS=()
  for scenario_id in ${SCENARIO_IDS[@]+"${SCENARIO_IDS[@]}"}; do
    SCENARIO_ARGS+=(--scenario "$scenario_id")
  done
  # `-u` and PYTHONUNBUFFERED keep the driver's progress unbuffered through `tee`:
  # buffered output makes a seven-minute live run look like a hung command.
  (
    set -a
    # shellcheck disable=SC1091
    . .local/demo-endpoints.env
    set +a
    PYTHONUNBUFFERED=1 uv run python -u -m opsaccord.cli run-live-scenarios \
      ${SCENARIO_ARGS[@]+"${SCENARIO_ARGS[@]}"} ${MODEL_ARGS[@]+"${MODEL_ARGS[@]}"} \
      --json "reports/local/live-scenarios-$TS.json"
  ) 2>&1 | tee "$LOGS/13-live-paths.log"
fi

say "done"
printf 'logs: %s\n' "$LOGS" | tee -a "$LOGS/summary.log"
