#!/usr/bin/env bash
# Pre-fix sensitivity check for the two safety defects closed in b508a0f.
#
# The handoff asks for evidence that the regression tests fail because of the
# original defects and pass with the fixes. Running the repository's own
# tests/integration/test_dispatch_safety.py against 5cbcce2 would only prove that
# a module added by the fix does not exist there, so this script runs a portable
# harness (scripts/prefix_sensitivity/) against *both* revisions instead.
#
# Nothing in the shared checkout is modified: each revision is materialised read
# -only with `git archive` into its own scratch directory, and the harness is
# copied in there. The pre-fix revision never touches the working tree, the
# current branch, or demo databases - only the dedicated *_test database, which
# the pytest session rebuilds.
#
# Usage: scripts/prefix-sensitivity.sh
#   PREFIX_REF=5cbcce2   revision whose defects are being demonstrated
#   FIXED_REF=HEAD       revision that must pass
#   SCRATCH=/tmp/...     where the two revisions are materialised
#   PYTHON=/path/python  interpreter that has the project dependencies
#
# Expects a running PostgreSQL (scripts/pg.sh start) and the dedicated test
# database. Evidence is written to reports/local/ (gitignored).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PREFIX_REF="${PREFIX_REF:-5cbcce2}"
FIXED_REF="${FIXED_REF:-HEAD}"
SCRATCH="${SCRATCH:-/tmp/opsaccord-prefix-sensitivity}"
PYTHON="${PYTHON:-$REPO_ROOT/.venv/bin/python}"
EVIDENCE_DIR="$REPO_ROOT/reports/local"
HARNESS_SRC="$REPO_ROOT/scripts/prefix_sensitivity/test_prefix_defect_sensitivity.py"
TEST_DATABASE_URL="${OPSACCORD_TEST_DATABASE_URL:-postgresql+psycopg://opsaccord@127.0.0.1:55432/opsaccord_test}"

[ -x "$PYTHON" ] || { echo "[prefix] error: no interpreter at $PYTHON (set PYTHON=...)" >&2; exit 1; }
mkdir -p "$EVIDENCE_DIR" "$SCRATCH"

materialise() { # $1 = ref, $2 = destination
  local ref="$1" destination="$2"
  mkdir -p "$destination"
  git -C "$REPO_ROOT" archive "$ref" | tar -x -C "$destination"
  mkdir -p "$destination/tests/prefix_sensitivity"
  cp "$HARNESS_SRC" "$destination/tests/prefix_sensitivity/"
  git -C "$REPO_ROOT" rev-parse "$ref" > "$destination/.reviewed-revision"
}

run_harness() { # $1 = revision directory, $2 = log file; echoes the exit status
  local directory="$1" log="$2"
  (
    cd "$directory"
    OPSACCORD_TEST_DATABASE_URL="$TEST_DATABASE_URL" \
      PYTHONPATH="$directory/src:$directory/tests" \
      UV_CACHE_DIR="${UV_CACHE_DIR:-/tmp/uvcache}" \
      "$PYTHON" -m pytest tests/prefix_sensitivity -q --tb=line -rf -p no:cacheprovider
  ) > "$log" 2>&1 && return 0 || return 1
}

expectation_met() { # $1 = log, $2 = "fail-prefix" | "pass"
  local log="$1" kind="$2"
  if [ "$kind" = "pass" ]; then
    grep -qE '^3 passed' "$log"
  else
    grep -qE '^1 failed|, 1 failed|2 failed' "$log" &&
      grep -q 'test_a_global_freeze_cannot_commit_inside_the_authorization_window' "$log" &&
      grep -q 'test_an_error_after_the_change_landed_stays_uncertain' "$log"
  fi
}

echo "[prefix] materialising $PREFIX_REF and $FIXED_REF under $SCRATCH"
materialise "$PREFIX_REF" "$SCRATCH/prefix"
materialise "$FIXED_REF" "$SCRATCH/fixed"

prefix_log="$EVIDENCE_DIR/prefix-sensitivity-$PREFIX_REF.log"
fixed_log="$EVIDENCE_DIR/prefix-sensitivity-fixed.log"

echo "[prefix] running the harness against $PREFIX_REF (expected: the two defect tests fail)"
prefix_status=0
run_harness "$SCRATCH/prefix" "$prefix_log" || prefix_status=$?
tail -n 8 "$prefix_log" | sed 's/^/[prefix]   /'

echo "[prefix] running the same harness against $FIXED_REF (expected: all pass)"
fixed_status=0
run_harness "$SCRATCH/fixed" "$fixed_log" || fixed_status=$?
tail -n 4 "$fixed_log" | sed 's/^/[prefix]   /'

problems=()
[ "$prefix_status" -ne 0 ] || problems+=("the pre-fix revision passed: the harness does not reproduce the defects")
expectation_met "$prefix_log" fail-prefix || problems+=("the pre-fix failures are not the two expected defect tests")
[ "$fixed_status" -eq 0 ] || problems+=("the fixed revision failed the harness")
expectation_met "$fixed_log" pass || problems+=("the fixed run did not report 3 passed")

echo "[prefix] evidence: $prefix_log"
echo "[prefix] evidence: $fixed_log"

# A run that could not reach PostgreSQL is not a result about either revision.
if grep -q 'OperationalError\|connection is bad' "$prefix_log" "$fixed_log"; then
  echo "[prefix] cannot run: no PostgreSQL at $TEST_DATABASE_URL" >&2
  echo "[prefix] start it with scripts/pg.sh start (and scripts/pg.sh status), then re-run" >&2
  exit 2
fi
if [ "${#problems[@]}" -ne 0 ]; then
  for problem in "${problems[@]}"; do echo "[prefix] UNEXPECTED: $problem" >&2; done
  exit 1
fi
echo "[prefix] as expected: $PREFIX_REF fails the two defect tests, $FIXED_REF passes all three"
