"""The demo helper's own failure modes, exercised without Docker or a live stack.

``scripts/demo.sh`` is the only path by which live evidence reaches a report, and two
of its commands were able to make their output *unreadable* rather than obviously
wrong:

* ``logs`` passed ``"${@:-}"`` to compose, which expands to one **empty** service
  name when no service was named - compose rejects that, so the documented bare
  ``demo.sh logs`` could not work;
* the same command ran inside a caller's ``$( ... )``, where its exit status is
  invisible, so an empty capture and a failed capture looked identical.

A stub docker binary is enough to pin both, and to pin that a failing ``inspect``
reports itself instead of printing nothing. No container is started, and the real
demo checkout is never touched.

The stub can also drain stdin, because that is what the real CLI does. On
2026-09-26 the live capture reported one container out of the twenty-five the stack
had started, and exited 0 while doing it: ``docker inspect`` reads stdin, so the
here-string the loop was reading was handed to the first ``inspect`` and every
remaining name was consumed with it.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from opsaccord.config import REPO_ROOT

#: The script under test. Overridable so a pre-fix copy can be run against the same
#: tests; the default is the real one.
DEMO_SCRIPT = Path(os.environ.get("DEMO_SCRIPT_UNDER_TEST") or REPO_ROOT / "scripts" / "demo.sh")
SEPARATOR = "\x1f"

STUB = """#!/usr/bin/env bash
# Records every invocation's argv, one invocation per line, then answers the few
# subcommands scripts/demo.sh needs. Nothing here touches Docker.
argv0="$1"
{
  printf '%s' "$argv0"
  shift
  for arg in "$@"; do printf '%s%s' "$(printf '\\x1f')" "$arg"; done
  printf '\\n'
} >>"$STUB_ARGV_LOG"
case "$argv0" in
  version) printf '29.7.2\\n' ;;
  ps)
    if [ -n "${STUB_NO_CONTAINERS:-}" ]; then exit 0; fi
    # Rows are "project|service|id|name" and the --filter arguments are honoured rather
    # than ignored: a test that puts a *second* project's container in the engine, or a
    # second container with the same service label in this one, is only meaningful if
    # the exclusion comes from the filter.
    project="${OPSACCORD_DEMO_PROJECT:-opsaccord-demo}"
    rows=(
      "$project|product-catalog|9f2c1b7a0011|product-catalog"
      "$project|checkout|5a1e0c9d0022|checkout"
    )
    if [ -n "${STUB_NAMES_DIFFER:-}" ]; then
      rows=(
        "$project|product-catalog|9f2c1b7a0011|demo-catalog-1"
        "$project|checkout|5a1e0c9d0022|checkout"
      )
    fi
    if [ -n "${STUB_COLLECTOR:-}" ]; then
      rows+=("$project|otel-collector|7d3f8e2a0033|otel-collector")
    fi
    if [ -n "${STUB_SECOND_PROJECT:-}" ]; then
      rows+=("$STUB_SECOND_PROJECT|product-catalog|deadbeef0044|product-catalog")
    fi
    if [ -n "${STUB_DUPLICATE_SERVICE:-}" ]; then
      rows+=("$project|$STUB_DUPLICATE_SERVICE|beefca110055|second-$STUB_DUPLICATE_SERVICE")
    fi
    want_project=""; want_service=""; want_id=0
    for arg in "$@"; do
      case "$arg" in
        label=com.docker.compose.project=*)
          want_project="${arg#label=com.docker.compose.project=}" ;;
        label=com.docker.compose.service=*)
          want_service="${arg#label=com.docker.compose.service=}" ;;
        *'{{.ID}}'*) want_id=1 ;;
      esac
    done
    for row in "${rows[@]}"; do
      IFS='|' read -r rproject rservice rid rname <<<"$row"
      if [ -n "$want_project" ] && [ "$rproject" != "$want_project" ]; then continue; fi
      if [ -n "$want_service" ] && [ "$rservice" != "$want_service" ]; then continue; fi
      if [ "$want_id" = 1 ]; then
        printf '%s %s\\n' "$rid" "$rname"
      else
        printf '%s\\n' "$rname"
      fi
    done ;;
  inspect)
    if [ -n "${STUB_EATS_STDIN:-}" ]; then cat >/dev/null; fi
    if [ -n "${STUB_INSPECT_RC:-}" ]; then exit "$STUB_INSPECT_RC"; fi
    if [ -n "${STUB_RICH_INSPECT_FAILS:-}" ]; then
      case "$*" in
        *HostConfig*)
          # What the live CLI answered on 2026-10-15 for each of the six
          # containers that have no healthcheck, verbatim.
          printf 'template parsing error: template: :1:52: executing "" at ' >&2
          printf '<.State.Health>: map has no entry for key "Health"\\n' >&2
          exit 1 ;;
      esac
    fi
    # `docker inspect` names a container with a leading slash; the helper is
    # expected to strip it, so the stub supplies it.
    container="$3"
    # The caller may name a container or hand back the id a resolver returned; both
    # are the same container, and the knobs below are written in terms of the
    # *service*, so a test can say "the catalog is unhealthy" without having to know
    # which of the two strings the helper chose.
    case "$container" in
      9f2c1b7a0011|product-catalog|demo-catalog-1) ckey=product-catalog ;;
      5a1e0c9d0022|checkout) ckey=checkout ;;
      7d3f8e2a0033|otel-collector) ckey=otel-collector ;;
      *) ckey="$container" ;;
    esac
    # What the pinned revision configures: the catalog carries a `grpc_health_probe`
    # check, the rest of the stack does not (`healthcheck=none` and `health=none` for
    # the six containers the live 2026-10-15 inspect read that way).
    case "$ckey" in
      product-catalog) healthcheck='["CMD","/bin/grpc_health_probe","-addr=:3550"]' ;;
      *) healthcheck=none ;;
    esac
    if [ -n "${STUB_HEALTHCHECK_ALL:-}" ]; then healthcheck="$STUB_HEALTHCHECK_ALL"; fi
    case " ${STUB_NO_HEALTHCHECK:-} " in *" $ckey "*) healthcheck=none ;; esac
    state=running
    case "$healthcheck" in none) health=none ;; *) health=healthy ;; esac
    case " ${STUB_STOPPED:-} " in *" $ckey "*) state=exited; health=none ;; esac
    case " ${STUB_UNHEALTHY:-} " in *" $ckey "*) health=unhealthy ;; esac
    case " ${STUB_HEALTH_STARTING:-} " in *" $ckey "*) health=starting ;; esac
    case "$*" in
      *HostConfig*)
        printf '/%s state=running health=unhealthy restarts=0 exit=0 ' "$container"
        printf 'oom_killed=false memory_limit=20971520 nano_cpus=0 '
        printf 'started=2026-09-25T11:23:30Z healthcheck=[]\\n' ;;
      # `cmd_inspect` and `cmd_ensure` read the probe's log through a variable
      # bound with `index` (the key may be absent), so the template says `.Log`
      # rather than `.State.Health.Log`; either form answers here.
      *.Log*)
        if [ -n "${STUB_HEALTH_LOG:-}" ]; then
          printf '%s\\n' "$STUB_HEALTH_LOG"
        else
          printf '[{"ExitCode":1,"Output":"failed to connect service :3550 '
          printf 'within 1s"}]\\n'
        fi ;;
      *GOMEMLIMIT*) printf 'GOMEMLIMIT=16MiB\\n' ;;
      # `demo.sh ensure` reads each container's own state plus its compose
      # *service* label, which is what it hands to `compose up`: upstream sets
      # `container_name:` on several services, so the two are not the same string.
      *'index .Config "Labels"'*)
        printf '/%s %s %s %s\\n' "$container" "$state" "$health" "$container" ;;
      # The project recheck's row: every container's state and health, by name.
      *'index .State "Health"}}{{.Name}}'*)
        printf '/%s %s %s\\n' "$container" "$state" "$health" ;;
      # The degraded row `cmd_inspect` falls back to when the CLI refuses the
      # per-container template: `docker inspect` cannot be asked for the fields
      # that were just refused, so only the name and status are claimed.
      *'{{.Name}} state={{.State.Status}}'*)
        printf '/%s state=running\\n' "$container" ;;
      # `verify-recovery` reads the configured check, the health status alone, and the
      # state alone from the container it resolved.
      *'index .Config "Healthcheck"'*) printf '%s\\n' "$healthcheck" ;;
      *'index .State "Health"'*) printf '%s\\n' "$health" ;;
      *'{{.State.Status}}'*) printf '%s\\n' "$state" ;;
    esac ;;
  pull)
    if [ -n "${STUB_EATS_STDIN:-}" ]; then cat >/dev/null; fi
    exit "${STUB_PULL_RC:-0}" ;;
  compose)
    case " $* " in
      *" config --images "*)
        printf 'ghcr.io/example/a:1.0\\nghcr.io/example/b:1.0\\nghcr.io/example/c:1.0\\n'
        exit 0 ;;
      *" port "*)
        if [ -n "${STUB_COMPOSE_RC:-}" ]; then
          printf 'compose: port lookup failed\\n' >&2
          exit "$STUB_COMPOSE_RC"
        fi
        if [ -n "${STUB_NO_PORT:-}" ]; then exit 0; fi
        printf '0.0.0.0:65235\\n'
        exit 0 ;;
    esac
    printf 'product-catalog stdout line\\n'; exit "${STUB_COMPOSE_RC:-0}" ;;
  stats)
    if [ -n "${STUB_STATS_RC:-}" ]; then exit "$STUB_STATS_RC"; fi
    last=""
    for arg in "$@"; do last="$arg"; done
    printf '%s cpu=0.00%% mem=1MiB / 20MiB mem_pct=5.00%% pids=9\\n' "$last" ;;
esac
"""


def _stub_environment(tmp_path: Path, **extra: str) -> dict[str, str]:
    """A shell environment that cannot inherit another project's live settings."""
    stub = tmp_path / "docker-stub"
    stub.write_text(STUB, encoding="utf-8")
    stub.chmod(0o755)
    demo_dir = tmp_path / "demo"
    demo_dir.mkdir(exist_ok=True)
    (demo_dir / "compose.yaml").write_text("", encoding="utf-8")
    (demo_dir / ".env").write_text("", encoding="utf-8")

    inherited = {
        key: value for key, value in os.environ.items() if not key.startswith("OPSACCORD_")
    }
    inherited.update(
        {
            "OPSACCORD_DOCKER_BIN": str(stub),
            "OPSACCORD_DEMO_DIR": str(demo_dir),
            "OPSACCORD_DEMO_PROJECT": "opsaccord-demo-script-test",
            "OPSACCORD_DEMO_ENDPOINT_FILE": str(tmp_path / "demo-endpoints.env"),
            "STUB_ARGV_LOG": str(tmp_path / "argv.log"),
            **extra,
        }
    )
    return inherited


def _invocations(tmp_path: Path, subcommand: str) -> list[list[str]]:
    log = tmp_path / "argv.log"
    return [
        line.split(SEPARATOR)
        for line in log.read_text(encoding="utf-8").splitlines()
        if line.startswith(f"{subcommand}{SEPARATOR}")
    ]


def _run(tmp_path: Path, *args: str, **extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(DEMO_SCRIPT), *args],
        cwd=REPO_ROOT,
        env=_stub_environment(tmp_path, **extra),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _probe_stubs(tmp_path: Path) -> Path:
    """A bin directory whose ``curl`` and ``timeout`` stand in for the probes.

    ``demo.sh health`` decides its verdict from two external commands: ``curl`` for
    the HTTP checks, and ``timeout ... /dev/tcp`` for the TCP ones. This sandbox
    denies loopback sockets outright, so the real ones cannot answer here - and what
    is under test is the helper's *decision* (was every container already up? which
    services did it start?), not the probe itself. Replacing the probe is the same
    seam the docker stub already uses; nothing about Docker or compose changes.

    The curl stub answers with ``STUB_CURL_CODE`` and exits ``STUB_CURL_RC``, because
    ``verify-recovery`` has to tell three things apart: an accepted OTLP export (200),
    an answer that is not the documented one, and no answer at all. Its default is a
    code no HTTP server sends, so a test that forgets to set it fails rather than
    passing by accident.
    """
    bin_dir = tmp_path / "probe-bin"
    bin_dir.mkdir(exist_ok=True)
    curl = bin_dir / "curl"
    curl.write_text(
        '#!/usr/bin/env bash\nprintf \'%s\' "${STUB_CURL_CODE:-000}"\nexit "${STUB_CURL_RC:-0}"\n',
        encoding="utf-8",
    )
    curl.chmod(0o755)
    timeout = bin_dir / "timeout"
    timeout.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    timeout.chmod(0o755)
    return bin_dir


def _probed(tmp_path: Path, **extra: str) -> dict[str, str]:
    """`_run` extras that put the probe stubs first on `PATH`."""
    return {"PATH": f"{_probe_stubs(tmp_path)}:{os.environ['PATH']}", **extra}


def _compose_up_args(tmp_path: Path) -> list[list[str]]:
    """Only the `up` invocations: `health` calls compose too, for `port`."""
    return [argv[argv.index("up") :] for argv in _invocations(tmp_path, "compose") if "up" in argv]


def test_logs_forwards_a_named_service_and_no_empty_argument(tmp_path: Path) -> None:
    result = _run(tmp_path, "logs", "checkout")

    assert result.returncode == 0, result.stderr
    compose_calls = [argv for argv in _invocations(tmp_path, "compose") if "--tail" in argv]
    assert len(compose_calls) == 1, compose_calls
    assert compose_calls[0][-1] == "checkout"
    assert "" not in compose_calls[0]


def test_logs_without_a_service_named_passes_no_empty_name(tmp_path: Path) -> None:
    result = _run(tmp_path, "logs")

    assert result.returncode == 0, result.stderr
    compose_calls = [argv for argv in _invocations(tmp_path, "compose") if "--tail" in argv]
    assert len(compose_calls) == 1, compose_calls
    # The whole point: the tail bound is the last argument, not an empty service.
    assert compose_calls[0][-1] == "80"
    assert "" not in compose_calls[0]


def test_a_failed_log_capture_reports_its_status_instead_of_looking_empty(tmp_path: Path) -> None:
    result = _run(tmp_path, "logs", "checkout", STUB_COMPOSE_RC="7")

    assert result.returncode == 7
    assert "exited 7" in result.stderr


def test_inspect_reports_per_container_state_and_its_own_failures(tmp_path: Path) -> None:
    ok = _run(tmp_path, "inspect")

    assert ok.returncode == 0, ok.stderr
    assert "listing project containers exited 0" in ok.stdout
    # `docker inspect` names the container with a leading slash; the report wants
    # the name to line up with `docker ps` and with the stats samples.
    assert "/product-catalog" not in ok.stdout
    assert "product-catalog state=running health=unhealthy" in ok.stdout
    assert "restarts=0 exit=0 oom_killed=false" in ok.stdout
    assert "GOMEMLIMIT=16MiB" in ok.stdout

    failed = _run(tmp_path, "inspect", STUB_INSPECT_RC="1")

    assert failed.returncode == 1
    assert "failed" in failed.stderr


def test_inspect_and_pull_keep_their_name_list_out_of_docker_stdin(tmp_path: Path) -> None:
    """A `while read` loop is fed from the same stdin `docker inspect` consumes.

    On 2026-09-26 two live captures reported **one** container out of the twenty-five
    the stack had started, and exited 0 while doing it, so the per-container state
    that the whole pass is judged on arrived incomplete and looked complete. The
    cause is not Docker's output: `docker inspect` reads stdin, so the here-string the
    loop was reading from was handed to the first `inspect`, which consumed every
    remaining name. `cmd_pull` had the identical shape over its image list. The stub
    drains stdin only when asked, so this stays a regression test rather than a test
    of the stub.
    """
    inspected = _run(tmp_path, "inspect", STUB_EATS_STDIN="1")

    assert inspected.returncode == 0, inspected.stderr
    assert "product-catalog state=running health=unhealthy" in inspected.stdout
    assert "checkout state=running health=unhealthy" in inspected.stdout

    pulled = _run(tmp_path, "pull", STUB_EATS_STDIN="1")

    assert pulled.returncode == 0, pulled.stderr
    images = [argv[-1] for argv in _invocations(tmp_path, "pull")]
    assert images == ["ghcr.io/example/a:1.0", "ghcr.io/example/b:1.0", "ghcr.io/example/c:1.0"]


def test_a_container_the_cli_will_not_render_still_gets_a_row(tmp_path: Path) -> None:
    """Six of twenty-five containers vanished this way on the live 2026-10-15 run.

    The host's Docker CLI answered
    ``template parsing error: template: :1:52: executing "" at <.State.Health>:
    map has no entry for key "Health"`` for each of the six containers that have no
    healthcheck - ``cart``, ``otel-collector``, ``jaeger``, ``flagd``, ``grafana`` and
    ``prometheus`` - so those six had no row while the other nineteen did, and a
    container with no row reads exactly like a container that is not there. The live
    trigger is per-container and is **not** explained by "the field form is always
    rejected": ``cmd_ensure`` reads ``.State.Health`` the same way and was accepted for
    those same six containers in the same run. The stub therefore stands in for "the CLI
    refuses this container's rich row" and drives the fallback, rather than modelling the
    rule. The helper now falls back to the two always-present fields and marks the rest
    missing, so the row survives the refusal; the exit status still reports it.
    """
    result = _run(tmp_path, "inspect", STUB_RICH_INSPECT_FAILS="1")

    assert result.returncode == 1
    assert "the CLI rejected the per-container template" in result.stderr
    assert "product-catalog state=running per_container_detail=missing" in result.stdout
    assert "checkout state=running per_container_detail=missing" in result.stdout


def test_optional_inspect_keys_are_read_with_index_not_with_a_field() -> None:
    """`.State.Health` is absent for a container that has no healthcheck.

    A `.Field` access on a map is what the live CLI rejected on 2026-10-15, and the
    keys that may be absent are exactly the ones a container can be missing: its
    health, its healthcheck configuration and its labels. They are read with the
    ``index`` builtin and bound to a variable, which is not subject to the CLI's
    missing-key policy; the fields read *from* that variable are then safe, because
    the variable is only non-empty when the key was there. This test fails on a
    dotted access to an optional key so the next one is caught here rather than by a
    live run that quietly loses six containers.
    """
    script = DEMO_SCRIPT.read_text(encoding="utf-8")
    templates = re.findall(r"'({{[^']*}})'", script)
    assert templates, "no --format template found; this guard would be vacuous"

    optional = (r"\.State\.Health", r"\.Config\.Healthcheck", r"\.Config\.Labels")
    offending = [
        f"{pattern} in {template}"
        for template in templates
        for pattern in optional
        if re.search(pattern, template)
    ]
    assert not offending, offending


def test_a_failing_pull_is_reported_rather_than_only_the_last_one(tmp_path: Path) -> None:
    """`pull` returns one status for a loop of images; a failure must reach the caller."""
    result = _run(tmp_path, "pull", STUB_PULL_RC="5")

    assert result.returncode == 5
    assert len(_invocations(tmp_path, "pull")) == 3


def test_the_inspect_templates_use_no_go_function_the_cli_may_lack() -> None:
    """`trimPrefix` cost a live run its per-container state; keep it out.

    The 2026-09-25 capture answered
    ``template parsing error: function "trimPrefix" not defined`` for the
    inspect line, so the block that was meant to record health, restart counts,
    limits and the health probe's own message recorded none of it. ``trimPrefix``
    needs Go 1.19 in the template engine, and the Docker CLI on this host is the
    Windows interop binary, whose templating is not the part that follows the
    Linux release notes. The template functions that are safe are older than
    that, so they are listed here and anything else fails the test on purpose:
    the fix for a new one is to check the running CLI supports it and add it, not
    to discover it in a live run.
    """
    allowed = {
        "and",
        "call",
        "else",
        "end",
        "eq",
        "html",
        "if",
        "index",
        "js",
        "json",
        "len",
        "ne",
        "not",
        "or",
        "print",
        "printf",
        "println",
        "range",
        "slice",
        "urlquery",
        "with",
    }
    script = DEMO_SCRIPT.read_text(encoding="utf-8")
    templates = re.findall(r"'({{[^']*}})'", script)
    assert templates, "no --format template found; this guard would be vacuous"

    offending: list[str] = []
    for template in templates:
        for action in re.findall(r"{{(.*?)}}", template):
            # String literals are not identifiers, and a `.Field` access is a
            # field, not a function, so only bare words are candidates.
            body = re.sub(r'"[^"]*"', " ", action)
            for token in re.findall(r"(?<![\w.$])([a-z][A-Za-z0-9]*)", body):
                if token not in allowed:
                    offending.append(f"{token!r} in {template}")
    assert not offending, offending


def test_restart_forwards_the_named_services_and_reports_its_status(tmp_path: Path) -> None:
    """A restart is the reversible primitive; it must name exactly what it restarts."""
    named = _run(tmp_path, "restart", "product-catalog", "otel-collector")

    assert named.returncode == 0, named.stderr
    calls = _invocations(tmp_path, "compose")
    assert len(calls) == 1, calls
    assert calls[0][-3:] == ["restart", "product-catalog", "otel-collector"]
    assert "exited 0" in named.stderr

    failed = _run(tmp_path, "restart", "product-catalog", STUB_COMPOSE_RC="4")

    assert failed.returncode == 4
    assert "exited 4" in failed.stderr


def test_restart_without_a_name_restarts_the_whole_project(tmp_path: Path) -> None:
    result = _run(tmp_path, "restart")

    assert result.returncode == 0, result.stderr
    calls = _invocations(tmp_path, "compose")
    assert len(calls) == 1, calls
    assert calls[0][-1] == "restart"


def test_every_documented_OPSACCORD_variable_exists_somewhere_in_the_repository() -> None:
    """A documented setting that does not exist sends the reader to a silent no-op.

    ``docs/development.md`` told the reader to point ``OPSACCORD_DEMO_CHECKOUT`` at a
    checkout in another location; ``scripts/demo.sh`` reads ``OPSACCORD_DEMO_DIR``, so
    following the instruction changed nothing and the helper kept using the default
    path. The check is deliberately repo-wide rather than script-wide, because these
    settings legitimately live in several places.
    """
    documented = set(
        re.findall(r"OPSACCORD_[A-Z_]+", (REPO_ROOT / "docs" / "development.md").read_text("utf-8"))
    )
    assert documented, "no settings found; this guard would be vacuous"

    sources = [
        path
        for path in list((REPO_ROOT / "scripts").iterdir()) + list((REPO_ROOT / "tests").iterdir())
        if path.suffix in {".sh", ".py"}
    ]
    sources.append(REPO_ROOT / "docs" / "development.md")
    text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sources
        if path.is_file() and path.name != "development.md"
    )

    missing = sorted(name for name in documented if name not in text)
    assert not missing, missing


def test_logs_bounds_its_window_only_when_asked(tmp_path: Path) -> None:
    """A line bound alone can span hours on the collector; the window is opt-in."""
    unbounded = _run(tmp_path, "logs", "otel-collector")
    assert unbounded.returncode == 0, unbounded.stderr
    assert "--since" not in unbounded.stdout

    bounded = _run(tmp_path, "logs", "otel-collector", OPSACCORD_DEMO_LOG_SINCE="20m")
    assert bounded.returncode == 0, bounded.stderr
    compose_calls = [argv for argv in _invocations(tmp_path, "compose") if "--tail" in argv]
    assert len(compose_calls) == 2, compose_calls
    assert "--since" not in compose_calls[0]
    assert compose_calls[1][-3:] == ["--since", "20m", "otel-collector"]


def test_stats_samples_twice_and_reports_a_failure(tmp_path: Path) -> None:
    ok = _run(tmp_path, "stats", OPSACCORD_DEMO_STATS_SLEEP="0")

    assert ok.returncode == 0, ok.stderr
    samples = _invocations(tmp_path, "stats")
    assert len(samples) == 2, samples
    assert samples[0][:3] == ["stats", "--no-stream", "--format"]
    assert samples[0][-3:] == ["product-catalog", "checkout", "payment"]
    assert "cpu=" in ok.stdout

    failed = _run(tmp_path, "stats", OPSACCORD_DEMO_STATS_SLEEP="0", STUB_STATS_RC="1")
    assert failed.returncode == 1


def test_the_health_probe_surface_covers_the_measured_path() -> None:
    """A keep-the-list-honest guard, because the gap was real.

    ``demo.sh health`` probes the ports listed in ``ENDPOINTS`` and nothing else, and
    ``PRODUCT_CATALOG`` was missing from that list while the catalog was the failing
    service - so the health command that is meant to show the stack's surface could
    not show it. This asserts the list, not the probe behaviour, which needs Docker.
    """
    script = DEMO_SCRIPT.read_text(encoding="utf-8")

    for name in ("CHECKOUT", "PRODUCT_CATALOG", "PAYMENT"):
        assert f'"{name} ' in script, f"{name} is not on the health probe surface"


def test_endpoints_names_the_real_cause_when_it_cannot_resolve(tmp_path: Path) -> None:
    """`endpoints` must not blame a stopped stack for a Docker failure.

    The script's port lookup discards the compose error inside its command
    substitution, so the old message always said "the stack is not up". That is the
    wrong instruction when the engine never answered, and the pasted evidence block
    treats this command as its stop condition.
    """
    result = _run(tmp_path, "endpoints", STUB_COMPOSE_RC="3")

    assert result.returncode == 1
    assert "cannot resolve the stack" in result.stderr
    assert "exited 3" in result.stderr
    assert not (tmp_path / "demo-endpoints.env").exists()


# --- `ensure`: three container states, three different actions ---------------


def test_ensure_starts_the_pinned_stack_only_when_the_project_is_empty(
    tmp_path: Path,
) -> None:
    """An absent stack is the one state where starting everything is the right move."""
    result = _run(tmp_path, "ensure", **_probed(tmp_path, STUB_NO_CONTAINERS="1"))

    assert result.returncode == 0, result.stderr
    assert _compose_up_args(tmp_path) == [["up", "-d", "--no-build"]]
    assert "no containers in project" in result.stdout


def test_ensure_starts_only_the_stopped_services_and_names_them(tmp_path: Path) -> None:
    """A stopped service is started by name, so nothing that already runs is touched."""
    result = _run(tmp_path, "ensure", **_probed(tmp_path, STUB_STOPPED="checkout"))

    assert result.returncode == 0, result.stderr
    assert _compose_up_args(tmp_path) == [["up", "-d", "--no-build", "checkout"]]
    # The per-container state it decided from, printed before it acted.
    assert "checkout exited none checkout" in result.stdout
    assert "product-catalog running healthy product-catalog" in result.stdout


def test_ensure_leaves_a_healthy_project_alone(tmp_path: Path) -> None:
    """Running and healthy means nothing to do - not even a probe of the stack."""
    result = _run(tmp_path, "ensure", **_probed(tmp_path))

    assert result.returncode == 0, result.stderr
    assert _invocations(tmp_path, "compose") == []
    assert "nothing was started" in result.stdout


def test_ensure_refuses_a_running_but_unhealthy_container_without_recreating_anything(
    tmp_path: Path,
) -> None:
    """A service that is *up* keeps its evidence, so it is never the one to blanket-`up`.

    `compose up -d` recreates a container whose configuration or image differs, so
    reaching for it because a probe failed would throw away the running container,
    its restart count and its health history - exactly the readings a diagnosis is
    made of. An unhealthy container produces its own evidence and the command stops
    there, whatever state the other containers are in.

    The two bounded samples belong to that evidence: the state a repair would replace
    exists only while the container is in it, so a stop that hands back the health
    output and the log tail but not the numbers that decide the repair costs the user
    another host run - which is what the 2026-10-15 run did.
    """
    result = _run(
        tmp_path,
        "ensure",
        **_probed(tmp_path, STUB_UNHEALTHY="checkout", OPSACCORD_DEMO_STATS_SLEEP="0"),
    )

    assert result.returncode == 1
    assert "running but not healthy" in result.stderr
    # The probe's own message, and a bounded log tail, for the container in question.
    assert "failed to connect service :3550" in result.stderr
    assert "logs --tail 80 checkout" in result.stderr
    # The numbers a repair is read from, taken while the state they describe exists.
    samples = _invocations(tmp_path, "stats")
    assert len(samples) == 2, samples
    assert all(argv[-1] == "checkout" for argv in samples), samples
    assert "the sample pair exited 0" in result.stderr
    assert "STOP" in result.stderr
    # Nothing was started, recreated, stopped or restarted.
    assert _compose_up_args(tmp_path) == []
    mutating = [
        argv for argv in _invocations(tmp_path, "compose") if {"down", "restart"} & set(argv)
    ]
    assert mutating == [], mutating


def test_ensure_still_refuses_when_the_sample_pair_fails(tmp_path: Path) -> None:
    """A failed measurement must not turn into a different verdict.

    `docker stats` can fail while `inspect` answers, and the refusal is about the
    container's health, not about the helper's reading of it: the command still
    exits 1, still prints the STOP line, reports the sample's own status, and starts,
    stops and restarts nothing.
    """
    result = _run(
        tmp_path,
        "ensure",
        **_probed(
            tmp_path,
            STUB_UNHEALTHY="checkout",
            OPSACCORD_DEMO_STATS_SLEEP="0",
            STUB_STATS_RC="1",
        ),
    )

    assert result.returncode == 1
    assert "the sample pair exited 1" in result.stderr
    assert "STOP" in result.stderr
    assert _compose_up_args(tmp_path) == []


# --- container identity: the compose labels, never a name ---------------------


def test_service_container_resolves_by_project_and_service_label(tmp_path: Path) -> None:
    """A container name is not an identity, and the block that was handed off assumed one.

    The post-restart poll ran ``docker inspect ... product-catalog``. A name is global on
    the engine: upstream pins ``container_name:`` on most services, so a second project -
    or a differently-configured one - can hold the same string, and the poll would have
    read *that* container's health. The identity this resolver uses is this project's
    compose label plus ``com.docker.compose.service``, and what it hands back is the id,
    so every later read names the container the engine resolved.
    """
    result = _run(tmp_path, "service-container", "product-catalog", "checkout")

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "product-catalog\t9f2c1b7a0011 product-catalog",
        "checkout\t5a1e0c9d0022 checkout",
    ]
    logged = _invocations(tmp_path, "ps")
    assert len(logged) == 2, logged
    assert "label=com.docker.compose.project=opsaccord-demo-script-test" in logged[0]
    assert "label=com.docker.compose.service=product-catalog" in logged[0]
    assert "label=com.docker.compose.service=checkout" in logged[1]


def test_a_container_name_that_differs_from_its_service_still_resolves(tmp_path: Path) -> None:
    """``container_name:`` is a deployment choice; the service label is the identity."""
    result = _run(tmp_path, "service-container", "product-catalog", STUB_NAMES_DIFFER="1")

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["product-catalog\t9f2c1b7a0011 demo-catalog-1"]


def test_another_projects_container_with_the_same_name_is_not_a_candidate(tmp_path: Path) -> None:
    """The project label is what makes the match unambiguous, so it is what is filtered."""
    result = _run(
        tmp_path,
        "service-container",
        "product-catalog",
        STUB_SECOND_PROJECT="another-project",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["product-catalog\t9f2c1b7a0011 product-catalog"]
    assert "deadbeef0044" not in result.stdout


def test_a_service_the_project_does_not_hold_stops_instead_of_guessing(tmp_path: Path) -> None:
    """Zero matches is not the same fact as a stopped container, and is not a fallback."""
    result = _run(tmp_path, "service-container", "product-catalog", STUB_NO_CONTAINERS="1")

    assert result.returncode == 1
    assert "expected exactly one container for service product-catalog" in result.stderr
    assert "found 0" in result.stderr
    assert _invocations(tmp_path, "inspect") == []


def test_two_matching_containers_stop_instead_of_picking_one(tmp_path: Path) -> None:
    """The second match is exactly the ambiguity a hard-coded name hides."""
    result = _run(
        tmp_path,
        "service-container",
        "product-catalog",
        STUB_DUPLICATE_SERVICE="product-catalog",
    )

    assert result.returncode == 1
    assert "found 2" in result.stderr
    assert "9f2c1b7a0011" in result.stderr
    assert "beefca110055" in result.stderr
    assert _invocations(tmp_path, "inspect") == []


def test_verify_recovery_reads_the_container_the_engine_resolved(tmp_path: Path) -> None:
    """The readings are taken from the resolved id, not from the name it resolves to."""
    result = _run(
        tmp_path,
        "verify-recovery",
        "product-catalog",
        **_probed(tmp_path, STUB_NAMES_DIFFER="1"),
    )

    assert result.returncode == 0, result.stderr
    assert "product-catalog = container 9f2c1b7a0011, configured healthcheck" in result.stdout
    state_reads = [
        argv for argv in _invocations(tmp_path, "inspect") if argv[2] == "{{.State.Status}}"
    ]
    assert state_reads, _invocations(tmp_path, "inspect")
    assert {argv[3] for argv in state_reads} == {"9f2c1b7a0011"}


# --- `verify-recovery`: both restarted services, one bounded wait -------------


def test_the_collector_is_judged_by_its_documented_surface_because_it_has_no_healthcheck(
    tmp_path: Path,
) -> None:
    """The pinned revision gives ``otel-collector`` no check, and "running" is not readiness.

    Read from the pinned checkout this pass: no ``healthcheck:`` stanza for the service in
    ``compose.yaml``, ``compose.observability.yaml`` or ``demo/compose.opsaccord.yaml``; no
    ``health_check`` extension in ``src/otel-collector/otelcol-config.yml``, so there is no
    :13133 to ask; and the live inspect read ``healthcheck=none`` for it. What that config
    does document is the OTLP HTTP receiver, whose port ``.env`` fixes at 4318. That is
    what this requires, and it is only *process* readiness of the receive path.
    """
    result = _run(
        tmp_path,
        "verify-recovery",
        "otel-collector",
        **_probed(tmp_path, STUB_COLLECTOR="1", STUB_CURL_CODE="200"),
    )

    assert result.returncode == 0, result.stderr
    assert (
        "no configured healthcheck; requiring the surface the pinned configuration documents"
        in result.stdout
    )
    assert "POST /v1/traces on the published 4318" in result.stdout
    assert "POST /v1/traces on 127.0.0.1:65235 answered HTTP 200" in result.stdout
    ports = [argv[-2:] for argv in _invocations(tmp_path, "compose") if "port" in argv]
    assert ["otel-collector", "4318"] in ports, ports
    assert "process readiness is not export" in result.stdout


def test_a_configured_healthcheck_is_used_instead_of_the_documented_surface(tmp_path: Path) -> None:
    """The documented surface is the fallback for *no* check, not a preference over one."""
    result = _run(
        tmp_path,
        "verify-recovery",
        "otel-collector",
        **_probed(
            tmp_path,
            STUB_COLLECTOR="1",
            STUB_HEALTHCHECK_ALL='["CMD","wget","-q","-O-","http://127.0.0.1:13133"]',
            STUB_CURL_CODE="503",
        ),
    )

    assert result.returncode == 0, result.stderr
    wget_check = '["CMD","wget","-q","-O-","http://127.0.0.1:13133"]'
    assert f"configured healthcheck {wget_check}" in result.stdout
    assert "POST /v1/traces" not in result.stdout
    assert not [argv for argv in _invocations(tmp_path, "compose") if "4318" in argv]


def test_a_service_with_no_check_and_no_documented_surface_is_refused(tmp_path: Path) -> None:
    """Neither a check nor a documented surface means readiness cannot be established."""
    result = _run(
        tmp_path,
        "verify-recovery",
        "checkout",
        **_probed(tmp_path, OPSACCORD_DEMO_VERIFY_SLEEP="0"),
    )

    assert result.returncode == 1
    assert "readiness cannot be established" in result.stderr
    assert "READY" not in result.stdout


def test_an_unready_collector_stops_the_run_though_the_catalog_came_back(
    tmp_path: Path,
) -> None:
    """ "The catalog is healthy again" is not the property the capture needs."""
    result = _run(
        tmp_path,
        "verify-recovery",
        "product-catalog",
        "otel-collector",
        **_probed(
            tmp_path,
            STUB_COLLECTOR="1",
            STUB_CURL_CODE="503",
            OPSACCORD_DEMO_VERIFY_SLEEP="0",
            OPSACCORD_DEMO_VERIFY_SECONDS="0",
        ),
    )

    assert result.returncode == 1
    assert "answered HTTP 503, which is not the documented export answer" in result.stdout
    assert "did not come ready inside the one 0 s window" in result.stderr
    assert "READY" not in result.stdout


def test_an_unhealthy_catalog_stops_the_run_though_the_collector_is_ready(
    tmp_path: Path,
) -> None:
    """The stop hands back the reading that decided it, not just the verdict."""
    result = _run(
        tmp_path,
        "verify-recovery",
        "product-catalog",
        "otel-collector",
        **_probed(
            tmp_path,
            STUB_COLLECTOR="1",
            STUB_CURL_CODE="200",
            STUB_UNHEALTHY="product-catalog",
            STUB_HEALTH_LOG='[{"ExitCode":-1,"Output":"Health check exceeded timeout (5s)"}]',
            OPSACCORD_DEMO_VERIFY_SLEEP="0",
            OPSACCORD_DEMO_VERIFY_SECONDS="0",
        ),
    )

    assert result.returncode == 1
    unhealthy_catalog = "product-catalog (container 9f2c1b7a0011): state=running health=unhealthy"
    assert unhealthy_catalog in result.stdout
    assert "HTTP 200" in result.stdout
    # What the stop hands back: the configured check's own history, and a bounded log tail.
    assert "Health check exceeded timeout (5s)" in result.stderr
    assert "bounded log tail" in result.stderr
    assert "logs --tail 80 product-catalog" in result.stderr
    assert "READY" not in result.stdout


def test_a_dependency_that_is_unhealthy_after_the_restart_stops_the_run(tmp_path: Path) -> None:
    """A recheck of the whole project, so a regression during the restart is not ignored."""
    result = _run(
        tmp_path,
        "verify-recovery",
        "product-catalog",
        **_probed(tmp_path, STUB_UNHEALTHY="checkout", OPSACCORD_DEMO_VERIFY_SLEEP="0"),
    )

    assert result.returncode == 1
    assert "recheck checkout state=running health=unhealthy" in result.stdout
    assert "outside the verified pair are not running-and-healthy" in result.stderr
    assert "READY" not in result.stdout


def test_a_stopped_dependency_after_the_restart_stops_the_run(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        "verify-recovery",
        "product-catalog",
        **_probed(tmp_path, STUB_STOPPED="checkout", OPSACCORD_DEMO_VERIFY_SLEEP="0"),
    )

    assert result.returncode == 1
    assert "recheck checkout state=exited health=none" in result.stdout
    assert "READY" not in result.stdout


def test_both_services_passing_is_what_allows_the_run_to_continue(tmp_path: Path) -> None:
    """The gate the block calls before it refreshes the endpoints and captures."""
    result = _run(
        tmp_path,
        "verify-recovery",
        "--budget-seconds",
        "120",
        "product-catalog",
        "otel-collector",
        **_probed(tmp_path, STUB_COLLECTOR="1", STUB_CURL_CODE="200"),
    )

    assert result.returncode == 0, result.stderr
    assert 'configured healthcheck ["CMD","/bin/grpc_health_probe","-addr=:3550"]' in result.stdout
    assert "POST /v1/traces on 127.0.0.1:65235 answered HTTP 200" in result.stdout
    assert "every named service came ready inside the one 120 s window" in result.stdout
    assert "recheck product-catalog state=running health=healthy" in result.stdout
    assert "recheck otel-collector state=running health=none" in result.stdout
    assert "recheck: every container in project" in result.stdout
    assert "demo: READY: product-catalog otel-collector verified" in result.stdout


def test_one_deadline_covers_every_named_service(tmp_path: Path) -> None:
    """One overall wait, not a long wait per probe: the window is the caller's, once."""
    result = _run(
        tmp_path,
        "verify-recovery",
        "product-catalog",
        **_probed(
            tmp_path,
            STUB_HEALTH_STARTING="product-catalog",
            OPSACCORD_DEMO_VERIFY_SECONDS="1",
            OPSACCORD_DEMO_VERIFY_SLEEP="1",
        ),
    )

    assert result.returncode == 1
    attempts = re.findall(r"demo: attempt (\d+):", result.stdout)
    assert attempts == ["1", "2"], attempts
    assert "state=running health=starting" in result.stdout
    assert "did not come ready inside the one 1 s window" in result.stderr
    assert "READY" not in result.stdout


def test_an_inspection_that_fails_is_not_read_as_readiness(tmp_path: Path) -> None:
    """`running` cannot be claimed from a command that did not answer."""
    result = _run(
        tmp_path,
        "verify-recovery",
        "product-catalog",
        STUB_INSPECT_RC="1",
        **_probed(tmp_path, OPSACCORD_DEMO_VERIFY_SLEEP="0"),
    )

    assert result.returncode == 1
    assert "could not be inspected" in result.stderr
    assert "an unknown check is not readiness" in result.stderr
    assert "READY" not in result.stdout


def test_a_service_with_no_published_port_is_not_read_as_ready(tmp_path: Path) -> None:
    """The documented surface has no address, so the check cannot be established."""
    result = _run(
        tmp_path,
        "verify-recovery",
        "otel-collector",
        **_probed(
            tmp_path,
            STUB_COLLECTOR="1",
            STUB_NO_PORT="1",
            OPSACCORD_DEMO_VERIFY_SLEEP="0",
        ),
    )

    assert result.returncode == 1
    assert "publishes no host port for 4318" in result.stdout
    assert "readiness could not be read" in result.stderr
    assert "READY" not in result.stdout


def test_verify_recovery_refuses_an_ambiguous_service_before_reading_anything(
    tmp_path: Path,
) -> None:
    result = _run(
        tmp_path,
        "verify-recovery",
        "product-catalog",
        **_probed(
            tmp_path,
            STUB_DUPLICATE_SERVICE="product-catalog",
            OPSACCORD_DEMO_VERIFY_SLEEP="0",
        ),
    )

    assert result.returncode == 1
    assert "did not resolve to exactly one container" in result.stderr
    assert "attempt 1" not in result.stdout
    assert _invocations(tmp_path, "inspect") == []


def test_the_documented_readiness_table_matches_the_pinned_revision() -> None:
    """The guard for the table itself, because a test cannot read the pinned checkout.

    The pinned checkout lives outside this repository, so what is pinned here is that the
    table exists, names the service whose configuration has no healthcheck, and cites the
    surfaces that decision came from: the OTLP HTTP receiver at the port `.env` fixes, and
    the absence of a health_check extension. Without it, the next edit could delete the
    fallback and leave the collector passing on "running" - which is exactly the reading
    the live 2026-10-15 inspect showed proves nothing.
    """
    script = DEMO_SCRIPT.read_text(encoding="utf-8")

    assert '"otel-collector 4318 /v1/traces"' in script
    assert "src/otel-collector/otelcol-config.yml" in script
    assert "health_check extension" in script
    assert 'index .Config "Healthcheck"' in script


def _report_bash_blocks() -> list[str]:
    """Every bash fence in the implementation report, in document order."""
    report = (REPO_ROOT / "docs" / "reports" / "phase1-implementation.md").read_text(
        encoding="utf-8"
    )
    fences = re.findall(r"```bash\n(.*?)\n```", report, flags=re.DOTALL)
    assert fences, "no bash block found in the report; these guards would be vacuous"
    return fences


def _newest_fence_containing(marker: str) -> str:
    """The last fence that names *marker*, which is the copy a reader would take."""
    matching = [block for block in _report_bash_blocks() if marker in block]
    assert matching, f"no bash block in the report mentions {marker!r}"
    return matching[-1]


def test_the_capture_block_reads_readiness_before_it_captures() -> None:
    """The capture artifact reads the stack it measures, and its order is what makes that valid.

    The shape of the *run* changed on 2026-10-15 and this guard changed with it: the
    recovery restart is no longer part of the handed-off block, because the user has
    already performed it and repeating it would replace the state being measured with a
    new one. What the artifact must still do is read the stack it is about to measure -
    refresh the endpoints, probe every published port and the three adapter APIs - and
    reach the fresh capture **only** past that gate, with a failure propagating instead
    of being read as health. It must also start nothing at all.

    It is no longer the *last* block in the report - the handed-off artifact is now the
    focused live path - but it is kept there as the reproduction command for the
    baseline, so its guarantees are still pinned rather than dropped.
    """
    block = _newest_fence_containing("capture-baseline-checkout.py")

    assert "scripts/demo.sh endpoints" in block
    assert "scripts/demo.sh health" in block
    assert "capture-baseline-checkout.py" in block
    endpoints_at = block.index("scripts/demo.sh endpoints")
    health_at = block.index("scripts/demo.sh health")
    capture_at = block.index("capture-baseline-checkout.py")
    assert endpoints_at < health_at < capture_at
    # The probe's own status decides whether the capture runs, and the run's status is
    # the capture's: an ungated probe would let a stack that is not answering be
    # measured, and a swallowed status would report the block's exit as the tee's.
    gate = block[health_at:capture_at]
    assert "health_rc=${PIPESTATUS[0]}" in gate
    assert 'if [ "$health_rc" -ne 0 ]' in gate
    assert "exit 1" in gate
    assert "cap_rc=${PIPESTATUS[0]}" in block[capture_at:]
    assert 'exit "$cap_rc"' in block[capture_at:]
    # The endpoint settings are scoped to the subshell, and the project environment is
    # named explicitly rather than inherited from whatever shell the user is in.
    assert "set -a; . .local/demo-endpoints.env; set +a" in block
    assert 'UV_PROJECT_ENVIRONMENT="$PWD/.venv"' in block
    # It reads. It starts nothing: no database, no container, no scenario, no fault.
    for mutating in (
        "scripts/demo.sh start",
        "scripts/demo.sh ensure",
        "scripts/demo.sh restart",
        "scripts/demo.sh pull",
        "scripts/pg.sh start",
        "scripts/pg.sh ensure",
        "validate.sh --live",
        "run-live-scenarios",
    ):
        assert mutating not in block, mutating


def test_the_handed_off_block_delegates_to_the_live_runner_and_keeps_its_status() -> None:
    """The handed-off artifact runs live paths through the runner, and hands its status back.

    With the fault-free baseline verified, the next action moved from capturing to the
    live paths, and this guard follows the artifact the user is asked to run. The checks
    differ from the capture block's on purpose: a live scenario *does* start the stack and
    inject a fault, so what has to hold is that the block delegates that to the runner -
    a `--scenario` selection or the full set, the runner's own gates, the endpoint
    settings inside this shell only - and that the block's exit status is the runner's,
    not `tee`'s. It must not capture a baseline, and it must not restart the stack behind
    the runner's back. The scenario list is deliberately not pinned here: which paths
    remain is a scheduling decision, while the guarantees below are not.
    """
    block = _report_bash_blocks()[-1]

    assert re.search(r"scripts/validate\.sh --live( --scenario P1-\d\d)*\b", block)
    # The runner owns the stack and the fault; the block only reads health first.
    assert "scripts/demo.sh health" in block
    assert "capture-baseline-checkout.py" not in block
    # Explicit environment, and endpoint settings scoped to this subshell.
    assert 'UV_PROJECT_ENVIRONMENT="$PWD/.venv"' in block
    assert "source .venv/bin/activate" in block
    assert "set -a; . .local/demo-endpoints.env; set +a" in block
    # The runner's status is the block's status, and the failure survives the pipe.
    assert "rc=${PIPESTATUS[0]}" in block
    assert 'exit "$rc"' in block
    # Cleanup is read back from the flag store rather than assumed.
    assert "/api/read" in block
    for behind_its_back in (
        "scripts/demo.sh restart",
        "scripts/pg.sh start",
        "scripts/pg.sh ensure",
    ):
        assert behind_its_back not in block, behind_its_back
