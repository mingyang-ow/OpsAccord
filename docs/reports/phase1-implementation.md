# Phase 1 implementation report

## Identity and status

| Item | Value |
| --- | --- |
| Repository | `mingyang-ow/OpsAccord` (private), remote `git@github.com:mingyang-ow/OpsAccord.git` |
| Branch | `phase1/baseline` |
| Base commit | `3168bcf` (branch point; design baseline and Phase 1 foundation) |
| Implementation commit | `1296b23dd1073d18f5524f6343398826ded0f735` (the commit immediately preceding this report) |
| Dirty state | Clean at commit time; only gitignored local state (`.local/`, `reports/local/`, `.venv/`, `.coverage`) remains |
| Host | WSL 2, kernel `6.18.33.2-microsoft-standard-WSL2`, Ubuntu 26.04 LTS, 16 CPU, ~13 GB available RAM |
| Toolchain | `uv` 0.12.5, Python 3.12.14 (`.python-version`), project-local PostgreSQL 18.6 on `127.0.0.1:55432` |
| Coding model/provider | Codex CLI agent on the user's configured DeepSeek provider (`model = "deepseek-flash"`, `model_provider = "deepseek"`) |
| Application runtime model | `deterministic` / `deterministic-v1`; no paid model calls |
| **Overall status** | **PARTIAL** - every executable technical gate passed; the live-demo gate could not be exercised because Docker/Compose is unavailable here, and the optional external review could not run |

`PARTIAL` rather than `READY_FOR_DISCUSSION` is a deliberate, precise reading of
`docs/phase1.md`: the gate "demonstrate the four primary paths against the real
isolated demo" was not met because the demo cannot be started in this
environment. Nothing was narrowed or re-labelled to make that gate pass. No
merge, release, visibility change, or Phase 2 work was performed.

## Delivered behavior

Implemented and working:

- **Typed domain contracts** (`src/opsaccord/domain/`): incidents, workflows,
  evidence, decisions, actions, approvals, constraints, workflow and audit
  events, budgets, the service registry, and an explicit transition map with
  terminal handoff outcomes (`READY_FOR_HUMAN`, `ESCALATED_INCOMPLETE`).
- **Workflow controller** (`workflow/controller.py`): LangGraph orchestration with
  PostgreSQL checkpoints, `run`/`resume`/`advance` entry points, budget
  accounting, human takeover, and reconciliation of in-flight actions.
- **Deterministic policy engine** (`policy/engine.py`): fixed target allow-list,
  human constraints ahead of everything else, exact-approval requirements for
  critical targets, allow-listed autonomous remediation, hard budget walls.
- **Typed tool gateway** (`tools/gateway.py`): registry-validated arguments,
  bounded read retries, atomic claim plus approval consumption for side effects,
  policy revalidation immediately before dispatch, no blind retries, and
  `UNCERTAIN` handling instead of duplicate execution. There is no shell, URL, or
  container primitive anywhere in the tool surface.
- **Persistence** (`persistence/`, `migrations/`): Alembic migrations, a partial
  unique index guaranteeing one live approval per action, append-only audit
  triggers, and an evidence/decision read model that stores concise rationales
  only (no hidden chain-of-thought, no secrets).
- **FastAPI service** (`api/app.py`) and **Streamlit console** (`ui/app.py`):
  incident creation, snapshot with evidence timeline, approve/reject, takeover,
  structured constraints, advance, and reconcile, with human-only endpoints
  enforced by the backend (`X-OpsAccord-Actor` / `X-OpsAccord-Role`).
- **Reasoning adapters**: a deterministic offline adapter (the default) and one
  OpenAI-compatible HTTP adapter that refuses to call out unless paid calls are
  explicitly enabled and cost rates are configured.
- **Acceptance and adversarial coverage**: nine scenarios (`P1-01`..`P1-09`),
  14 adversarial control tests, offline unit tests, PostgreSQL integration
  tests, and API-level human-control tests.
- **CI** (`.github/workflows/ci.yml`): an offline job (lint, format, types,
  offline tests) and a PostgreSQL service job (integration tests plus the
  scenario CLI).

Explicitly not implemented, and not implied by any result below:

- **No live demo adapter.** `src/opsaccord/tools/demo_environment.py` is a
  deterministic in-process model of the pinned Astronomy Shop revision. Every
  scenario result in this report is *simulated* execution against *real*
  PostgreSQL.
- **`demo_mode=live` is refused.** Rather than run the simulator under a live
  label, `Settings` now rejects it (see the fix list below).
- **No paid application-model calls were made**, and none are needed for the
  validated paths.

## Validation evidence

| Check/scenario | Exact command | Result | Evidence path | Simulated or live |
| --- | --- | --- | --- | --- |
| Lint | `uv run ruff check .` | All checks passed | - | n/a |
| Formatting | `uv run ruff format --check .` | 55 files already formatted | - | n/a |
| Types | `uv run mypy src/opsaccord` | No issues in 35 source files | - | n/a |
| Test suite | `uv run pytest -q` | 83 passed, 0 failed, 0 skipped (40 offline unit, 43 PostgreSQL integration/acceptance) | `tests/` | Real PostgreSQL; simulated demo surface |
| Coverage | `uv run pytest -q --cov=opsaccord --cov-report=term` | 78% (2648 statements, 583 missed) | - | Real PostgreSQL |
| Scenarios P1-01..P1-09 | `uv run python -m opsaccord.cli run-scenarios --json reports/local/scenarios.json` | 9/9 PASS | `docs/reports/phase1-scenarios.json` (committed copy) | Simulated demo; real PostgreSQL |
| Migrations and graph checkpoints | `uv run python -m opsaccord.cli init-db` | Alembic `upgrade head` plus the LangGraph checkpoint schema created | `.local/pg/` cluster | Live PostgreSQL |
| Payment path over HTTP | `uv run python -m opsaccord.cli serve-api`, then create/advance/approve/advance/snapshot | Create -> `WAITING_FOR_APPROVAL`; agent approval `403`; human approval `APPROVED`; advance -> `RESOLVED`; 1 action `SUCCEEDED` (`external_outcome=CONFIRMED_APPLIED`); approval `CONSUMED`; 19 audit events; 8 evidence records | `tests/acceptance/test_api_controls.py` | Real PostgreSQL; simulated demo surface |
| Streamlit console | `uv run python -m opsaccord.cli serve-ui` (port 8511) plus a Streamlit `AppTest` run of the script | No exceptions; rendered `Status / Service / Evidence / Steps` metrics (RESOLVED, payment-service, 8 evidence, 10/16 steps), tabs State, Evidence timeline, Proposals & actions, Human controls, and the approve/reject, takeover, constraint, advance, and reconcile controls | Manual run (see limitations) | Real PostgreSQL through the live API |
| CI workflow | Not executed here (no GitHub Actions run available) | Workflow validated as YAML and reviewed against the local commands | `.github/workflows/ci.yml` | n/a |
| Live Astronomy Shop paths | Not runnable | Blocked: `docker` absent, `podman` absent, `/var/run/docker.sock` missing | `docs/astronomy-shop.md` | Not run |
| Live telemetry/action adapter | Not implemented | No live adapter exists, and `demo_mode=live` is refused | `docs/astronomy-shop.md` | Not run |
| Paid application-model usage/cost | `OPSACCORD_ALLOW_PAID_MODEL_CALLS=false` | Not run; cost unknown, not zero | - | Not run |

### Scenario outcomes

All nine scenarios pass with executable assertions. Statuses are the *expected*
outcomes from `docs/phase1.md`, not adjusted results.

| Scenario | Title | Final status | Executed actions | Duplicate side effects | Forbidden actions |
| --- | --- | --- | --- | --- | --- |
| P1-01 | Payment unreachable | RESOLVED | 1 | 0 | 0 |
| P1-02 | Ad high CPU | RESOLVED | 1 | 0 | 0 |
| P1-03 | Cart failure, no permitted remediation | READY_FOR_HUMAN | 0 | 0 | 0 |
| P1-04 | Metrics read timeout | ESCALATED_INCOMPLETE | 0 | 0 | 0 |
| P1-05 | Payment approval rejected | READY_FOR_HUMAN | 0 | 0 | 0 |
| P1-06 | Payment action times out after dispatch | ESCALATED_INCOMPLETE | 1 | 0 | 0 |
| P1-07 | Action succeeds but the service stays unhealthy | ESCALATED_INCOMPLETE | 1 | 0 | 0 |
| P1-08 | Malicious runbook proposes a shell command | READY_FOR_HUMAN | 0 | 0 | 0 |
| P1-09 | Runbook aims an allow-listed remediation at a non-permitted target | READY_FOR_HUMAN | 0 | 0 | 0 |

`P1-06` and `P1-07` execute exactly one action and still end in
`ESCALATED_INCOMPLETE`: the uncertainty is preserved instead of being retried or
reported as recovery. `P1-07` records `VERIFICATION_FAILURE`, so a successful
command is never treated as evidence of recovery.

### Adversarial checks (14 control tests plus the 9 scenario safety assertions, all passing)

Approval bound to action *and* intent (tamper test), consumed-approval reuse,
expiry, two-thread claim race, constraint added after approval, uncertain
dispatch reconciled rather than retried, crash after claim before dispatch,
restart while waiting for approval resuming from PostgreSQL, soft/hard budget
walls and extension limits, malicious runbook never reaching the tool layer,
verification freshness, self-approval refusal, success-is-not-recovery, and
proposal attempts to widen the tool surface.

## Optional external review, findings, and risk

**Optional external review not run.** The authorized subscription-backed Claude
Code harness is installed and authenticated (`claude --version` -> `2.1.267`;
`claude auth status` -> `loggedIn: true`, `authMethod: "claude.ai"`,
`subscriptionType: "pro"`), and it was never routed through the DeepSeek
endpoint or given an API key. It could not be used:

1. A bounded probe (`claude -p "Reply with exactly: harness-ready"
   --output-format json --tools ""`) ran inside the restricted sandbox and
   returned `terminal_reason: "api_error"`, `result: "Request timed out"`, and
   `total_cost_usd: 0` after 185 s: the sandbox has no network egress.
2. The documented escalation retry for that sandbox-network failure was rejected
   by the environment's automatic approval reviewer, which is misconfigured for
   this setup. The rejection text was: `The supported API model names are
   deepseek-flash, deepseek-v4-pro, but you passed codex-auto-review.` followed by
   `This is a review failure, not a determination that the action is unsafe. Do
   not bypass the approval check; resolve the error or ask the user for
   guidance.`

That is a deterministic configuration failure rather than a transient one, so no
further attempts were made and no approval check was bypassed. Fallback used
instead: the DeepSeek-backed Codex agent plus the deterministic test and
scenario suites described above. No Claude implementation help was used either,
so there is no partially reviewed work to reconcile, and
`docs/reports/phase1-review.md` was not created.

### Findings fixed during implementation

Defects found by the checks above and fixed on this branch:

- **`advance` chose the wrong graph entry point (found by this session's API
  smoke test, not by the existing suite).** `POST /incidents/{id}/advance` called
  `resume` on a workflow that had never started, so the first advance for a new
  incident returned HTTP 500 (`KeyError: 'incident_id'` from the `investigate`
  node). The console's only control for moving an incident forward was therefore
  broken end to end. Fixed by adding `WorkflowController.advance`, which resumes
  only when the persisted status is `WAITING_FOR_APPROVAL` (the approval
  interrupt) and otherwise starts or continues the graph, and by routing the
  endpoint through it. Covered by `tests/acceptance/test_api_controls.py` (3
  tests) so the HTTP surface can no longer regress unnoticed.
- **Simulated evidence could be labelled live.** `OPSACCORD_DEMO_MODE=live` was
  accepted while every tool result still came from the in-process simulator, and
  the health endpoint and console banner would have reported `demo_mode=live`.
  `Settings` now rejects `live` outright until a live adapter exists (unit test
  `test_live_demo_mode_is_refused_while_only_the_simulator_exists`).
- **Wrong upstream pin.** The revision was recorded as `1.11.1`, which does not
  carry the verified flag set; it is now pinned to `3.1.0` (commit
  `dedc0178918e260823323b8d95005a8cb924b007`) with the provenance in
  `docs/astronomy-shop.md`.
- **Checkpoints in the wrong database.** `checkpoint_url` silently fell back to
  the development database during test runs, so a recovery test could have
  resumed from checkpoints written against a different database than the
  authoritative rows. Checkpoints now follow the state database for the selected
  environment.
- **Allow-listed remediation could be blocked by a catalog flag.** The gateway
  treated the action catalog's `requires_approval=True` as authoritative even
  when the deterministic policy matched the allow-listed autonomous rule, which
  broke the P1-02 path. The gateway now requires the named
  `ALLOW_LISTED_REMEDIATION` rule instead of trusting a bare flag.
- **Re-entering the approval node minted a fresh approval**, which would have
  discarded a human rejection, an expiry, or a consumed decision on resume. One
  approval request is now created per action.
- **A registry/typed refusal was reported as an escalation** rather than a
  settled handoff; a refusal is now a `READY_FOR_HUMAN` outcome.
- **Degraded telemetry could still produce a proposal.** Recorded collection
  errors now escalate instead of proposing from a lone health probe, and
  untrusted telemetry text is parsed defensively.
- **Approval consumption did not verify the action/intent pair**, leaving an
  impersonation gap where one action's approval could authorise another action's
  dispatch. Consumption now requires both the action id and the intent digest.
- **A human freeze arriving mid-verification crashed the graph.** Verification
  reads now treat a policy block as `VERIFICATION_INCONCLUSIVE`.

### Unresolved findings, limitations, and risk

- **Live coverage is absent** (blocker below). Everything reported as passing
  above ran against the simulated demo surface with real PostgreSQL. No claim is
  made about live Astronomy Shop behavior.
- **The UI script is not covered by the automated suite.** Coverage reports 0%
  for `src/opsaccord/ui/app.py` and `src/opsaccord/cli.py` because they are entry
  points; the console was validated by running it and by a Streamlit `AppTest`
  run recorded manually, not by a committed test. An automated UI-level test is
  outstanding work.
- **`reasoning/openai_compatible.py` is 29% covered** by design: the adapter
  refuses to call out unless paid calls are enabled and cost rates are
  configured, so its HTTP path is untested here.
- **The CI workflow has never run.** It is reviewed and YAML-validated only.
- **`restart_workload`** exists as a typed remediation but is exercised only as a
  gated capability, not as a scenario outcome.
- **Approval expiry is enforced by an explicit sweep**
  (`expire_stale_approvals`) plus a claim-time check rather than a background
  scheduler, so a long-idle workflow relies on the next request to trigger it.
- **The test suite and the scenario CLI must not run concurrently.** The
  session fixture resets the shared `*_test` schema, so starting
  `run-scenarios` while `pytest` is running lets the reset pull tables out from
  under the scenarios (observed once during this session, as
  `UndefinedTable: relation "checkpoints" does not exist`). Run them
  sequentially; `scripts/run_scenarios.sh` does this correctly.

## Remaining work and discussion

### Blockers and deviations

1. **Live demonstration is blocked by missing infrastructure.** `docker` is not
   on `PATH` in this WSL distribution, `podman` is absent, and
   `/var/run/docker.sock` does not exist, so the pinned Astronomy Shop stack
   could not be started. Consequence: the four primary paths were demonstrated
   against the simulator only, and no live telemetry/action adapter exists. This
   is the single reason the status is PARTIAL.
2. **Optional external review was blocked by the environment**, as recorded
   above: no sandbox network egress, and the escalation path failed inside the
   approval reviewer before reaching a decision.
3. **Deviation: the retired reviewer config was deleted.** The committed file
   `.codex/agents/luna_reviewer.toml` configured a `gpt-6-luna` reviewer that is
   not present in the model catalog and that the revised handoff explicitly
   retires. It was removed so no future run tries to launch it. Say the word if
   it should be restored for a later phase.
4. **Deviation: `docs/phase1.md` now lists nine scenarios**, adding the two
   injection cases (`P1-08`, `P1-09`) that the suite already implements, with the
   completion-gate wording updated to match. No acceptance criterion was
   weakened.

### Decisions for discussion

- Whether PARTIAL is the right call, or whether the live-demo gate is considered
  deferrable and the branch should be discussed as-is.
- Whether to implement the live adapter next (typed Prometheus/Jaeger reads and a
  flagd-ui read-modify-write client), and in which environment.
- Whether to fix the automatic approval reviewer (it accepts only DeepSeek model
  names) so an optional Claude review can be requested, or to run that review
  manually from a session with network access.
- Whether to add an automated Streamlit `AppTest` test and a live-adapter test to
  CI, and whether CI should run on this branch before merge.
- Whether a background approval-expiry sweep is wanted instead of the current
  request-triggered sweep.

### Startup, demonstration, verification, and teardown

```
# startup
uv sync --all-groups
scripts/pg.sh start
uv run python -m opsaccord.cli init-db
uv run python -m opsaccord.cli serve-api      # http://127.0.0.1:8410
uv run python -m opsaccord.cli serve-ui       # http://127.0.0.1:8511

# demonstration
scripts/run_scenarios.sh                                          # all nine scenarios
uv run python -m opsaccord.cli run-scenarios --scenario P1-01     # one scenario

# verification
uv run pytest -q                              # all 83 tests
uv run pytest -m "not integration"            # offline unit tests only
uv run pytest -m integration                  # PostgreSQL-backed tests only
uv run ruff check . && uv run ruff format --check . && uv run mypy src/opsaccord

# teardown
#   Ctrl-C the API and UI processes, then:
scripts/pg.sh stop
scripts/pg.sh status
```

Console walkthrough: open the console, select the `P1-01` incident (or create one
from the sidebar with scenario `P1-01`), press **Advance workflow** to reach the
approval gate, read the evidence timeline, then approve and advance again to see
`RESOLVED`. **Take over (freeze automation)** freezes execution, and the
constraint form denies a tool or a target for that incident.

### Pull request

No PR was opened from this session, and the branch was **not** pushed. The work
is committed locally on `phase1/baseline` (`1296b23` implementation, followed by
the commit that adds this report). `git push -u origin phase1/baseline` failed
twice for environment reasons and no approval check was bypassed:

1. In the sandbox it failed with `Bad owner or permissions on
   /etc/ssh/ssh_config.d/20-systemd-ssh-proxy.conf` and
   `fatal: Could not read from remote repository`.
2. The escalated retry was rejected by the same misconfigured approval reviewer
   recorded in the external-review section (`The supported API model names are
   deepseek-flash, deepseek-v4-pro, but you passed codex-auto-review.`).

If a PR is opened later it should be a draft, and the branch must not be merged.
