"""FastAPI application exposing incidents, evidence, and human controls."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, HTTPException, status
from pydantic import BaseModel, Field

from opsaccord.api.deps import Actor, current_actor, require_human
from opsaccord.config import Settings, get_settings
from opsaccord.domain.enums import ApprovalDecision, ConstraintKind
from opsaccord.persistence import repositories as repo
from opsaccord.persistence.db import Database
from opsaccord.reasoning.base import ReasoningProvider
from opsaccord.reasoning.deterministic import DeterministicAdapter
from opsaccord.reasoning.openai_compatible import OpenAICompatibleAdapter
from opsaccord.scenarios.fixtures import build_environment_for_scenario
from opsaccord.scenarios.runner import SCENARIOS
from opsaccord.tools.contracts import Environment
from opsaccord.tools.demo_environment import DemoEnvironment
from opsaccord.tools.factory import build_environment
from opsaccord.tools.gateway import ToolGateway
from opsaccord.workflow.checkpoints import ensure_checkpoint_schema
from opsaccord.workflow.controller import WorkflowController


class CreateIncidentRequest(BaseModel):
    title: str = Field(min_length=3, max_length=200)
    service: str
    severity: Literal["SEV1", "SEV2", "SEV3"] = "SEV2"
    scenario_id: str | None = None


class ApprovalRequest(BaseModel):
    decision: ApprovalDecision
    reason: str | None = Field(default=None, max_length=500)


class ConstraintRequest(BaseModel):
    kind: ConstraintKind
    tool_name: str | None = None
    target: str | None = None
    note: str | None = Field(default=None, max_length=300)


def build_provider(settings: Settings) -> ReasoningProvider:
    """Select the application runtime model from explicit configuration."""
    if settings.model_provider == "deterministic":
        return DeterministicAdapter()
    if settings.model_provider == "openai_compatible":
        return OpenAICompatibleAdapter(
            base_url=settings.model_base_url,
            model_id=settings.model_id,
            api_key_env=settings.model_api_key_env,
            timeout_seconds=settings.model_timeout_seconds,
            max_output_tokens=settings.model_max_output_tokens,
            allow_paid_calls=settings.allow_paid_model_calls,
            input_cost_per_1k_usd=settings.model_input_cost_per_1k_usd,
            output_cost_per_1k_usd=settings.model_output_cost_per_1k_usd,
        )
    raise ValueError(f"unsupported model_provider {settings.model_provider!r}")


def create_app(
    *,
    settings: Settings | None = None,
    db: Database | None = None,
    environment: Environment | None = None,
    provider: ReasoningProvider | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    database = db or Database(settings.state_database_url)
    environment = environment or build_environment(settings)
    provider = provider or build_provider(settings)
    gateway = ToolGateway(db=database, environment=environment, settings=settings)
    ensure_checkpoint_schema(settings.checkpoint_url)
    controller = WorkflowController(
        db=database, gateway=gateway, provider=provider, settings=settings
    )

    app = FastAPI(
        title="OpsAccord",
        version="0.1.0.dev0",
        description=(
            "Incident response with evidence-backed diagnosis and policy-controlled "
            "remediation. The API binds to localhost in development."
        ),
    )
    app.state.settings = settings
    app.state.db = database
    app.state.environment = environment
    app.state.controller = controller
    app.state.gateway = gateway

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {
            "status": "ok",
            "demo_mode": settings.demo_mode,
            "demo_revision": environment.revision,
            "model_provider": settings.model_provider,
            "model_id": settings.model_id,
            "simulated_demo": environment.is_simulated,
        }

    @app.get("/scenarios")
    def list_scenarios() -> list[dict[str, str]]:
        return [
            {"id": spec.id, "title": spec.title, "service": spec.service}
            for spec in SCENARIOS.values()
        ]

    @app.post("/incidents", status_code=status.HTTP_201_CREATED)
    def create_incident(
        request: CreateIncidentRequest, actor: Annotated[Actor, Depends(current_actor)]
    ) -> dict[str, Any]:
        if request.scenario_id:
            spec = SCENARIOS.get(request.scenario_id)
            if spec is None:
                raise HTTPException(status_code=400, detail="unknown scenario id")
            if not isinstance(app.state.environment, DemoEnvironment):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "built-in scenarios install simulated faults and are unavailable in "
                        "live mode; inject the fault in the demo stack instead"
                    ),
                )
            app.state.environment.install_from(build_environment_for_scenario(spec))
        try:
            incident_id = controller.create_incident(
                title=request.title,
                service=request.service,
                severity=request.severity,
                scenario_id=request.scenario_id,
            )
        except repo.RepositoryError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        with database.session() as session:
            repo.audit(
                session,
                actor=actor.name,
                actor_type=actor.type,
                event_type="api.incident.created",
                incident_id=incident_id,
                subject_type="incident",
                subject_id=incident_id,
            )
        return {"incident_id": incident_id}

    @app.get("/incidents")
    def list_incidents(_: Annotated[Actor, Depends(current_actor)]) -> list[dict[str, Any]]:
        with database.read_session() as session:
            return [
                {
                    "id": item.id,
                    "title": item.title,
                    "service": item.service,
                    "status": str(item.status),
                    "severity": item.severity,
                    "scenario_id": item.scenario_id,
                    "updated_at": item.updated_at.isoformat(),
                }
                for item in repo.list_incidents(session)
            ]

    def _snapshot_or_404(incident_id: str) -> repo.IncidentSnapshot:
        with database.read_session() as session:
            snapshot = repo.snapshot(session, incident_id)
        if snapshot is None:
            raise HTTPException(status_code=404, detail="unknown incident")
        return snapshot

    @app.get("/incidents/{incident_id}")
    def get_incident(
        incident_id: str, _: Annotated[Actor, Depends(current_actor)]
    ) -> dict[str, Any]:
        snapshot = _snapshot_or_404(incident_id)
        return {
            "incident": snapshot.incident.model_dump(mode="json"),
            "workflow": snapshot.workflow.model_dump(mode="json") if snapshot.workflow else None,
            "evidence": [item.model_dump(mode="json") for item in snapshot.evidence],
            "decisions": [item.model_dump(mode="json") for item in snapshot.decisions],
            "actions": [item.model_dump(mode="json") for item in snapshot.actions],
            "approvals": [item.model_dump(mode="json") for item in snapshot.approvals],
            "constraints": [item.model_dump(mode="json") for item in snapshot.constraints],
            "events": list(snapshot.events),
            "pending_approval_id": snapshot.pending_approval_id,
            "awaiting_resume": snapshot.awaiting_resume,
        }

    @app.post("/incidents/{incident_id}/advance")
    def advance(
        incident_id: str, actor: Annotated[Actor, Depends(current_actor)]
    ) -> dict[str, Any]:
        _snapshot_or_404(incident_id)
        with database.session() as session:
            repo.expire_stale_approvals(session)
        outcome = controller.advance(incident_id)
        return {
            "status": str(outcome.status),
            "interrupted": outcome.interrupted,
            "approval_id": outcome.approval_id,
            "action_id": outcome.action_id,
            "terminal_reason": outcome.terminal_reason,
            "notes": outcome.notes,
            "advanced_by": actor.name,
        }

    @app.post("/incidents/{incident_id}/approvals/{approval_id}")
    def decide_approval(
        incident_id: str,
        approval_id: str,
        request: ApprovalRequest,
        actor: Annotated[Actor, Depends(require_human)],
    ) -> dict[str, Any]:
        snapshot = _snapshot_or_404(incident_id)
        with database.session() as session:
            approval = repo.get_approval(session, approval_id)
            if approval is None or approval.incident_id != incident_id:
                raise HTTPException(status_code=404, detail="unknown approval for this incident")
            try:
                decided = repo.decide_approval(
                    session,
                    approval_id=approval_id,
                    approver=actor.name,
                    decision=request.decision,
                    reason=request.reason,
                )
            except repo.RepositoryError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {
            "approval_id": decided.id,
            "status": str(decided.status),
            "decision": str(decided.decision),
            "approver": decided.approver,
            "action_id": decided.action_id,
            "workflow_status": str(snapshot.workflow.status) if snapshot.workflow else None,
        }

    @app.post("/incidents/{incident_id}/takeover")
    def takeover(
        incident_id: str, actor: Annotated[Actor, Depends(require_human)]
    ) -> dict[str, Any]:
        _snapshot_or_404(incident_id)
        with database.session() as session:
            workflow = repo.set_takeover(session, incident_id=incident_id, actor=actor.name)
        return {"incident_id": incident_id, "takeover_actor": workflow.takeover_actor}

    @app.post("/incidents/{incident_id}/constraints", status_code=status.HTTP_201_CREATED)
    def add_constraint(
        incident_id: str,
        request: ConstraintRequest,
        actor: Annotated[Actor, Depends(require_human)],
    ) -> dict[str, Any]:
        _snapshot_or_404(incident_id)
        with database.session() as session:
            try:
                constraint = repo.add_constraint(
                    session,
                    kind=request.kind,
                    actor=actor.name,
                    incident_id=incident_id,
                    tool_name=request.tool_name,
                    target=request.target,
                    note=request.note,
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"constraint_id": constraint.id, "kind": str(constraint.kind)}

    @app.post("/incidents/{incident_id}/reconcile")
    def reconcile(
        incident_id: str, actor: Annotated[Actor, Depends(current_actor)]
    ) -> dict[str, Any]:
        _snapshot_or_404(incident_id)
        notes = controller.reconcile(incident_id)
        with database.session() as session:
            repo.audit(
                session,
                actor=actor.name,
                actor_type=actor.type,
                event_type="api.reconcile.requested",
                incident_id=incident_id,
                payload={"notes": notes},
            )
        return {"notes": notes}

    return app
