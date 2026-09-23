"""Human controls and workflow continuation through the FastAPI service.

The controller-level scenarios drive the graph directly, so the HTTP surface
needs its own coverage: an operator reaches the approval gate, the console, and
the takeover controls only through these endpoints. The regression these tests
exist for is choosing ``run`` versus ``resume`` wrongly, which either crashes on
a missing state key or silently re-enters a node that already ran.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from opsaccord.api.app import create_app
from opsaccord.config import Settings
from opsaccord.domain.enums import ActionStatus, ApprovalStatus, IncidentStatus
from opsaccord.persistence.db import Database
from opsaccord.reasoning.deterministic import DeterministicAdapter
from opsaccord.tools import demo_environment as demo

pytestmark = pytest.mark.integration

HUMAN = {"X-OpsAccord-Actor": "on-call-operator", "X-OpsAccord-Role": "human"}
AGENT = {"X-OpsAccord-Actor": "controller", "X-OpsAccord-Role": "agent"}


@pytest.fixture
def client(db: Database, settings: Settings):
    app = create_app(
        settings=settings,
        db=db,
        environment=demo.build_demo_environment(),
        provider=DeterministicAdapter(),
    )
    with TestClient(app) as test_client:
        yield test_client


def _open_payment_incident(client: TestClient) -> str:
    created = client.post(
        "/incidents",
        headers=HUMAN,
        json={
            "title": "Payment unreachable during checkout",
            "service": "payment-service",
            "scenario_id": "P1-01",
        },
    )
    assert created.status_code == 201, created.text
    return created.json()["incident_id"]


def test_advance_starts_a_fresh_incident_and_stops_at_the_approval_gate(client: TestClient) -> None:
    incident_id = _open_payment_incident(client)

    advanced = client.post(f"/incidents/{incident_id}/advance", headers=HUMAN)

    assert advanced.status_code == 200, advanced.text
    body = advanced.json()
    assert body["status"] == IncidentStatus.WAITING_FOR_APPROVAL
    assert body["interrupted"] is True
    assert body["approval_id"]


def test_an_agent_cannot_approve_and_the_gate_stays_open(client: TestClient) -> None:
    incident_id = _open_payment_incident(client)
    gate = client.post(f"/incidents/{incident_id}/advance", headers=HUMAN).json()

    refused = client.post(
        f"/incidents/{incident_id}/approvals/{gate['approval_id']}",
        headers=AGENT,
        json={"decision": "APPROVED", "reason": "agent self-approval attempt"},
    )
    assert refused.status_code == 403

    snapshot = client.get(f"/incidents/{incident_id}", headers=HUMAN).json()
    assert snapshot["incident"]["status"] == IncidentStatus.WAITING_FOR_APPROVAL
    assert snapshot["approvals"][0]["status"] == ApprovalStatus.PENDING
    assert [
        action for action in snapshot["actions"] if action["status"] == ActionStatus.SUCCEEDED
    ] == []


def test_approval_then_advance_executes_once_and_resolves(client: TestClient) -> None:
    incident_id = _open_payment_incident(client)
    gate = client.post(f"/incidents/{incident_id}/advance", headers=HUMAN).json()

    decided = client.post(
        f"/incidents/{incident_id}/approvals/{gate['approval_id']}",
        headers=HUMAN,
        json={"decision": "APPROVED", "reason": "Payment evidence checked"},
    )
    assert decided.status_code == 200, decided.text

    finished = client.post(f"/incidents/{incident_id}/advance", headers=HUMAN)
    assert finished.status_code == 200, finished.text
    assert finished.json()["status"] == IncidentStatus.RESOLVED

    snapshot = client.get(f"/incidents/{incident_id}", headers=HUMAN).json()
    assert snapshot["incident"]["status"] == IncidentStatus.RESOLVED
    assert len(snapshot["actions"]) == 1
    assert snapshot["actions"][0]["status"] == ActionStatus.SUCCEEDED
    assert snapshot["approvals"][0]["status"] == ApprovalStatus.CONSUMED
    assert snapshot["evidence"]
    assert "workflow.resolved" in [event["event_type"] for event in snapshot["events"]]
