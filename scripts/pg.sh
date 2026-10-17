#!/usr/bin/env bash
# Project-scoped PostgreSQL 18 lifecycle for OpsAccord.
#
# No root, no system service, no Docker: official Ubuntu packages are extracted
# under .local/pg/root and run as the current user against a project data dir.
#
# Usage: scripts/pg.sh provision|init|start|stop|status|psql|reset|env|url|ensure
#
# `ensure` is the state-dependent one: start only if provably stopped, otherwise
# verify (running) or refuse (unknown, exit 4).
#
# Safe to source; only a direct execution dispatches. See tests/unit/test_pg_lifecycle.py.
#
# `status` exits 0 running, 3 stopped, 4 *unknown* - the last meaning this shell
# cannot establish whether a server owns $PG_DATA, which `start` and `stop` treat
# as a refusal rather than as "stopped". See the note above `pg_state`.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PG_HOME="${OPSACCORD_PG_HOME:-$REPO_ROOT/.local/pg}"
PG_ROOT="$PG_HOME/root"
PG_DATA="${OPSACCORD_PG_DATA:-$PG_HOME/data}"
PG_RUN="${OPSACCORD_PG_RUN:-$PG_HOME/run}"
PG_PORT="${OPSACCORD_PG_PORT:-55432}"
PG_USER="${OPSACCORD_PG_USER:-opsaccord}"
PG_DB="${OPSACCORD_PG_DB:-opsaccord}"
PG_TEST_DB="${OPSACCORD_PG_TEST_DB:-opsaccord_test}"
PG_SCENARIO_DB="${OPSACCORD_PG_SCENARIO_DB:-opsaccord_scenarios}"
PG_PKGS="postgresql-18 postgresql-client-18 postgresql-common libpq5 libnuma1 libicu78 liburing2"

export LD_LIBRARY_PATH="$PG_ROOT/usr/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
PGBIN="$PG_ROOT/usr/lib/postgresql/18/bin"

log() { printf '[pg] %s\n' "$*" >&2; }
die() { printf '[pg] error: %s\n' "$*" >&2; exit 1; }

# --- Cluster state, established from independent signals ---------------------
#
# `pg_ctl status` decides liveness by reading postmaster.pid and testing that PID
# with kill(0). That test is namespace-sensitive: a shell in a different PID
# namespace cannot see the owning process, so a *live* server reads as stopped.
# On 2026-09-26 that is what happened here - a shell with no socket capability
# read a live cluster as "not running", called `pg_ctl start`, and the postmaster
# it launched overwrote the live server's postmaster.pid. The live server then
# shut itself down: "performing immediate shutdown because data directory lock
# file is invalid".
#
# So liveness is decided from signals that survive a namespace boundary, and
# never from one failed probe:
#
#   1. the PID in postmaster.pid, if this shell can see that process at all;
#   2. whether anything answers on 127.0.0.1:$PG_PORT - a probe that separates
#      "refused" (definitively nothing listening) from "cannot probe" (this shell
#      has no socket capability, which is NOT evidence that a server is down).
#
# What the two cannot agree on is reported as `unknown`, and every lifecycle
# command refuses rather than guesses. No path here writes, deletes or renames a
# pid file, and a stale one is never removed on the operator's behalf.

# The only line here that opens a socket, kept separate so a test can replace the
# *attempt* and still exercise the classification below it: a shell that cannot open
# any socket cannot be made to produce a refused connection, so in such a shell the
# two outcomes this script's safety rests on are otherwise indistinguishable.
probe_connect() { exec 3<>/dev/tcp/127.0.0.1/"$PG_PORT"; }

# 0 = a connection was accepted, 1 = definitively refused, 2 = cannot probe.
port_reachable() {
  local err rc=0
  # No `set +e` here. Toggling errexit inside a function changes it for the whole
  # script, and restoring it before returning non-zero aborts the caller that is
  # reading this very status - which is how a stopped cluster turned into a silent
  # `pg.sh start` failure. `|| rc=$?` captures the status without touching the option.
  err="$(probe_connect 2>&1)" || rc=$?
  [ "$rc" -eq 0 ] && return 0
  # bash words a refused connect "Connection refused" and its other failures - no
  # socket capability, an unusable address - as something else. Only the first is
  # evidence that nothing is listening.
  case "${err,,}" in
    *"connection refused"*) return 1 ;;
  esac
  printf '%s' "${err:-the shell gave no reason}" >&2
  return 2
}

# running | stopped | unknown
pg_state() {
  local pidfile="$PG_DATA/postmaster.pid" pid="" visible=no rc=0
  if [ -s "$pidfile" ]; then
    pid="$(head -n 1 "$pidfile" 2>/dev/null || true)"
    case "$pid" in
      '' | *[!0-9]*) pid="" ;;
      *) kill -0 "$pid" 2>/dev/null && visible=yes ;;
    esac
  fi
  # A visible PID settles it, and the port is then left alone: a bare TCP connect
  # makes the postmaster log a failed startup packet, and postgres.log is read as
  # evidence here.
  if [ "$visible" = yes ]; then printf 'running\n'; return 0; fi
  port_reachable 2>/dev/null || rc=$?
  case "$rc" in
    0) printf 'running\n' ;;
    1) printf 'stopped\n' ;;
    *) printf 'unknown\n' ;;
  esac
}

pg_refuse_unknown() {
  local action="$1" status="${2:-1}"
  log "refusing to $action: $PG_DATA/postmaster.pid and the probe of 127.0.0.1:$PG_PORT do not agree with what this shell can see, so a server may be running and this shell cannot tell. A shell that cannot open a socket must not start or stop a cluster - run this from the host shell that owns it."
  exit "$status"
}

pg_env() {
  echo "export LD_LIBRARY_PATH=$PG_ROOT/usr/lib/x86_64-linux-gnu\${LD_LIBRARY_PATH:+:\$LD_LIBRARY_PATH}"
  echo "export PATH=$PGBIN:\$PATH"
}

cmd_env() { pg_env; }

cmd_provision() {
  [ -x "$PGBIN/postgres" ] && { log "already provisioned at $PG_ROOT"; return 0; }
  command -v apt-get >/dev/null || die "apt-get not available"
  local stage="$PG_HOME/stage"
  mkdir -p "$stage" "$PG_ROOT"
  ( cd "$stage" && apt-get download $PG_PKGS )
  for f in "$stage"/*.deb; do dpkg -x "$f" "$PG_ROOT"; done
  [ -x "$PGBIN/postgres" ] || die "provision failed: $PGBIN/postgres missing"
  log "provisioned $("$PGBIN/postgres" --version)"
}

cmd_init() {
  [ -x "$PGBIN/initdb" ] || die "run 'provision' first"
  if [ -s "$PG_DATA/PG_VERSION" ]; then log "cluster already initialized at $PG_DATA"; return 0; fi
  mkdir -p "$PG_DATA" "$PG_RUN"
  chmod 700 "$PG_DATA"
  "$PGBIN/initdb" -D "$PG_DATA" -U "$PG_USER" --auth-local=trust --auth-host=trust \
    --encoding=UTF8 --locale=C.UTF-8 >/dev/null
  # Project-scoped socket + loopback only; never exposed beyond this host.
  cat >> "$PG_DATA/postgresql.conf" <<CONF
listen_addresses = '127.0.0.1'
port = $PG_PORT
unix_socket_directories = '$PG_RUN'
fsync = off
synchronous_commit = off
full_page_writes = off
max_connections = 50
CONF
  log "initialized cluster at $PG_DATA (port $PG_PORT)"
}

# Three databases, one per environment, so a destructive schema reset in the
# pytest session can never reach scenario or demo records (see docs/development.md).
cmd_ensure_dbs() {
  for db in "$PG_DB" "$PG_SCENARIO_DB" "$PG_TEST_DB"; do
    "$PGBIN/psql" -h 127.0.0.1 -p "$PG_PORT" -U "$PG_USER" -d postgres -tAc \
      "SELECT 1 FROM pg_database WHERE datname='$db'" | grep -q 1 \
      || "$PGBIN/createdb" -h 127.0.0.1 -p "$PG_PORT" -U "$PG_USER" "$db"
  done
}

cmd_start() {
  [ -x "$PGBIN/pg_ctl" ] || die "run 'provision' first"
  local state
  state="$(pg_state)"
  case "$state" in
    running) log "already running"; cmd_ensure_dbs; return 0 ;;
    unknown) pg_refuse_unknown start ;;
  esac
  [ -s "$PG_DATA/PG_VERSION" ] || cmd_init

  # Serialised, because two starts against one data directory is the same hazard
  # as a start against a live one. `flock` releases on process exit, so a crashed
  # start cannot leave a lock behind to block the next one.
  command -v flock >/dev/null || die "flock is required to serialise concurrent starts"
  mkdir -p "$PG_HOME"
  exec 9>"$PG_HOME/start.lock"
  flock -n 9 || die "another 'pg.sh start' is already running for $PG_DATA"

  # Re-checked inside the lock: the attempt that held it may already have started
  # the very server this one was about to create.
  state="$(pg_state)"
  case "$state" in
    running) log "already running"; cmd_ensure_dbs; return 0 ;;
    unknown) pg_refuse_unknown start ;;
  esac

  mkdir -p "$PG_RUN"
  "$PGBIN/pg_ctl" -D "$PG_DATA" -l "$PG_HOME/postgres.log" -w -t 30 start
  "$PGBIN/psql" -h 127.0.0.1 -p "$PG_PORT" -U "$PG_USER" -d postgres -v ON_ERROR_STOP=1 -q \
    -c "SELECT 1" >/dev/null
  cmd_ensure_dbs
  log "running on 127.0.0.1:$PG_PORT (dbs: $PG_DB, $PG_SCENARIO_DB, $PG_TEST_DB)"
}

# Bring the cluster up only when this shell can *prove* it is stopped.
#
# The recovery the handoff asks for is exactly this question, and the three states
# need three different answers. `running` is verified rather than restarted;
# `stopped` is started once through `cmd_start`, which does its own checks and
# serialises with `flock`; anything else - `unknown` - is a refusal, because from a
# shell that cannot see the owner a start is the 2026-09-26 hazard and a stop would
# signal a PID it cannot verify. Exit 4 keeps `unknown` distinguishable from a
# failed start (1), the way `status` already does.
cmd_ensure() {
  local state
  state="$(pg_state)"
  case "$state" in
    running) log "already running"; cmd_ensure_dbs; return 0 ;;
    stopped) cmd_start ;;
    *) pg_refuse_unknown ensure 4 ;;
  esac
}

cmd_stop() {
  local state
  state="$(pg_state)"
  case "$state" in
    stopped) log "not running"; return 0 ;;
    unknown) pg_refuse_unknown stop ;;
  esac
  # `pg_ctl stop` signals the PID in $PG_DATA/postmaster.pid and nothing else, so
  # it cannot reach another cluster - but it is only sent once `pg_ctl` itself can
  # identify that process, so a host shell never signals an unrelated PID that
  # happens to sit in a stale pid file.
  "$PGBIN/pg_ctl" -D "$PG_DATA" status >/dev/null 2>&1 \
    || die "refusing to stop: something answers on 127.0.0.1:$PG_PORT but pg_ctl cannot identify a server in $PG_DATA. Stop it from the shell that started it."
  "$PGBIN/pg_ctl" -D "$PG_DATA" -m fast -w -t 30 stop
}

cmd_status() {
  local state
  state="$(pg_state)"
  case "$state" in
    running)
      log "running on 127.0.0.1:$PG_PORT, data=$PG_DATA"
      "$PGBIN/psql" -h 127.0.0.1 -p "$PG_PORT" -U "$PG_USER" -d "$PG_DB" -tAc 'select version()' ;;
    stopped)
      log "not running (data=$PG_DATA)"; return 3 ;;
    *)
      log "state unknown: cannot tell from this shell whether a server owns $PG_DATA"
      return 4 ;;
  esac
}

cmd_psql() { exec "$PGBIN/psql" -h 127.0.0.1 -p "$PG_PORT" -U "$PG_USER" -d "${1:-$PG_DB}"; }

cmd_reset() {
  # A reset deletes the data directory, so it may only run against a cluster this
  # shell can prove is stopped. `cmd_stop || true` used to reach `rm -rf` even when
  # the stop had refused or failed, which is the same "live server I cannot see"
  # hazard in a more destructive form.
  cmd_stop
  [ "$(pg_state)" = stopped ] || die "refusing to reset: the cluster is not stopped"
  rm -rf "$PG_DATA"
  cmd_init
  cmd_start
}

cmd_url() { echo "postgresql+psycopg://$PG_USER@127.0.0.1:$PG_PORT/${1:-$PG_DB}"; }

pg_main() {
  case "${1:-}" in
    provision|init|start|stop|status|psql|reset|env|ensure) "cmd_$1" "${@:2}" ;;
    ensure-dbs) cmd_ensure_dbs ;;
    url) case "${2:-dev}" in dev) cmd_url "$PG_DB" ;; test) cmd_url "$PG_TEST_DB" ;; scenario) cmd_url "$PG_SCENARIO_DB" ;; *) cmd_url "$2" ;; esac ;;
    *) die "usage: $0 {provision|init|start|stop|status|psql|reset|env|url|ensure} [args]" ;;
  esac
}

# Dispatch only when executed. Sourcing must not run a command: the lifecycle tests
# source this file to replace `probe_connect` and then call the real functions.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  pg_main "$@"
fi
