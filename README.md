# OpsAccord

An incident-response agent with evidence-backed diagnosis, policy-controlled remediation, and human approval.

**Status: Phase 1 implemented and validated locally; awaiting review with the user.**
The controller, deterministic policy engine, typed tool gateway, PostgreSQL persistence, FastAPI
service, Streamlit console, scenarios, and adversarial checks are implemented and exercised end to
end. Both execution surfaces exist: a deterministic simulated adapter, and a live adapter over the
real Astronomy Shop stack (Prometheus, Jaeger, and the flagd control plane), selected with
`OPSACCORD_DEMO_MODE`. Docker Desktop runs the pinned `3.1.0` stack locally, and the seven live
paths have been executed against it with zero simulated-provenance records. See
[the Phase 1 implementation report](docs/reports/phase1-implementation.md) for exact commands,
counts, and which evidence is simulated rather than live.

The current safety pass (`b508a0f`, control ordering and uncertain dispatch outcomes)
is implemented and offline-verified; its PostgreSQL-backed and live validation, and its
optional independent review, are outstanding. Run `scripts/prefix-sensitivity.sh` to
reproduce the two defects on the pre-fix revision and confirm they are closed.

## Phase 1

Build and validate these paths in order against a local OpenTelemetry Astronomy Shop environment:

1. Payment unreachable: investigate, diagnose, request approval, execute, verify.
2. Ad service high CPU: bounded, allow-listed, low-risk remediation.
3. Cart failure: evidence-backed human handoff without unsupported remediation.
4. Metrics timeout: bounded read retries and safe handling of missing evidence.

Payment variations cover rejection, ambiguous action timeout, and failed recovery verification. All nine scenarios run as `P1-01`..`P1-09` through `uv run python -m opsaccord.cli run-scenarios`.

## Architecture

Streamlit provides the human controls. FastAPI exposes the application API. A workflow controller owns state transitions, with LangGraph coordinating execution and PostgreSQL checkpoints. PostgreSQL stores authoritative operational facts and append-only audit events. Models propose decisions; deterministic policy, the typed tool gateway, and human approvals control execution.

Two reasoning adapters ship: a deterministic offline adapter (the default, no credentials) and one OpenAI-compatible HTTP adapter that stays disabled unless paid calls are explicitly enabled. The model used to develop this repository does not select the application's runtime model.

## Read next

- [Architecture and safety contract](docs/architecture.md)
- [Phase 1 acceptance criteria](docs/phase1.md)
- [Evaluation roadmap](docs/evaluation.md)
- [WSL development](docs/development.md)
- [Astronomy Shop revision and fault mapping](docs/astronomy-shop.md)
- [Phase 1 implementation report](docs/reports/phase1-implementation.md)
- [Implementation report template](docs/report-template.md)

No credentials, production integrations, or live remediation are configured, and no real payment system is ever touched. The application model is deterministic unless paid calls are explicitly enabled with a budget. No software license has been selected yet.
