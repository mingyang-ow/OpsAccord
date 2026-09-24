"""Focused tests for the live scenario lifecycle.

These tests never touch the demo stack: the flag document and the health probes
are fakes, the controller is stubbed, and every stage timeout is short. What they
pin is the part a live run cannot demonstrate safely:

* cleanup happens on *every* exit path, including a failure after the fault was
  armed and Ctrl+C while polling;
* an interrupted run says so instead of recording a result it never reached;
* a scenario that cannot restore the baseline stops the suite;
* the Payment fault is proved by evidence the fault can really produce, and a
  quiet demo is never mistaken for a fault.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from opsaccord.scenarios import live as live_scenarios
from opsaccord.scenarios.live_evidence import LiveProgress
from opsaccord.tools.live_environment import LiveEnvironmentUnavailable
from support.live_fakes import (
    UNRELATED_FLAG,
    UNUSED_DB,
    FakeFlagStore,
    FakeStack,
    StubController,
    health,
    live_settings,
    stack_health,
    use_fake_stack,
)


def test_a_failure_before_injection_is_recorded_and_arms_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The demo never carried traffic, so nothing was injected and nothing scored."""
    store = FakeFlagStore()
    stack = use_fake_stack(monkeypatch, FakeStack(store, stack_health()))

    run = live_scenarios.run_live_suite(
        db=UNUSED_DB,
        settings=live_settings(),
        scenario_ids=["P1-01"],
        scenario_budget_seconds=0.3,
        poll_seconds=0.01,
    )

    assert stack.writes == []
    (result,) = run.results
    assert result.status == "NOT_MEASURED"
    assert result.stage == "traffic-wait"
    assert "saw no traffic" in " ".join(result.reasons)
    assert result.cleanup is not None and "no fault was armed" in result.cleanup
    # The suite itself finished: it ran every scenario it selected, and one of them
    # produced no measurement. Those are different statements on purpose.
    assert run.status == "COMPLETED"


def test_a_failure_after_injection_restores_the_fault(monkeypatch: pytest.MonkeyPatch) -> None:
    """Traffic existed, the fault was armed, and its visibility never appeared."""
    store = FakeFlagStore()
    probes = stack_health(**{"payment-service": health(calls=0.2)})
    use_fake_stack(monkeypatch, FakeStack(store, probes))

    run = live_scenarios.run_live_suite(
        db=UNUSED_DB,
        settings=live_settings(),
        scenario_ids=["P1-01"],
        scenario_budget_seconds=0.3,
        poll_seconds=0.01,
    )

    (result,) = run.results
    assert result.status == "NOT_MEASURED"
    assert result.stage == "fault-visibility"
    assert "never showed the armed fault" in " ".join(result.reasons)
    # Armed, then restored: an injected fault is never left behind by a failure
    # that happens after the injection.
    assert store.writes == [("paymentUnreachable", "on"), ("paymentUnreachable", "off")]
    assert store.variants["paymentUnreachable"] == "off"
    assert result.cleanup is not None and "restored to 'off'" in result.cleanup


def test_a_workflow_exception_is_recorded_and_stops_the_suite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exception from the shared controller is not repeated once per scenario."""
    store = FakeFlagStore()
    probes = stack_health(**{"payment-service": health(calls=0.2, error_rate=0.4)})
    use_fake_stack(
        monkeypatch,
        FakeStack(store, probes),
        StubController(error=RuntimeError("the controller fell over")),
    )

    run = live_scenarios.run_live_suite(
        db=UNUSED_DB,
        settings=live_settings(),
        scenario_ids=["P1-01", "P1-02"],
        scenario_budget_seconds=0.3,
        poll_seconds=0.01,
    )

    assert run.status == "STOPPED"
    assert [result.scenario_id for result in run.results] == ["P1-01", "P1-02"]
    first, second = run.results
    assert first.status == "NOT_MEASURED"
    assert first.stage == "workflow"
    assert "RuntimeError: the controller fell over" in " ".join(first.reasons)
    assert second.stage == "suite"
    assert ("adHighCpu", "on") not in store.writes
    assert store.writes == [("paymentUnreachable", "on"), ("paymentUnreachable", "off")]


def test_ctrl_c_while_polling_cleans_up_and_is_never_a_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Ctrl+C during a poll restores the fault, records the run, and propagates."""
    store = FakeFlagStore()
    probes = stack_health(**{"payment-service": health(calls=0.2)})
    stack = FakeStack(store, probes, interrupt_on="payment-service", interrupt_after=2)
    use_fake_stack(monkeypatch, stack)
    evidence = tmp_path / "live-evidence.json"

    with pytest.raises(KeyboardInterrupt):
        live_scenarios.run_live_suite(
            db=UNUSED_DB,
            settings=live_settings(),
            scenario_ids=["P1-01"],
            evidence_path=evidence,
            scenario_budget_seconds=1.0,
            poll_seconds=0.01,
        )

    assert store.writes == [("paymentUnreachable", "on"), ("paymentUnreachable", "off")]
    document = json.loads(evidence.read_text(encoding="utf-8"))
    assert document["run"]["status"] == "INTERRUPTED"
    assert document["records"] == []
    assert document["active"]["scenario_id"] == "P1-01"
    assert document["active"]["stage"] == "fault-visibility"
    assert document["active"]["status"] == "INTERRUPTED"
    assert document["run"]["pending"] == ["P1-01"]


def test_ctrl_c_during_the_startup_baseline_is_still_an_interrupted_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Interrupted before the first scenario: the file must not read as RUNNING.

    An operator who interrupts the baseline check and then reads the evidence file
    has to be able to tell a finished process from a running one.
    """
    stack = FakeStack(FakeFlagStore(), stack_health(), interrupt_on_flag_read=0)
    use_fake_stack(monkeypatch, stack)
    evidence = tmp_path / "live-evidence.json"

    with pytest.raises(KeyboardInterrupt):
        live_scenarios.run_live_suite(
            db=UNUSED_DB,
            settings=live_settings(),
            scenario_ids=["P1-01"],
            evidence_path=evidence,
            scenario_budget_seconds=0.3,
            poll_seconds=0.01,
        )

    document = json.loads(evidence.read_text(encoding="utf-8"))
    assert document["run"]["status"] == "INTERRUPTED"
    assert document["records"] == []
    assert document["active"] is None
    assert document["run"]["pending"] == ["P1-01"]
    # Nothing was injected before the interrupt, so nothing was left armed.
    assert stack.writes == []
    assert any(
        "interrupted during the startup baseline check" in note for note in document["notes"]
    )


def test_a_cleanup_failure_stops_the_suite_but_keeps_the_measurement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A measured scenario is not discarded because the demo could not be cleaned."""
    store = FakeFlagStore(refuse={"off"})
    probes = stack_health(**{"payment-service": health(calls=0.2, error_rate=0.4)})
    use_fake_stack(monkeypatch, FakeStack(store, probes), StubController())

    run = live_scenarios.run_live_suite(
        db=UNUSED_DB,
        settings=live_settings(),
        scenario_ids=["P1-01", "P1-02"],
        scenario_budget_seconds=0.3,
        poll_seconds=0.01,
    )

    assert run.status == "STOPPED"
    assert [result.scenario_id for result in run.results] == ["P1-01", "P1-02"]
    first, second = run.results
    assert first.status == "MEASURED"
    assert first.cleanup is not None and "could not be restored" in first.cleanup
    assert any("run stopped" in reason for reason in first.reasons)
    assert second.stage == "suite"
    # The demo is honestly dirty, and the suite did not pretend otherwise.
    assert store.variants["paymentUnreachable"] == "on"


def test_a_cleanup_failure_keeps_the_original_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both failures survive: the setup failure that happened, and the cleanup one."""
    store = FakeFlagStore(refuse={"off"})
    probes = stack_health(**{"payment-service": health(calls=0.2)})
    use_fake_stack(monkeypatch, FakeStack(store, probes))

    run = live_scenarios.run_live_suite(
        db=UNUSED_DB,
        settings=live_settings(),
        scenario_ids=["P1-01"],
        scenario_budget_seconds=0.3,
        poll_seconds=0.01,
    )

    (result,) = run.results
    text = " ".join(result.reasons)
    assert result.status == "NOT_MEASURED"
    assert "never showed the armed fault" in text
    assert "could not be restored" in text
    assert run.status == "STOPPED"


def test_restoring_a_fault_touches_only_what_the_scenario_owns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A residual fault is repaired, this scenario's fault is restored, nothing else is written.

    Driven through one scenario rather than the suite, so the repair under test is
    the per-scenario baseline check and not the suite's startup repair.
    """
    store = FakeFlagStore({"adFailure": "on"})
    probes = stack_health(**{"payment-service": health(calls=0.2, error_rate=0.4)})
    use_fake_stack(monkeypatch, FakeStack(store, probes), StubController())

    result = live_scenarios.run_live_scenario(
        live_scenarios.SCENARIOS["P1-01"],
        live_scenarios.LIVE_PLANS["P1-01"],
        db=UNUSED_DB,
        settings=live_settings(),
        budget_seconds=0.3,
        poll_seconds=0.01,
    )

    assert result.status == "MEASURED"
    assert store.writes == [
        ("adFailure", "off"),  # residual from an earlier run, repaired before this one
        ("paymentUnreachable", "on"),  # this scenario's own fault
        ("paymentUnreachable", "off"),  # ...and restored after it
    ]
    assert store.unrelated[UNRELATED_FLAG]["defaultVariant"] == "off"
    assert UNRELATED_FLAG not in [flag_id for flag_id, _ in store.writes]
    assert any("baseline repaired" in reason for reason in result.reasons)


def test_residual_faults_are_repaired_before_the_first_scenario(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fault left armed by an earlier run is cleared at startup and recorded."""
    store = FakeFlagStore({"paymentUnreachable": "on", "adHighCpu": "on"})
    probes = stack_health(**{"payment-service": health(calls=0.2, error_rate=0.4)})
    use_fake_stack(monkeypatch, FakeStack(store, probes), StubController())

    run = live_scenarios.run_live_suite(
        db=UNUSED_DB,
        settings=live_settings(),
        scenario_ids=["P1-01"],
        scenario_budget_seconds=0.3,
        poll_seconds=0.01,
    )

    # Repair first (in document order), then this scenario's own arm-and-restore.
    assert store.writes[:2] == [("adHighCpu", "off"), ("paymentUnreachable", "off")]
    assert store.writes[2:] == [("paymentUnreachable", "on"), ("paymentUnreachable", "off")]
    assert any("residual scenario faults at startup" in note for note in run.notes)
    assert run.status == "COMPLETED"


def test_an_empty_flag_document_is_not_read_as_a_clean_baseline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Absence is not safety: a document without the flags is not a safe baseline."""
    store = FakeFlagStore()
    store.variants = {}
    stack = use_fake_stack(monkeypatch, FakeStack(store, stack_health()))

    with pytest.raises(live_scenarios.LiveBaselineUnavailable) as excinfo:
        live_scenarios.detect_residual_faults(stack)
    assert "baseline cannot be established" in str(excinfo.value)

    # And through the suite: nothing is measured, no scenario claims a clean
    # baseline, and the run stops instead of reading absence as safety.
    run = live_scenarios.run_live_suite(
        db=UNUSED_DB,
        settings=live_settings(),
        scenario_ids=["P1-01"],
        scenario_budget_seconds=0.3,
        poll_seconds=0.01,
    )

    assert run.status == "STOPPED"
    (result,) = run.results
    assert result.stage == "baseline"
    assert "baseline cannot be established" in " ".join(result.reasons)
    assert not any("baseline clean" in note for note in run.notes)
    assert store.writes == []


class FlushRecorder(io.StringIO):
    """A stream that records the text and the persisted record count at each flush."""

    def __init__(self, evidence: Path) -> None:
        super().__init__()
        self.flushes: list[tuple[str, int | None]] = []
        self._evidence = evidence

    def flush(self) -> None:
        completed: int | None = None
        if self._evidence.exists():
            completed = json.loads(self._evidence.read_text(encoding="utf-8"))["run"]["completed"]
        self.flushes.append((self.getvalue(), completed))
        super().flush()


def test_progress_is_flushed_while_the_run_is_still_going(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Progress reaches a piped reader during the run, not when the process exits."""
    store = FakeFlagStore()
    use_fake_stack(monkeypatch, FakeStack(store, stack_health()))
    evidence = tmp_path / "live-evidence.json"
    stream = FlushRecorder(evidence)

    run = live_scenarios.run_live_suite(
        db=UNUSED_DB,
        settings=live_settings(),
        scenario_ids=["P1-01", "P1-02"],
        progress=LiveProgress(stream=stream),
        evidence_path=evidence,
        scenario_budget_seconds=0.2,
        poll_seconds=0.01,
    )

    first_lines = [
        (text, completed)
        for text, completed in stream.flushes
        if "P1-01" in text and "[traffic-wait]" in text
    ]
    assert first_lines, stream.getvalue()
    # The line describing the first stage was flushed while no scenario had been
    # recorded yet, which is the whole point of writing incrementally.
    assert any(completed == 0 for _, completed in first_lines)
    first_p1_01 = next(index for index, (text, _) in enumerate(stream.flushes) if "P1-01" in text)
    first_p1_02 = next(index for index, (text, _) in enumerate(stream.flushes) if "P1-02" in text)
    assert first_p1_01 < first_p1_02
    document = json.loads(evidence.read_text(encoding="utf-8"))
    assert document["run"]["completed"] == 2
    assert len(run.results) == 2


def test_an_interrupted_run_keeps_what_completed_and_records_what_did_not(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Partial evidence survives: one measured record, one explicit interruption."""
    store = FakeFlagStore()
    probes = stack_health(**{"payment-service": health(calls=0.2, error_rate=0.4)})
    stack = FakeStack(store, probes, interrupt_on="ad-service", interrupt_after=0)
    use_fake_stack(monkeypatch, stack, StubController())
    evidence = tmp_path / "live-evidence.json"

    with pytest.raises(KeyboardInterrupt):
        live_scenarios.run_live_suite(
            db=UNUSED_DB,
            settings=live_settings(),
            scenario_ids=["P1-01", "P1-02"],
            evidence_path=evidence,
            scenario_budget_seconds=0.3,
            poll_seconds=0.01,
        )

    document = json.loads(evidence.read_text(encoding="utf-8"))
    assert document["run"]["status"] == "INTERRUPTED"
    assert [record["scenario_id"] for record in document["records"]] == ["P1-01"]
    assert document["records"][0]["status"] == "MEASURED"
    assert document["records"][0]["final_status"] == "ESCALATED_INCOMPLETE"
    assert document["active"]["scenario_id"] == "P1-02"
    assert document["active"]["stage"] == "traffic-wait"
    assert document["active"]["status"] == "INTERRUPTED"
    assert document["run"]["pending"] == ["P1-02"]
    assert store.variants["paymentUnreachable"] == "off"


def test_unreachable_payment_visibility_uses_the_callers_failing_rpcs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The connection failure removes the payment series; the caller still proves it."""
    probes = stack_health(
        **{
            "payment-service": health(calls=0.0, error_rate=None),
            "checkout-service": health(calls=1.0, error_rate=0.75),
        }
    )
    stack = FakeStack(FakeFlagStore(), probes)

    probe = live_scenarios.wait_for_fault(
        stack,
        target="payment-service",
        corroborating_target="checkout-service",
        timeout_seconds=0.2,
        poll_seconds=0.01,
    )

    assert probe["attribution"] == "caller"
    assert probe["corroborating"]["target"] == "checkout-service"
    # Zero traffic on the payment target is what the fault *does*; it is recorded,
    # and it is explicitly not the signal that decided anything.
    assert probe["probe"]["calls_per_second"] == 0.0
    assert live_scenarios._fault_visible("error_rate", probe["probe"]) is False
    assert "checkout-service" in live_scenarios._fault_reason(probe)


def test_a_quiet_payment_path_is_a_setup_failure_not_a_fault(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No calls and no errors on either target: no fault is claimed."""
    stack = FakeStack(FakeFlagStore(), stack_health())

    with pytest.raises(live_scenarios.LiveSetupFailed) as excinfo:
        live_scenarios.wait_for_fault(
            stack,
            target="payment-service",
            corroborating_target="checkout-service",
            timeout_seconds=0.2,
            poll_seconds=0.01,
        )

    assert "never showed the armed fault" in str(excinfo.value)
    assert "'error_rate': None" in str(excinfo.value)


def test_traffic_without_errors_is_not_a_fault(monkeypatch: pytest.MonkeyPatch) -> None:
    """A caller with traffic and a clean error share keeps the harness waiting."""
    stack = FakeStack(
        FakeFlagStore(),
        stack_health(
            **{
                "payment-service": health(calls=0.2, error_rate=0.0),
                "checkout-service": health(calls=3.0, error_rate=0.0),
            }
        ),
    )

    with pytest.raises(live_scenarios.LiveSetupFailed):
        live_scenarios.wait_for_fault(
            stack,
            target="payment-service",
            corroborating_target="checkout-service",
            timeout_seconds=0.2,
            poll_seconds=0.01,
        )

    # ...while the payment target's own failing RPCs still prove the fault on their
    # own, which is the attribution the report has to keep.
    proving = FakeStack(
        FakeFlagStore(),
        stack_health(**{"payment-service": health(calls=0.05, error_rate=0.33)}),
    )
    probe = live_scenarios.wait_for_fault(
        proving,
        target="payment-service",
        corroborating_target="checkout-service",
        timeout_seconds=0.2,
        poll_seconds=0.01,
    )
    assert probe["attribution"] == "target"
    assert probe["corroborating"] is None or probe["corroborating"]["probe"]["error_rate"] is None


def test_a_simulated_surface_is_refused_by_the_driver() -> None:
    """The flag API is not enough: a live plan may never write through a simulator."""
    with pytest.raises(LiveEnvironmentUnavailable):
        live_scenarios.arm_fault(object(), flag_id="paymentUnreachable", variant="on")

    # A surface with the right API but no live provenance is refused too, which is
    # the check that matters: a simulated write is a no-op that would let a
    # scenario "measure" a fault that was never injected.
    simulated = FakeStack(FakeFlagStore())
    simulated.is_simulated = True
    with pytest.raises(LiveEnvironmentUnavailable):
        live_scenarios.arm_fault(simulated, flag_id="paymentUnreachable", variant="on")
    assert simulated.writes == []

    # ...while an adversarial wrapper around a live surface is unwrapped, so the
    # write goes to the real flag document rather than to the wrapper.
    live = FakeStack(FakeFlagStore())
    assert live_scenarios._as_live(_LiveWrapper(live)) is live


class _LiveWrapper:
    """The shape of the adversarial surfaces: live, delegating to a real one."""

    is_simulated = False

    def __init__(self, inner: FakeStack) -> None:
        self._environment = inner

    def __getattr__(self, name: str) -> object:
        return getattr(self._environment, name)


def test_an_unacknowledged_arm_write_is_restored_defensively(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The write landed but its answer did not, so the fault is still restored."""
    store = FakeFlagStore(apply_then_fail={"on"})
    probes = stack_health(**{"payment-service": health(calls=0.2, error_rate=0.4)})
    use_fake_stack(monkeypatch, FakeStack(store, probes))

    run = live_scenarios.run_live_suite(
        db=UNUSED_DB,
        settings=live_settings(),
        scenario_ids=["P1-01"],
        scenario_budget_seconds=0.3,
        poll_seconds=0.01,
    )

    (result,) = run.results
    assert result.status == "NOT_MEASURED"
    assert result.stage == "fault-injection"
    assert "DispatchTimeout" in " ".join(result.reasons)
    # Applied, unacknowledged, and restored anyway: the demo is not left carrying a
    # fault that the record would claim was never armed.
    assert store.writes == [("paymentUnreachable", "on"), ("paymentUnreachable", "off")]
    assert store.variants["paymentUnreachable"] == "off"
    assert result.cleanup is not None and "never acknowledged" in result.cleanup
    assert run.status == "STOPPED"


def test_an_unexpected_failure_is_recorded_in_the_stage_it_happened(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refused flag read mid-lifecycle is not reported as a workflow failure."""
    store = FakeFlagStore()
    probes = stack_health(**{"payment-service": health(calls=0.2, error_rate=0.4)})
    # Read 1 is the suite's startup baseline; read 2 is this scenario's baseline.
    use_fake_stack(monkeypatch, FakeStack(store, probes, flag_read_error_after=1), StubController())

    run = live_scenarios.run_live_suite(
        db=UNUSED_DB,
        settings=live_settings(),
        scenario_ids=["P1-01"],
        scenario_budget_seconds=0.3,
        poll_seconds=0.01,
    )

    (result,) = run.results
    assert result.stage == "baseline"
    assert "NOT MEASURED at the baseline stage" in " ".join(result.reasons)
    assert "DispatchRefused" in " ".join(result.reasons)
    assert store.writes == []
    assert run.status == "STOPPED"


def test_an_evidence_write_failure_never_skips_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A reporting failure cannot leave a fault armed or lose the measurement."""
    store = FakeFlagStore()
    probes = stack_health(**{"payment-service": health(calls=0.2, error_rate=0.4)})
    use_fake_stack(monkeypatch, FakeStack(store, probes), StubController())

    run = live_scenarios.run_live_suite(
        db=UNUSED_DB,
        settings=live_settings(),
        scenario_ids=["P1-01"],
        # A directory where the document should be: every write fails, starting with
        # the first progress line, which is before the fault is armed.
        evidence_path=tmp_path,
        scenario_budget_seconds=0.3,
        poll_seconds=0.01,
    )

    assert store.writes == [("paymentUnreachable", "on"), ("paymentUnreachable", "off")]
    assert store.variants["paymentUnreachable"] == "off"
    (result,) = run.results
    assert result.status == "MEASURED"
    # ...and the failure is reported rather than swallowed: a live result that could
    # not be persisted is not a delivered result.
    assert run.evidence_errors
    assert "could not be written" in run.evidence_errors[0]


def test_a_residual_baseline_that_cannot_be_cleared_stops_the_suite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A repair that does not take effect stops the run before any measurement."""
    store = FakeFlagStore({"adFailure": "on"}, ignore_writes={"adFailure"})
    probes = stack_health(**{"payment-service": health(calls=0.2, error_rate=0.4)})
    use_fake_stack(monkeypatch, FakeStack(store, probes), StubController())

    run = live_scenarios.run_live_suite(
        db=UNUSED_DB,
        settings=live_settings(),
        scenario_ids=["P1-01", "P1-02"],
        scenario_budget_seconds=0.3,
        poll_seconds=0.01,
    )

    assert run.status == "STOPPED"
    assert [result.stage for result in run.results] == ["baseline", "baseline"]
    assert "could not be cleared at startup" in " ".join(run.results[0].reasons)
    # The demo is honestly dirty, and nothing was measured against it.
    assert store.variants["adFailure"] == "on"
    assert all(result.status == "NOT_MEASURED" for result in run.results)


def test_a_caller_that_was_already_erroring_is_not_the_fault(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The corroborating series has to rise, not merely sit above the bound."""
    stack = FakeStack(
        FakeFlagStore(),
        stack_health(
            **{
                "payment-service": health(calls=0.0, error_rate=None),
                "checkout-service": health(calls=1.0, error_rate=0.75),
            }
        ),
    )

    with pytest.raises(live_scenarios.LiveSetupFailed):
        live_scenarios.wait_for_fault(
            stack,
            target="payment-service",
            corroborating_target="checkout-service",
            corroborating_before={"error_rate": 0.75},
            timeout_seconds=0.2,
            poll_seconds=0.01,
        )


def test_ctrl_c_with_a_failed_cleanup_still_ends_as_an_interrupt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Both facts survive: the interrupt, and the cleanup that could not run."""
    store = FakeFlagStore(refuse={"off"})
    probes = stack_health(**{"payment-service": health(calls=0.2)})
    stack = FakeStack(store, probes, interrupt_on="payment-service", interrupt_after=2)
    use_fake_stack(monkeypatch, stack)
    evidence = tmp_path / "live-evidence.json"

    with pytest.raises(KeyboardInterrupt):
        live_scenarios.run_live_suite(
            db=UNUSED_DB,
            settings=live_settings(),
            scenario_ids=["P1-01"],
            evidence_path=evidence,
            scenario_budget_seconds=1.0,
            poll_seconds=0.01,
        )

    document = json.loads(evidence.read_text(encoding="utf-8"))
    assert document["run"]["status"] == "INTERRUPTED"
    reasons = " ".join(document["records"][0]["reasons"])
    assert "could not be restored" in reasons
    assert "KeyboardInterrupt" in reasons
    assert any("cleanup failure" in note for note in document["notes"])
