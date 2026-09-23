# Phase 1 implementation report

## Identity and status

| Item | Value |
| --- | --- |
| Repository | `mingyang-ow/OpsAccord` (private), remote `git@github.com:mingyang-ow/OpsAccord.git` |
| Branch | `phase1/baseline` |
| Base commit | `3168bcf` (branch point: design baseline and Phase 1 foundation) |
| Implementation commit | `5102dbe` (the commit immediately preceding this report) |
| Branch commits | `1296b23`, `821414a`, `b4b75aa`, `5102dbe`, then `5cbcce2` and `b508a0f` - the per-revision totals are reproducible with `git diff --shortstat 3168bcf <rev>`; for the branch tip as it stood at `b508a0f` that reports 87 files changed, 18404 insertions(+), 42 deletions(-) |
| Dirty state | Clean apart from this report and the two evidence files it commits; only gitignored local state (`.local/`, `reports/local/`, `.venv/`) otherwise remains |
| Host | WSL 2, kernel `6.18.33.2-microsoft-standard-WSL2`, Ubuntu 26.04 LTS, 16 CPU, 15.5 GB RAM |
| Toolchain | `uv` 0.12.5, Python 3.12.14 (`.python-version`), project-local PostgreSQL 18.6 on `127.0.0.1:55432`, Docker 29.7.2 (client and server), Docker Compose v5.4.0 |
| Coding agent | Codex CLI agent on the user's configured DeepSeek provider; the environment reports `deepseek-flash` / `deepseek-v4-pro` as its supported models |
| Application runtime model | `deterministic` / `deterministic-v1` - no paid application-model calls were made |
| Optional second review | Claude Code 2.1.267, model `claude-opus-5`, subscription-backed - see `docs/reports/phase1-review.md` |
| **Overall status at `5102dbe`** | **READY_FOR_DISCUSSION** |
| Safety-pass status (branch tip) | **PARTIAL** - both follow-up defects are implemented with regression tests and every offline check passes, but the PostgreSQL concurrency, recovery, scenario and live checks could not run and no optional review ran. See "Safety pass after `5cbcce2`". |
| Validation-pass status | **PARTIAL** - the environment was diagnosed precisely (host services up, sandbox networking denied) and a runnable pre-fix sensitivity harness was added, but no database-backed check has still executed. See "Validation attempt after `b508a0f`". |

`READY_FOR_DISCUSSION` means every executable technical gate in `docs/phase1.md`
passed on this commit, including the four primary paths against the real isolated
demo stack. It is **not** user acceptance, it is not permission to merge, and no
merge, release, visibility change, or Phase 2 work was performed. The one gate
that cannot be satisfied from this environment is delivery: the branch could not
be pushed or opened as a draft PR because the harness's automatic approval
review is misconfigured (see "Remaining work and discussion").

## Delivered behavior

Implemented and working, against the pinned demo stack:

- **Typed domain contracts** (`domain/`): incidents, workflows, evidence,
  decisions, actions, approvals, constraints, budgets, audit events, a service
  registry, and an explicit transition map with terminal handoff outcomes
  (`READY_FOR_HUMAN`, `ESCALATED_INCOMPLETE`).
- **Workflow controller** (`workflow/controller.py`): LangGraph orchestration with
  PostgreSQL checkpoints, `run`/`resume`/`advance`/`reconcile` entry points,
  budget accounting, human takeover, and reconciliation of in-flight actions.
- **Deterministic policy engine** (`policy/engine.py`): fixed target allow-list,
  human constraints and takeover ahead of everything else, exact-approval
  requirements for critical services, an allow-listed autonomous path scoped to
  the service under investigation, and a hard budget wall. It performs no I/O and
  is a pure function of persisted rows.
- **Typed tool gateway** (`tools/gateway.py`): registry-validated arguments on
  both the read and write paths, bounded read retries, atomic claim plus
  approval consumption, full policy revalidation under the incident row lock
  immediately before dispatch, no blind retries, ledger durability across budget
  errors, and fresh-verification enforcement.
- **Two reasoning adapters behind one interface** (`reasoning/`): the
  deterministic adapter used by every scenario, and an OpenAI-compatible adapter
  for a real provider. The runtime model is configuration, never the coding
  agent's provider.
- **Live and simulated environments behind one contract** (`tools/contracts.py`):
  a fixture-backed demo adapter used by the offline suite and a live adapter that
  reads the real Prometheus, Jaeger, and flagd-ui surfaces for the pinned
  Astronomy Shop revision. Live mode fails closed - an unreachable stack produces
  `COLLECTION_ERROR` evidence, never a simulated success.
- **FastAPI service and Streamlit console**: incident state, evidence timeline,
  approvals, rejection, constraints, and takeover, all enforced on the backend.
- **Demo tooling** (`scripts/demo.sh`, `scripts/pg.sh`,
  `scripts/run_live_scenarios.sh`, `demo/compose.opsaccord.yaml`): reproducible
  local start, health, endpoint discovery, and teardown for the project's own
  resources only.

Placeholders, stated plainly: the OpenAI-compatible reasoning adapter is
implemented but has never been exercised against a paid endpoint, so no
application-model behaviour is evidenced anywhere in this report. The Streamlit
console is a minimal operator surface, not a production UI. Recovery is
demonstrated for the workflow/action lifecycle, not for a multi-process
deployment.

## Validation evidence

| Check / scenario | Exact command | Result | Evidence path | Simulated or live |
| --- | --- | --- | --- | --- |
| Lint | `uv run ruff check .` | clean | - | offline |
| Format | `uv run ruff format --check .` | 68 files already formatted | - | offline |
| Types | `uv run mypy src/opsaccord` | success, 39 source files | - | offline |
| Offline + integration suite | `uv run pytest -q` | **135 passed, 7 skipped**, 1 warning, 11.2s | - | simulated |
| P1-01 payment unreachable | `uv run python -m opsaccord.cli run-scenarios` | PASS, `RESOLVED`, 1 action, 1 approval, `VERIFICATION_SUCCESS` | `docs/reports/phase1-scenarios.json` | simulated |
| P1-02 ad high CPU | as above | PASS, `RESOLVED`, 1 allow-listed action, `VERIFICATION_SUCCESS` | same | simulated |
| P1-03 cart failure | as above | PASS, `READY_FOR_HUMAN`, 0 actions | same | simulated |
| P1-04 metrics timeout | as above | PASS, `ESCALATED_INCOMPLETE`, 0 actions | same | simulated |
| P1-05 approval rejected | as above | PASS, `READY_FOR_HUMAN`, 0 actions | same | simulated |
| P1-06 action timeout | as above | PASS, `ESCALATED_INCOMPLETE`, 1 action preserved | same | simulated |
| P1-07 success but unhealthy | as above | PASS, `ESCALATED_INCOMPLETE`, 1 action, `VERIFICATION_FAILURE`, not resolved | same | simulated |
| P1-08 malicious runbook | as above | PASS, `READY_FOR_HUMAN`, 0 actions | same | simulated |
| P1-09 runbook retarget | as above | PASS, `READY_FOR_HUMAN`, 0 actions | same | simulated |
| Seven live paths | `set -a; . .local/demo-endpoints.env; set +a; uv run python -m opsaccord.cli run-live-scenarios --json reports/local/live-scenarios-final.json` | **7/7 PASS**, 0 duplicate side effects, 0 forbidden actions, 0 simulated evidence records | `docs/reports/phase1-live-scenarios.json` | **live** |
| Live integration tests | `OPSACCORD_LIVE_DEMO=1 uv run pytest -m live_demo -v --durations=0` | **7 passed**, 161.7s (payment path 90.6s) | `reports/local/live-pytest-final.log` (untracked) | **live** |
| PostgreSQL isolation | `uv run pytest -q tests/integration/test_isolation.py` | passed: three environments resolve to three databases, the destructive reset refuses every non-`*_test` database, a session reset leaves demo rows intact, and a second concurrent session is refused by the advisory lock | `tests/support/isolation.py` | simulated |
| Restart / recovery | `uv run pytest -q tests/acceptance tests/integration` | passed: restart while waiting for approval resumes from PostgreSQL and reaches `RESOLVED`; a crash after claim is not blind-retried; a crash after dispatch does not resolve | `tests/acceptance/test_adversarial_controls.py` | simulated |
| Action deduplication | included in the suites and both scenario runs | 0 duplicate side effects in 9 simulated and 7 live runs; the claim is a conditional update and the approval is consumed inside the same transaction | scenario JSON records | both |
| Adversarial controls | `uv run pytest -q tests/acceptance/test_adversarial_controls.py` | passed: approval bound to action+intent identity, expiry, and reuse; competing workers; takeovers and constraints; budget exhaustion; malicious runbook; false recovery | same | simulated |
| Optional second review | `claude -p --permission-mode dontAsk --allowed-tools ... --output-format json` | 11 findings, all confirmed and fixed; reverting the fixes fails 14 tests | `docs/reports/phase1-review.md` | review |
| Upstream revision | `git -C /mnt/c/Users/admin/opsaccord-demo/opentelemetry-demo describe --tags` | tag `3.1.0`, commit `dedc0178918e260823323b8d95005a8cb924b007` | `docs/astronomy-shop.md` | live |

Every skip is explained: the seven skipped tests are the opt-in live tests plus
the live-marked cases, which require `OPSACCORD_LIVE_DEMO=1` and the running
stack - and are run separately, as the table above shows.

Application-model usage and cost: **not run**. The deterministic adapter is the
only reasoning provider used for evidence, so there is no token spend to report.
The OpenAI-compatible adapter is configuration-complete but untested against a
paid endpoint.

### What "live" does and does not mean here

The live runs are **live tools with deterministic reasoning**. Prometheus, Jaeger
and flagd-ui are the real pinned stack, the induced faults are real feature-flag
writes, the evidence is real, and `simulated_evidence` is 0 in every record. The
diagnosis is produced by the deterministic adapter, not by a model, so this is
**not** evidence of live model behaviour, and P1-06/P1-07 are honest about the
faults that a feature flag cannot produce: the flag control plane is genuinely
unroutable after approval, and P1-07 re-arms the flag right after dispatch.

## Environment arrangement

Docker Desktop on Windows, driven from WSL through the integration:
`DC=/mnt/c/Users/admin/AppData/Local/Programs/DockerDesktop/resources/bin/docker.exe`,
Compose v5.4.0, 29.7.2 client and server. The demo is the pinned upstream checkout
(`/mnt/c/Users/admin/opsaccord-demo/opentelemetry-demo`, tag `3.1.0`, commit
`dedc0178`); `scripts/demo.sh status` reports 25 containers with the stack
healthy. Only `frontend-proxy` (8080) and `prometheus` (9090) hold stable host
ports - every other service publishes an ephemeral port, so endpoints are
discovered, written to gitignored `.local/demo-endpoints.env`, and never assumed.
PostgreSQL is project-local on `127.0.0.1:55432`; three databases are used
(`opsaccord`, `opsaccord_scenarios`, `opsaccord_test`) so scenario runs, the
offline suite, and live runs cannot overwrite each other, and the suite holds a
database-scoped advisory lock while it rebuilds its own schema.

## Calibration findings (measured against the pinned revision)

- **Span names**: payment `oteldemo.PaymentService/Charge`, ad
  `oteldemo.AdService/GetAds`, checkout `oteldemo.CheckoutService/PlaceOrder`,
  cart `POST /oteldemo.CartService/EmptyCart` (ASP.NET naming).
- **`cartFailure` breaks `EmptyCart`, not `GetCart`** - reading `GetCart` was why
  the first live cart run measured nothing.
- **Two telemetry cadences**: span metrics publish every ~60s, so a probe window
  shorter than 120s reads empty (measured: a 60s window was empty in 24 of 24
  samples; 90s in 12 of 24; 120s and above never). Container metrics are never
  older than ~8s.
- **`container_cpu_utilization_ratio` is unusable on this stack** (it read 4.015
  while `docker stats` said 0.11% and stayed wrong after the burn stopped). CPU is
  read as `sum(rate(container_cpu_usage_nanoseconds_total{...}[W])) / 1e9`, which
  matched `docker stats` to two decimal places.
- **Measured fault signatures**: Charge error share 0.0 to ~0.33; PlaceOrder
  error share 0.0 to ~0.75; ad CPU ~0.010 to ~1.06 cores; cart `EmptyCart`
  success frozen with the error series growing.
- **The load generator is bursty**, so a quiet window is neither a symptom nor a
  recovery; the live driver waits for real traffic before arming a fault and waits
  until the fault is visible in telemetry before measuring.
- **Resource deviation**: the pinned `checkout` service never becomes healthy at
  its upstream `memory: 20M` / `GOMEMLIMIT=16MiB` cap (it spins at ~700% CPU and
  never accepts a gRPC connection). `demo/compose.opsaccord.yaml` raises only the
  memory limit and `GOMEMLIMIT` (256M / 192MiB) and changes nothing else - no
  image, port, address, or flag. `checkout` is the service that calls payment, so
  the Payment path cannot be demonstrated without it. This is recorded as an
  environment deviation, not a change to the fault/remediation model.

## Optional external review, findings, and risk

**The optional second review ran.** Tool: Claude Code CLI `2.1.267`, model
`claude-opus-5[1m]`, subscription-backed (`authMethod: claude.ai`, first-party
provider, no API key), reviewed revision `b4b75aa`, read-only access. It reported
**11 findings, all of which were reproduced in the code, confirmed, fixed, and
pinned with a regression test**; reverting the source fixes makes 14 tests fail.
Its findings, reproductions, dispositions, and limitations are recorded in full in
`docs/reports/phase1-review.md`, kept separate from the self-checks above.

Two of the findings were serious enough to change the safety story:

- `_route_after_verify` blocked `RESOLVED` only on `UNCERTAIN`, so an action left
  in `DISPATCHING` - a real side effect whose outcome was never recorded - did not
  prevent resolution. Verified empirically: with the old predicate the route
  returns `resolved` for a state where the new one escalates.
- The autonomous, approval-free remediation path was not bound to the incident's
  service (`incident_service` was populated with an incident id and never read by
  policy), so untrusted runbook text could aim it at a *different* allow-listed
  service. It now requires an exact human approval for a cross-service target.

Unresolved findings and known limitations, stated rather than hidden:

- The reviewer executed nothing (read-only access), so its concurrency findings
  were analytical. The control-versus-dispatch race was then reproduced
  deterministically by the implementation agent and closed with the incident row
  lock; the competing-worker claim race remains covered by test rather than by a
  live reproduction.
- The incident row lock serialises takeover and incident-scoped constraints
  against dispatch. A **global** constraint (`incident_id IS NULL`) has no
  incident row to lock, so it is not serialised against an in-flight dispatch.
  Global constraints are not used by any Phase 1 path; this is a residual gap.
- Reconciliation is now deliberately more conservative: an idempotency key with no
  record reconciles to `UNKNOWN`, so the simulated crash case ends in a handoff
  (`ESCALATED_INCOMPLETE`) instead of a confident `FAILED`. This is safer and it
  changes one pre-existing test expectation, which is why that test now asserts
  `UNCERTAIN` and non-resolution.
- Live verification is slow by construction (~60s span-metric cadence, 120s probe
  windows, up to six 30s settle rounds), which is why a live run takes minutes.
- `P1-04` deliberately points at an unroutable address, so its live PASS is a
  property of the plan meeting the expectation, not of the stack being exercised
  by that scenario; the CLI preflight is what proves the stack answered at all.
  The live result now derives its provenance from collected evidence and records
  an explicit reason if simulator evidence ever appears.
- No paid application-model call was made, so nothing here evidences model
  behaviour.

## Safety pass after `5cbcce2` - control ordering and uncertain outcomes

**Status: PARTIAL.** The two defects the planning conversation asked to close are
implemented on this branch with regression tests, and every offline check passes.
The database-backed concurrency and recovery tests, the scenario suites and the
seven live paths could not run here, because this sandbox forbids **all** socket
operations: no PostgreSQL, no network, and no escalated command (the harness's
automatic approval review is misconfigured). Optional review did not run either.
Nothing below is called verified unless it says how it was verified.

### What was fixed

**A2 - a global constraint was not ordered against dispatch.**

The incident row lock serialises takeover and incident-scoped constraints against
a dispatcher's revalidate-and-claim window, but `repositories.add_constraint` only
took it when `incident_id is not None`. A constraint with `incident_id IS NULL` is
read by *every* incident's policy evaluation
(`list_active_constraints` matches `incident_id IS NULL OR incident_id = :id`) and
yet has no incident row to lock, so under READ COMMITTED an insert committing
after the dispatcher's control read and before its claim was invisible and the
side effect went out. Locking the existing constraint rows could not fix this:
an `INSERT` is not blocked by a lock on rows that already exist, and with no
constraints recorded there is nothing to lock at all.

The fix introduces one global transaction-scoped advisory lock - the *control
gate*, `pg_advisory_xact_lock(11296, 1)` in `src/opsaccord/persistence/locks.py` -
and takes it as the first lock of every side-effect authorization and every human
control write (`add_constraint` for both scopes, `set_takeover`). The documented
lock order is **control gate -> incident row -> workflow row**, so the design has
no lock cycle by construction; the gate is held only for the short in-database
authorization transaction, never across the external call. Transaction scope is
load-bearing twice: the server releases the lock on commit, rollback, or backend
death, so a crashed worker cannot freeze controls, and it cannot leak into the
next borrowed pooled connection.

**A1 - an unclassified execution error asserted non-execution.**

`execute_action`'s general handler recorded `FAILED` / `CONFIRMED_NOT_APPLIED` for
any `Exception`. An adapter can apply a change and then fail while reading,
parsing, constructing or recording its acknowledgement, so that asserted a
non-execution the code had no evidence for; the adjacent `DispatchTimeout` branch
already recorded `UNCERTAIN` / `UNKNOWN`.

The fix moves the whole mapping into one place, `classify_dispatch_failure` in
`src/opsaccord/tools/contracts.py`: a proved pre-dispatch refusal
(`DispatchRefused`, new) is a definite non-execution, a `DispatchTimeout` is
unknown, and **anything else is unknown**. Adapters gained the matching
obligation, stated in the protocol: report `succeeded=False` only when the change
provably did not happen. Reconciliation was corrected on the same principle: the
live adapter used to infer `CONFIRMED_NOT_APPLIED` from the desired flag value
being absent, which is a statement about state, not about who wrote it - another
actor may have changed the flag afterwards - so absence now reconciles to
`UNKNOWN`. `plan_action` already reuses an action id for the same intent unless the
action is `FAILED` / `CANCELLED` / `REJECTED`, so an `UNCERTAIN` action can never
be replaced by a fresh id, and `DISPATCHING`/`UNCERTAIN` both escalate.

### The guarantee as implemented

- An action is committed to execution when the authorization transaction commits.
- A control that commits before that reads as active and blocks the dispatch; a
  control that commits after it cannot cancel the dispatch. There is no
  half-committed third case a dispatcher can read.
- Approval decisions need no gate: the claim's conditional update requires the
  action row to be `APPROVED` *and* the bound approval to be unconsumed,
  unexpired, and matched on action id and intent digest, so a rejection that
  commits first makes the claim fail.
- Residual limitation, documented in the API and in `docs/architecture.md`: a
  freeze or takeover cannot recall a request that is already in flight, because
  the demo surfaces have no cancellation primitive. The `action.claimed` audit
  event now carries the constraint snapshot the authorization actually evaluated,
  so an operator can reconstruct which side of the boundary a control landed on.
- `created_at` and `dispatch_started_at` are transaction-local clocks, not commit
  timestamps: comparing them is evidence of ordering, not proof. The gate, not the
  timestamp, establishes the ordering at runtime.

### Verification performed in this session

| Check | Exact command | Result |
| --- | --- | --- |
| Lint | `uv run ruff check .` | clean |
| Format | `uv run ruff format --check .` | 73 files already formatted |
| Types | `uv run mypy src/opsaccord` | success, 40 source files |
| Offline suite | `uv run pytest -m "not integration" -q` | **89 passed, 7 skipped, 78 deselected** |
| New outcome-contract tests | `uv run pytest tests/unit/test_dispatch_outcomes.py tests/unit/test_persistence_locks.py -q` | 12 passed |
| New adapter-contract tests | `uv run pytest tests/unit/test_demo_adapter_outcomes.py -q` | 6 passed |
| New integration tests | `uv run pytest tests/integration/test_dispatch_safety.py --collect-only -q` | 14 tests collected (not run: no PostgreSQL) |
| Pre-fix reproduction | `uv run python reports/local/safety-fix-prefix-repro.py` | both defects reproduced from the `5cbcce2` source; see below |

The seven skipped tests are the opt-in live tests, which need
`OPSACCORD_LIVE_DEMO=1` and the running demo stack.

Every offline check above was re-run in the delivery session that committed this
pass, with identical results (lint clean, 73 files formatted, 40 source files
typed, 89 passed / 7 skipped / 78 deselected, and the reproduction script exiting
`0` with both verdicts `True`). The two sessions were the same environment, so
this is a repeat measurement, not independent verification.

### Verification that could not run

The blocker is environmental and precise: `socket.socket(AF_INET)` raises
`PermissionError [Errno 1]`, `AF_UNIX` `bind()` and `connect()` raise the same,
and any escalated command is rejected by the harness's automatic approval review
(*"The supported API model names are deepseek-flash, deepseek-v4-pro, but you
passed codex-auto-review"*). A PostgreSQL-backed test therefore errors during
fixture setup in 0.21s: `sqlalchemy.exc.OperationalError: (psycopg.OperationalError)
connection is bad: no error details available`.

Outstanding because of it, and not claimed as passing:

- `tests/integration/test_dispatch_safety.py` - all 14 concurrency, ordering and
  uncertainty tests, including the two that are *written* to fail on the pre-fix
  code (the probe that must find the gate held across revalidate-and-claim, and
  the freeze writer that must block while the gate is held). That expectation is
  the implementation agent's design argument about code it wrote; neither the
  pre-fix failures nor the post-fix passes were executed, because the suite needs
  PostgreSQL. It is outstanding, not established.
  `scripts/prefix-sensitivity.sh` covers the same two defects with a portable
  harness that *can* run against `5cbcce2`, but it has not run either, and it does
  not replace these 14 tests: it is a smaller, revision-portable demonstration.
- The pre-fix *concurrency* reproduction: the rule-level reproduction below does
  not need a database, but the race itself needs two concurrent transactions.
- `tests/acceptance` and the remaining `tests/integration` suites (64 tests), the
  nine simulated scenarios, and the seven live paths. The change touches dispatch
  and recovery, so the live suite is the check that matters most.
- `reports/local/live-scenarios-final.json` and the live pytest run remain at
  their `5cbcce2` values; they do not describe this revision.

### Pre-fix reproduction, and its exact scope

`reports/local/safety-fix-prefix-repro.py` (untracked, output in
`reports/local/safety-fix-prefix-repro.txt`) loads the `5cbcce2` sources with
`git show` and demonstrates both decisions:

- **A2:** the pre-fix `add_constraint(incident_id=None)` issued **no lock
  statement at all** against a recording session; the fixed function issues
  `SELECT pg_advisory_xact_lock(:classid, :objid)`.
- **A1:** the demo adapter applies the change and then fails while building its
  acknowledgement (`ValueError: malformed acknowledgement ...`), leaving
  `applied` populated and the service recovered. The two `5cbcce2` handler lines
  are extracted verbatim from `gateway.py:394-395` and executed against that real
  exception: they record `FAILED` / `CONFIRMED_NOT_APPLIED`. The fixed classifier
  records `UNCERTAIN` / `UNKNOWN` for the same exception.

This reproduces the *decision* each defect turned on, not a full pre-fix
execution of the dispatch path, which needs a database. The concurrency itself -
a freeze committing inside an authorization window, or a probe finding the gate
held - is not reproduced here and is exactly what the unrun integration tests
establish.

### Validation attempt after `b508a0f`

The follow-up pass was asked to restore the environment that previously ran the live
suite, then run the database-backed and scenario checks. It could not, and the reason
is not a stopped service.

**Diagnosis: host services are up; the sandbox denies every socket.** The
project-local cluster is running - `.local/pg/data/postmaster.pid` records PID 22096 on
port 55432 with socket directory `.local/pg/run`, and `.local/pg/run/.s.PGSQL.55432`
was rewritten at 20:48, long after the last commit. From inside an agent command,
however, both `socket.socket(AF_INET).connect(("127.0.0.1", 55432))` and
`socket.socket(AF_UNIX).connect(...)` raise `PermissionError [Errno 1] Operation not
permitted`, and the sandbox process itself is launched as
`codex-linux-sandbox ... "network":"restricted" --apply-seccomp-then-exec`. That is a
seccomp denial inside the agent's own process tree, not a stopped cluster, not a bad
DSN, and not a Docker problem. The same applies to the demo stack: it cannot be probed
from here at all.

**Escalation: attempted once, rejected by the harness.** Running the integration suite
through the normal supported approval mechanism returned the harness's own failure,
*"The supported API model names are deepseek-flash, deepseek-v4-pro, but you passed
codex-auto-review."* Per the handoff, that route was not retried, and nothing was
bypassed or disabled.

**Prepared instead: a runnable pre-fix sensitivity harness.** `scripts/prefix-sensitivity.sh`
materialises `5cbcce2` and the fixed revision read-only with `git archive`, copies
`scripts/prefix_sensitivity/test_prefix_defect_sensitivity.py` into each, and runs it
against the dedicated `*_test` database. The harness is deliberately portable: it uses
the advisory key as a literal, monkeypatches `repositories.claim_action` rather than
calling it with the new `commit_context` argument, and defines its own apply-then-raise
adapter, so on `5cbcce2` it *imports and executes* instead of failing on a module the fix
added. Expected: the control test passes in both revisions, and the two defect tests fail
on `5cbcce2` and pass on the fixed revision. Verified offline here: both materialisations
import the right source (`5cbcce2`'s `contracts.py`, which has no `DispatchRefused`) and
the harness collects three tests in each; the run itself stops at fixture setup with
`psycopg.OperationalError: connection is bad`, which the script now reports distinctly
as "cannot run" (exit 2) rather than as a result.

Commands for a normal WSL terminal, with expected results:

| Step | Command | Expected |
| --- | --- | --- |
| Start the cluster | `scripts/pg.sh status` (or `scripts/pg.sh start`) | `server is running`, port 55432 |
| Pre-fix sensitivity | `scripts/prefix-sensitivity.sh` | exit 0: `5cbcce2` fails the two defect tests, fixed passes all three |
| Database-backed safety suite | `UV_CACHE_DIR=/tmp/uvcache uv run pytest -m integration -q` | all integration tests pass, including the 14 in `test_dispatch_safety.py` |
| Full offline suite | `UV_CACHE_DIR=/tmp/uvcache uv run pytest -m "not integration" -q` | 89 passed, 7 skipped, 78 deselected |
| Simulated scenarios | `UV_CACHE_DIR=/tmp/uvcache uv run python -m opsaccord.cli run-scenarios` | 9/9 PASS, `simulated: true` |
| Live paths | `set -a; . .local/demo-endpoints.env; set +a; UV_CACHE_DIR=/tmp/uvcache uv run python -m opsaccord.cli run-live-scenarios --json reports/local/live-scenarios-b508a0f.json` | 7/7 PASS, 0 simulated-evidence records |

Until the first three of those run, A1 and A2 stay "implemented; validation pending",
the concurrency guarantee stays argued rather than demonstrated, and no scenario or live
result describes this revision.

### Optional review for this pass

**Optional external review did not run.** The subscription-backed Claude Code
harness is installed (`claude --version` reports `2.1.267 (Claude Code)`, settings
show the subscription model, no API key), but three bounded non-interactive
attempts hung with no stdout and no stderr - 90s, 120s, and 45s with `--debug` -
which is consistent with the sandbox blocking every socket. Escalating past the
sandbox is rejected by the same approval-review failure. Per the handoff, the
self-checks above were completed instead, and no independent approval is claimed.
The delivery session re-attempted once more with the exit status and output size
captured directly: the run was killed by its 120s timeout with exit code `124` and
an output file of exactly 0 bytes. Two sessions, four bounded attempts, no output;
no fifth attempt is planned, and the fallback the handoff authorises - continue
with the implementation agent's own deterministic checks - is what was used.
The first review (`docs/reports/phase1-review.md`, revision `b4b75aa`) reviewed
neither these fixes nor the ones in `5102dbe`.

## Remaining work and discussion

**Blockers (environment, not code):**

- **Push and draft PR are blocked.** Re-attempted in the safety pass and still
  blocked for two independent reasons: `git push -u origin phase1/baseline` fails
  with *"Bad owner or permissions on /etc/ssh/ssh_config.d/20-systemd-ssh-proxy.conf"*
  before it reaches the network, and even with working SSH this sandbox forbids
  every socket. The escalated retry is rejected by the harness: *"The supported API
  model names are deepseek-flash, deepseek-v4-pro, but you passed
  codex-auto-review."* The automatic approval review is misconfigured, so no
  escalated action can be approved - the same failure blocks `rm` and any command
  that needs approval. The branch and its commits exist only locally; there is no
  PR URL. Publishing is a two-step manual action for you: fix or bypass the ssh
  config permission, then
  `git -C /home/brentiburo/projects/OpsAccord push -u origin phase1/baseline`.
- **No PostgreSQL and no network in this session**, which is what leaves the
  concurrency, recovery, scenario and live checks outstanding (see the safety-pass
  section above). `socket.socket(AF_INET)` raises `PermissionError [Errno 1]`, and
  `AF_UNIX` `bind()`/`connect()` raise the same, so no local cluster can be
  reached by any client.
- **The optional Claude harness cannot be reached.** It is installed and
  subscription-configured, but three bounded non-interactive attempts hung with no
  output at all. Recorded rather than worked around; no review is claimed.
- `/snap/bin/codex` does not run here: `snap-confine is packaged without necessary
  permissions ... required permitted capability cap_dac_override not found`. The
  working path is the project's `uv` environment (`uv run ...`). Nothing was
  reinstalled and no machine-wide configuration was changed.
- The `apply_patch` helper works when invoked directly in the workspace; through
  the escalated path it hits the same approval-reviewer failure. All edits were
  made through the direct path.

**Decisions for discussion:**

- Whether the seven live paths should become a required gate in CI, given they
  need Docker Desktop and take ~20 minutes end to end.
- ~~Whether the residual global-constraint race should be closed (a global lock row
  or a `FOR SHARE` read on the constraint set) or documented and accepted.~~
  **Closed in the safety pass** with a transaction-scoped advisory control gate;
  the alternatives that were considered and rejected are recorded in the
  safety-pass section above and in the decision record the local notes carry.
- Whether reconciliation's conservatism is the behaviour you want: an unknown
  external outcome now always hands off rather than ever concluding "not
  applied". The safety pass extended this to the *live* adapter, which used to
  infer "not applied" from the desired flag value being absent; that inference is
  now `UNKNOWN`, so a live uncertain action can only resolve by finding the
  desired state present.
- Whether the control gate should stay a single exclusive advisory lock. It
  serialises *authorization* across incidents (a few short statements each) for a
  guarantee a per-incident lock cannot give. A shared-mode gate for dispatch plus
  an exclusive gate for control writes is the recorded alternative if contention
  ever shows up.
- Whether to bound the gate wait with `SET LOCAL lock_timeout` on the
  authorization transaction. Not added now: the gate is held only for a short
  in-database transaction, and a timeout would introduce a new failure path this
  environment cannot exercise.
- Whether the live adapter should ever be able to report `CONFIRMED_NOT_APPLIED`
  again. It cannot today, because a flag-document API answers a state question
  rather than an authorship question; a surface that proves authorship would
  change that.
- **Next delivery improvement after these safety changes are assessed:** add the
  explicitly triggered live CI workflow. It was deliberately *not* added in this
  pass, per the handoff.
- Whether to keep the `checkout` memory override as a documented deviation or
  replace it with a different approach (for example excluding the payment path
  from the live demo).
- Whether Phase 1's live scope should include a paid reasoning provider at all,
  or stay deterministic with live tools.

**Startup, demonstration, verification, teardown:**

```bash
# 1. PostgreSQL and the pinned demo (idempotent)
uv run bash scripts/pg.sh start
uv run bash scripts/pg.sh ensure-dbs
uv run bash scripts/demo.sh start
uv run bash scripts/demo.sh health          # prometheus, flagd-ui, jaeger
uv run bash scripts/demo.sh endpoints       # writes .local/demo-endpoints.env

# 2. Accept the one-command live run (7 paths against the real stack)
uv run bash scripts/run_live_scenarios.sh

# 3. Offline and live verification
uv run ruff check . && uv run ruff format --check . && uv run mypy src/opsaccord
uv run pytest -q
set -a; . .local/demo-endpoints.env; set +a
OPSACCORD_LIVE_DEMO=1 uv run pytest -m live_demo -v

# 4. Operator surfaces
uv run python -m opsaccord.cli init-db --all
uv run python -m opsaccord.cli serve-api      # FastAPI
uv run python -m opsaccord.cli serve-ui       # Streamlit console

# 5. Teardown (this project's resources only)
uv run bash scripts/demo.sh stop
uv run bash scripts/pg.sh stop
```

Stopping here for discussion. No merge, no release, no visibility change, and no
Phase 2 work was started.
