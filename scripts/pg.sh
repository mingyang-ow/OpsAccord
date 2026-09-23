#!/usr/bin/env bash
# Project-scoped PostgreSQL 18 lifecycle for OpsAccord.
#
# No root, no system service, no Docker: official Ubuntu packages are extracted
# under .local/pg/root and run as the current user against a project data dir.
#
# Usage: scripts/pg.sh provision|init|start|stop|status|psql|reset|env
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
PG_PKGS="postgresql-18 postgresql-client-18 postgresql-common libpq5 libnuma1 libicu78 liburing2"

export LD_LIBRARY_PATH="$PG_ROOT/usr/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
PGBIN="$PG_ROOT/usr/lib/postgresql/18/bin"

log() { printf '[pg] %s\n' "$*" >&2; }
die() { printf '[pg] error: %s\n' "$*" >&2; exit 1; }

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

cmd_start() {
  [ -x "$PGBIN/pg_ctl" ] || die "run 'provision' first"
  [ -s "$PG_DATA/PG_VERSION" ] || cmd_init
  if "$PGBIN/pg_ctl" -D "$PG_DATA" status >/dev/null 2>&1; then log "already running"; return 0; fi
  mkdir -p "$PG_RUN"
  "$PGBIN/pg_ctl" -D "$PG_DATA" -l "$PG_HOME/postgres.log" -w -t 30 start
  "$PGBIN/psql" -h 127.0.0.1 -p "$PG_PORT" -U "$PG_USER" -d postgres -v ON_ERROR_STOP=1 -q \
    -c "SELECT 1" >/dev/null
  for db in "$PG_DB" "$PG_TEST_DB"; do
    "$PGBIN/psql" -h 127.0.0.1 -p "$PG_PORT" -U "$PG_USER" -d postgres -tAc \
      "SELECT 1 FROM pg_database WHERE datname='$db'" | grep -q 1 \
      || "$PGBIN/createdb" -h 127.0.0.1 -p "$PG_PORT" -U "$PG_USER" "$db"
  done
  log "running on 127.0.0.1:$PG_PORT (db: $PG_DB, test db: $PG_TEST_DB)"
}

cmd_stop() {
  "$PGBIN/pg_ctl" -D "$PG_DATA" status >/dev/null 2>&1 || { log "not running"; return 0; }
  "$PGBIN/pg_ctl" -D "$PG_DATA" -m fast -w -t 30 stop
}

cmd_status() {
  if "$PGBIN/pg_ctl" -D "$PG_DATA" status >/dev/null 2>&1; then
    log "running on 127.0.0.1:$PG_PORT, data=$PG_DATA"
    "$PGBIN/psql" -h 127.0.0.1 -p "$PG_PORT" -U "$PG_USER" -d "$PG_DB" -tAc 'select version()'
  else
    log "not running (data=$PG_DATA)"; return 3
  fi
}

cmd_psql() { exec "$PGBIN/psql" -h 127.0.0.1 -p "$PG_PORT" -U "$PG_USER" -d "${1:-$PG_DB}"; }

cmd_reset() { cmd_stop || true; rm -rf "$PG_DATA"; cmd_init; cmd_start; }

cmd_url() { echo "postgresql+psycopg://$PG_USER@127.0.0.1:$PG_PORT/${1:-$PG_DB}"; }

case "${1:-}" in
  provision|init|start|stop|status|psql|reset|env) "cmd_$1" "${@:2}" ;;
  url) cmd_url "${2:-}" ;;
  *) die "usage: $0 {provision|init|start|stop|status|psql|reset|env|url} [args]" ;;
esac
