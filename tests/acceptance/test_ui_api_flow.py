"""Automated UI/API regression for the whole incident path.

The operator console is a thin client over the API, so this test drives the
console's own client and click handlers -- through a recording Streamlit stub --
against a real FastAPI app backed by the real PostgreSQL test database and the
simulated demo surface. One run covers the flow the console is built for:

    create incident -> evidence timeline -> approval request -> human approval
    -> advance -> single action -> fresh verification -> RESOLVED

and the variations where a human rejects the action or where the agent tries to
approve its own work.
"""

from __future__ import annotations

import importlib
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from opsaccord.api.app import create_app
from opsaccord.config import Settings
from opsaccord.persistence.db import Database
from opsaccord.persistence.tables import VerificationRow
from opsaccord.reasoning.deterministic import DeterministicAdapter
from opsaccord.tools import demo_environment as demo
from support.streamlit_stub import RecordingStreamlit, install

pytestmark = pytest.mark.integration


class _AsgiHttpx:
    """Routes the console's httpx calls directly into the ASGI app."""

    def __init__(self, client: TestClient) -> None:
        self._client = client
        self.HTTPError = httpx.HTTPError

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        kwargs.pop("timeout", None)
        return self._client.request(method, url, **kwargs)


@pytest.fixture
def console(monkeypatch: pytest.MonkeyPatch) -> Any:
    return importlib.import_module("opsaccord.ui.app")


@pytest.fixture
def operator(console: Any, db: Database, settings: Settings, monkeypatch) -> Any:
    app = create_app(
        settings=settings,
        db=db,
        environment=demo.build_demo_environment(),
        provider=DeterministicAdapter(),
    )
    with TestClient(app) as test_client:
        monkeypatch.setattr(console, "httpx", _AsgiHttpx(test_client))
        yield console.ApiClient("http://127.0.0.1:8410", "on-call-operator")


def _create(operator: Any, scenario_id: str) -> str:
    created = operator.post(
        "/incidents",
        {
            "title": "Payment unreachable",
            "service": "payment-service",
            "severity": "SEV1",
            "scenario_id": scenario_id,
        },
    )
    return str(created["incident_id"])


def _verifications(db: Database, incident_id: str) -> list[str]:
    with db.read_session() as session:
        return [
            str(row.outcome)
            for row in session.scalars(
                sa.select(VerificationRow).where(VerificationRow.incident_id == incident_id)
            ).all()
        ]


def _advance_in_console(
    console: Any, operator: Any, incident_id: str, monkeypatch: pytest.MonkeyPatch
) -> RecordingStreamlit:
    recorder = install(monkeypatch, console, pressed={"Advance workflow"})
    console.render_incident(operator, incident_id)
    assert recorder.errors == []
    return recorder


def test_console_drives_incident_from_evidence_to_resolved(
    console: Any, operator: Any, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    incident_id = _create(operator, "P1-01")
    assert operator.get(f"/incidents/{incident_id}")["incident"]["status"] == "NEW"

    # The console's first click starts the investigation and stops at the gate.
    _advance_in_console(console, operator, incident_id, monkeypatch)
    snapshot = operator.get(f"/incidents/{incident_id}")
    assert snapshot["incident"]["status"] == "WAITING_FOR_APPROVAL"
    assert snapshot["pending_approval_id"]
    assert {"METRIC", "TRACE", "HEALTH"} <= {item["kind"] for item in snapshot["evidence"]}

    approving = install(monkeypatch, console, pressed={"Approve action"})
    console.render_incident(operator, incident_id)
    assert approving.errors == []
    assert approving.calls_named("button")

    # The human decision records APPROVED and clears the pending gate, but the
    # approval is only CONSUMED when the controller claims the action for
    # dispatch -- which happens on the next advance, not on the click itself.
    decided = operator.get(f"/incidents/{incident_id}")
    assert decided["approvals"][0]["decision"] == "APPROVED"
    assert decided["approvals"][0]["status"] == "APPROVED"
    assert decided["pending_approval_id"] is None
    assert decided["incident"]["status"] == "WAITING_FOR_APPROVAL"

    _advance_in_console(console, operator, incident_id, monkeypatch)

    final = operator.get(f"/incidents/{incident_id}")
    assert final["approvals"][0]["status"] == "CONSUMED"
    assert final["incident"]["status"] == "RESOLVED"
    assert len(final["actions"]) == 1
    action = final["actions"][0]
    assert action["status"] == "SUCCEEDED"
    assert action["external_outcome"] == "CONFIRMED_APPLIED"
    assert len([a for a in final["actions"] if a["dispatch_started_at"]]) == 1
    assert _verifications(db, incident_id) == ["VERIFICATION_SUCCESS"]

    shown: RecordingStreamlit = install(monkeypatch, console)
    console.render_incident(operator, incident_id)
    assert shown.errors == []
    status_metrics = [
        args for args, _ in shown.calls_named("metric") if args and args[0] == "Status"
    ]
    assert status_metrics and "RESOLVED" in str(status_metrics[0][1])


def test_console_rejection_stops_execution_and_keeps_the_incident_for_a_human(
    console: Any, operator: Any, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    incident_id = _create(operator, "P1-05")
    _advance_in_console(console, operator, incident_id, monkeypatch)

    rejecting = install(monkeypatch, console, pressed={"Reject action"})
    console.render_incident(operator, incident_id)
    assert rejecting.errors == []

    after_rejection = operator.get(f"/incidents/{incident_id}")
    assert after_rejection["approvals"][0]["decision"] == "REJECTED"
    assert after_rejection["approvals"][0]["status"] == "REJECTED"
    assert after_rejection["pending_approval_id"] is None

    # The controller leaves the approval gate on the next advance and hands the
    # incident to a human, never executing the rejected action.
    _advance_in_console(console, operator, incident_id, monkeypatch)

    snapshot = operator.get(f"/incidents/{incident_id}")
    assert snapshot["incident"]["status"] == "READY_FOR_HUMAN"
    assert [a for a in snapshot["actions"] if a["dispatch_started_at"]] == []


def test_console_client_cannot_approve_with_an_agent_role(console: Any, operator: Any) -> None:
    incident_id = _create(operator, "P1-01")
    snapshot = operator.get(f"/incidents/{incident_id}")
    approval_id = snapshot["pending_approval_id"]

    agent = console.ApiClient("http://127.0.0.1:8410", "autonomous-agent", role="agent")
    with pytest.raises(console.ApiError, match="403"):
        agent.post(f"/incidents/{incident_id}/approvals/{approval_id}", {"decision": "APPROVED"})
