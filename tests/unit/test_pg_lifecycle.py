"""The PostgreSQL helper's lifecycle refusals, in isolated fixtures.

On 2026-09-26 the live project cluster shut itself down with "performing immediate
shutdown because data directory lock file is invalid". The start attempt that caused
it came from a shell that could not open a socket: ``pg_ctl status`` decides liveness
by reading ``postmaster.pid`` and testing that PID with ``kill(0)``, which a shell in
another PID namespace cannot see. The live server therefore read as *stopped*, and the
next ``pg_ctl start`` replaced the lock file it owned.

These tests pin the refusals added for that failure. The distinction that matters is
the one a single failed probe cannot make: *nothing is listening* versus *this shell
cannot tell*, because a shell with no socket capability must never be the one to
start or stop a cluster.

Nothing here starts, stops or resets a real cluster. Every fixture is a temporary
directory with its own data dir, run dir and port, ``pg_ctl`` is a stub that records
its argv instead of running, and the only seam replaced in the script under test is
the socket attempt itself - the classification, refusals, locking and argv all come
from ``scripts/pg.sh``. Point ``PG_SCRIPT_UNDER_TEST`` at another copy of the script
to show these tests fail against the pre-fix version.
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from opsaccord.config import REPO_ROOT

#: The script under test. Overridable so a pre-fix copy can be run against the same
#: tests; the default is the real one.
PG_SCRIPT = Path(os.environ.get("PG_SCRIPT_UNDER_TEST") or REPO_ROOT / "scripts" / "pg.sh")
SEPARATOR = "\x1f"
BIN_RELATIVE = Path("root/usr/lib/postgresql/18/bin")

#: A PID that cannot be running, standing in for a server this shell's namespace
#: cannot see. From inside such a shell a live server and a stale pid file are
#: indistinguishable - which is exactly why the answer is a refusal, not a guess.
INVISIBLE_PID = "999999999"

HARNESS = r"""#!/usr/bin/env bash
# Drives one entry point of scripts/pg.sh against a fixture cluster.
#
# The only thing replaced is the socket attempt itself: a shell that cannot open any
# socket cannot be made to produce a *refused* connection, so on such a shell the two
# outcomes this script's safety rests on cannot otherwise be told apart. Everything
# above this function - the classification, the refusals, the flock, the argv handed
# to pg_ctl and the post-start verification - is the real code.
set -euo pipefail
source "$PG_SCRIPT_UNDER_TEST"

probe_connect() {
  : >"$PG_PROBE_LOG"
  case "${PG_PROBE:?PG_PROBE must name an outcome}" in
    reachable) return 0 ;;
    refused)
      printf '%s\n' "bash: connect: Connection refused" >&2
      return 1 ;;
    denied)
      printf '%s\n' "bash: connect: Operation not permitted" >&2
      return 1 ;;
    *)
      printf 'unhandled PG_PROBE=%s\n' "$PG_PROBE" >&2
      return 9 ;;
  esac
}

if [ "${1:-}" = "--call" ]; then
  shift
  "$@"
else
  pg_main "$@"
fi
"""

STUB = r"""#!/usr/bin/env bash
# Stands in for a PostgreSQL binary: records argv and answers as told. Nothing here
# can start, stop, or connect to a real cluster.
set -u
name="$(basename "$0")"
{
  printf '%s' "$name"
  for arg in "$@"; do printf '%s%s' "$(printf '\x1f')" "$arg"; done
  printf '\n'
} >>"$PG_ARGV_LOG"

last=""
for arg in "$@"; do last="$arg"; done

case "$name" in
  pg_ctl)
    case "$last" in
      status) exit "${STUB_PGCTL_STATUS_RC:-0}" ;;
      stop) exit "${STUB_PGCTL_STOP_RC:-0}" ;;
      start) exit "${STUB_PGCTL_START_RC:-0}" ;;
    esac
    exit 0 ;;
  psql)
    case "$*" in *-tAc*) printf '1\n' ;; esac
    exit "${STUB_PSQL_RC:-0}" ;;
  *) exit 0 ;;
esac
"""


@dataclass(frozen=True)
class Fixture:
    """One disposable cluster directory plus the harness that drives the script."""

    harness: Path
    env: dict[str, str]
    home: Path
    data: Path

    def run(self, *args: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(self.harness), *args],
            cwd=REPO_ROOT,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    def stub_calls(self, name: str) -> list[list[str]]:
        log = Path(self.env["PG_ARGV_LOG"])
        if not log.exists():
            return []
        return [
            line.split(SEPARATOR)
            for line in log.read_text(encoding="utf-8").splitlines()
            if line.startswith(f"{name}{SEPARATOR}")
        ]

    def probe_ran(self) -> bool:
        """True when the port probe was consulted at all."""
        return Path(self.env["PG_PROBE_LOG"]).exists()

    def write_pidfile(self, contents: str) -> Path:
        pidfile = self.data / "postmaster.pid"
        pidfile.write_text(contents, encoding="utf-8")
        return pidfile


def _fixture(tmp_path: Path, probe: str, **extra: str) -> Fixture:
    home = tmp_path / "pg"
    data = home / "data"
    run = home / "run"
    bindir = home / BIN_RELATIVE
    for directory in (data, run, bindir):
        directory.mkdir(parents=True, exist_ok=True)
    (data / "PG_VERSION").write_text("18\n", encoding="utf-8")
    for name in ("pg_ctl", "psql", "createdb", "initdb", "postgres"):
        stub = bindir / name
        stub.write_text(STUB, encoding="utf-8")
        stub.chmod(0o755)

    harness = tmp_path / "harness.sh"
    harness.write_text(HARNESS, encoding="utf-8")

    env = {key: value for key, value in os.environ.items() if not key.startswith("OPSACCORD_")}
    env.update(
        {
            "PG_SCRIPT_UNDER_TEST": str(PG_SCRIPT),
            "OPSACCORD_PG_HOME": str(home),
            "OPSACCORD_PG_DATA": str(data),
            "OPSACCORD_PG_RUN": str(run),
            # Never the project's own 55432: even the real probe, run by mistake,
            # must not be able to connect to the live cluster.
            "OPSACCORD_PG_PORT": "55439",
            "PG_PROBE": probe,
            "PG_PROBE_LOG": str(tmp_path / "probe.log"),
            "PG_ARGV_LOG": str(tmp_path / "argv.log"),
            **extra,
        }
    )
    instance = Fixture(harness=harness, env=env, home=home, data=data)
    # A guard against a later edit pointing these tests at the real cluster.
    assert Path(env["OPSACCORD_PG_DATA"]).is_relative_to(tmp_path)
    assert env["OPSACCORD_PG_PORT"] != "55432"
    return instance


def _wait_until_locked(lock: Path, timeout: float = 10.0) -> None:
    """Wait until someone holds `lock`, so the test races nothing."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        held = subprocess.run(["flock", "-n", str(lock), "true"], capture_output=True, check=False)
        if held.returncode != 0:
            return
        time.sleep(0.05)
    raise AssertionError(f"{lock} was never locked")


def test_a_server_this_shell_can_see_is_never_started_again(tmp_path: Path) -> None:
    """The live case. A visible PID settles liveness, before any probe."""
    fixture = _fixture(tmp_path, probe="denied")
    pidfile = fixture.write_pidfile(f"{os.getpid()}\n")
    before = pidfile.read_bytes()

    result = fixture.run("start")

    assert result.returncode == 0, result.stderr
    assert "already running" in result.stderr
    assert fixture.stub_calls("pg_ctl") == []
    assert pidfile.read_bytes() == before
    assert not fixture.probe_ran(), "a visible PID settles it; the port is left alone"


def test_a_shell_that_cannot_see_the_server_refuses_instead_of_starting(tmp_path: Path) -> None:
    """The demonstrated failure, as a refusal.

    The old code asked ``pg_ctl status``, which could not see the server, and called
    ``pg_ctl start`` against the live data directory. Here the same state must stop
    before touching the cluster - and leave the pid file exactly as it found it.
    """
    fixture = _fixture(tmp_path, probe="denied")
    pidfile = fixture.write_pidfile(f"{INVISIBLE_PID}\n")
    before = pidfile.read_bytes()

    result = fixture.run("start")

    assert result.returncode == 1
    assert "refusing to start" in result.stderr
    assert fixture.stub_calls("pg_ctl") == [], "no start against an unestablished cluster"
    assert pidfile.exists(), "a pid file this shell cannot interpret is not its to delete"
    assert pidfile.read_bytes() == before, "and not its to rewrite either"


def test_status_separates_stopped_from_unknown(tmp_path: Path) -> None:
    """Two different answers, two different exit codes: 3 stopped, 4 unknown."""
    stopped = _fixture(tmp_path / "stopped", probe="refused")
    nothing_listening = stopped.run("status")
    assert nothing_listening.returncode == 3
    assert "not running" in nothing_listening.stderr

    unknown = _fixture(tmp_path / "unknown", probe="denied")
    pidfile = unknown.write_pidfile(f"{INVISIBLE_PID}\n")
    cannot_tell = unknown.run("status")
    assert cannot_tell.returncode == 4
    assert "state unknown" in cannot_tell.stderr
    assert pidfile.exists()


def test_start_of_a_stopped_cluster_starts_once_and_verifies(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, probe="refused")

    result = fixture.run("start")

    assert result.returncode == 0, result.stderr
    starts = [argv for argv in fixture.stub_calls("pg_ctl") if argv[-1] == "start"]
    assert len(starts) == 1, fixture.stub_calls("pg_ctl")
    assert "-D" in starts[0] and str(fixture.data) in starts[0], "start targets the fixture only"
    assert [argv for argv in fixture.stub_calls("psql") if "SELECT 1" in argv], "no verification"
    assert not (fixture.data / "postmaster.pid").exists(), "the helper never fabricates a pid file"


def test_ensure_refuses_an_unestablished_cluster_without_attempting_a_start(
    tmp_path: Path,
) -> None:
    """``ensure`` must not become a second way to start a cluster this shell cannot see.

    The state-dependent block hands the decision to this one command, so the
    refusal it inherits from ``start``/``stop`` has to survive the new entry point:
    an unestablished cluster means no ``pg_ctl`` call of any kind, and the pid file
    exactly as it was found.
    """
    fixture = _fixture(tmp_path, probe="denied")
    pidfile = fixture.write_pidfile(f"{INVISIBLE_PID}\n")
    before = pidfile.read_bytes()

    result = fixture.run("ensure")

    assert result.returncode == 4, result.stderr
    assert "refusing to ensure" in result.stderr
    assert fixture.stub_calls("pg_ctl") == [], "no start, and no status call either"
    assert pidfile.read_bytes() == before


def test_ensure_starts_a_provably_stopped_cluster_exactly_once(tmp_path: Path) -> None:
    """The one state that authorises a start, and it authorises exactly one."""
    fixture = _fixture(tmp_path, probe="refused")

    result = fixture.run("ensure")

    assert result.returncode == 0, result.stderr
    starts = [argv for argv in fixture.stub_calls("pg_ctl") if argv[-1] == "start"]
    assert len(starts) == 1, fixture.stub_calls("pg_ctl")
    assert str(fixture.data) in starts[0], starts[0]
    assert not (fixture.data / "postmaster.pid").exists()


def test_ensure_verifies_a_running_cluster_and_restarts_nothing(tmp_path: Path) -> None:
    """A cluster this shell can see is *verified*, not restarted and not re-started."""
    fixture = _fixture(tmp_path, probe="reachable")

    result = fixture.run("ensure")

    assert result.returncode == 0, result.stderr
    assert "already running" in result.stderr
    touched = [argv for argv in fixture.stub_calls("pg_ctl") if argv[-1] in {"start", "stop"}]
    assert touched == [], touched
    assert fixture.stub_calls("psql"), "a running cluster is verified, not assumed"


def test_a_failed_start_and_a_failed_verification_are_both_propagated(tmp_path: Path) -> None:
    failed_start = _fixture(tmp_path / "start", probe="refused", STUB_PGCTL_START_RC="3")
    assert failed_start.run("start").returncode == 3

    failed_check = _fixture(tmp_path / "verify", probe="refused", STUB_PSQL_RC="1")
    result = failed_check.run("start")

    assert result.returncode == 1, "a successful pg_ctl start is not evidence of a usable cluster"
    assert [argv[0] for argv in failed_check.stub_calls("psql")], result.stderr
    order = [
        line[0] for line in (failed_check.stub_calls("pg_ctl") + failed_check.stub_calls("psql"))
    ]
    assert order == ["pg_ctl", "psql"], "the check must follow the start it is checking"


def test_a_second_start_is_refused_while_another_holds_the_lock(tmp_path: Path) -> None:
    """Two starts against one data directory is the same hazard as a start on a live one."""
    fixture = _fixture(tmp_path, probe="refused")
    lock = fixture.home / "start.lock"
    lock.touch()
    holder = subprocess.Popen(["flock", str(lock), "sleep", "30"])
    try:
        _wait_until_locked(lock)
        result = fixture.run("start")
    finally:
        holder.terminate()
        holder.wait(timeout=10)

    assert result.returncode == 1
    assert "already running for" in result.stderr
    assert [argv for argv in fixture.stub_calls("pg_ctl") if argv[-1] == "start"] == []


def test_stop_refuses_to_signal_a_server_it_cannot_identify(tmp_path: Path) -> None:
    """The port answers, but nothing in the data directory can be identified as the owner."""
    fixture = _fixture(tmp_path, probe="reachable", STUB_PGCTL_STATUS_RC="3")

    result = fixture.run("stop")

    assert result.returncode == 1
    assert "refusing to stop" in result.stderr
    assert [argv for argv in fixture.stub_calls("pg_ctl") if argv[-1] == "stop"] == []


def test_stop_does_nothing_when_stopped_and_targets_only_the_fixture_when_running(
    tmp_path: Path,
) -> None:
    stopped = _fixture(tmp_path / "stopped", probe="refused")
    harmless = stopped.run("stop")
    assert harmless.returncode == 0, harmless.stderr
    assert stopped.stub_calls("pg_ctl") == []

    running = _fixture(tmp_path / "running", probe="reachable")
    result = running.run("stop")
    assert result.returncode == 0, result.stderr
    calls = running.stub_calls("pg_ctl")
    assert [argv[-1] for argv in calls] == ["status", "stop"]
    assert str(running.data) in calls[1]
    assert "fast" in calls[1]


def test_reset_cannot_reach_its_delete_without_proving_the_cluster_is_stopped(
    tmp_path: Path,
) -> None:
    """``cmd_stop || true`` used to reach ``rm -rf`` even when the stop had refused."""
    unknown = _fixture(tmp_path / "unknown", probe="denied")
    canary = unknown.data / "canary"
    canary.write_text("kept\n", encoding="utf-8")

    refused = unknown.run("reset")

    assert refused.returncode == 1
    assert "refusing to stop" in refused.stderr
    assert canary.read_text(encoding="utf-8") == "kept\n", "the data directory is still there"

    unidentifiable = _fixture(
        tmp_path / "unidentifiable", probe="reachable", STUB_PGCTL_STATUS_RC="3"
    )
    also_kept = unidentifiable.data / "canary"
    also_kept.write_text("kept\n", encoding="utf-8")
    assert unidentifiable.run("reset").returncode == 1
    assert also_kept.exists()


@pytest.mark.parametrize(
    ("probe", "expected"),
    [("reachable", 0), ("refused", 1), ("denied", 2)],
)
def test_only_a_refused_connection_counts_as_nothing_listening(
    tmp_path: Path, probe: str, expected: int
) -> None:
    """The whole refusal rests on this: a probe that cannot probe is not a "stopped"."""
    fixture = _fixture(tmp_path, probe=probe)

    result = fixture.run("--call", "port_reachable")

    assert fixture.probe_ran(), "the classification must have consulted the probe"
    assert result.returncode == expected, result.stderr


def test_the_script_dispatches_when_executed_and_not_when_sourced(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, probe="reachable")

    usage = subprocess.run(
        ["bash", str(PG_SCRIPT)],
        cwd=REPO_ROOT,
        env=fixture.env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert usage.returncode == 1
    assert "usage:" in usage.stderr

    # A shell that sources the file has its own $0; only a direct execution has the
    # script's own path there, which is the whole basis of the guard.
    sourced = subprocess.run(
        ["bash", "-c", 'source "$1" && echo sourced-ok', "sourcing-shell", str(PG_SCRIPT)],
        cwd=REPO_ROOT,
        env=fixture.env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert sourced.returncode == 0, sourced.stderr
    assert "sourced-ok" in sourced.stdout
    assert "usage:" not in sourced.stderr
