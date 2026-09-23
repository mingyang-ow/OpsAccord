"""Streamlit operator console for OpsAccord.

The console is a thin client over the FastAPI service: it renders persisted
incident state and posts human decisions. It holds no authority of its own --
approval, takeover, and constraint changes are enforced by the backend, which
requires a human-authenticated actor. The console can never decide on the
agent's behalf, and it never talks to the demo environment directly.

Run with: ``uv run python -m opsaccord.cli serve-ui``
"""

from __future__ import annotations

import os
from contextlib import suppress
from datetime import datetime
from typing import Any

import httpx
import streamlit as st

DEFAULT_API_BASE_URL = os.environ.get("OPSACCORD_API_BASE_URL", "http://127.0.0.1:8410")

EVIDENCE_COLUMNS = ("collected_at", "kind", "source", "service", "summary")
ACTION_COLUMNS = (
    "created_at",
    "tool_name",
    "target",
    "status",
    "external_outcome",
    "attempt",
    "error",
)
CONSTRAINT_COLUMNS = ("created_at", "kind", "tool_name", "target", "note", "active")

STATUS_TONE = {
    "RESOLVED": "🟢",
    "READY_FOR_HUMAN": "🟠",
    "ESCALATED_INCOMPLETE": "🟣",
    "WAITING_FOR_APPROVAL": "🔵",
    "ACTIONING": "🟡",
    "VERIFYING": "🟡",
}

TERMINAL_STATUSES = {"RESOLVED", "READY_FOR_HUMAN", "ESCALATED_INCOMPLETE"}


class ApiError(RuntimeError):
    pass


class ApiClient:
    """Minimal client; every call carries the acting human identity in headers."""

    def __init__(self, base_url: str, actor: str, role: str = "human") -> None:
        self.base_url = base_url.rstrip("/")
        self.headers = {"X-OpsAccord-Actor": actor, "X-OpsAccord-Role": role}

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = httpx.request(
                method,
                f"{self.base_url}{path}",
                headers=self.headers,
                timeout=30.0,
                **kwargs,
            )
        except httpx.HTTPError as exc:
            raise ApiError(f"cannot reach the OpsAccord API at {self.base_url}: {exc}") from exc
        if response.status_code >= 400:
            detail = response.text
            with suppress(ValueError):
                detail = response.json().get("detail", detail)
            raise ApiError(f"{response.status_code}: {detail}")
        if not response.content:
            return None
        return response.json()

    def get(self, path: str) -> Any:
        return self._request("GET", path)

    def post(self, path: str, payload: dict[str, Any] | None = None) -> Any:
        return self._request("POST", path, json=payload or {})


def _short(value: object, limit: int = 90) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _stamp(value: object) -> str:
    if not value:
        return ""
    text = str(value)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).strftime("%H:%M:%S")
    except ValueError:
        return text


def _table(rows: list[dict[str, Any]], columns: tuple[str, ...], empty: str) -> None:
    if not rows:
        st.caption(empty)
        return
    visible = [{column: _short(row.get(column), 140) for column in columns} for row in rows]
    st.dataframe(visible, width="stretch", hide_index=True)


def render_overview(snapshot: dict[str, Any]) -> None:
    incident = snapshot["incident"]
    workflow = snapshot.get("workflow") or {}
    status = str(incident["status"])
    status_col, service_col, evidence_col, steps_col = st.columns(4)
    status_col.metric("Status", f"{STATUS_TONE.get(status, '⚪')} {status}")
    service_col.metric("Service", incident["service"], incident["severity"])
    evidence_col.metric("Evidence", len(snapshot.get("evidence", [])))
    steps_col.metric(
        "Steps",
        f"{workflow.get('steps_used', 0)}/{workflow.get('hard_step_cap', '?')}",
        f"soft {workflow.get('soft_step_budget', '?')}",
    )
    if incident.get("summary"):
        st.write(incident["summary"])
    if workflow.get("takeover_actor"):
        st.warning(
            f"Human takeover is active ({workflow['takeover_actor']}). "
            "Automated execution is suspended by backend policy.",
            icon="✋",
        )
    if workflow.get("terminal_reason"):
        st.info(f"Terminal reason: {workflow['terminal_reason']}", icon="📝")


def render_lifecycle(snapshot: dict[str, Any]) -> None:
    events = snapshot.get("events", [])
    if not events:
        st.caption("No workflow events recorded yet.")
        return
    rows = [
        {
            "at": _stamp(event.get("created_at")),
            "event": event.get("event_type") or event.get("type"),
            "actor": event.get("actor"),
            "detail": _short(event.get("payload"), 120),
        }
        for event in events
    ]
    st.dataframe(rows, width="stretch", hide_index=True)


def render_evidence(snapshot: dict[str, Any]) -> None:
    _table(snapshot.get("evidence", []), EVIDENCE_COLUMNS, "No evidence collected.")


def render_decisions(snapshot: dict[str, Any]) -> None:
    decisions = snapshot.get("decisions", [])
    if not decisions:
        st.caption("No model proposals recorded.")
        return
    for decision in decisions:
        payload = decision.get("payload") or {}
        header = (
            f"{decision.get('decision_type')} · {decision.get('provider')}"
            f"/{decision.get('model_id')} · {_stamp(decision.get('created_at'))}"
        )
        with st.expander(header, expanded=False):
            st.write(decision.get("rationale", ""))
            proposed = payload.get("proposed_action")
            if proposed:
                st.code(
                    f"{proposed.get('tool_name')} -> {proposed.get('target')}\n"
                    f"arguments: {proposed.get('arguments')}",
                    language="text",
                )
            if payload.get("escalate"):
                st.caption(f"proposed escalation: {payload.get('escalate_reason')}")


def render_actions(snapshot: dict[str, Any]) -> None:
    _table(snapshot.get("actions", []), ACTION_COLUMNS, "No action was ever requested.")
    for action in snapshot.get("actions", []):
        st.caption(
            f"{action['id']} · intent {str(action.get('intent_digest'))[:12]} · "
            f"idempotency {str(action.get('idempotency_key'))[:12]}"
        )


def render_approvals(snapshot: dict[str, Any], client: ApiClient, incident_id: str) -> bool:
    approvals = snapshot.get("approvals", [])
    if not approvals:
        st.caption("No approval was requested for this incident.")
        return False
    pending_id = snapshot.get("pending_approval_id")
    changed = False
    for approval in approvals:
        with st.container(border=True):
            st.markdown(f"**{approval['id']} · {approval['status']} · {approval['target']}**")
            st.caption(
                f"intent {str(approval.get('intent_digest'))[:12]} · "
                f"requested by {approval.get('requested_by')} · "
                f"expires {_stamp(approval.get('expires_at'))}"
                + (f" · decided by {approval['approver']}" if approval.get("approver") else "")
            )
            if approval["id"] != pending_id:
                continue
            reason = st.text_input("Decision reason", key=f"reason-{approval['id']}")
            approve_col, reject_col = st.columns(2)
            if approve_col.button(
                "Approve action", key=f"approve-{approval['id']}", type="primary"
            ):
                client.post(
                    f"/incidents/{incident_id}/approvals/{approval['id']}",
                    {"decision": "APPROVED", "reason": reason or "approved in console"},
                )
                changed = True
            if reject_col.button("Reject action", key=f"reject-{approval['id']}"):
                client.post(
                    f"/incidents/{incident_id}/approvals/{approval['id']}",
                    {"decision": "REJECTED", "reason": reason or "rejected in console"},
                )
                changed = True
    return changed


def render_constraints(snapshot: dict[str, Any], client: ApiClient, incident_id: str) -> bool:
    constraints = [
        {**item, "created_at": _stamp(item.get("created_at"))}
        for item in snapshot.get("constraints", [])
    ]
    _table(constraints, CONSTRAINT_COLUMNS, "No human constraints are in force.")
    with st.form(f"constraint-{incident_id}", border=False):
        kind = st.selectbox(
            "Constraint kind",
            ("FREEZE_ACTIONS", "REQUIRE_MANUAL", "DENY_TARGET", "DENY_ACTION"),
            help=(
                "FREEZE_ACTIONS suspends every side effect; DENY_TARGET and DENY_ACTION "
                "narrow the block to one target or one tool for this incident."
            ),
        )
        tool_col, target_col = st.columns(2)
        tool_name = tool_col.text_input("Tool name (DENY_ACTION)")
        target = target_col.text_input("Target (DENY_TARGET)")
        note = st.text_input("Note")
        if st.form_submit_button("Add constraint"):
            client.post(
                f"/incidents/{incident_id}/constraints",
                {
                    "kind": kind,
                    "tool_name": tool_name or None,
                    "target": target or None,
                    "note": note or None,
                },
            )
            return True
    return False


def render_controls(snapshot: dict[str, Any], client: ApiClient, incident_id: str) -> bool:
    workflow = snapshot.get("workflow") or {}
    terminal = workflow.get("status") in TERMINAL_STATUSES
    changed = False
    advance_col, takeover_col, reconcile_col = st.columns(3)
    label = (
        "Advance (approval pending)" if snapshot.get("pending_approval_id") else "Advance workflow"
    )
    if advance_col.button(label, key=f"advance-{incident_id}", disabled=terminal):
        client.post(f"/incidents/{incident_id}/advance")
        changed = True
    if takeover_col.button(
        "Take over (freeze automation)",
        key=f"takeover-{incident_id}",
        disabled=bool(workflow.get("takeover_actor")) or terminal,
    ):
        client.post(f"/incidents/{incident_id}/takeover")
        changed = True
    if reconcile_col.button("Reconcile uncertain action", key=f"reconcile-{incident_id}"):
        client.post(f"/incidents/{incident_id}/reconcile")
        changed = True
    return changed


def render_incident(client: ApiClient, incident_id: str) -> None:
    snapshot = client.get(f"/incidents/{incident_id}")
    render_overview(snapshot)
    st.subheader(snapshot["incident"]["title"])
    state_tab, evidence_tab, proposal_tab, control_tab = st.tabs(
        ["State", "Evidence timeline", "Proposals & actions", "Human controls"]
    )
    with state_tab:
        render_lifecycle(snapshot)
    with evidence_tab:
        render_evidence(snapshot)
    with proposal_tab:
        render_decisions(snapshot)
        st.divider()
        render_actions(snapshot)
    with control_tab:
        st.caption(
            "Every control below is enforced by the backend. The console cannot "
            "approve on the agent's behalf, and the agent cannot approve itself."
        )
        changed = render_approvals(snapshot, client, incident_id)
        st.divider()
        changed = render_constraints(snapshot, client, incident_id) or changed
        st.divider()
        changed = render_controls(snapshot, client, incident_id) or changed
    if changed:
        st.rerun()


def render_sidebar(client: ApiClient) -> None:
    with st.sidebar:
        st.header("OpsAccord")
        st.caption(f"API `{client.base_url}` · actor `{client.headers['X-OpsAccord-Actor']}`")
        st.divider()
        st.subheader("New incident")
        with st.form("new-incident"):
            scenarios = {item["id"]: item for item in client.get("/scenarios")}
            scenario_id = st.selectbox("Scenario", ["(custom)", *scenarios])
            spec = scenarios.get(scenario_id)
            title = st.text_input("Title", value=spec["title"] if spec else "Manual investigation")
            service = st.text_input("Service", value=spec["service"] if spec else "payment-service")
            severity = st.selectbox("Severity", ("SEV1", "SEV2", "SEV3"))
            if st.form_submit_button("Create and investigate"):
                created = client.post(
                    "/incidents",
                    {
                        "title": title,
                        "service": service,
                        "severity": severity,
                        "scenario_id": scenario_id if spec else None,
                    },
                )
                st.session_state["incident_id"] = created["incident_id"]
                st.rerun()
        st.divider()
        incidents = client.get("/incidents")
        if incidents:
            options = [item["id"] for item in incidents]
            labels = {
                item["id"]: f"{item['id'][:10]} · {item['status']} · {item['service']}"
                for item in incidents
            }
            selected = st.radio(
                "Incidents",
                options,
                format_func=lambda value: labels[value],
                index=options.index(st.session_state["incident_id"])
                if st.session_state.get("incident_id") in options
                else 0,
            )
            st.session_state["incident_id"] = selected


def main() -> None:
    st.set_page_config(page_title="OpsAccord console", page_icon="🧭", layout="wide")
    actor = os.environ.get("OPSACCORD_UI_ACTOR", "on-call-operator")
    client = ApiClient(DEFAULT_API_BASE_URL, actor)
    try:
        health = client.get("/healthz")
    except ApiError as exc:
        st.error(str(exc))
        st.stop()
    st.title("Incident response console")
    st.caption(
        f"model {health['model_provider']}/{health['model_id']} · demo revision "
        f"{health['demo_revision']} · simulated={health['simulated_demo']} · "
        f"demo_mode={health['demo_mode']}"
    )
    render_sidebar(client)
    incident_id = st.session_state.get("incident_id")
    if not incident_id:
        st.info("Create an incident from the sidebar, or pick one to inspect.")
        return
    try:
        render_incident(client, incident_id)
    except ApiError as exc:
        st.error(str(exc))


main()
