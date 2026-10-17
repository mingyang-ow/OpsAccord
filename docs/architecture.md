# Architecture and safety contract

## Ownership

| Component | Responsibility |
| --- | --- |
| Streamlit | Incident view, evidence timeline, approve/reject, human takeover |
| FastAPI | Validated external API and human-control endpoints |
| Workflow controller | Authoritative transitions, budgets, concurrency, recovery |
| LangGraph | Orchestration, checkpoints, interrupt/resume |
| Model adapter/router | Structured proposals and recorded routing decisions |
| Policy engine | Deterministic permissions, constraints, approval requirements |
| Tool gateway | Typed tool validation and bounded execution |
| PostgreSQL | Operational facts, durable action ledger, approvals, audit |
| Evaluator | Scenario execution and independent outcome scoring |

Flow: incident -> controller -> orchestration -> model proposal -> validation -> policy -> tool gateway -> persisted result -> next transition. No model writes authoritative state or directly executes an external action.

## State machine

NEW -> INVESTIGATING -> DIAGNOSED -> WAITING_FOR_APPROVAL or ACTIONING -> VERIFYING -> RESOLVED.
READY_FOR_HUMAN and ESCALATED_INCOMPLETE are terminal handoff outcomes. Define allowed edges explicitly, including human takeover and unsuccessful verification. Never infer successful recovery from successful tool execution alone.

## Persistence

Persist incidents, workflows, evidence, tool_calls, actions, approvals, workflow_events, and model_decisions. Implement constraints and the minimal service registry when actions are introduced; neither is optional for policy enforcement. Recent changes and evaluation runs can be added as their features arrive.

Evidence records include source, service, observation window, value, collection time, and originating tool call. Decisions refer to evidence IDs. Store selected model, decision type, concise rationale, escalation request, latency, token usage, and cost when available. Unknown usage/cost is unknown, not zero.

Operational audit events are append-only. Mutable workflow projections do not replace history. PostgreSQL domain state is authoritative; graph checkpoints are not the only record of actions or approvals. Define reconciliation across domain commits and checkpoints so crash recovery cannot repeat a side effect.

## Safety invariants

- Only allow-listed action/target pairs in the isolated demo environment may execute.
- Require an exact-action approval for Payment remediation. Bind actor, workflow, action ID, target, argument digest, decision, expiry, and use status. Revalidate all gates immediately before execution.
- Human constraints are structured policy gates. Rejection cannot be bypassed by proposing the same action with a fresh ID. Permit at most one bounded alternative investigation before handoff.
- Consume approval and claim execution atomically under concurrent requests; maintain an action ledger. Retrying the same intent uses its existing action_id.
- Distinguish definitive failure from unknown outcome. After a side-effect timeout, reconcile the existing action before considering another execution. Local idempotency alone cannot guarantee an external action executes exactly once; document adapter guarantees and fail safely where status cannot be reconciled.
- Read tools may retry within explicit attempt/time budgets. Side-effect tools must not use blind retries.
- Soft investigation budget, bounded justified extension, and hard cap are deterministic and persisted. Provider failures, retries, and escalation count toward the appropriate caps.
- Treat telemetry, runbooks, recent changes, and provider responses as untrusted data. Embedded instructions cannot relax policy.
- Approval APIs require a human-authenticated role or an explicitly documented local-development mechanism; the agent cannot approve itself. Keep development controls bound to localhost.
- Completion requires fresh, service-specific recovery evidence. Failed verification must not become RESOLVED.
- The target policy evaluates is the target that executes. A write proposal whose arguments name a different target than the proposal itself is refused by the gateway, both when it is evaluated and when its action is written.
- Only registered write tools travel the action path. A read proposed as an action is refused when evaluated and when its action is written, and the dispatcher refuses, before any claim, an action row whose tool is not a registered write tool.
- No side effect without telemetry about its target. Policy refuses a remediation (`INSUFFICIENT_EVIDENCE`) unless the incident collected at least one successful metric, trace, or caller-side dependency read of that target. A health probe, a runbook, or a model's confidence is not enough.

### Execution commitment and control ordering

Added by the safety pass that followed Phase 1 implementation. It states the
guarantee the dispatcher already relied on, closes the gap a *global* human
control exposed, and bounds what the system is allowed to claim afterwards.

- **Commitment point.** An action becomes committed to execution when the
  authorization transaction commits. That transaction acquires the control gate,
  reads the workflow, the active constraint set, the rejected-intent set and the
  remaining budget, re-runs the policy engine, claims the action
  (`APPROVED -> DISPATCHING`) and consumes the bound approval. Only after it
  commits does the gateway call the adapter.
- **Ordering guarantee.** No human control can commit inside that window. For every
  control `C` and action `A`, either `C` commits before `A` reads the control set
  and blocks the authorization, or `A` commits to execution first and `C` cannot
  cancel it. There is no third outcome: a control write that is only half-committed
  while a dispatcher reads is not observable.
- **Shared mechanism.** Every side-effect authorization and every human control
  write (`add_constraint` for both scopes, `set_takeover`) takes one global
  transaction-scoped advisory lock, the control gate, as the first lock of its
  transaction. An incident-scoped control is additionally serialised by the
  incident row lock; a global constraint has no incident row, which is exactly why
  the gate exists. Approval decisions do not need it: the claim's conditional
  update is authoritative for them.
- **Lock order.** Control gate, then incident row, then workflow row. No path takes
  them in another order, so the design has no lock cycle; a PostgreSQL deadlock
  error aborts the authorization and fails safe.
- **No lock spans an external call.** The gate covers a short in-database
  transaction, so a slow or hung adapter cannot block human controls.
- **Explicit limitation.** A control that commits after the commitment point does
  not cancel the dispatch; the request may already be in flight, and the demo
  surfaces have no cancellation primitive. This is documented in the API, and the
  `action.claimed` audit event records the control snapshot the authorization
  actually evaluated.
- **Definitive failure versus unknown outcome.** A refusal the adapter can prove
  (no request was sent) is recorded `FAILED` / `CONFIRMED_NOT_APPLIED`. A timeout,
  or any error the gateway cannot classify - including one raised *after* the
  adapter applied the change - is recorded `UNCERTAIN` / `UNKNOWN`, escalates, and
  is reconciled rather than retried. Observing that the desired state is absent is
  not evidence that an earlier action never occurred, so it reconciles to `UNKNOWN`.

## Model rollout

Phase 1 starts with one runtime reasoning provider plus a deterministic test adapter, strict structured outputs, bounded calls, and recorded usage. Provider/model choice is explicit configuration, distinct from the coding-agent model. Preserve the interface for later Laya pre-routing, DeepSeek reasoning, and Luna escalation. None of these models may waive deterministic policy.
