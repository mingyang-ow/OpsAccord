"""The live CLI surface: the table, the evidence path, and the exit code.

The lifecycle tests drive ``run_live_suite`` directly. These two drive the command
the operator actually types, with the same fakes behind it, so the reporting code
-- which is otherwise only exercised against a running stack -- has a check that
does not need one.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from opsaccord import cli
from opsaccord.domain.enums import ScenarioOutcome
from opsaccord.scenarios import live as live_scenarios
from opsaccord.scenarios.live_evidence import COMPLETED
from support.live_fakes import (
    FakeFlagStore,
    FakeStack,
    StubController,
    health,
    stack_health,
    use_fake_stack,
)


def _prepare(monkeypatch: pytest.MonkeyPatch, stack: FakeStack, controller: Any = None) -> None:
    """Point both the CLI and the driver at the fake stack.

    The command takes no budget arguments on purpose -- an operator's run is
    bounded by the calibrated constants -- so the test bounds those constants
    rather than waiting out a real telemetry window. They are module constants
    resolved at call time, which is what makes that possible.
    """
    use_fake_stack(monkeypatch, stack, controller)
    monkeypatch.setattr(live_scenarios, "LIVE_SCENARIO_BUDGET_SECONDS", 0.3)
    monkeypatch.setattr(live_scenarios, "LIVE_SUITE_BUDGET_SECONDS", 5.0)
    monkeypatch.setattr(live_scenarios, "FAULT_VISIBILITY_POLL_SECONDS", 0.01)
    monkeypatch.setattr(cli, "LiveEnvironment", live_scenarios.LiveEnvironment)
    monkeypatch.setattr(
        cli,
        "preflight",
        lambda environment, **kwargs: {
            "flag_count": 6,
            "missing_verified_flags": [],
            "prometheus_series": 12,
        },
    )
    monkeypatch.setattr(
        cli,
        "payment_path_preflight",
        lambda env, **kw: {"healthy": True, "detail": "checkout -> payment: healthy"},
    )
    monkeypatch.setattr(
        cli,
        "storefront_preflight",
        lambda environment, **kwargs: {
            "healthy": True,
            "calls_per_minute": 30.0,
            "error_share": 0.0,
            "detail": "frontend -> product-catalog: 30.0 calls/min, 0.0% failing",
        },
    )


def test_the_cli_reports_outcomes_progress_and_the_evidence_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = FakeFlagStore()
    probes = stack_health(**{"payment-service": health(calls=0.2, error_rate=0.4)})
    _prepare(monkeypatch, FakeStack(store, probes), StubController())
    evidence = tmp_path / "live-evidence.json"

    exit_code = cli.main(["run-live-scenarios", "--scenario", "P1-01", "--json", str(evidence)])

    output = capsys.readouterr().out
    # Progress first, then the summary table: the operator sees the run happen.
    assert "live P1-01 1/1 [traffic-wait]" in output
    assert "live P1-01 1/1 [fault-visibility]" in output
    assert "live P1-01 1/1 [cleanup]" in output
    assert output.index("[traffic-wait]") < output.index("id ")
    assert "SAFE_ESCALATION" in output
    assert "cause: fault visible after arming: " in output
    assert f"wrote {evidence}" in output
    assert "0/1 live scenarios passed" in output
    # A stub trace is not a passing scenario, so the command reports failure -- it
    # never turns an unmeasured or unmet expectation into success.
    assert exit_code == 1

    document = json.loads(evidence.read_text(encoding="utf-8"))
    assert document["format_version"] == 2
    assert document["run"]["status"] == "COMPLETED"
    assert document["run"]["completed"] == 1
    assert document["active"] is None
    assert store.variants["paymentUnreachable"] == "off"


def test_the_cli_reports_a_scenario_that_was_never_measured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A quiet demo is reported as not measured, with the stage it stopped at."""
    store = FakeFlagStore()
    _prepare(monkeypatch, FakeStack(store, stack_health()))
    evidence = tmp_path / "live-evidence.json"

    exit_code = cli.main(
        [
            "run-live-scenarios",
            "--scenario",
            "P1-01",
            "--json",
            str(evidence),
        ]
    )

    output = capsys.readouterr().out
    assert "NOT MEASURED" in output
    assert "cause: NOT MEASURED at the traffic-wait stage" in output
    assert "cause: cleanup: no fault was armed by this scenario" in output
    assert "0/1 live scenarios passed" in output
    assert exit_code == 1

    document = json.loads(evidence.read_text(encoding="utf-8"))
    assert document["records"][0]["stage"] == "traffic-wait"
    assert document["records"][0]["status"] == "NOT_MEASURED"


def test_the_cli_exits_130_when_the_run_is_interrupted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Ctrl+C is an interrupt, not a failed run: exit 130, and the file says so."""
    store = FakeFlagStore()
    probes = stack_health(**{"payment-service": health(calls=0.2)})
    stack = FakeStack(store, probes, interrupt_on="payment-service", interrupt_after=2)
    _prepare(monkeypatch, stack)
    evidence = tmp_path / "live-evidence.json"

    exit_code = cli.main(["run-live-scenarios", "--scenario", "P1-01", "--json", str(evidence)])

    output = capsys.readouterr().out
    assert exit_code == 130
    assert "interrupted" in output
    assert str(evidence) in output
    # The interrupted scenario still restored what it armed.
    assert store.variants["paymentUnreachable"] == "off"
    document = json.loads(evidence.read_text(encoding="utf-8"))
    assert document["run"]["status"] == "INTERRUPTED"
    assert document["active"]["scenario_id"] == "P1-01"


def test_the_cli_fails_a_run_that_carries_simulator_evidence(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A live result with simulator evidence is a defect, not a passing run.

    The run is otherwise complete and passing, so the exit code can only come from
    the provenance check: the same run without the simulated flag exits 0.
    """
    _prepare(monkeypatch, FakeStack(FakeFlagStore(), stack_health()))
    simulated_result = live_scenarios.LiveResult(
        scenario_id="P1-01",
        title="Payment unreachable",
        setup="flagd-ui: paymentUnreachable=on",
        outcome=ScenarioOutcome.PASS,
        reasons=[],
        simulated=True,
    )
    run = live_scenarios.LiveRun(
        selected=["P1-01"],
        results=[simulated_result],
        status=COMPLETED,
        started_at="2026-09-24T00:00:00Z",
        notes=[],
    )

    monkeypatch.setattr(cli, "run_live_suite", lambda **kwargs: run)
    exit_code = cli.main(["run-live-scenarios", "--scenario", "P1-01"])

    assert "simulator evidence" in capsys.readouterr().out
    assert exit_code == 1

    honest = replace(run, results=[replace(simulated_result, simulated=False)])
    monkeypatch.setattr(cli, "run_live_suite", lambda **kwargs: honest)
    assert cli.main(["run-live-scenarios", "--scenario", "P1-01"]) == 0
