# OpsAccord development contract

Read README.md, docs/architecture.md, docs/phase1.md, and docs/evaluation.md before implementation.

- Keep repository prose focused on the system, experiments, technical findings, and operational use cases.
- Preserve the agreed architecture. Models propose; the controller, deterministic policy engine, and tool gateway enforce authority.
- Work in WSL's Linux filesystem on a feature branch. Preserve existing work; never reset another task's changes.
- Build Payment end to end first, then Ad, Cart, and metrics-timeout paths. Do not claim the entire phase is complete after one path.
- Use PostgreSQL for operational state and LangGraph checkpoints for execution continuity. Do not substitute in-memory state or SQLite for the persistence acceptance tests.
- Isolate local Astronomy Shop actions. Never expose arbitrary shell commands, unrestricted URLs, or Docker socket access to a model.
- Do not run against production or unrelated containers. Do not make paid application-model calls without an explicit budget. The requested coding agent and reviewer are separate from application evaluation calls.
- Keep tokens, credentials, raw provider payloads, local notes, and local runtime configuration out of Git. Log concise decision rationales and structured outcomes, not hidden chain-of-thought.
- Use mocks for fast tests, and real local integration checks for persistence, telemetry adapters, recovery, and action safety. Clearly identify which evidence is simulated.
- Add tests that exercise behavior and failure modes. Never weaken a test merely to obtain a passing run.
- Development tooling is authorized explicitly: the DeepSeek-backed Codex agent is primary, and the authenticated subscription-backed Claude Code harness may be used for bounded implementation help or an optional second review. Record which tool and model actually ran; never claim a review that did not happen. If a tool is unavailable, continue with the others and state the fallback.
- External review is optional and is never a completion gate. Final review and acceptance happen with the user in their planning conversation, so finish with an honest report and stop. Use READY_FOR_DISCUSSION / PARTIAL / BLOCKED in docs/report-template.md rather than declaring completion.
- Report exact branch and commit, commands and results, scenarios, findings and fixes, limitations, and next decisions using docs/report-template.md.
- Do not merge implementation work, publish a release, or change repository visibility without explicit instruction.
