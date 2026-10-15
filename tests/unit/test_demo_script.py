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

DEMO_SCRIPT = REPO_ROOT / "scripts" / "demo.sh"
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
    printf '%s\\n' "product-catalog" "checkout" ;;
  inspect)
    if [ -n "${STUB_EATS_STDIN:-}" ]; then cat >/dev/null; fi
    if [ -n "${STUB_INSPECT_RC:-}" ]; then exit "$STUB_INSPECT_RC"; fi
    # `docker inspect` names a container with a leading slash; the helper is
    # expected to strip it, so the stub supplies it.
    container="$3"
    case "$*" in
      *HostConfig*)
        printf '/%s state=running health=unhealthy restarts=0 exit=0 ' "$container"
        printf 'oom_killed=false memory_limit=20971520 nano_cpus=0 '
        printf 'started=2026-09-25T11:23:30Z healthcheck=[]\\n' ;;
      *Health.Log*)
        printf '[{"ExitCode":1,"Output":"failed to connect service :3550 '
        printf 'within 1s"}]\\n' ;;
      *GOMEMLIMIT*) printf 'GOMEMLIMIT=16MiB\\n' ;;
      # `demo.sh ensure` reads each container's own state plus its compose
      # *service* label, which is what it hands to `compose up`: upstream sets
      # `container_name:` on several services, so the two are not the same string.
      *State.Status*)
        state=running; health=healthy
        case " ${STUB_STOPPED:-} " in *" $container "*) state=exited; health=none ;; esac
        case " ${STUB_UNHEALTHY:-} " in *" $container "*) health=unhealthy ;; esac
        printf '/%s %s %s %s\\n' "$container" "$state" "$health" "$container" ;;
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
    """A bin directory whose ``curl`` and ``timeout`` always succeed.

    ``demo.sh health`` decides its verdict from two external commands: ``curl`` for
    the HTTP checks, and ``timeout ... /dev/tcp`` for the TCP ones. This sandbox
    denies loopback sockets outright, so the real ones cannot answer here - and what
    is under test is the helper's *decision* (was every container already up? which
    services did it start?), not the probe itself. Replacing the probe is the same
    seam the docker stub already uses; nothing about Docker or compose changes.
    """
    bin_dir = tmp_path / "probe-bin"
    bin_dir.mkdir(exist_ok=True)
    for name in ("curl", "timeout"):
        stub = bin_dir / name
        stub.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        stub.chmod(0o755)
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
    """
    result = _run(tmp_path, "ensure", **_probed(tmp_path, STUB_UNHEALTHY="checkout"))

    assert result.returncode == 1
    assert "running but not healthy" in result.stderr
    # The probe's own message, and a bounded log tail, for the container in question.
    assert "failed to connect service :3550" in result.stderr
    assert "logs --tail 80 checkout" in result.stderr
    assert "STOP" in result.stderr
    # Nothing was started, recreated, stopped or restarted.
    assert _compose_up_args(tmp_path) == []
    mutating = [
        argv for argv in _invocations(tmp_path, "compose") if {"down", "restart"} & set(argv)
    ]
    assert mutating == [], mutating
