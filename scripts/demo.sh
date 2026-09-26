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
  "PRODUCT_CATALOG product-catalog 3550"
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
  mapping="$(compose port "$service" "$container_port" 2>/dev/null </dev/null | head -1)" || true
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
  local rc=0
  # `compose pull` skips every service that also declares `build:` -- which is most
  # of this stack -- so the images would silently never arrive and `start` would
  # then try to build them on the Windows side. Resolving the image references from
  # the compose model and pulling them directly is what actually pins the revision.
  #
  # The list is read into an array before the loop rather than streamed into it.
  # A `while read` loop takes its input from stdin, and `docker pull` is a CLI
  # that may read stdin; when it does, it consumes the list the loop is reading
  # and every image after the first is silently skipped. Both loops here had that
  # shape, and `docker inspect` demonstrably consumed it on 2026-09-26 - which is
  # how `inspect` came to report one container out of twenty-five on two runs
  # while still exiting 0.
  local images=()
  mapfile -t images < <(compose config --images | sort -u)
  local image
  for image in "${images[@]}"; do
    [ -n "$image" ] || continue
    printf 'pulling %s\n' "$image"
    "$DOCKER_BIN" pull "$image" </dev/null || rc=$?
  done
  return "$rc"
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

# Restart this project's containers in place, or only the ones named.
#
# This is deliberately *not* `stop` followed by `start`: `down` removes the
# containers, which throws away the running state that a diagnosis is reading -
# restart counts, the health probe's own history, and how long a wedged process
# has been wedged. `restart` keeps the container, its configuration and its
# anonymous state, touches no volume, and leaves the image at the pinned
# revision, so a service that is wedged in its own runtime can be given a fresh
# process and the result compared. Every argument is passed straight to compose,
# so a named service is restarted only when it is named.
cmd_restart() {
  require_docker; require_checkout
  local rc=0
  if (($# > 0)); then
    compose restart "$@" || rc=$?
    printf 'demo: compose restart %s exited %s\n' "$*" "$rc" >&2
  else
    compose restart || rc=$?
    printf 'demo: compose restart (whole project) exited %s\n' "$rc" >&2
  fi
  return "$rc"
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
  local host prom jaeger flagd reason rc
  host="$(resolve_host)"
  prom="$(published_port prometheus 9090)"
  jaeger="$(published_port jaeger 16686)"
  flagd="$(published_port frontend-proxy 8080)"
  if [ -z "$prom" ] || [ -z "$jaeger" ] || [ -z "$flagd" ]; then
    # An empty port has two causes that need different responses - a stopped stack,
    # and a Docker engine that never answered - and `published_port` deliberately
    # discards the compose error in its command substitution. One raw call is made
    # again here so the message names which one it was instead of guessing.
    set +e
    reason="$(compose port prometheus 9090 2>&1)"
    rc=$?
    set -e
    reason="${reason//$'\n'/ | }"
    die "cannot resolve the stack (compose port prometheus 9090 exited $rc: ${reason:-no output}); run scripts/demo.sh start first"
  fi
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

# A bounded snapshot of this project's container output, with its own result.
#
# Two defects lived here. `"${@:-}"` expands to a single *empty* service name when
# no service is named, which compose rejects, so the documented bare `demo.sh logs`
# could not work; the service list is now appended only when it is non-empty. And a
# caller that wraps this in `$( ... )` cannot see the exit status of a `die`, which
# makes an empty capture indistinguishable from a failed one - so the compose call
# runs in the foreground and announces its status. That distinction matters here
# because an empty capture is *not* evidence of silence: this revision routes the
# Go services' slog output over OTLP (`otel-config.yml` declares no stdout
# exporter), so their container logs are normally empty and the collector holds
# whatever they emitted.
cmd_logs() {
  require_docker; require_checkout
  local args=(logs --tail "${OPSACCORD_DEMO_LOG_LINES:-80}") rc=0
  if [ -n "${OPSACCORD_DEMO_LOG_SINCE:-}" ]; then args+=(--since "$OPSACCORD_DEMO_LOG_SINCE"); fi
  if (($# > 0)); then args+=("$@"); fi
  compose "${args[@]}" || rc=$?
  printf 'demo: compose %s exited %s\n' "${args[*]}" "$rc" >&2
  return "$rc"
}

# Per-container state that `status` cannot show and a diagnosis needs.
#
# `docker ps` prints one status word: no restart count, no exit code, no OOM flag,
# no configured limit and no health-probe output. Those are exactly the values that
# separate a wedged-but-alive process from an OOM loop and from a service that never
# reached its listener, so they are collected here, one container at a time, with
# every command's exit status reported rather than hidden behind a pipeline.
cmd_inspect() {
  require_docker
  local names_out rc=0 name line probe gomem
  names_out="$("$DOCKER_BIN" ps -a --filter "label=com.docker.compose.project=$PROJECT" --format '{{.Names}}')" || rc=$?
  printf 'demo: listing project containers exited %s\n' "$rc"
  [ "$rc" -eq 0 ] || return "$rc"
  if [ -z "$names_out" ]; then
    printf 'demo: no containers in project %s\n' "$PROJECT"
    return 0
  fi
  # An array, not a `while read` loop: `docker inspect` reads stdin, so a loop fed
  # from a here-string hands it the rest of the container list and stops after the
  # first. Each call also gets /dev/null, so there is nothing left to consume.
  local names=()
  mapfile -t names <<<"$names_out"
  for name in "${names[@]}"; do
    [ -n "$name" ] || continue
    line="$("$DOCKER_BIN" inspect --format \
      '{{.Name}} state={{.State.Status}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} restarts={{.RestartCount}} exit={{.State.ExitCode}} oom_killed={{.State.OOMKilled}} memory_limit={{.HostConfig.Memory}} nano_cpus={{.HostConfig.NanoCpus}} started={{.State.StartedAt}} healthcheck={{if .Config.Healthcheck}}{{json .Config.Healthcheck.Test}}{{else}}none{{end}}' \
      "$name" </dev/null)" || { printf 'demo: inspect %s failed\n' "$name" >&2; rc=1; continue; }
    # `docker inspect` reports the name with a leading slash. The template
    # functions that strip it (`trimPrefix`) need Go 1.19 and the Windows
    # interop binary answered `template parsing error: function "trimPrefix" not
    # defined` on the 2026-09-25 run, which cost that run its per-container
    # state entirely; the slash is removed here, where it cannot depend on the
    # CLI's Go version.
    printf '%s\n' "${line#/}"
    # Only for a container that is not healthy: the probe's own message is what
    # separates "connect timed out" from "the server answered not-serving".
    probe="$("$DOCKER_BIN" inspect --format \
      '{{if .State.Health}}{{if ne .State.Health.Status "healthy"}}{{json .State.Health.Log}}{{end}}{{end}}' \
      "$name" </dev/null)" || rc=1
    if [ -n "$probe" ]; then printf '  health_log=%s\n' "$probe"; fi
    # The Go memory policy is the value under test when a service is wedged, so it
    # is printed. `printf "%.11s"` keeps the filter exact without dumping the whole
    # environment, which carries a database DSN with a password.
    gomem="$("$DOCKER_BIN" inspect --format \
      '{{range .Config.Env}}{{if eq (printf "%.11s" .) "GOMEMLIMIT="}}{{.}}{{end}}{{end}}' \
      "$name" </dev/null)" || rc=1
    if [ -n "$gomem" ]; then printf '  %s\n' "$gomem"; fi
  done
  return "$rc"
}

# Two bounded `docker stats` samples, because a single non-streaming sample has no
# previous reading to difference against and can report 0.00% CPU for a process that
# is in fact spinning. CPU near the core count with RSS held at the cgroup limit is
# the signature the override in demo/compose.opsaccord.yaml was written for; a
# restart count of 0 with OOMKilled false rules the OOM-loop reading out.
cmd_stats() {
  require_docker
  local rc=0 sample
  local targets=("$@")
  if [ "${#targets[@]}" -eq 0 ]; then targets=(product-catalog checkout payment); fi
  for sample in first second; do
    printf 'demo: %s sample of %s\n' "$sample" "${targets[*]}"
    "$DOCKER_BIN" stats --no-stream \
      --format '{{.Name}} cpu={{.CPUPerc}} mem={{.MemUsage}} mem_pct={{.MemPerc}} pids={{.PIDs}}' \
      "${targets[@]}" || rc=1
    if [ "$sample" = first ]; then sleep "${OPSACCORD_DEMO_STATS_SLEEP:-10}"; fi
  done
  return "$rc"
}

usage() {
  cat <<USAGE
usage: scripts/demo.sh <command>

  pull        download every image the pinned compose model references
  start       start the core + observability stack and wait until it is healthy
  stop        stop and remove this project's containers
  restart     restart this project's containers in place (optionally: names only)
  status      show this project's containers
  inspect     per-container health, restarts, exit/OOM state and limits
  stats       two bounded CPU/memory samples for the Go services
  health      probe every published port and the three APIs the adapter uses
  override    copy demo/compose.opsaccord.yaml into the demo checkout
  wait        block until the stack is healthy
  endpoints   write the adapter's OPSACCORD_* settings to $ENDPOINT_FILE
  urls        print the browser URLs of the running stack
  host        print the resolved host address for published ports
  logs        bounded snapshot of this project's logs (not a follow)

environment overrides:
  OPSACCORD_DEMO_DIR           default $DEMO_DIR
  OPSACCORD_DOCKER_BIN         default $DOCKER_BIN
  OPSACCORD_DEMO_PROJECT       default $PROJECT
  OPSACCORD_DEMO_ENDPOINT_FILE default $ENDPOINT_FILE
  OPSACCORD_DEMO_LOG_LINES     default 80 (lines per service for logs)
  OPSACCORD_DEMO_LOG_SINCE     unset; compose --since window for logs, e.g. 20m
  OPSACCORD_DEMO_STATS_SLEEP   default 10 (seconds between stats samples)

An empty logs capture is not evidence that a service was silent: this revision
routes the Go services' slog output over OTLP, and otel-config.yml declares no
stdout exporter, so their container logs are normally empty. Look in the collector.

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
    restart) cmd_restart "$@" ;;
    status) cmd_status "$@" ;;
    inspect) cmd_inspect "$@" ;;
    health) cmd_health "$@" ;;
    stats) cmd_stats "$@" ;;
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
