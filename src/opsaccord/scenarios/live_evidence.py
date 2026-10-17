"""Progress and evidence for a live run.

A live run takes minutes and writes real feature flags, so two properties matter
here more than anywhere else in the codebase: the operator can see what is
happening *while* it happens, and an interrupted run leaves evidence that says
exactly how far it got.

Both live in this module, apart from the driver:

* :class:`LiveProgress` writes one flushed, timestamped line per event. A live run
  is normally piped through ``tee``, and Python buffers a piped stream, so an
  unflushed line is the difference between visible progress and a command that
  looks hung.
* :class:`LiveEvidenceWriter` rewrites the whole evidence document after every
  event through a temporary file and ``os.replace``, so whatever is on disk is a
  complete, well-formed document. An interrupted run leaves the completed records
  plus an explicit interrupted record for the active scenario, never a
  half-written array that a reader could mistake for a finished run.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

#: Evidence format written here. Version 1 was a bare JSON array of per-scenario
#: records, which could not say whether the run it described had finished.
#: Version 2 is an object that carries the run's own status, so a partial run is
#: never mistaken for a complete one.
EVIDENCE_FORMAT = "opsaccord.live-evidence"
EVIDENCE_FORMAT_VERSION = 2

RUNNING = "RUNNING"
COMPLETED = "COMPLETED"
STOPPED = "STOPPED"
INTERRUPTED = "INTERRUPTED"
MEASURED = "MEASURED"
NOT_MEASURED = "NOT_MEASURED"


def utc_now() -> datetime:
    return datetime.now(UTC)


def utc_stamp(moment: datetime) -> str:
    """Second-resolution UTC: sorts lexically, stays readable in a log."""
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def atomic_write_json(path: Path, payload: Any) -> None:
    """Write ``payload`` so a reader never sees a partial document.

    The temporary file is written beside the target and then replaced, which is
    atomic on one filesystem. A crash mid-write leaves the previous document
    intact instead of a truncated one.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


class LiveProgress:
    """Flushed, timestamped progress for one live run.

    Every line names the scenario and its position in the suite, the stage, how
    long the whole scenario has been running, how much of the current stage's
    budget is left, and -- while polling -- what the harness last observed. The
    stamp and the elapsed times come from injected clocks so a test can pin them.
    """

    def __init__(
        self,
        *,
        stream: IO[str] | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._stream = stream
        self._clock = clock
        self._wall_clock = wall_clock
        self._on_line: Callable[[], None] | None = None
        #: Reporting failures that must not stop the run. Carried out on the run so
        #: the command can fail at the end instead of aborting while a fault is armed.
        self.errors: list[str] = []
        self._reported: set[str] = set()
        self.scenario_id = "-"
        self.position = 0
        self.total = 0
        self.setup = ""
        self.stage = "start"
        self.stage_budget_seconds: float | None = None
        self.scenario_started_at: str | None = None
        self.last_observation: str | None = None
        self.body_stage: str | None = None
        self.body_observation: str | None = None
        self._scenario_started = clock()
        self._stage_started = clock()

    def listen(self, callback: Callable[[], None]) -> None:
        """Run ``callback`` after every emitted line.

        The suite uses this to rewrite the evidence document, so progress and
        evidence can never disagree about how far the run got.
        """
        self._on_line = callback

    @property
    def active(self) -> bool:
        return self.scenario_id != "-"

    def begin_scenario(
        self, scenario_id: str, position: int, total: int, *, setup: str = ""
    ) -> None:
        now = self._clock()
        self.scenario_id = scenario_id
        self.position = position
        self.total = total
        self.setup = setup
        self.stage = "start"
        self.stage_budget_seconds = None
        self.scenario_started_at = utc_stamp(self._wall_clock())
        self.last_observation = None
        self._scenario_started = now
        self._stage_started = now
        self.emit(f"scenario {scenario_id} ({position}/{total}): {setup}")

    def finish_scenario(self, outcome: object, *, stage: str = "complete") -> None:
        self.stage = stage
        self.emit(f"outcome {outcome} for {self.scenario_id}")
        self.clear()

    def enter_stage(
        self, stage: str, *, budget_seconds: float | None = None, message: str = ""
    ) -> None:
        self.stage = stage
        self.stage_budget_seconds = budget_seconds
        self._stage_started = self._clock()
        suffix = f" (stage budget {budget_seconds:.0f}s)" if budget_seconds is not None else ""
        self.emit(f"{message}{suffix}")

    def observation(self, message: str) -> None:
        """Record and report the latest thing the harness saw."""
        self.last_observation = message
        self.emit(message)

    def mark_body_finished(self, stage: str) -> None:
        """Remember the stage the scenario body ended in.

        Cleanup always runs afterwards -- including for an interrupted scenario --
        so without this the stage a failure or an interruption happened in would be
        overwritten by "cleanup" before anything recorded it.
        """
        self.body_stage = stage
        self.body_observation = self.last_observation

    def notice(self, message: str) -> None:
        """A suite-level message that is not an observation of a probe."""
        self.emit(message)

    def emit(self, message: str) -> None:
        line = (
            f"{utc_stamp(self._wall_clock())} live {self.scenario_id} "
            f"{self.position}/{self.total} [{self.stage}] {message}{self._timing()}"
        )
        if self._stream is not None:
            try:
                self._stream.write(f"{line}\n")
                self._stream.flush()
            except (OSError, ValueError) as exc:
                # A closed or broken pipe costs the operator one line. It must not
                # abort a run that has a fault armed.
                self.fail(f"progress could not be written: {type(exc).__name__}: {exc}")
        if self._on_line is not None:
            try:
                self._on_line()
            except Exception as exc:
                self.fail(
                    f"the evidence document could not be updated: {type(exc).__name__}: {exc}"
                )

    def fail(self, message: str) -> None:
        """Record a reporting failure without letting it stop the run.

        Progress and the evidence document are *reporting*. Neither may gate the
        cleanup that restores an armed fault, so a failure here is recorded once,
        echoed to stderr, and carried out on the run, where the command turns it
        into a non-zero exit instead of a silent gap.
        """
        if message in self._reported:
            return
        self._reported.add(message)
        self.errors.append(message)
        print(f"!! {message}", file=sys.stderr)

    def _timing(self) -> str:
        now = self._clock()
        parts = [f"scenario_elapsed={now - self._scenario_started:.0f}s"]
        if self.stage_budget_seconds is not None:
            stage_elapsed = now - self._stage_started
            parts.append(f"stage_elapsed={stage_elapsed:.0f}s")
            remaining = max(0.0, self.stage_budget_seconds - stage_elapsed)
            parts.append(f"stage_remaining={remaining:.0f}s")
        return " | " + " ".join(parts)

    def active_snapshot(self, *, status: str = RUNNING) -> dict[str, Any] | None:
        """The explicit record for a scenario that has not produced a result yet."""
        if not self.active:
            return None
        return {
            "scenario_id": self.scenario_id,
            "position": self.position,
            "total": self.total,
            "setup": self.setup,
            "stage": self.body_stage or self.stage,
            "started_at": self.scenario_started_at,
            "elapsed_seconds": round(self._clock() - self._scenario_started, 1),
            "stage_budget_seconds": self.stage_budget_seconds,
            "last_observation": self.body_observation or self.last_observation,
            "status": status,
        }

    def clear(self) -> None:
        self.scenario_id = "-"
        self.position = 0
        self.total = 0
        self.setup = ""
        self.stage = "start"
        self.stage_budget_seconds = None
        self.scenario_started_at = None
        self.last_observation = None
        self.body_stage = None
        self.body_observation = None


class LiveEvidenceWriter:
    """Incremental evidence for a live run, rewritten atomically after each event."""

    def __init__(
        self,
        path: Path | str | None,
        *,
        selected: Sequence[str],
        started_at: str,
        revision: str | None = None,
    ) -> None:
        self._path = Path(path) if path is not None else None
        self._selected = list(selected)
        self._started_at = started_at
        self._revision = revision

    @property
    def path(self) -> Path | None:
        return self._path

    def write(
        self,
        *,
        status: str,
        records: Sequence[dict[str, Any]],
        notes: Sequence[str] = (),
        active: dict[str, Any] | None = None,
        pending: Sequence[str] = (),
    ) -> dict[str, Any]:
        # ``completed`` counts records and ``measured`` counts scenarios that
        # produced a measurement. They differ for a scenario that was recorded as
        # not measured, which is exactly the distinction a reader needs.
        measured = sum(1 for record in records if record.get("status") == MEASURED)
        document: dict[str, Any] = {
            "format": EVIDENCE_FORMAT,
            "format_version": EVIDENCE_FORMAT_VERSION,
            "run": {
                "started_at": self._started_at,
                "updated_at": utc_stamp(utc_now()),
                "status": status,
                "selected": self._selected,
                "completed": len(records),
                "measured": measured,
                "pending": list(pending),
                "pinned_revision": self._revision,
            },
            "active": active,
            "records": list(records),
            "notes": list(notes),
        }
        if self._path is not None:
            atomic_write_json(self._path, document)
        return document


__all__ = [
    "COMPLETED",
    "EVIDENCE_FORMAT",
    "EVIDENCE_FORMAT_VERSION",
    "INTERRUPTED",
    "MEASURED",
    "NOT_MEASURED",
    "RUNNING",
    "STOPPED",
    "LiveEvidenceWriter",
    "LiveProgress",
    "atomic_write_json",
    "utc_now",
    "utc_stamp",
]
