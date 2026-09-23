#!/usr/bin/env bash
# Pinned OpenTelemetry Astronomy Shop stack: start, stop, and health checks.
#
# The demo runs through Docker Desktop's Windows engine (the `docker.exe` WSL
# interop binary). Three details of that arrangement are load-bearing, and they
# are why this script exists instead of a documented one-liner:
#
#   1. Compose resolves each service's *build context* relative to the compose
#      file, so the stack must be launched from a checkout that lives on the
#      Windows filesystem. Bind mounts behave the same way: a Windows path works,
#      a /mnt/c path arrives empty.
#   2. Upstream sets a fixed `container_name:` on several services, so container
#      names are global. Only one project's stack can exist at a time; this
#      script always uses one project name and touches nothing else.
#   3. Only two host ports are fixed by the pinned revision -- the frontend proxy
#      (8080) and Prometheus (9090). Every other `ports:` entry is a bare
#      container port, so Docker assigns an ephemeral host port at runtime.
#      Nothing may hard-code those; they are resolved here and published as
#      OPSACCORD_* settings by `scripts/demo.sh endpoints`.
#
# Everything here is scoped to this project's own stack: no image is removed, no
# unrelated container is stopped, and no service outside the pinned revision is
# started.
set -euo pipefail

PROJECT="${OPSACCORD_DEMO_PROJECT:-opsaccord-demo}"
DEMO_DIR="${OPSACCORD_DEMO_DIR:-/mnt/c/Users/admin/opsaccord-demo/opentelemetry-demo}"
DOCKER_BIN="${OPSACCORD_DOCKER_BIN:-/mnt/c/Users/admin/AppData/Local/Programs/DockerDesktop/resources/bin/docker.exe}"
REVISION="${OPSACCORD_DEMO_REVISION:-3.1.0}"
ENDPOINT_FILE="${OPSACCORD_DEMO_ENDPOINT_FILE:-.local/demo-endpoints.env}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OVERRIDE_SRC="$REPO_ROOT/demo/compose.opsaccord.yaml"
OVERRIDE_DST="$DEMO_DIR/compose.opsaccord.yaml"

COMPOSE_FILES=(-f compose.yaml -f compose.observability.yaml -f compose.opsaccord.yaml)

# Logical endpoint -> "service container_port". These are the published ports the
# adapter and the operator console actually need, named after the pinned
# revision's own port variables.
ENDPOINTS=(
  "FRONTEND_PROXY frontend-proxy 8080"
  "PROMETHEUS prometheus 9090"
  "JAEGER_UI jaeger 16686"
  "FLAGD_UI flagd-ui 4000"
  "FLAGD_OFREP flagd 8016"
  "FLAGD_GRPC flagd 8013"
  "OTEL_HTTP otel-collector 4318"
  "OTEL_GRPC otel-collector 4317"
  "GRAFANA grafana 3000"
  "AD ad 9555"
  "CART cart 7070"
  "CHECKOUT checkout 5050"
  "PAYMENT payment 50051"
)

die() { printf 'demo: %s\n' "$*" >&2; exit 1; }

require_docker() {
  [ -x "$DOCKER_BIN" ] || die "docker binary not found at $DOCKER_BIN (set OPSACCORD_DOCKER_BIN)"
  "$DOCKER_BIN" version --format '{{.Server.Version}}' >/dev/null 2>&1 \
    || die "the Docker engine did not answer; start Docker Desktop first"
}

require_checkout() {
  [ -f "$DEMO_DIR/compose.yaml" ] || die "no compose.yaml under $DEMO_DIR (set OPSACCORD_DEMO_DIR)"
  [ -f "$DEMO_DIR/.env" ] || die "no .env under $DEMO_DIR"
}

# The repo is the source of truth for the local resource override; the demo
# checkout is only a deployment target, so the file is copied in before compose
# reads it. Everything else in the checkout stays exactly at the pinned revision.
sync_override() {
  require_checkout
  cp "$OVERRIDE_SRC" "$OVERRIDE_DST"
}

compose() {
  sync_override
  ( cd "$DEMO_DIR" && "$DOCKER_BIN" compose -p "$PROJECT" --env-file .env "${COMPOSE_FILES[@]}" "$@" )
}

# The host port Docker assigned to one published container port. Empty when the
# container is not running, which the callers treat as "not up yet".
published_port() {
  local service="$1" container_port="$2" mapping
  mapping="$(compose port "$service" "$container_port" 2>/dev/null | head -1)" || true
  printf '%s' "${mapping##*:}"
}

# The address the *Linux* side must use. Docker Desktop publishes on the Windows
# host, and WSL normally forwards localhost, but not always -- so probe.
resolve_host() {
  local candidate
  for candidate in 127.0.0.1 "$(ip route show default 2>/dev/null | awk '{print $3; exit}')"; do
    [ -n "$candidate" ] || continue
    if curl -fsS --max-time 3 "http://$candidate:9090/-/healthy" >/dev/null 2>&1; then
      printf '%s' "$candidate"; return 0
    fi
  done
  printf '127.0.0.1'
}

cmd_pull() {
  require_docker; require_checkout
  local image
  # `compose pull` skips every service that also declares `build:` -- which is most
  # of this stack -- so the images would silently never arrive and `start` would
  # then try to build them on the Windows side. Resolving the image references from
  # the compose model and pulling them directly is what actually pins the revision.
  while read -r image; do
    printf 'pulling %s\n' "$image"
    "$DOCKER_BIN" pull "$image"
  done < <(compose config --images | sort -u)
}

cmd_start() {
  require_docker; require_checkout
  # --no-build is deliberate: the images are the pinned published revision, pulled
  # by `pull`. Building them here would silently substitute locally built binaries
  # for the pinned ones and invalidate the provenance of every result.
  compose up -d --no-build
  cmd_wait
}

cmd_stop() {
  require_docker; require_checkout
  compose down --remove-orphans
}

cmd_status() {
  require_docker
  printf 'project: %s   revision: %s\n' "$PROJECT" "$REVISION"
  "$DOCKER_BIN" ps --filter "label=com.docker.compose.project=$PROJECT" \
    --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
}

cmd_health() {
  local host failed=0 spec name service port hp
  host="$(resolve_host)"
  printf 'host used for published ports: %s\n' "$host"
  for spec in "${ENDPOINTS[@]}"; do
    read -r name service port <<<"$spec"
    hp="$(published_port "$service" "$port")"
    if [ -z "$hp" ]; then
      printf 'FAIL  %-14s %s:%s -> no published port\n' "$name" "$service" "$port"; failed=1
    elif timeout 4 bash -c "exec 3<>/dev/tcp/$host/$hp" 2>/dev/null; then
      printf 'ok    %-14s %s:%s -> %s\n' "$name" "$service" "$port" "$hp"
    else
      printf 'FAIL  %-14s %s:%s -> %s\n' "$name" "$service" "$port" "$hp"; failed=1
    fi
  done
  local prom flagd jaeger
  prom="$(published_port prometheus 9090)"; flagd="$(published_port flagd-ui 4000)"
  jaeger="$(published_port jaeger 16686)"
  if curl -fsS --max-time 5 "http://$host:${prom:-0}/-/healthy" >/dev/null 2>&1; then
    printf 'ok    prometheus /-/healthy\n'
  else
    printf 'FAIL  prometheus /-/healthy\n'; failed=1
  fi
  if curl -fsS --max-time 5 "http://$host:${flagd:-0}/api/read" >/dev/null 2>&1; then
    printf 'ok    flagd-ui /api/read\n'
  else
    printf 'FAIL  flagd-ui /api/read\n'; failed=1
  fi
  if curl -fsS --max-time 5 "http://$host:${jaeger:-0}/jaeger/ui/api/services" >/dev/null 2>&1; then
    printf 'ok    jaeger /jaeger/ui/api/services\n'
  else
    printf 'FAIL  jaeger /jaeger/ui/api/services\n'; failed=1
  fi
  return "$failed"
}

cmd_wait() {
  local deadline=$((SECONDS + ${OPSACCORD_DEMO_WAIT_SECONDS:-900}))
  while (( SECONDS < deadline )); do
    if cmd_health >/dev/null 2>&1; then
      cmd_health
      return 0
    fi
    sleep 10
  done
  cmd_health || true
  die "stack did not become healthy within the wait budget"
}

# Emit the adapter's settings for the live stack, resolving the ephemeral ports.
# Written to a git-ignored local file so a run never guesses a port.
cmd_endpoints() {
  local host prom jaeger flagd
  host="$(resolve_host)"
  prom="$(published_port prometheus 9090)"
  jaeger="$(published_port jaeger 16686)"
  flagd="$(published_port frontend-proxy 8080)"
  [ -n "$prom" ] && [ -n "$jaeger" ] && [ -n "$flagd" ] \
    || die "the stack is not up; run scripts/demo.sh start first"
  mkdir -p "$(dirname "$ENDPOINT_FILE")"
  cat >"$ENDPOINT_FILE" <<EOF
# Generated by scripts/demo.sh endpoints -- do not edit, do not commit.
# Pinned Astronomy Shop revision $REVISION, resolved at $(date -u +%Y-%m-%dT%H:%M:%SZ).
OPSACCORD_DEMO_MODE=live
OPSACCORD_PROMETHEUS_BASE_URL=http://$host:$prom
OPSACCORD_JAEGER_BASE_URL=http://$host:$jaeger/jaeger/ui
OPSACCORD_FLAG_CONTROL_BASE_URL=http://$host:$flagd/feature
EOF
  printf 'wrote %s\n' "$ENDPOINT_FILE"
  cat "$ENDPOINT_FILE"
}

cmd_urls() {
  local host
  host="$(resolve_host)"
  cat <<URLS
frontend      http://$host:$(published_port frontend-proxy 8080)
grafana       http://$host:$(published_port grafana 3000)
jaeger        http://$host:$(published_port jaeger 16686)/jaeger/ui
prometheus    http://$host:$(published_port prometheus 9090)
flagd-ui      http://$host:$(published_port frontend-proxy 8080)/feature
URLS
}

cmd_host() { resolve_host; printf '\n'; }

cmd_logs() {
  require_docker; require_checkout
  compose logs --tail "${OPSACCORD_DEMO_LOG_LINES:-80}" "${@:-}"
}

usage() {
  cat <<USAGE
usage: scripts/demo.sh <command>

  pull        download every image the pinned compose model references
  start       start the core + observability stack and wait until it is healthy
  stop        stop and remove this project's containers
  status      show this project's containers
  health      probe every published port and the three APIs the adapter uses
  override    copy demo/compose.opsaccord.yaml into the demo checkout
  wait        block until the stack is healthy
  endpoints   write the adapter's OPSACCORD_* settings to $ENDPOINT_FILE
  urls        print the browser URLs of the running stack
  host        print the resolved host address for published ports
  logs        tail this project's logs

environment overrides:
  OPSACCORD_DEMO_DIR           default $DEMO_DIR
  OPSACCORD_DOCKER_BIN         default $DOCKER_BIN
  OPSACCORD_DEMO_PROJECT       default $PROJECT
  OPSACCORD_DEMO_ENDPOINT_FILE default $ENDPOINT_FILE

Pinned revision: $REVISION. Only the frontend proxy (8080) and Prometheus (9090)
have fixed host ports; the rest are resolved at runtime. The stack is
intentionally one project, because upstream sets fixed container names and a
second stack would collide rather than coexist.

demo/compose.opsaccord.yaml is applied on top of the pinned compose files. It
changes resource limits only (see the file header for the measured reason).
USAGE
}

main() {
  local command="${1:-usage}"
  shift || true
  case "$command" in
    pull) cmd_pull "$@" ;;
    start) cmd_start "$@" ;;
    stop) cmd_stop "$@" ;;
    status) cmd_status "$@" ;;
    health) cmd_health "$@" ;;
    wait) cmd_wait "$@" ;;
    endpoints) cmd_endpoints "$@" ;;
    override) sync_override && printf 'copied override to %s\n' "$OVERRIDE_DST" ;;
    urls) cmd_urls "$@" ;;
    host) cmd_host "$@" ;;
    logs) cmd_logs "$@" ;;
    usage|-h|--help|help) usage ;;
    *) usage; die "unknown command: $command" ;;
  esac
}

main "$@"
