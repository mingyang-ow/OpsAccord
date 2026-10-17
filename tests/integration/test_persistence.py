"""Persistence invariants against a real PostgreSQL server.

These checks are about the database being the authority: the schema enforces
the invariants the controller relies on, audit history is append-only, and an
illegal edge cannot be written even if a caller asks for it.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from opsaccord.config import Settings
from opsaccord.domain.enums import ActorType, ApprovalDecision, IncidentStatus
from opsaccord.persistence import repositories as repo
from opsaccord.persistence.db import Database
from opsaccord.scenarios.fixtures import SCENARIOS
from opsaccord.scenarios.runner import run_scenario
from opsaccord.workflow.checkpoints import ensure_checkpoint_schema

pytestmark = pytest.mark.integration

EXPECTED_TABLES = {
    "incidents",
    "workflows",
    "workflow_events",
    "audit_events",
    "evidence",
    "model_decisions",
    "tool_calls",
    "actions",
    "approvals",
    "constraints",
    "services",
    "verifications",
    "alembic_version",
}


def test_schema_contains_the_operational_and_checkpoint_tables(
    db: Database, settings: Settings
) -> None:
    ensure_checkpoint_schema(settings.checkpoint_url)
    with db.engine.connect() as connection:
        names = set(sa.inspect(connection).get_table_names(schema="public"))
        inspector = sa.inspect(connection)
        indexes = {index["name"] for index in inspector.get_indexes("approvals")}
    assert names >= EXPECTED_TABLES
    assert any(name.startswith("checkpoint") for name in names)
    assert "uq_approvals_live_action" in indexes


def test_one_live_approval_per_action_is_enforced_by_the_database(db: Database) -> None:
    incident_id, workflow_id = _incident_with_workflow(db, service="payment-service")
    with db.session() as session:
        action = repo.create_action(
            session,
            incident_id=incident_id,
            workflow_id=workflow_id,
            tool_name="apply_remediation",
            target="payment-service",
            arguments={
                "target": "payment-service",
                "remediation_id": "disable_feature_flag",
                "flag_id": "paymentUnreachable",
            },
            rationale="integration test action",
            decision_id=None,
            requires_approval=True,
        )
        repo.create_approval(session, action=action, requested_by="controller", ttl_seconds=900)
    with pytest.raises(sa.exc.IntegrityError), db.session() as session:
        repo.create_approval(session, action=action, requested_by="controller", ttl_seconds=900)


def test_audit_and_workflow_events_are_append_only(db: Database) -> None:
    incident_id, _ = _incident_with_workflow(db, service="ad-service")
    with db.session() as session:
        counts = {
            table: session.scalar(sa.text(f"SELECT count(*) FROM {table}"))
            for table in ("audit_events", "workflow_events")
        }
    assert counts["audit_events"] > 0
    assert counts["workflow_events"] > 0
    for statement in (
        "UPDATE audit_events SET event_type = 'tampered'",
        "DELETE FROM audit_events",
        "UPDATE workflow_events SET event_type = 'tampered'",
        "DELETE FROM workflow_events",
    ):
        with pytest.raises(sa.exc.DBAPIError), db.session() as session:
            session.execute(sa.text(statement))
    with db.read_session() as session:
        assert (
            session.scalar(sa.text("SELECT count(*) FROM audit_events")) == counts["audit_events"]
        )
        assert (
            session.scalar(sa.text("SELECT count(*) FROM workflow_events"))
            == counts["workflow_events"]
        )
    assert incident_id


def test_illegal_transitions_are_rejected_not_silently_accepted(db: Database) -> None:
    incident_id, _ = _incident_with_workflow(db, service="ad-service")
    with pytest.raises(repo.IllegalTransition), db.session() as session:
        repo.transition(
            session,
            incident_id=incident_id,
            target=IncidentStatus.RESOLVED,
            actor="test",
            actor_type=ActorType.SYSTEM,
            event_type="test.illegal",
        )
    with db.read_session() as session:
        workflow = repo.get_workflow(session, incident_id)
    assert workflow is not None and workflow.status is IncidentStatus.INVESTIGATING


def test_service_registry_records_the_remediation_posture(db: Database) -> None:
    with db.read_session() as session:
        postures = {
            name: repo.get_service(session, name)
            for name in (
                "payment-service",
                "ad-service",
                "cart-service",
                "checkout-service",
            )
        }
    assert postures["payment-service"] is not None
    assert postures["payment-service"].auto_remediation == "forbidden"
    assert postures["ad-service"].auto_remediation == "allow_listed"
    assert postures["cart-service"].auto_remediation == "none"
    assert postures["checkout-service"].auto_remediation == "forbidden"


def test_snapshot_exposes_the_full_audit_read_model(db: Database, settings: Settings) -> None:
    trace = run_scenario(SCENARIOS["P1-01"], db=db, settings=settings)
    with db.read_session() as session:
        snapshot = repo.snapshot(session, trace.incident.id)
    assert snapshot is not None
    assert snapshot.incident.status is IncidentStatus.RESOLVED
    assert snapshot.workflow is not None
    assert snapshot.evidence
    assert snapshot.decisions
    assert snapshot.actions
    assert snapshot.approvals
    assert snapshot.events
    assert snapshot.pending_approval_id is None
    assert snapshot.awaiting_resume is False
    approval = snapshot.approvals[0]
    assert approval.decision is not None
    assert str(approval.decision) == str(ApprovalDecision.APPROVED)
    assert approval.approver


def test_decisions_never_persist_reasoning_beyond_the_recorded_rationale(
    db: Database, settings: Settings
) -> None:
    trace = run_scenario(SCENARIOS["P1-02"], db=db, settings=settings)
    with db.read_session() as session:
        snapshot = repo.snapshot(session, trace.incident.id)
    assert snapshot is not None
    for decision in snapshot.decisions:
        keys = set(decision.payload)
        assert "chain_of_thought" not in keys
        assert "reasoning" not in keys
        assert decision.rationale


def test_demo_environment_records_are_marked_simulated(db: Database, settings: Settings) -> None:
    trace = run_scenario(SCENARIOS["P1-01"], db=db, settings=settings)
    with db.read_session() as session:
        snapshot = repo.snapshot(session, trace.incident.id)
    assert snapshot is not None
    assert all(item.is_simulated for item in snapshot.evidence)
    assert all(item.is_simulated for item in snapshot.actions)


def _incident_with_workflow(db: Database, *, service: str) -> tuple[str, str]:
    with db.session() as session:
        incident = repo.create_incident(
            session, title="integration incident", service=service, severity="SEV2"
        )
        workflow = repo.ensure_workflow(
            session,
            incident_id=incident.id,
            soft_step_budget=8,
            hard_step_cap=16,
            max_extensions=2,
        )
        # One legal transition so the append-only workflow_events table has a row.
        repo.transition(
            session,
            incident_id=incident.id,
            target=IncidentStatus.INVESTIGATING,
            actor="test",
            actor_type=ActorType.SYSTEM,
            event_type="test.investigating",
        )
    return incident.id, workflow.id
