# OpsAccord

An incident-response agent with evidence-backed diagnosis, policy-controlled remediation, and human approval.

**Status: Phase 1 READY_FOR_DISCUSSION; awaiting review and acceptance with the user.**
The controller, deterministic policy engine, typed tool gateway, PostgreSQL persistence, FastAPI
service, Streamlit console, scenarios, and adversarial checks are implemented and exercised end to
end. There are two execution surfaces, selected with `OPSACCORD_DEMO_MODE`: a deterministic simulated
adapter, and a live adapter over the real Astronomy Shop stack (Prometheus, Jaeger, and the flagd
control plane). All nine scenarios pass in simulation. All seven live paths (P1-01..P1-07) have passed
against the pinned `3.1.0` stack on the same runtime code, with zero forbidden actions, zero duplicate
side effects, and zero simulated-provenance records. The branch has not been pushed or merged. See
[the Phase 1 implementation report](docs/reports/phase1-implementation.md) for exact commands, counts,
and the remaining single seven-path run.

`scripts/prefix-sensitivity.sh` reproduces the two safety-pass defects on the pre-fix revision
(`5cbcce2`) and confirms they are closed at `HEAD`.

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
