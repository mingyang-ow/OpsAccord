# OpsAccord

An incident-response agent with evidence-backed diagnosis, policy-controlled remediation, and human approval.

**Status: Phase 0 design baseline; Phase 1 implementation has not started.**
This repository currently contains the project foundation and implementation contract, not a working incident-response system.

## Phase 1

Build and validate these paths in order against a local OpenTelemetry Astronomy Shop environment:

1. Payment unreachable: investigate, diagnose, request approval, execute, verify.
2. Ad service high CPU: bounded, allow-listed, low-risk remediation.
3. Cart failure: evidence-backed human handoff without unsupported remediation.
4. Metrics timeout: bounded read retries and safe handling of missing evidence.

Payment variations cover rejection, ambiguous action timeout, and failed recovery verification.

## Architecture

Streamlit provides the human controls. FastAPI exposes the application API. A workflow controller owns state transitions, with LangGraph coordinating execution. PostgreSQL stores authoritative operational facts and audit events. Models propose decisions; deterministic policy and a typed tool gateway control execution.

Start with a single configurable reasoning-model adapter and a deterministic offline adapter. The later Laya / DeepSeek / Luna routing experiment remains separate from the initial baseline. The model used to develop this repository does not select the application's runtime model.

## Read next

- [Architecture and safety contract](docs/architecture.md)
- [Phase 1 acceptance criteria](docs/phase1.md)
- [Evaluation roadmap](docs/evaluation.md)
- [WSL development](docs/development.md)
- [Implementation report template](docs/report-template.md)

No credentials, production integrations, or live remediation are configured. Runtime configuration, dependency locking, migrations, application services, and CI belong to Phase 1. No software license has been selected yet.
