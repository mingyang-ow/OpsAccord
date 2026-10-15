# Phase 1 implementation report

## Identity and status

| Item | Value |
| --- | --- |
| Repository | `mingyang-ow/OpsAccord` (private), remote `git@github.com:mingyang-ow/OpsAccord.git` |
| Branch | `phase1/baseline` |
| Base commit | `3168bcf` (branch point: design baseline and Phase 1 foundation) |
| Implementation commit | `5102dbe` (the commit immediately preceding this report) |
| Branch commits | `1296b23`, `821414a`, `b4b75aa`, `5102dbe`, `5cbcce2`, `b508a0f`, `cb60242`, the deadlock-correction commits `ed0d132` (committed user guidance), `7b66f11` (the fix), `7becf98` (review fixes), `e8a8e76` (records, which is what the user ran), the configuration-isolation commits `fe16796` (tests), `843b34d` (validation runner), `a60cecb` (records), `5294575` (corrected a test-selection count) and `7f8c71e` (review fixes), the live-run-recovery commits `74ac1f2` and `2286565`, the dependency-attribution commits `8165c3b`, `fe1a0ea` and `d873dae` (the live edge read pinned offline), the verification-reason commit `4ccca4d`, the braced-selector commit `952b045`, the test-scope correction `b4d80e7`, the live-premise correction `79d733e`, the records commits `d905465` and `9058671` (which is the revision the user ran for this pass), then the code-and-test commit `e6232c2` (the scoped health predicate, the attributed premise, the new fakes and tests, the bounded diagnostic capture), the records commit `723fe7b` (which is the revision the user ran for this pass), the fault-evidence commit `56fab5a` (the four-class evidence vocabulary, the corrected verdict aggregation and the bounded read-only trace exporter) and the trace-query commit `a686617` (the bounded trace read, the shared `JaegerClient`, the progressive read-only exporter and 24 new tests), followed by this report's record commit, which is the tip (`git log --oneline -1`) - per-revision totals are reproducible with `git diff --shortstat 3168bcf <rev>`; for the branch tip as it stood at `b508a0f` that reports 87 files changed, 18404 insertions(+), 42 deletions(-). The commits after `4ccca4d` that change code are `952b045`, `b4d80e7`, `79d733e`, `e6232c2`, `56fab5a` and `a686617` - each also carries its own report update - while the rest are records only. |
| Dirty state | Clean except the user's own uncommitted `AGENTS.md` reviewer-policy change (`M AGENTS.md`, preserved and never staged); only gitignored local state (`.local/`, `reports/local/`, `.venv/`) otherwise remains |
| Host | WSL 2, kernel `6.18.33.2-microsoft-standard-WSL2`, Ubuntu 26.04 LTS, 16 CPU, 15.5 GB RAM |
| Toolchain | `uv` 0.12.5, Python 3.12.14 (`.python-version`), project-local PostgreSQL 18.6 on `127.0.0.1:55432`, Docker 29.7.2 (client and server), Docker Compose v5.4.0 |
| Coding agent | Codex CLI agent on the user's configured DeepSeek provider; the environment reports `deepseek-flash` / `deepseek-v4-pro` as its supported models |
| Application runtime model | `deterministic` / `deterministic-v1` - no paid application-model calls were made |
| **Overall status at `5102dbe`** | **READY_FOR_DISCUSSION** |
| Safety-pass status (branch tip) | **PARTIAL** - both follow-up defects are implemented with regression tests and every offline check passes, but the PostgreSQL concurrency, recovery, scenario and live checks could not run and no optional review ran. See "Safety pass after `5cbcce2`". |
| Validation-pass status | **PARTIAL** - the environment was diagnosed precisely (host services up, sandbox networking denied) and a runnable pre-fix sensitivity harness was added, but no database-backed check has still executed. See "Validation attempt after `b508a0f`". |
| **Deadlock-correction status (branch tip with this report)** | **PARTIAL** - the deadlock the user's integration run found is fixed, an independent review found and this pass fixed two further defects in the new tests, and every offline check passes; the corrected test, the pre-fix harness, the nine scenarios and the seven live paths remain unrun because this sandbox denies socket creation. See "Deadlock correction after the integration suite". |
| **Configuration-isolation status** | **PARTIAL** - the offline failure is fixed and the offline suite is independent of the operator's shell in either spelling of an override (99 passed, 7 skipped, 78 deselected); a fresh-context review falsified two guarantees in the first version of the fix and both are closed at `7f8c71e`. The user's integration results were measured on `e8a8e76`, and the pre-fix harness, the nine scenarios and the seven live paths have not run on these test-only changes. See "Configuration-test isolation". |
| **Live-run-recovery status (branch tip with this report)** | **PARTIAL** - a live run is now observable while it happens and safe to interrupt, cleanup runs on every exit path, and the Payment fault premise is differential instead of a wait for an observation the fault prevents; the new behaviour is pinned by 26 new tests and every offline check passes, but no live path, simulated scenario or database-backed check has run against this revision, and the only live evidence is the user's interrupted run. See "Live-run recovery". |
| **Test-scope / live-Payment status** | **PARTIAL** - at `2812871` the user's run cleared every stage except one acceptance check, so the harness stopped at integration and the live Payment path did not run. The failure was the check's own observation scope: it asked the whole incident history for the absence of a dependency read, and the P1-01 *diagnosis* plan reads the edge on purpose. The check is now scoped by persisted tool-call id and additionally asserts the persisted verdict; the user's `131320` run then verified it live. The live schema question is measured: the braced selector answers `200` with 160 series exposing `service_name` and `span_kind`. See "The test-scope correction (`114517`)". |
| **Live-premise status (branch tip with this report)** | **PARTIAL** - the user's `131320` run is the first live run with attributable caller-side evidence (`checkout-service -> payment-service`, `Charge`, `span_kind=client`) and a verification that reports a *measured* continuing failure, but the premise of that run is not attributable: visibility was accepted 0.0s after the write from a 120s window that predates it, and both pre-injection readings already carried a 100% error share. The predicate is now a rise over pre-injection readings taken *before* the write, committed at `79d733e` and pinned by three new tests and a falsification log; the integration suite, the sensitivity harness, the simulated scenarios and the live path have not been re-run here. P1-01 has still never reached `RESOLVED`. See "The live Payment premise (`b4d80e7` measured, corrected at `79d733e`)". |
| **Payment-evidence status (branch tip with this report)** | **PARTIAL** - at `9058671` the user's run cleared every stage (offline 197/7 in both variants, integration 85, static checks, sensitivity harness, 9/9 simulated scenarios) and the live P1-01 ended `SAFE_ESCALATION` / `READY_FOR_HUMAN` with zero approvals and zero actions, 541.9s, cleanup `on -> off` read back `off`. Its premise was accepted on a *generic* caller-side failure (checkout `PlaceOrder` 17.66%) while the attributed Charge edge read `error_rate` 0.0, so the controller had no attributable Payment evidence and handed off - correctly, on the evidence it had. Two corrections are committed at `e6232c2`: service-health reads are now scoped to the target's own spans, and the premise requires the attributed caller-side reading (`LivePlan.corroborating_dependency`) instead of a generic parent-span rise. P1-01 remains unverified on this revision, and Payment recovery has still never been demonstrated. See "The Payment fault's behaviour and a scoped premise (`9058671` measured, corrected at `e6232c2`)". |
| **Payment fault-evidence status (branch tip with this report)** | **PARTIAL** - at `723fe7b` the user's diagnostic capture (30 polls, 00:56:03-01:05:48 UTC, flag armed and restored with a read-back) measured the mismatch decisively: at 00:58:04 checkout's `PlaceOrder` read a **0.3333** error share while the attributed Charge series read 0.0334 calls/s on `checkout`'s client spans and 0.0167 calls/s on `payment`'s, both at **0.0** error share, and the closing polls read a **1.0** caller error share with no Charge traffic at all. The pinned source confirms the injected failure *should* appear on the selected client span (same RPC, same operation name, same `otelgrpc` handler), so the inconsistency is real rather than a selector artefact. The traces that would settle it are unavailable: `_jaeger_traces` sends no `start`/`end`, so the capture's six distinct payloads were unbounded newest-N sets with no trace IDs, and Jaeger's store is bounded by `max_traces` rather than by age *(a bounded store is not a demonstrated eviction - corrected in the trace-query pass below)*. This pass corrected the diagnostic's reporting - four disjoint evidence classes, the *strongest* reading instead of the commonest one (which reported the least informative class by construction), `recovery: assessed: false`, and an exit status that is recorded and printed - and added a bounded read-only trace exporter so the missing evidence can be collected in one shot; both are committed as `56fab5a`. Offline `218 passed, 7 skipped` in both variants; lint, format and `mypy` clean. P1-01 remains unverified and Payment recovery has still never been demonstrated. See "The payment fault-evidence pass (`723fe7b` measured)". |
| **Trace-query status (branch tip with this report)** | **PARTIAL** - the user's `022150` run (revision `6b7fe61`) is the first to reach the trace exporter: 18 armed observations resolved to 6 `generic_caller_failure_only`, 6 `traffic_without_failure`, 6 `no_usable_observation`, 0 `attributable_failure`, with the strongest-class aggregation working live, and `paymentUnreachable` restored with a read-back. The exporter exited `0` and returned **0 PlaceOrder traces**, and suggested eviction. Auditing the request showed it was well-formed - correct base path, real service names, microsecond UTC bounds, percent-encoded operation, no credentials - while the *entry filter* was an assumption: the pinned collector rewrites span names with `set_semconv_span_name`, the registry already carries two different naming forms, and no run has ever returned this operation. The **read** was the confirmed defect: `read_traces` reported an `observation_window_start/end` it never asked for, since the request carried `lookback` and `limit=20` and no bounds, and the same capture's `charge_traces` payload changed only twice in 18 polls. This pass bounds the read, retains trace IDs and cap/out-of-window provenance, makes an API error raise instead of reading as empty, and rebuilds the exporter as a progressive read-only sequence with an explicit "cause unknown" empty-result explanation; `a686617`. Offline `242 passed, 7 skipped` in both variants; lint, format and `mypy` clean. The historical interval has still not been read, P1-01 is unverified, and Payment recovery has still never been demonstrated. See "The trace-query pass (`6b7fe61` measured)". |
| **Recovered-trace analysis status (branch tip with this report)** | **PARTIAL** - the user's `025435` run (revision `504fbd0`) is the first to retrieve traces from the fault interval: 17 services, 8 advertised Checkout operations, 40 recent unfiltered Checkout traces, `oteldemo.CheckoutService/PlaceOrder` confirmed in a returned trace, the bounded retrieval check succeeded, and **two** historical traces came back (`ce5e79a180be61a3c87ac161b474ef1d`, `798301119dc69ac06ecc4704cf100bcc`) with the exporter exiting `0`. The extract holds **summaries only**, so the requested ancestry reconstruction cannot be completed from it, and the run's own `evidence.sufficiency: sufficient` was a statement about retrieval presented as a statement about the subject - a claim this report retracts. Reading the summaries against the code shows the confirmed classification defects: the span called the "root" was a `frontend` client span (the load generator's HTTP span above it is the actual root), `child_of_root=False` was an artefact of that selection, both frontend durations (563.9 s, 826.6 s) outlast the 344 s fault interval, and `failed: 0` was computed from one span's direct children while six and nine spans in the same traces recorded errors. This pass resolves root, attempt and edge relationship from `CHILD_OF` references, makes span status three-valued with the deciding signal named, counts errors across the whole trace, fixes the observed-range arithmetic, replaces the single "sufficiency" with a five-level ladder, and corrects the same status defect in the runtime `read_traces` adapter. Offline `256 passed, 7 skipped` in both variants; lint, format and `mypy` clean. The two traces have **not** been positioned against injection and restoration, Payment is neither shown to have served nor to have failed, recovery remains undemonstrated, and P1-01 remains unverified on this revision. See "The recovered-trace analysis pass (`504fbd0` measured)". |

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
`dedc0178`); `scripts/demo.sh status` originally reported 25 containers with the
stack healthy - **no artifact for that dump is retained**, and the only retained
status dump (11:28:31) shows every container up with `product-catalog` unhealthy, so
the earlier wording is kept as a historical measurement and is not restated as a
current property (see the baseline diagnosis pass below). Only `frontend-proxy` (8080) and `prometheus` (9090) hold stable host
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

## Deadlock correction after the integration suite (`7b66f11`)

The user ran the suites from their own WSL terminal and reported: pre-fix sensitivity
harness behaving as designed, **77 passed / 1 failed** for the integration suite, 89
passed / 7 skipped offline, 9/9 simulated scenarios, live preflight succeeded with no
final result supplied. Those are the user's measurements, not this session's.

### The failure: a deadlock, and then a misleading KeyError

`tests/integration/test_control_hardening.py::test_a_human_control_that_commits_during_dispatch_is_not_ignored`
failed with a PostgreSQL deadlock. The `KeyError: 'execution'` that followed was a
symptom, not a second defect: the dispatch thread died on the deadlock before it
stored its result, and the test read `results["execution"]` regardless.

### Root cause: the test composed two helpers in the reverse of the documented order

The old test body took the incident row lock by hand and then called
`set_takeover` inside that same transaction. Every human control write takes the
global control gate first, so that transaction was holding the incident row and
*then* asking for the gate, while `ToolGateway.execute_action` holds the gate and
then asks for the incident row. Two sessions, two opposite orders, one cycle -
PostgreSQL is correct to abort it.

The order that must hold is **control gate -> incident row -> workflow row**.

### Was production affected? No - and the audit is the reason that claim is made

Every production caller was read in this pass, not assumed:

| Caller | Order it takes | Verdict |
| --- | --- | --- |
| `ToolGateway.execute_action` (`tools/gateway.py:377,385`) | gate, then incident row, then claim | correct |
| `repositories.set_takeover` (`repositories.py:602-603`) | gate, incident row, workflow row | correct |
| `repositories.add_constraint` (`repositories.py:1320-1322`) | gate, then incident row when scoped | correct |
| `repositories.lock_incident` (`repositories.py:378`) | gate **now**, then incident row | was the gap |
| the old test body | incident row, then `set_takeover`'s gate | inverted |

No production path inverted the order, so no production defect was fixed here and no
production behaviour changed. The honest statement of the defect is narrower and more
useful than "the test was wrong": **`lock_incident` had no guard of its own**, so the
documented order was a convention callers had to remember, and a caller that forgot
got a deadlock instead of an error. The test was the caller that forgot.

### The correction

- `repositories.lock_incident` now takes the control gate before the row lock, so the
  order is a property of the helper. Re-acquiring a transaction-scoped lock inside a
  transaction that already holds it is free, so callers that take the gate themselves
  are unaffected. `persistence/locks.py` now says both why that is safe and why an
  inversion produces a deadlock rather than a wrong result.
- The test was rewritten onto the supported path - it calls `set_takeover` and lets the
  repository take the locks in order - while keeping the assertion it has always
  carried: a takeover committed before the authorization is observed by revalidation
  and the side effect never reaches the demo.
- Thread handling was improved as the handoff required. Coordination is event-based
  with bounded waits (the 0.5s `time.sleep` is gone). Worker exceptions are captured in
  a list and asserted empty, so a database error now reports itself instead of being
  laundered into `KeyError: 'execution'`. Both workers are asserted terminated, the
  writer is shown to be blocked on the gate by observing an ungranted advisory-lock
  request in `pg_locks`, and the gate is released in a `finally` that joins only threads
  that actually started, so a failed assertion cannot leave a thread blocked and cannot
  be replaced by a `join()` error. The first version of both of those properties was
  defective, and the independent review below is what found it; `7becf98` fixes it.
- `tests/unit/test_persistence_locks.py` gained two database-free guards that assert
  `lock_incident` issues the gate statement *before* it requests the row lock, and that
  an unknown incident still fails after the gate is taken.

No timeout was increased, no deadlock was suppressed, no retry was added around the
external action, and the safety assertion was not weakened.

### Verification performed in this session

In-sandbox, after the corrections, on the working tree that became `7b66f11`:

| Check | Exact command | Result |
| --- | --- | --- |
| Lint | `UV_CACHE_DIR=/tmp/uvcache uv run ruff check .` | clean |
| Format | `UV_CACHE_DIR=/tmp/uvcache uv run ruff format --check .` | 75 files already formatted |
| Types | `UV_CACHE_DIR=/tmp/uvcache uv run mypy src/opsaccord` | success, 40 source files |
| Offline suite | `UV_CACHE_DIR=/tmp/uvcache uv run pytest -m "not integration" -q` | **91 passed, 7 skipped, 78 deselected** (was 89 passed: +2 new unit guards) |
| New lock-order guards | `UV_CACHE_DIR=/tmp/uvcache uv run pytest tests/unit/test_persistence_locks.py -v` | 4 passed |
| Integration collection | `UV_CACHE_DIR=/tmp/uvcache uv run pytest tests/integration/test_control_hardening.py tests/integration/test_dispatch_safety.py --collect-only -q` | 25 tests collected (imports clean) |

The same four commands were re-run after the review fixes in `7becf98` with identical
results (lint clean, 75 files formatted, 40 source files typed, 91 passed / 7 skipped /
78 deselected).

Collection is not execution, and it is not offered as evidence of the concurrency
guarantee. It only shows the corrected files import.

### Verification that could not run, and why

The blocker is the same one as in the previous two passes, re-confirmed directly rather
than inherited: this session's own probe found `socket.socket(AF_INET)` raising
`PermissionError [Errno 1] Operation not permitted` before any `connect` was attempted.
That is a sandbox seccomp denial inside the agent's process tree - not a stopped
cluster, not a DSN problem. The project cluster is up on the host
(`.local/pg/data/postmaster.pid`, PID 22096, port 55432).

Consequently these remain outstanding for the corrected revision, and none of them is
claimed as passing:

- the corrected test in isolation, and any bounded repeat of it;
- `tests/integration/test_dispatch_safety.py` (14) and the rest of the integration suite;
- `scripts/prefix-sensitivity.sh` against `5cbcce2`;
- the nine simulated scenarios and the seven live paths.

The critical open question is narrow and specific: **does the corrected test now pass
under real PostgreSQL?** It exercises a takeover that commits before the authorization
and after the gate is released, and it is written to fail rather than pass if the
ordering assumption is wrong. Only a database can answer it, so it is a user-run check
below.

### Optional review for this pass

Two reviewers were prepared and both are unavailable; neither is a completion gate.

- **A GPT-6 Luna reviewer could not be started.** One bounded attempt was made through
  the agent's sub-agent channel and the provider rejected it:
  *"The supported API model names are deepseek-flash, deepseek-v4-pro, but you passed
  gpt-6-luna."* No retry, per the bounded-attempt rule.
- **The Claude harness could not be invoked.** `claude --version` reports
  `2.1.267 (Claude Code)`, but the invocation itself was denied before it ran: the
  harness's automatic approval review rejected the command with its own failure,
  *"The supported API model names are deepseek-flash, deepseek-v4-pro, but you passed
  codex-auto-review."* That is the same misconfiguration recorded in the previous two
  passes, so the known-failing escalation route was not retried, and no 90-120s
  hang-prone run was repeated.
- **What did run instead.** A fresh-context independent reviewer agent accepted the
  review task on the harness default model (`gpt-5.5-codex`, reported by the agent
  itself). Its scope, findings and my responses are in the "Independent agent review"
  subsection below. This is a separate agent instance and a different model, but it is
  the same harness and the same provider family, so it is **not** an external
  independent review and is not presented as one.

### Independent agent review (not the external reviewer, and not Claude)

A fresh-context reviewer agent was given commit `7b66f11` and an explicitly read-only
brief: audit the lock order in every caller, check whether taking the gate inside
`lock_incident` introduces a new problem, check whether the rewritten test still proves
both properties, and check whether the database-free guard can actually fail. It
reported findings rather than editing anything, and reported its own model as
`gpt-5.5-codex`.

**Verdict: sound with caveats** - no production defect, and the audit confirmed
independently. It checked that the only `FOR UPDATE` row locks in the tree are on
`IncidentRow` (`repositories.py:379`), `WorkflowRow` (`:387`) and `ApprovalRow`
(`:1174`); that `lock_incident` is the only entry point to the incident row and all
three production callers already took the gate first; and that `claim_action`
(action -> approval) and `decide_approval` (approval -> action) are opposite orders but
not a reachable cycle, because `claim_action` locks the action row only for an
`APPROVED` action, and an `APPROVED` row cannot be decided again.

It also found two real defects in *this pass's* work. Both are fixed in `7becf98`:

| Finding | Kind | Status |
| --- | --- | --- |
| The database-free guard could not detect a re-inversion: `execute` and `get` were recorded in two separate lists, so `lock_incident` taking the row first and the gate second still satisfied all four assertions. The reviewer reproduced it, and a bare swap is exactly the regression the guard existed to prevent. | **defect** | fixed - the stand-in records one merged sequence and the guard asserts it; `reports/local/lock-order-guard-falsification.{py,log}` shows the new guard failing on the inversion and the old guard passing on it |
| `finally` joined `writer` unconditionally, but a failed `gate_held.wait()` means `writer.start()` never ran, and `Thread.join()` on an unstarted thread raises `RuntimeError: cannot join thread before it is started` - which would have replaced the real `AssertionError` with a confusing error. | **defect** | fixed - the gate is released, then only threads with `ident is not None` are joined |
| `writer.is_alive()` was offered as evidence the writer was waiting on the gate, but it only means "not finished", so the ordering check could pass without the gate ever being contended. | caveat | fixed - the test now requires observing an ungranted advisory-lock request for the gate key in `pg_locks`, and fails with a clear message if none appears |
| `persistence/locks.py` said PostgreSQL answers the inversion with a deadlock, unconditionally. A lone transaction taking the row first and the gate second succeeds. | overstatement | fixed - the text now names the cycle (a gate-holding dispatcher waiting on the same row) that is aborted |
| The test name and docstring implied a takeover committing concurrently with a dispatch, which this test no longer does: its gate holder never dispatches. | overstatement | docstring corrected, and it now names `test_dispatch_safety.py:194` as where the dispatcher's half is proven |

The review's remaining criticism is one this pass cannot close from inside the sandbox:
the corrected test needs PostgreSQL to run, so the reviewed revision is still unobserved.
That is the first item of the user-run block below, and the reason the status stays
PARTIAL.

Two things it confirmed are worth recording precisely because they were previously only
the author's argument: re-acquiring a transaction-scoped advisory lock inside the same
transaction really is free, and `lock_incident` really is the only other entry point to
the per-incident row lock. It also named the residual limit: nothing prevents a future
raw `session.get(..., with_for_update=True)` from bypassing the helper, and no test can
stop that.

## Configuration-test isolation (`fe16796`, `843b34d`)

### What the user's run found

Run from the user's own terminal, on `e8a8e76` — **the user's measurements, not this session's**:

| Check | Result |
| --- | --- |
| The corrected deadlock test | passed |
| Ten additional repetitions of it | all passed |
| `test_dispatch_safety.py` | 14 passed |
| Control/recovery tests | 24 passed |
| Full integration suite | 78 passed |
| Offline suite | **1 failed**, then passed once `OPSACCORD_DEMO_MODE` was removed from the subprocess environment: **91 passed, 7 skipped, 78 deselected** (`reports/local/offline-clean-env-20260924T062719.log`) |

The block was fail-fast, so it stopped at the offline failure and its later stages did not run. This
is also the point at which the concurrency guarantee moved from *argued* to *observed*: the corrected
control-ordering test and its ten repetitions are the first executions of that test anywhere.

### Root cause: configuration leaked from the shell into a defaults test

The failing test was
`tests/unit/test_domain_contracts.py::test_demo_mode_selects_the_adapter_and_never_falls_back`, which
asserted `Settings().demo_mode == "simulated"`. `Settings` reads `OPSACCORD_*` variables and the
`.env` file beside the working directory — that is the runtime override mechanism, and it is working
as designed. `scripts/demo.sh endpoints` writes `OPSACCORD_DEMO_MODE=live` into
`.local/demo-endpoints.env`, and the documented live command exports it with
`set -a; . .local/demo-endpoints.env; set +a`. The export outlives the run that created it, so the
test observed the developer's shell instead of the defaults.

The defect was in the test, not in the runtime: a test that asserts a *default* has to say which
environment it is observing.

### Scope: the leak was not specific to demo mode

Reproduced in this session before any edit, by exporting one variable at a time and running the
offline suite:

| Inherited variable | Test that failed |
| --- | --- |
| `OPSACCORD_DEMO_MODE=live` | `test_demo_mode_selects_the_adapter_and_never_falls_back` |
| `OPSACCORD_MODEL_PROVIDER=openai_compatible` | `test_application_runtime_model_is_explicit_and_not_inherited` |
| `OPSACCORD_ALLOW_PAID_MODEL_CALLS=true` | same test |
| `OPSACCORD_GRAPH_CHECKPOINT_URL=...` | `test_test_environment_keeps_state_and_checkpoints_in_one_database` |

All four produce `1 failed, 90 passed, 7 skipped, 78 deselected` — one failure each, no other
symptom. Budget overrides (`OPSACCORD_SOFT_STEP_BUDGET`, `OPSACCORD_HARD_STEP_CAP`) broke nothing
because no test asserts those defaults; that was checked rather than assumed.

### The correction

`tests/conftest.py` gained an **opt-in** `isolated_config` fixture. It removes the inherited
`OPSACCORD_*` variables and moves the working directory to an empty temporary one, for the duration
of the test that requests it. The three default-value tests now request it.

The design choices that matter:

- **Opt-in and never autouse.** A global fixture that stripped the environment would make the
  override tests meaningless and could break the integration fixtures, which take connectivity from
  `OPSACCORD_TEST_DATABASE_URL`. Only the three defaults tests are isolated.
- **No value is passed to the thing under test.** Each isolated test still constructs `Settings()`
  with no arguments; what is controlled is the ambient environment, not the asserted value.
- **Nothing global or user-owned changes.** The environment is restored by `monkeypatch`, the
  directory is a temporary one, and no `.env`, shell profile, credential or runtime default was
  touched.
- **The mechanism is unchanged.** `Settings` still reads the environment and `.env`; only the tests
  changed.

`tests/unit/test_config_isolation.py` pins both halves, so neither is bought with the other:

| Check | What it proves |
| --- | --- |
| `test_defaults_hold_in_an_isolated_configuration_context` | the defaults are observable as defaults |
| `test_an_explicit_environment_override_selects_live_mode` | an exported setting still selects the live adapter |
| `test_a_temporary_dotenv_is_read_and_the_process_environment_wins` | a `.env` file is read, and the environment outranks it |
| `test_live_mode_never_resolves_to_the_simulator` | live builds `LiveEnvironment` with `is_simulated is False`, simulated builds `DemoEnvironment`; no fallback either way |
| `test_the_default_tests_pass_while_the_shell_exports_live_mode` | a real pytest subprocess of the previously failing file passes while four overrides are exported |
| `test_the_live_session_variable_list_covers_what_demo_sh_exports` | the documented leak list cannot drift from what `scripts/demo.sh` writes |

The fifth check spawns a subprocess deliberately: the property is about a *process environment*, and
no fixture can prove a module is independent of the shell it is launched from while running inside
that same shell.

### The validation runner

`scripts/validate.sh` (new) runs the sequence in one pass, fail-fast, with per-stage logs under
`reports/local/validation-<timestamp>/`:

- `--offline-only` — offline suites, lint, formatting, types (no PostgreSQL);
- default — the above plus the PostgreSQL integration suite, the pre-fix sensitivity harness and the
  nine simulated scenarios;
- `--live` — the above plus the seven live paths.

Configuration is scoped per stage and documented in the script. The offline, integration and
simulated stages run through `without_live_session`, which removes exactly five variables
(`OPSACCORD_DEMO_MODE`, the three demo endpoint URLs, `OPSACCORD_LIVE_DEMO`). Connectivity settings
such as `OPSACCORD_TEST_DATABASE_URL` are deliberately kept, as are model, budget and any other
operator setting. `.local/demo-endpoints.env` is loaded **only** in the live stage, inside a nested
subshell, so no earlier stage can inherit it.

Two behaviours worth naming:

- The offline suite runs **twice**: normally, and with `OPSACCORD_DEMO_MODE=live` force-exported. If
  the suite ever regains a dependency on the operator's shell, the two stages diverge and the run
  fails.
- The live stage **refuses to start** while another live scenario driver is running (`pgrep`, exit
  3). Merely listing processes was explicitly rejected by the handoff, and a competing driver shares
  the demo stack's feature flags.

`OPSACCORD_LIVE_DEMO` is the one variable that cannot simply be left to the tests: it is the opt-in
switch for `tests/integration/test_live_demo.py`, which is deliberately *not* marked `integration`,
so those seven tests are already inside the `-m "not integration"` selection (measured: 104 tests
collected, 7 of them from this module) and the variable is the only thing that decides whether they
skip or **execute** against the live stack - where they would write real feature flags. The runner
removes it for the offline stages. Whether that module should carry the `integration` mark is a
discussion item below rather than a change made here, because it would redefine an
already-validated suite.

### Results from this session

In-sandbox, on the working tree that became `fe16796` and `843b34d`:

| Check | Result |
| --- | --- |
| Offline suite, clean environment | **97 passed, 7 skipped, 78 deselected** (was 91 passed; the six new isolation checks) |
| Offline suite, `OPSACCORD_DEMO_MODE=live` exported | **97 passed, 7 skipped, 78 deselected** — identical, which is the correction |
| Offline suite, live + model + paid-call + checkpoint overrides exported | same result for the offline tests |
| Focused files with live exported | `test_config_isolation.py` + `test_domain_contracts.py` → 18 passed |
| Lint / format / types | clean; 76 files formatted; 40 source files typed |
| `scripts/validate.sh --offline-only` with live exported | exit 0, both offline stages identical |

### What could not run here

The same environmental blocker as the previous two passes, re-measured rather than inherited:
`socket.socket(AF_INET)` raises `PermissionError [Errno 1] Operation not permitted` **at
construction**, so no client can reach the host cluster (still up: `.local/pg/data/postmaster.pid`).

The escalation route was attempted once for the integration suite, as the handoff directs, and was
rejected by the harness before the command ran: *"The supported API model names are deepseek-flash,
deepseek-v4-pro, but you passed codex-auto-review."* Per the handoff that route is not retried. So
the PostgreSQL integration suite, the pre-fix sensitivity harness, the nine simulated scenarios and
the seven live paths are all **unrun in this session**. The user's integration results above stand on
`e8a8e76`; the changes in this pass touch tests, one documentation file and one new script, and no
`src/` file, which is why the integration suite and the scenarios are not expected to move — but
that is an expectation, not a measurement, and `scripts/validate.sh` is what settles it.

### Independent agent review of the configuration-isolation pass

The handoff for this pass requires no external review, and the provider makes one impossible anyway:
a bounded attempt with `gpt-6-luna` was refused word-for-word as before, and a second bounded attempt
with `gpt-6-sol` was refused identically. That makes the cause the provider's allow-list
(`deepseek-flash`, `deepseek-v4-pro`) rather than one bad model name, so the agent tool advertises five
models and none of them can be selected. What did run is a **fresh-context reviewer sub-agent on the
harness default model**, read-only, given the range `fe16796^..5294575` and told to falsify the pass
rather than to read it. That is a second agent instance with an independent brief, not an external
review, and it is not presented as one.

**Verdict: the pass's core claims hold.** The three changed tests do fail against a leaked shell
without the fixture; the six new tests are non-vacuous; the corrected counts in this report reproduce
exactly; `scripts/validate.sh --offline-only` is genuinely fail-fast with working `pipefail` and
`tee`; and `git diff --name-only fe16796^..5294575 -- src/` is empty, so no runtime configuration or
safety assertion moved. **No production defect was found.**

It falsified two guarantees and found two narrower gaps. All four are fixed in `7f8c71e`, and new tests
pin the first two:

| Finding | Kind | Status |
| --- | --- | --- |
| `isolated_config` scrubbed names case-sensitively (`key.startswith("OPSACCORD_")`) while `Settings` matches environment names **case-insensitively** (pydantic-settings `case_sensitive=False`). Demonstrated: `env opsaccord_demo_mode=live uv run pytest ...::test_defaults_hold_in_an_isolated_configuration_context` -> `AssertionError: assert 'live' == 'simulated'`, and `env opsaccord_demo_mode=live scripts/validate.sh --offline-only` -> **exit 1, 3 failed**. `scripts/validate.sh` had the same hole, because `env -u` cannot match case-insensitively. | **defect** | fixed - the comparison upper-cases the name, the runner removes both spellings, and the mixed-case residual is documented rather than hidden. Pinned by a subprocess regression for both spellings and a direct test of the scrub |
| The fixture cleared `os.environ` but not the `lru_cache` on `get_settings()`, so a value read from the leaked shell outlived the scrub: `get_settings().demo_mode` stayed `live` after `OPSACCORD_DEMO_MODE` was removed, while `Settings()` said `simulated`. Latent today (only `persistence/db.py` calls `get_settings`, and no offline test does), but any future defaults test exercising application code would have observed the leaked value through a fixture that claimed the environment could not reach it. | **defect (latent)** | fixed - `get_settings.cache_clear()` runs when the fixture starts and when it ends; asserted directly |
| The competing-driver check ran in the stage-10 preflight, while the protected command is stage 13, after the demo-stack and endpoint stages - a window of minutes in which a driver (or a second `validate.sh --live`) could start undetected. | gap | fixed - the check is repeated immediately before the driver, and the pattern is anchored (`opsaccord\.cli`) instead of using an unanchored `.` wildcard |
| The drift-guard regex matched only `^OPSACCORD_[A-Z0-9_]+=`, so `export OPSACCORD_NEW=1`, an indented assignment, or a lower-case spelling would have escaped the guard that exists to catch exactly that drift. | gap | fixed - the pattern allows leading whitespace, an optional `export` prefix and either case, and uppercases before comparing |

The falsification is recorded rather than asserted: `reports/local/config-isolation-guard-falsification.log`
shows the two new tests failing (`2 failed, 1 passed` - the `[upper]` case still passes on the broken
version, which is the point) and the file passing 8/8 with the fix restored. After the fixes, the
offline suite reports **99 passed, 7 skipped, 78 deselected** in a clean environment, with the
upper-case override, and with the lower-case one; ruff, format and mypy stay clean; and
`scripts/validate.sh --offline-only` exits 0 with a lower-case live export in the parent shell, where
it exited 1 before.

One observation was deliberately **not** "fixed". An *inconsistent* pair of inherited budget overrides
(`OPSACCORD_SOFT_STEP_BUDGET=99` with the default hard cap of 16) makes `Settings()` raise
`ValidationError: hard_step_cap must be >= soft_step_budget` in 34 tests, because the runner
deliberately keeps budget settings. That is the application failing closed on an invalid
configuration, not a test observing the shell; isolating it away would hide a real configuration
error. A consistent pair leaves the suite at 99 passed.

## Live-run recovery (`db0bb31` base) - progress, cleanup on every exit path, and the Payment premise

**Status: PARTIAL.** This pass makes a live run observable while it happens and safe to interrupt,
and it removes a setup defect that could wait for an observation the Payment fault prevents from ever
existing. Every offline check passes and the new behaviour is pinned by **26 new tests** (22 in
`tests/unit/test_live_lifecycle.py`, 4 in `tests/unit/test_live_cli.py`). No live path, no scenario and
no database-backed check has executed against this revision: the agent sandbox denies socket creation,
the same environmental blocker that has applied to every pass on this branch. The seven live paths stay
the last unexecuted technical gate.

### The interrupted run, and what it left

The user stopped a `scripts/validate.sh --live` run with Ctrl+C because it produced no progress and
appeared to be waiting through unhealthy setup conditions. Its directory is
`reports/local/validation-20260924T071253/` (untracked). `summary.log` shows stage 00 recording the
revision under test (`db0bb31`), stages 01-09 completing, the live preflight and the pre-driver guard
both reporting "no competing live scenario driver", and stage 12 writing `.local/demo-endpoints.env` at
`2026-09-24T07:13:52Z`. `11-demo-stack.log` and `13-live-paths.log` are **0 bytes** and the summary has
no terminal line, so the run produced no live result. **Recorded as interrupted / incomplete. It is
never cited as a pass, and no earlier passing evidence is presented as validation of this revision.**

Checked before any change was made: `pgrep` finds no `opsaccord.cli run-live-scenarios` driver and no
`validate.sh` wrapper, so the interrupted run has exited. The project's PostgreSQL cluster is still up
(`.local/pg/data/postmaster.pid`, PID 22876, started 13:11) and was deliberately not restarted; the demo
stack was left running. No competing driver was started at any point in this pass.

### Observed baseline contamination - and what could not be established

Read-only probes taken **by the user, before this pass**, observed `paymentUnreachable=on`,
`adHighCpu=on` and `cartFailure=off`, with Payment unreachable and the ad service burning CPU. Those
observations do not establish **who** set each flag, **when**, or which scenario (if any) was active.
They cannot be re-measured from here: reading the flag document needs a socket, and socket creation is
denied in this sandbox, so nothing below upgrades the user's observation into an agent measurement.

What this pass changes is the consequence of that state: the suite now **refuses to proceed on a
baseline it has not read**. `detect_residual_faults` raises `LiveBaselineUnavailable` when the flag
document is absent or does not contain all six verified scenario flags - absence is not safety, and "no
residual faults" is a claim about state the harness actually observed. When a residual fault is present
the suite resets the scenario-owned flags, re-reads the document, and stops the suite if the repair does
not verify. The reset is a read-modify-write of the whole document, so unrelated flags are carried
through unchanged; no container, database, volume or user setting is touched.

### Setup and cleanup defects, and their fixes

The "no progress, appears hung" report had a structural cause and several distinct failure paths behind
it. All of the following are fixed in the working tree described by this section:

| # | Finding (with the failure it produces) | Kind | Fix |
| --- | --- | --- | --- |
| 1 | An evidence-write failure inside the suite raised out of the lifecycle, **skipping the fault restore** and escaping the suite with a fault still armed. | high | `persist()` is wrapped, the failure is recorded on `LiveRun.evidence_errors` and echoed to stderr, and cleanup still runs. |
| 2 | An arm write that was applied but never **acknowledged** left the flag armed while the record claimed nothing needed restoring. | high | `_ScenarioFault.attempted` gates cleanup instead of `armed`, and the record carries a "never acknowledged, position not established" caveat. |
| 3 | A real failure was recorded in the stage `workflow`, and the stage/observation/cleanup captured so far were dropped from the record. | medium | `_attach_lifecycle` attaches the true stage, observation and cleanup to any propagating exception; the suite's generic handler records them. |
| 4 | An absent or empty flag document was reported as a verified clean baseline. | medium | raised as `LiveBaselineUnavailable` (see above). |
| 5 | The Payment corroboration signal was not differential, so a checkout container broken for an unrelated reason could satisfy the premise. | medium-low | `wait_for_fault(..., corroborating_before=)` requires the caller's share to be **above the bound and higher than its own pre-arm baseline**, sampled by `_corroborating_baseline` before arming. |
| 6 | An in-progress scenario's evidence said `INTERRUPTED` while it was still running. | low | `active_snapshot(status=RUNNING)` is the default; only a real interrupt writes `INTERRUPTED`. |
| 7 | `scripts/run_live_scenarios.sh` had no competing-driver guard of its own. | low | the same `assert_no_competing_driver` as `validate.sh`, run at preflight **and** immediately before the driver. |
| 8 | Ctrl+C combined with a cleanup failure lost the interrupt: the suite exited 1 instead of 130. | low | the `KeyboardInterrupt` is re-raised (`raise ... from exc`), the run is recorded `INTERRUPTED`, and the CLI exits 130. |
| 9 | Labelling gaps: `run.completed` counted records rather than measurements, no `measured` field, a silent no-fault plan, a hardcoded `pinned_revision`, an over-stated budget comment, and a provenance defect that never reached the exit code. | low | `run.measured` added; the suite fails closed on an unknown `fault_flag`; `pinned_revision` comes from `LiveEnvironment.revision`; the budget comment says a timeout does not cancel an in-flight operation; the CLI exits non-zero on a provenance defect or simulator evidence. |

Two further gaps were found and fixed by the implementation agent itself, before the review: Ctrl+C
during the suite's startup baseline left the evidence file saying `RUNNING`, and `scripts/validate.sh`
printed its `logs:` line only at the end. The first now writes `INTERRUPTED`/`STOPPED` before
re-raising; the second prints the log directory before the first stage.

The lifecycle is now explicit and ordered: **baseline -> arm -> visibility -> workflow -> verification
-> cleanup -> outcome**, with cleanup keyed to what this scenario actually attempted, and the suite's
failure policy documented in `run_live_suite`'s docstring:

- a fault premise that never appears is recorded as **not measured** and the suite continues - an absent
  premise is scenario-specific;
- a scenario that could not restore the baseline, or whose baseline was already contaminated, **stops
  the suite**, because every later scenario would be measured against a demo that is not in its starting
  state;
- an unexpected exception is recorded and stops the suite rather than repeating a broken run once per
  scenario;
- Ctrl+C records the completed results plus an explicit interrupted record for the active scenario and is
  **re-raised, never converted into a result**.

Forced termination or host failure still cannot guarantee cleanup; that limitation is now documented in
the code and in `docs/development.md`, and the next startup detects residual scenario faults before
proceeding.

### The Payment evidence contract

`docs/astronomy-shop.md` measures `paymentUnreachable=on` as a fault that *checkout* observes: Charge
error share ~0.33, PlaceOrder error share ~0.75, and a Charge call rate of ~0.042/s - roughly four calls
a minute, bursty, with windows that legitimately contain none. Waiting only for the Payment target's own
error share can therefore wait for an observation the fault makes intermittent.

`wait_for_fault` now accepts `corroborating_target` and `corroborating_before`. The premise is satisfied
by either series, the returned probe always says which one carried it (`attribution`), and the caller's
series must be differential - above the telemetry bound **and** higher than the baseline sampled before
this scenario armed anything. The measurement contract is unchanged in the two places that matter: an
absent series is never read as a zero, and zero traffic is never read as a fault **or** as a recovery,
on either target. A quiet demo keeps waiting, and the failure mode is an honest setup failure rather
than a fabricated one. The one contract change - accepting a caller-side failure signal as
corroboration - is recorded here with its rationale, and it preserves service attribution.

### Progress and partial evidence

`src/opsaccord/scenarios/live_evidence.py` is new and owns both. Each `LiveProgress` line is flushed,
timestamped, and names the scenario and its position in the suite, the current stage, elapsed scenario
time, remaining stage budget, and the latest observation while polling; the stream and clocks are
injected so tests pin them. Reporting can no longer break a run that has a fault armed: a closed pipe or
a failed evidence rewrite is recorded once and echoed to stderr, and the command turns it into a
non-zero exit at the end instead of a silent gap.

Evidence is now an **object** (`{"format": "opsaccord.live-evidence", "format_version": 2, "run": {...},
"active": {...}, "records": [...], "notes": [...]}`) rewritten atomically after every event, so whatever
is on disk is a complete, well-formed document. An interrupted run leaves the completed records plus an
explicit record for the active scenario - never a half-written array a reader could mistake for a
finished run. Note the divergence: the committed `docs/reports/phase1-live-scenarios.json` is a legacy
**bare array** (`format_version` 1, no run status). It is preserved as a record, not rewritten.

### Tests, falsification, and what was verified in this session

New focused tests cover the cases the handoff named: failure before injection, failure after injection
but before workflow creation, a workflow exception, Ctrl+C during polling, a cleanup failure stopping
subsequent scenarios, preservation of unrelated flags, progress appearing before suite completion,
partial evidence surviving interruption, and unreachable Payment without Payment-server traffic. They use
injected clocks and fakes, so the unit suite stays at ~5 s and never waits through a real stage budget.

| Check | Exact command | Result | Evidence path | Simulated or live |
| --- | --- | --- | --- | --- |
| Lint | `uv run ruff check .` | clean | - | offline |
| Format | `uv run ruff format --check .` | 80 files already formatted | - | offline |
| Types | `uv run mypy src/opsaccord` | success, 41 source files | - | offline |
| Offline suite | `uv run pytest -q -m "not integration"` | **125 passed, 7 skipped, 78 deselected** (99 passed before this pass; +26 new) | - | offline |
| Validation runner | `scripts/validate.sh --offline-only` | exit 0, both offline stages identical at 125 passed | `reports/local/validation-20260924T080233/` | offline |
| Guard | `scripts/validate.sh --scenario P1-01 --offline-only` | exit 2, "refusing to run: --scenario needs --live" | - | offline |
| Shell syntax | `bash -n scripts/validate.sh scripts/run_live_scenarios.sh` | clean | - | offline |
| Falsification | flip one fix, run the named tests, restore | 5 failed / 17 passed in round 1; 2 failed / 24 passed in round 2; no other test moved | `reports/local/live-recovery-falsification.log` (untracked) | offline |
| Integration suite, pre-fix sensitivity harness, nine simulated scenarios, seven live paths | `scripts/validate.sh --live` | **not run** - socket creation denied in the sandbox; the interrupted user run above is the only live evidence on this revision | `reports/local/validation-20260924T071253/` (interrupted) | live/blocked |

The falsification log records the exact one-line flips and the pre- and post-experiment hashes verified
with `md5sum -c`. It shows each new check pins the fix named for it and nothing else, which is the
property that distinguishes a regression test from a test that happens to pass.

### Independent agent review of the live-run-recovery pass

A fresh-context reviewer sub-agent ran read-only, on the harness default model, briefed to falsify the
pass rather than to read it, and it changed no files. It reported **9 findings with reproductions** -
the two high-severity ones being the skipped fault restore on an evidence-write failure and the
unacknowledged arm write - and the implementation agent confirmed each in the code before fixing it. All
nine are fixed and pinned, as the table above records, and the falsification log shows the pins failing
when the fixes are removed and passing when they are restored.

This is a second agent instance with an independent brief, **not** the optional external review, and it
is not presented as one. The external review still does not exist for any revision on this branch: the
GPT-6 Luna reviewer was retired in `1296b23` (recorded in the untracked vault, D14), the provider advertises only
`deepseek-flash` and `deepseek-v4-pro`, and a bounded Claude Code probe returns no output because the
sandbox denies its socket. See `docs/reports/phase1-review.md` for the honest record.

### Open questions from this pass

- **The live behaviour of the Payment corroboration is argued, not observed.** The calibration table is
  the reason to expect a caller-side signal to appear; no run has yet measured it. If `P1-01` fails to
  see the premise live, the honest outcome is a not-measured record, not a weakened assertion.
- **Whether the six verified scenario flags are the right ownership set.** The reset only ever writes
  those, which preserves the user's own experiments but means a fault armed on a seventh flag would not
  be reset. It would, however, still be visible in `detect_residual_faults` only if it is one of the six.
- **Whether the evidence format change should be versioned into the committed evidence file.** The
  legacy array is preserved deliberately; migrating it would make the reader's job easier and would also
  assert that the old run's status can be reconstructed, which it cannot.

## Dependency attribution for Payment (`2286565` base) - the caller's own view of the edge

**Status: PARTIAL.** The measured Payment failure is fixed at its root cause, and the diagnostic and
verification contract behind it is now explicit and pinned by **38 new tests**. The offline suite, the
linter, the formatter and `mypy` pass on this revision. **No database-backed test, no scenario run and no
live path has executed against it**: the escalation that the integration suite needs is rejected before
the command runs by the approval provider's own error (`The supported API model names are deepseek-flash,
deepseek-v4-pro, but you passed codex-auto-review`), the same misconfiguration recorded in the previous
pass. The new integration tests are therefore *written but unexecuted*, and Payment has not been
re-measured live. Both are stated as gaps below, not as results.

That blocker was re-measured on this revision rather than inherited, and it is wider than the approval
provider: **this sandbox denies every socket syscall.** `AF_UNIX` `connect`, `AF_INET` socket creation
and netlink all return `Operation not permitted`, so the project cluster cannot even start here
(`pg_ctl` fails with `could not create IPv4 socket for address "127.0.0.1": Operation not permitted` and
then `FATAL: could not create any TCP/IP sockets`), and an HTTP read of the demo stack is impossible for
the same reason. The cluster left running for the planning discussion is no longer up: the previous
session's `postgres` process is gone and `.local/pg/run/` holds only its stale socket and lock file, so
`scripts/validate.sh` stage `01-database` is what starts it again on your terminal. The escalation
attempt fails identically to the previous pass, with the approval reviewer's provider error quoted
above. Neither the code nor the tests are implicated, and neither was changed to suit the environment.

### The measured failure

`scripts/validate.sh --live --scenario P1-01` (user's terminal, 2026-09-24T08:25Z, 161.0s) recorded
`SAFE_ESCALATION` / `READY_FOR_HUMAN`, 0 actions, 0 approvals, 5 evidence items, incident
`inc_01M398H2ZC36N648ZHPN7Y1DZ6` (`reports/local/live-scenarios-20260924T082533.json`). Cleanup
succeeded: `paymentUnreachable` restored to `off` and read back. The harness saw the fault on the
**caller**: payment's own probe was `SERVING` / `healthy: true` / `error_rate 0.0` /
`calls_per_second 0.133`, while checkout's probe was `NOT_SERVING` / `healthy: false` /
`error_rate 0.333`, with `"attribution": "caller"` after 120.4s. The persisted diagnosis was
`payment-service is serving normally`.

The same scenario with the same fault passed earlier (`reports/local/live-scenarios-final.json`,
`inc_01M36T7BYVC1STQHW5TJB8VAEY`, 14 evidence items, 1 action, RESOLVED). It passed because the
*payment-side aggregate* Charge error share happened to clear the 0.05 bound in that run; the
`paymentUnreachable` mechanism itself is unchanged.

### Root cause

Three defects, not one:

1. **Nothing collected the caller's view.** The Payment `READ_PLAN` read only target-scoped signals
   (`read_service_health`, `read_metrics`, `read_traces` on payment) plus changes and the runbook. When
   checkout fails to reach payment, payment receives nothing and emits nothing: every one of those probes
   stays quiet. The only observation that can see this fault class is the caller's own client span, and
   no tool collected it.
2. **The adapter terminated on the healthy probe.** `DeterministicAdapter` checked
   `if healthy: -> "payment-service is serving normally"` *before* it looked at any dependency signal, so
   the one probe that is structurally blind to an unreachable callee was the one that ended the
   investigation. A healthy server probe is not evidence that the path to it works.
3. **The historical signal is not a per-caller attribution.** The probe's selectors match
   `span_name` only (`_op_call_counts` / `_call_counts`), so the Charge series mixes the server side and
   the client side of the same RPC. In the failing run that aggregate measured `error_rate 0.0` while
   checkout's own PlaceOrder series measured `0.333`, which is why a threshold change was not the fix:
   the signal that fired historically is an aggregate whose composition varies run to run.

### The diagnostic contract

* **Allow-listed edges.** `DEPENDENCY_EDGES` in `src/opsaccord/tools/contracts.py` holds the verified
  edges as `(caller, callee, operation)`: `("checkout-service", "payment-service", "Charge")`. Nothing
  else is addressable.
* **A typed, bounded read.** `read_dependency` takes `target` (the *caller*), `dependency` (the callee)
  and a bounded window. The registry pins `target` to the callers in the edge list and, per target,
  `dependency` to that caller's verified callees, so `checkout-service -> ad-service` and
  `payment-service -> payment-service` are refused before dispatch. Reads stay policy-permitted within
  budget; only `apply_remediation` is a write.
* **Attribution is recorded, not assumed.** The read records `caller`, `destination`, `operation` and
  `span_kind: "client"` on the evidence. In live mode the adapter resolves the *label schema* from the
  server (`/api/v1/series` for the operation, then `/api/v1/label/<name>/values`) instead of assuming
  names or values: `service_name`/`service` for the emitting service, `span_kind`/`span_kind_client`
  with a value containing the token `client` for the RPC side.
* **Five answers, in this order** (`classify_dependency_edge`): not attributable -> *indeterminate*
  (with the reason); declared window unusable -> *indeterminate*; no calls in the window ->
  *indeterminate*; missing or non-numeric failure share -> *indeterminate*; otherwise the share is
  compared against the single calibrated bound `TELEMETRY_ERROR_BOUND = 0.05` -> *failing* or *healthy*.
  The order is the contract: **an unreachable edge can never be reported as a healthy edge**, and a
  quiet window is neither a fault nor a recovery.
* **An instrumentation gap is reported as a gap.** If the exposed schema cannot supply those label
  names, the live read succeeds with `attributable: false` and the reason, and the adapter escalates
  with that reason rather than concluding normal operation.
* **No hidden fault knowledge.** The scenario id, the injected flag and the harness's own observations
  never reach reasoning. The adapter reads evidence by source like every other item.

### The reasoning rule that changed

For `payment-service` the order is now: a **self-contradictory probe** (healthy and a failing share at
once) escalates; a **failed or unreachable payment probe** proposes the verified flag as before; an
**attributable failing caller-side edge** proposes the same remediation while stating the two-level
finding - *"the checkout-service to payment-service dependency path cannot be reached; the payment server
itself is serving normally"*; a **healthy probe with an unproven path** (no read collected, or
unattributable, or a quiet window) escalates and names the gap; only a **healthy probe with a healthy,
attributed edge** reports normal operation. A healthy edge on its own never justifies a remediation, and
there is no `P1-01`-specific rule anywhere: the remediation is the mapping the runbook and the registry
already carry, and it still requires an exact human approval because payment is critical.

### Recovery verification on the same path

`_collect_verification_evidence` now returns one `_VerificationRound` and adds the caller-side read for
any target with callers. The verdicts follow: the edge **still failing** after the action ->
`VERIFICATION_FAILURE` (never a success, so never RESOLVED, and no second side effect is attempted);
**unproven** (indeterminate, or the read failed, or it was no longer affordable, or a human control
blocked it) -> `VERIFICATION_INCONCLUSIVE`; **healthy** evidence collected at or after the action
completed -> the existing success path. Freshness is per read: an edge read collected before the action
makes the whole round stale, exactly like the health probe.

### Changes

| File | Change |
| --- | --- |
| `src/opsaccord/tools/contracts.py` | Edge list and helpers, `TELEMETRY_ERROR_BOUND` (single definition), `DependencyVerdict`, `classify_dependency_edge`, `Environment.read_dependency` |
| `src/opsaccord/tools/registry.py` | `read_dependency` tool spec with the caller/callee allow-list |
| `src/opsaccord/tools/demo_environment.py` | Simulated edge read: calls are non-zero in both conditions, the share comes from the callee state, an unverified edge is refused |
| `src/opsaccord/tools/live_environment.py` | `PrometheusClient.series_labels`/`label_values`, schema resolution, client-side selectors, `read_dependency` |
| `src/opsaccord/tools/gateway.py` | Evidence kind, dispatch, `_VerificationRound`, the edge read in verification, edge-aware verdicts, stop settling when the read is unaffordable |
| `src/opsaccord/workflow/controller.py` | The caller-side step in the Payment plan; steps bind their own target |
| `src/opsaccord/reasoning/deterministic.py` | Edge-aware interpretation and the four-way payment rule; `TELEMETRY_ERROR_BOUND` re-exported from the contract |
| `src/opsaccord/scenarios/live.py` | Imports the bound from the contract; `LIVE_HARD_STEP_CAP` 32 -> **36** |

The live budget is the one constant that changed, deliberately: the intended path now costs 6
investigation reads + 1 model decision + 1 dispatch + 6 settle rounds x 4 reads = **32**, so a cap of 32
would leave a real recovery with zero headroom and turn a budget landing into a spurious
`VERIFICATION_INCONCLUSIVE`. 36 keeps the cap a wall with four steps of slack, and
`test_settling_fits_inside_the_live_hard_cap` now asserts the headroom rather than only the sum.

### Tests in this session

Executed: `ruff check .`, `ruff format --check .`, `mypy src/opsaccord` (41 files) and
`pytest -q -m "not integration"` -> **159 passed, 7 skipped, 82 deselected**. New: 10 rule cases in
`tests/unit/test_deterministic_adapter.py` (a healthy payment with a failing caller edge is a path
diagnosis; a failing edge is never reported as the payment service failing; a healthy edge on both sides
is not remediated; a quiet window; a missing read; an unattributable read; malformed evidence; a
collection error; a self-contradictory probe) and 17 in `tests/unit/test_dependency_attribution.py`
(the classifier's answers including the bound itself, the edge list, the simulated edge surface, and the
registry's refusal of any other pair). Seven more pin the **live** edge read in
`tests/unit/test_live_adapter.py` against the fake stack, because that is the half of this pass that
cannot run offline against the real stack and would otherwise have had no executed coverage at all: the
caller-side triple (`span_name` + `service_name='checkout'` + `span_kind='SPAN_KIND_CLIENT'`) and the
error selector that carry the attribution, the four ways the schema can fail to attribute (no series, no
service label, no span-kind label, no client-side kind value) each reported as `attributable: false`
with its reason and **no** value query, a quiet window that reports no calls and no share, and the
refusal of an unverified pair. Both halves were mutation-checked: dropping the span-kind predicate from
the selector and hard-coding the service label each fail the new tests, and the failures were reverted.

Written, **not executed here** (they need PostgreSQL): `test_p1_01_attributes_the_fault_to_the_callers_own_view_of_the_edge`
in `tests/acceptance/test_scenarios.py` (the caller-side evidence exists with its attribution, and the
successful verification used edge evidence collected after the action); and three checks in
`tests/acceptance/test_adversarial_controls.py` - a recovered target probe is not a recovered path (one
action, `VERIFICATION_FAILURE`, never RESOLVED, no second action), a stale edge read cannot verify
recovery, and an unaffordable edge read is inconclusive rather than fatal. The fixtures derive from the
observed payloads with no sensitive values.

### What this pass could not do

* **Payment has not been re-measured live.** The next `--scenario P1-01` run is the real test of the
  premise, and it is the one command left outstanding.
* **The client-side Charge error share has not been measured on the pinned stack.** The Failure
  signature table records the *service* probes; the edge's own series is inferred from the documented
  span topology (Charge is emitted by both sides; the client side carries `STATUS_CODE_ERROR`, and
  `astronomy-shop.md` now records the adapter's selector). If that series is absent or carries a
  different status label, the honest outcome is `attributable: false` with the exposed label names in the
  reason, a bounded handoff, and a measurement to act on - not a fabricated number and not a weakened
  assertion.
* **The new integration tests have not run.** They are written against the fixtures and the persistence
  contract, and they collect cleanly, but "written" is not "verified"; they belong to the same command as
  the live run.
* **The live edge read is pinned offline, not proven live.** The seven new fake-stack checks in
  `tests/unit/test_live_adapter.py` fix the selector, the error label and the four unattributable
  answers, and they are mutation-checked, so a regression in the adapter's *reasoning* now fails
  offline. They cannot say whether the stack exports a client-side Charge series at all; that number
  exists only in a live run, and the read is written to report its absence rather than paper over it.
* **The demo stack's own state is unknown from here.** Every HTTP read is denied by the sandbox, so
  whether Docker Desktop still holds the pinned stack up is unverified; `scripts/validate.sh` treats
  the stack as idempotent (`demo.sh health || demo.sh start`) and its endpoint stage rewrites
  `.local/demo-endpoints.env`, so a stopped stack is recovered rather than assumed.

## The verification refusal's cause (`d873dae` base) - a budget wall reported as a human control

**Status: PARTIAL.** The misreported cause is fixed at its root, with the safety outcome and every
limit untouched. The offline suite, the linter, the formatter and `mypy` pass on this revision. **The
integration suite and the Payment live run still have not executed here** - the sandbox denies every
socket and the one escalation attempt this pass made was rejected before the command ran by the same
approval-provider error recorded below - so the new database-backed checks are written but
unexecuted, and this revision of the Payment path has not been re-measured live. The user's own run
of this revision reached the integration stage but was cut off inside it; it showed that the fix
alone was not enough, because the previously failing test's cap arithmetic was one read short of the
refusal it means to pin (see below).

*Since this section was written, both halves ran on the next revision: the user's `104258` run passed
the integration suite (`85 passed`) and took the focused live P1-01 path, which escalated for a
separate reason. See "The live Payment verification (`104258`)" above for the measured outcome; the
analysis below is kept as it stood.*

### The measured failure

The user ran the full validation on `d873dae` with the project's own environment
(`reports/local/validation-20260924T085852/`): offline `159 passed, 7 skipped`, lint, formatting and
types clean, **integration `81 passed, 1 failed`**, and the later stages - including the Payment live
path - were not reached, because the integration stage fails the run.

The failing check was
`tests/acceptance/test_adversarial_controls.py::test_an_unaffordable_edge_read_is_inconclusive_rather_than_fatal`.
Its outcome was already correct - `VERIFICATION_INCONCLUSIVE`, never RESOLVED - but its detail was:

```
verification is inconclusive: a human control blocked the dependency-path verification
read (hard step cap reached)
```

The test asserted that the explanation names an unaffordable read. It was right and the code was
wrong: nothing human happened. This is a **misreported cause**, not a wrong safety outcome, and the
historical `SAFE_ESCALATION` result is not relabelled.

### The second measured run (the fix, and a second failure it exposed)

The user then re-ran the full validation on this revision (`reports/local/validation-20260924T091302/`,
`.local/user-live-checks-20260924T091302.log`). The offline suite, lint, formatting and types passed
again - `191 passed, 7 skipped`, matching the offline runs recorded below - and the run was cut off
inside the integration stage, after 43 of the 85 integration checks. The last thing recorded is

```
...............F...........................
```

The sixteenth integration check in collection order is the same
`test_an_unaffordable_edge_read_is_inconclusive_rather_than_fatal`, so the fix removed the human
wording but the test still failed. The abort left no summary line and no traceback, so the cause was
established by reading the code and the accounting rather than from the run's own output.

### Root cause

There are two producers of a budget refusal, and the second one was being described as a human act:

1. `repo.consume_step` raises `BudgetExhausted` when the cap has already been reached. The gateway
   already handled this correctly, and its wording is a budget wall.
2. `policy_engine.evaluate` also refuses a read at the cap, with `allowed=False` and
   `matched_rule="BUDGET_HARD_CAP"` (and, for autonomous side effects, `BUDGET_SOFT_EXHAUSTED`).
   `ToolGateway.read` consumes the step first and evaluates policy second, so a read that lands
   exactly on the cap is charged first and refused second, and the refusal rolls that charge back
   with it. That refusal arrives as
   `PolicyBlocked`, and the verification round's `except PolicyBlocked` handler described **every**
   policy refusal as `a human control blocked ...`. A hard cap was therefore reported as a human
   decision, in the same sentence, every time the cap landed on a read rather than before it.

Two smaller defects in the same path:

3. The round kept reading after a refusal. Once the health or metrics read was refused, the
   dependency-path read was still attempted, so the refusal a human was shown was whichever came
   **last** rather than the one that stopped the round - and the budget was spent on a round that
   could not settle.
4. The classification was taken from the readable sentence (`str(exc)`, i.e. the verdict's `reasons`)
   rather than from the verdict's structured rule id, which is why prose and cause could disagree.

### The correction

`RefusalCause` (`BUDGET` / `HUMAN_CONTROL` / `POLICY`) and `refusal_cause(decision)` now live in
`policy/engine.py`, beside the rules, and classify a refused decision by `matched_rule` alone - never
by its text. The rule ids are partitioned into three explicit sets, and the specific `RefusalCause` is
carried alongside. The repository's `BudgetExhausted` maps to the same `BUDGET` cause, so both
producers hand a caller the same vocabulary.

The gateway builds a small `VerificationBlock(cause, detail)` for each refusal, with the sentence
derived from the cause: a cap reads `the <subject> is/are no longer affordable (...)`, a human control
reads `a human control blocked the <subject> (...)`, and any other refusal reads
`policy refused the <subject> (...)`. The dependency-path verdict's detail is composed from the same
block, so the two cannot drift apart. The round now stops at the first refusal
(`if callers and blocked is None`), which keeps the stated cause the one that actually stopped it.

Deliberately unchanged: the budget limits, `consume_step`'s ordering and accounting, the persistence
schema (`VerificationRow` still stores `outcome` and `detail`, so existing rows and readers are
unaffected), the sentence a successful verification writes, and the failing test itself - its
assertion was correct and is not weakened. The new structure is in-flight only; what is persisted is
the same two columns as before.

| File | Change |
| --- | --- |
| `src/opsaccord/policy/engine.py` | `RefusalCause`, `BUDGET_RULES`, `HUMAN_CONTROL_RULES`, `POLICY_RULES`, `refusal_cause()` |
| `src/opsaccord/tools/gateway.py` | `VerificationBlock`, `_verification_block()`, all three read refusals classified by cause, the round stops at the first refusal |

### The check's cap arithmetic, and why the fix alone was not enough

The refusals are now classified correctly, but the failing test's cap was one step short of the read
it means to refuse. `ToolGateway.read` consumes the step *before* it evaluates policy, and the
verdict's cap test is `steps_used >= hard_step_cap`; a refusal then rolls its own charge back
(`Database.session` rolls back on the exception). A read is therefore refused when it *would land on*
the cap, not after it. With `hard_step_cap = steps_used + 3` the health probe and the first metrics
read are affordable, the **second metrics read is the one refused**, and the dependency-path read is
never reached - so the verdict read `the verification reads are no longer affordable`, while the test
(and its docstring) expects the refusal on the dependency-path read.

That accounting is also what the measured failure proves: its detail names the *dependency-path* read
as the refusal, which is only reachable when `steps_used < hard_step_cap` at that read, i.e. when the
earlier refused metrics read was **not** left charged. A refused read spends no budget.

The correction is the test's: `hard_step_cap = steps_used + 4` leaves the health probe and both
metrics reads affordable and puts the refusal exactly on the dependency-path read, which is the case
the check exists to cover. Its assertions are unchanged and none is weakened; the added comment says
why the number is four. The same arithmetic was checked against both readings of the accounting (a
charge that persists and one that is rolled back) and refuses the dependency-path read either way.

### Tests and what was executed

Executed at this revision (`reports/local/validation-20260924T091147/`):
`scripts/validate.sh --offline-only` -> **exit 0**; offline suite **191 passed, 7 skipped, 85
deselected** in both variants (with and without live settings); `ruff check` clean; `ruff format`
clean (83 files); `mypy` clean (41 files).

New offline coverage (30 checks):

- `tests/unit/test_refusal_cause.py` (20) - each real rule id maps to its cause; an unknown id falls
  back to the generic policy cause rather than to a human or a budget; a **source scan of the engine**
  asserts the three sets cover every `matched_rule` the engine returns with `allowed=False`, in both
  directions, so a rule added without a cause fails the suite; and four tests build *real*
  `PolicyRequest`s for the dependency read and pin the engine's actual verdicts - a spent cap yields
  `BUDGET_HARD_CAP`, a tool-scoped denial yields `CONSTRAINT_DENY_ACTION`, a takeover yields
  `HUMAN_TAKEOVER`, and a read inside budget is still `ALLOW_READ_ONLY`.
- `tests/unit/test_verification_blocks.py` (12) - the real round and the real verdict driven against a
  scripted gateway: the measured failure's exact shape (a `BUDGET_HARD_CAP` policy refusal of the
  dependency-path read) reports a budget cause and never the word "human"; the exception path does the
  same; takeovers, freezes and manual-handling constraints report a human cause; an allow-list refusal
  reports the third cause; a refused round takes no further read and no read is retried; and a healthy
  round still returns `VERIFICATION_SUCCESS` with its four evidence ids.
- Mutation-checked, all reverted: hard-coding the human cause at the path read fails three of the new
  tests; removing the stop-at-first-refusal guard fails the no-extra-read test; deleting a rule id
  from a classification set, and adding an unclassified rule id to the engine, each fail the drift
  guard.

Written, **not executed here** (they need PostgreSQL): three additions to
`tests/acceptance/test_adversarial_controls.py` - a tool-scoped human denial of the path read
(`CONSTRAINT_DENY_ACTION`, human wording, probes still answered, no tool call for the refused read),
a takeover that stops the round before any read, and a cap spent before the round that refuses the
first read without taking one. The previously failing test is unchanged apart from one added
assertion that the detail does not say "human", and the `+ 3` -> `+ 4` cap correction described
above, which is what makes it reach the read it means to refuse. Until that suite runs, all four
checks are unverified here, so the acceptance fixtures - not the classification - remain the open
risk on this pass.

### What this pass could not do

* **The integration suite has still not run on this revision.** The four new database-backed checks
  and the previously failing test belong to the one command block below. The classification itself is
  covered offline, including the engine's real verdicts, so the remaining risk is the acceptance
  fixtures rather than the fix.
* **Payment has still not been re-measured live**, and the client-side Charge series is still
  unmeasured on the pinned stack (see the limitation above): if the stack does not expose
  `span_kind`, the run reports `attributable: false` with the labels it did see, which is a
  measurement to act on rather than a failure to paper over.
* **One boundary behaviour, recorded rather than changed:** `read` consumes before it evaluates, so a
  read that would *reach* the cap is refused by the engine's verdict (`BUDGET_HARD_CAP`) instead of
  by `consume_step`'s exception. Accounting itself is unchanged - the refusal rolls its own charge
  back, the read does not execute, and `steps_used` never exceeds the cap - but the two producers are
  reached at adjacent boundaries, which is what the new acceptance checks pin from both sides. At the
  live cap of 36 against an intended 32-step path the slack absorbs the difference; reordering budget
  accounting was out of scope for a classification fix.

## The live Payment verification (`104258`) - the edge schema query was unparseable

**Status: PARTIAL.** The user's full validation now passes end to end, and the focused P1-01 live
path ran for the first time on a revision that carried the previous fix. It escalated safely, and
the persisted evidence says why: the dependency read could not run at all against the live stack
because its schema query was a PromQL parse error, so no caller-side evidence was ever collected and
recovery stayed unproven. That read is corrected here, with a regression test that fails without the
correction. **The correction itself has not been exercised against the pinned stack** - the sandbox
still cannot open a socket and the approval route is still rejected - so the live half remains open.

### The user's measured run (revision `4ccca4d`)

`reports/local/validation-20260924T104258/`, `.local/user-live-checks-20260924T104258.log`,
`reports/local/live-scenarios-20260924T104258.json`:

| Stage | Result |
| --- | --- |
| 02 / 03 offline | `191 passed, 7 skipped, 85 deselected` in both variants |
| 04-06 lint, format, types | clean |
| 07 integration | **`85 passed`** - every acceptance check, including the corrected `steps_used + 4` cap check |
| 08 sensitivity harness | `5cbcce2` fails the two defect tests, HEAD passes all three |
| 09 simulated scenarios | 9/9 |
| 13 live P1-01 | `SAFE_ESCALATION`, `ESCALATED_INCOMPLETE`, 1 approval, 1 action, no successful verification, 432.4 s; cleanup read `paymentUnreachable` back as `off` |

This is the first run in which the whole database-backed suite passed, so the previous pass's cap
arithmetic correction is now observed rather than reasoned.

### What the persisted evidence establishes

* **Diagnosis and action.** `inc_01M39GGE3DJVR0SD8ASX7WCTCK`. The diagnosis was
  *"payment-service is refusing connections; Charge spans error while request rate collapses, which
  is consistent with the paymentUnreachable fault flag being enabled"* - the adapter's
  `failing or unreachable` rule, fed by the target probe (`health=NOT_SERVING`, `error_rate=1.00`) and
  the failing Charge spans. One exact approval, one action
  (`apply_remediation(disable_feature_flag, paymentUnreachable)`), dispatch acknowledged.
* **The action cleared the flag, and that is all it proves.** Cleanup found the flag already `off`
  (the controller disabled it) and read it back as `off`. A flag read-back is not evidence that the
  caller's dependency path recovered - and this run has no evidence that it did or did not.
* **No attributable caller evidence was ever received.** Every `read_dependency` call failed with
  `LiveEnvironmentUnavailable: prometheus /api/v1/series failed: Client error '400 Bad Request' for
  url '.../api/v1/series?match%5B%5D=span_name%3D%27oteldemo.PaymentService%2FCharge%27'`. The
  diagnosis therefore fell back to the target signature, and the verification had nothing to read.
* **The stop cause was a collection failure**, not budget, human control, policy, insufficient
  traffic or a measured failure: the dependency read *failed*, so the round reported
  *"the dependency-path verification read failed (LiveEnvironmentUnavailable: ...) so recovery was
  not established"*, and the verdict was `VERIFICATION_INCONCLUSIVE`.
* **The workflow exhausted its attempts rather than stopping early.** 30 evidence items decompose as
  the 6 diagnosis reads plus 6 verification attempts x 4 reads (health, two metrics, the failed
  dependency read); 6 attempts mean 5 settle sleeps of 30 s = 150 s, matching the 151 s workflow
  stage. A failed read is not a refusal, so the settle loop kept refreshing evidence.
* **The same edge was checked for recovery as for diagnosis**: the verification round reads
  `dependency_callers("payment-service")` -> `checkout-service`, the same
  `checkout-service -> payment-service` path the diagnosis plan reads.
* **Not observable from the persisted summary:** the values each attempt's health/metrics reads
  returned, and therefore whether the 120 s windows still contained pre-remediation errors. No query
  values are persisted for a failed read.

### Root cause

`/api/v1/series` parses `match[]` as a **series selector**. The adapter sent
`span_name='oteldemo.PaymentService/Charge'` - a label matcher without the braces - which is a PromQL
parse error, and the API answers it with `400 Bad Request`. Two facts make this unambiguous:

* a selector that matches *nothing* is not a 400. The API returns `200` with an empty list, and the
  adapter has an explicit path for that (`no series matches span_name=...`). A 400 can only be a
  request-level failure, so the schema query was malformed rather than unlucky.
* the same run's other label lookups worked: the preflight's `347 prometheus series`
  (`/api/v1/label/__name__/values`) and every metric/span query (braced selectors inside
  `sum(rate(...))`) came back with data. Only the `match[]` value's shape differed.

So `_resolve_edge_labels` could never resolve a schema against the real stack:
`read_dependency` failed on every call, every edge observation became a `COLLECTION_ERROR`, and the
verification could not see the caller's side of the path even after the remediation landed. The
offline suite could not catch it because the fake stack answered *any* selector with `200` - a
synthetic schema test proving only that the adapter was consistent with itself.

### The correction

| File | Change |
| --- | --- |
| `src/opsaccord/tools/live_environment.py` | `_series_selector()` builds a braced series selector, and `_resolve_edge_labels()` uses it |
| `src/opsaccord/tools/live_environment.py` | `PrometheusClient._get` reports a bounded, single-lined response body for HTTP error statuses, so a 4xx says *why* (the live evidence said only `400 Bad Request`) |
| `tests/unit/test_live_adapter.py` | the fake stack now **rejects** a selector the real server rejects (`400`, `bad_data`, `parse error`) instead of answering every selector, and the previously pinned unbraced selector expectation is corrected |
| `tests/acceptance/test_adversarial_controls.py` | the `steps_used + 4` check now asserts the *shape*, not only the message |

No selector was loosened, no bound moved: a schema the server cannot supply still reports
`attributable: false` with its reason and still hands off. The three unattributable cases, the quiet
window and the refusal of an unverified pair are unchanged.

### Tests

Executed (`reports/local/validation-20260924T112600/`): `scripts/validate.sh --offline-only` ->
**exit 0**; offline **194 passed, 7 skipped, 85 deselected** in both variants; `ruff check` clean;
`ruff format` clean (83 files); `mypy` clean (41 files).

New offline coverage in `tests/unit/test_live_adapter.py` (28 checks in the file now, 3 new):
the edge read's schema selector is the braced form; the fake refuses the unbraced form the server
refuses; and a refused query carries the server's own reason rather than only a status code.
**Mutation-checked:** reverting `_resolve_edge_labels` to the unbraced selector fails 7 checks -
including the four unattributable-path checks and the quiet-edge check - with the live run's own
`400 Bad Request` in the message. The mutation was reverted.

The acceptance check for the cap was strengthened as instructed: it asserts that `read_service_health`
and both `read_metrics` reads were recorded under the cap and that no `read_dependency` call was, so the
refused read is the dependency-path read specifically. The `+ 4` arithmetic underneath it is measured -
the pre-strengthening version is one of the 85 integration tests the user's `104258` run passed - and
the strengthened version has since run as well: in the user's `114517` run it failed, not on the
arithmetic but on its own observation scope. That is corrected in
"The test-scope correction (`114517`)".

### What this pass did not establish

* **The live fix is unconfirmed.** Whether the braced selector resolves a usable schema (a service
  label, a span-kind label with a client value) on the pinned stack is only answerable by the next
  live run. If it cannot, the read reports `attributable: false` and the run escalates with a named
  instrumentation gap - a valid measurement, not a failure to paper over.
* **Post-remediation recovery is unmeasured.** The flag was disabled and read back, but no live
  read demonstrated that the caller's Charge path recovered.
* **Timing was left alone.** Six settle attempts of 30 s span 150 s against a 120 s window, so the
  final attempt can be clean while the first cannot. That is consistent, not contradictory, and no
  measurement here justifies raising a timeout, a retry count or a budget.
* **A deterministic adapter failure is retried six times.** ~151 s of the 432 s run re-ran a read
  that could not succeed. Retrying is defensible (a Prometheus restart is transient), so it is
  recorded as an observation for discussion rather than changed.

## The test-scope correction (`114517`) - the cap check asked the wrong history

The user's full `scripts/validate.sh --live --scenario P1-01` on `2812871` cleared every stage except
one acceptance check, so the harness stopped at integration and the live Payment path never ran. **No
live outcome is claimed for this revision**, and none of the remaining scenarios was started.

### The user's measured run (revision `2812871`)

| Stage | Result | Evidence |
| --- | --- | --- |
| Selector probe (its first measurement) | unbraced `span_name='oteldemo.PaymentService/Charge'` -> `http 400 ... bad_data ... parse error: unexpected "="`; braced -> `http 200, 160 series` | `.local/user-live-checks-20260924T114508/series-selector.log` |
| Offline suite, both variants | **194 passed, 7 skipped**, 85 deselected | `reports/local/validation-20260924T114517/02-offline-suite.log`, `03-...` |
| Lint / format / mypy | clean; 83 files; 41 source files | `04-`, `05-`, `06-` |
| Integration suite | **84 passed, 1 failed** - `test_an_unaffordable_edge_read_is_inconclusive_rather_than_fatal` | `07-integration-suite.log` |
| Live P1-01 | **did not run** - the harness stops on the first failing stage | - |

### The defect: the check observed the whole incident, not the round

The verdict and its wording were right; the assertion after them was not:

```
assert "read_dependency" not in where
E   AssertionError: assert 'read_dependency' not in ['read_service_health', 'read_metrics',
    'read_traces', 'read_dependency', 'read_recent_changes', 'read_runbook', ...]
```

`where` was the incident's whole tool-call history, and that history *should* contain a dependency
read: the P1-01 diagnosis plan reads the caller's own view of the edge before anything is remediated.
`READ_PLAN["payment-service"]` is `read_service_health`, `read_metrics`, `read_traces`,
**`read_dependency` (`checkout-service -> payment-service`, `Charge`, 300 s)**, `read_recent_changes`,
`read_runbook`. A whole-history absence test can therefore never pass, and its failure says nothing
about the verification round. The assertion directly above it - which *did* scope itself to the round -
passed, and the refusal really was the dependency-path read.

### The correction

`before` is now the set of persisted tool-call ids and the round's own reads are selected by id:

```
added = [call for call in tool_calls(db, case.incident_id) if call.id not in before]
```

`tool_calls()` orders by `started_at` alone, which is not a total order (two reads can share a
timestamp), so identity comes from the persisted id rather than from position. The assertions kept,
now scoped to the round: the outcome is `VERIFICATION_INCONCLUSIVE`; the refusal is the dependency-path
budget refusal, by name; it is not described as a human control; exactly `read_service_health` and two
`read_metrics` calls were added - which also proves they were affordable *and* answered, because the
verdict only reaches the dependency branches with `health.result.succeeded` and `metrics_ok` both true;
no `read_dependency` call was added; and the **persisted** verification row carries the same outcome and
the same cause as the returned verdict. `assert_no_resolution_and_one_action` still closes the test.

No runtime behaviour, budget arithmetic or expected safety outcome changed.

### What was executed here, and what was not

* Executed: `scripts/validate.sh --offline-only` -> **exit 0**, 194 passed / 7 skipped in both variants,
  ruff and format clean (83 files), mypy clean (41 files) (`reports/local/validation-20260924T120447/`);
  `pytest -m integration --collect-only` -> **85 collected**, so the edited test still collects; ruff
  check and format clean on the edited file.
* **Not executed: the corrected test.** Running it needs PostgreSQL, and this sandbox denies sockets -
  `AF_INET` and the project's own `AF_UNIX` socket both fail with `Operation not permitted`, and the
  direct attempt stops at `psycopg.OperationalError: connection is bad`. Written is not verified; the
  next run is what confirms it. No escalation was retried.
* Premises checked executably rather than assumed: `READ_PLAN["payment-service"]` does contain the
  diagnostic `read_dependency`; and against the measured schema, the two label names the resolution
  needs - `service_name` and `span_kind` - are present in the 160-series label set, so the "no service
  label" and "no span-kind label" unattributable outcomes are excluded by measurement.

### What the selector probe does and does not establish

It proves request syntax and label presence: the server parses the braced selector, returns 160 series
for the Charge span, and the schema exposes `service_name`, `span_kind`, `span_name` and `status_code` -
the labels the caller-side and error-rate queries need. It does **not** prove that the controller
receives attributable caller-side evidence, nor that recovery verification succeeds. Still unmeasured:
whether `span_kind` holds a client-side value, and whether the caller's Charge series carries traffic
in the window after remediation.

## The live Payment premise (`b4d80e7` measured, corrected at `79d733e`) - the visibility check accepted telemetry the write had not caused

### The user's measured run

Revision `b4d80e7`, logs `reports/local/validation-20260924T131320/`, live evidence
`reports/local/live-scenarios-20260924T131320.json`, user log directory
`.local/user-live-checks-20260924T131309/`. Every measurement below is the
**user's**, taken in a normal WSL terminal; nothing in this pass ran the stack.

- Offline `194 passed, 7 skipped` in both environment variants; lint, formatting, `mypy`
  and the sensitivity harness clean; simulated scenarios `9/9`.
- Integration `85 passed` - the test-observation-scope correction from `b4d80e7` is now
  verified in a real run.
- Live P1-01: `SAFE_ESCALATION` / `ESCALATED_INCOMPLETE`, one approval, one action,
  30 evidence records, 151.9s, incident `inc_01M39RV5QE944KHF48MXKAXPKE`, cleanup
  `paymentUnreachable` `off -> off` with `off` read back.

### What the run finally established, and what it did not

The dependency read now works live. The verification verdict is **`VERIFICATION_FAILURE`**
(not a collection error and not a refusal), and its recorded reason names the attributed
edge and the measurement:

> the affected dependency path is still failing after the action: checkout-service ->
> payment-service failed on 100.00% of 0.033 calls/second in the window, above the 5%
> telemetry bound; target probe health=NOT_SERVING error_rate=1.0

That is A14's fix confirmed against the pinned stack: `read_dependency` resolved the
schema, attributed `caller=checkout-service`, `destination=payment-service`,
`operation=Charge` and read the caller's own client spans. The stop cause is a **measured
continuing failure** after six honest settle rounds (5 x 30s of the 151.9s), not a budget,
policy, human-control or collection refusal. **Payment recovery is still unverified on this
revision; the outcome is not a pass.**

### The setup-timing concern is confirmed as a defect

| Stage | Window | Read | Result | Attribution | Measurement |
| --- | --- | --- | --- | --- | --- |
| baseline | n/a | flag document | ok | - | all six verified flags at their safe variant |
| traffic-wait | 120s | `read_service_health(payment-service)` | ok, first poll | - | `calls_per_second` 0.0167 |
| fault-injection | n/a | `set_flag_variant(paymentUnreachable=on)` | acknowledged | - | - |
| fault-visibility | 120s, ending at the write | `read_service_health(payment-service)` | **accepted** | target | `NOT_SERVING`, `error_rate` 1.0, `calls_per_second` 0.0167, **elapsed 0.0s** |
| diagnosis | 120s | health, metrics, traces, dependency, changes, runbook | ok | controller read | "payment-service is refusing connections" |
| approval | n/a | exact approval | granted | - | one approval, one action |
| action | n/a | `disable_feature_flag(paymentUnreachable)` | acknowledged | - | no duplicate, no forbidden action |
| verification, 6 rounds | 120s each | 4 reads per round, including `read_dependency` | ok | checkout -> payment, `span_kind=client` | 100% of 0.033 calls/s, above the 5% bound; target probe `NOT_SERVING` 1.0 |
| cleanup | n/a | `restore_owned_fault` | ok | - | `off` written, `off` read back |

The per-round rows (every tool call, every evidence record, every verification reason)
exist only in the operational database, not in the JSON or the teed log: the file carries
the aggregate "30 evidence" and one final verdict string. `scripts/diagnose-live-incident.py`
was added for exactly that export and is described under "Next action" below.

**What "baseline clean" verifies is the flag document, and only that.**
`_establish_scenario_baseline` calls `detect_residual_faults`, which reads the six verified
flags; the note it writes says so in as many words ("every scenario fault flag is at its
safe variant"). No telemetry-health baseline was ever taken, so a demo whose *services*
were already failing passed the baseline check cleanly.

**The accepted observation cannot have been produced by the write.** The probe averages a
120s window, and the pinned stack's span metrics publish on a ~60s cadence (measured: no
span-derived window shorter than 120s ever reads as anything but empty). A reading accepted
0.0s after the write therefore averages two minutes of samples that predate it. It was
accepted because the predicate was a *level* (`error_rate > 0.05`) with no comparison to
anything - and the pre-injection state it silently inherited was already a total failure:
`corroborating_before` (`checkout-service`, read *after* the write) shows `NOT_SERVING`
with `error_rate` 1.0 and `calls_per_second` 0.0333. The run recorded "fault visible after
arming" for a fault it never showed.

Two further facts are visible in the code, and both are the same defect:

- `_corroborating_baseline` was called **after** `arm_fault`, while its own docstring says
  it carries the reading "from *before* this scenario armed anything"; the differential it
  feeds therefore compared the armed state with itself.
- The target branch returned before the corroborating branch was ever read, so the
  differential was structurally unreachable and `corroborating` is `null` in the record.
- Where a baseline was absent, `(corroborating_before or {}).get("error_rate")` read it as
  `0.0`, so any caller-side failure counted as a rise - an absent reading treated as a
  clean one.

**A second occurrence had not been recorded.** `reports/local/live-scenarios-20260924T121118.json`
(user directory `.local/user-live-checks-20260924T121110/`) is the same shape on the same
revision: `SAFE_ESCALATION` / `ESCALATED_INCOMPLETE`, `attribution: target`,
`elapsed_seconds: 0.0`, pre-injection `error_rate` 1.0, 172.2s, incident
`inc_01M39NA4SPRDPF8KWJPZVBPMAW`. Its log also shows the traffic wait reading payment at
`calls_per_second=0.0` before the injection, so Charge traffic is intermittent and a single
window proves nothing on its own.

Across the five formatted runs:

| Run | Attribution | Pre-injection (payment) | Accepted probe | Fault visible after | Total |
| --- | --- | --- | --- | --- | --- |
| `081025` | caller | SERVING 0.0, 0.083/s | NOT_SERVING, `error_rate` none, 0.0/s | 240.8s | 362.0s |
| `082533` | caller | SERVING 0.0, 0.033/s | SERVING 0.0, 0.133/s | 120.4s | 161.0s |
| `104258` | target | SERVING 0.0, 0.0397/s | NOT_SERVING 1.0001, 0.0301/s | 240.8s | 432.4s |
| `121118` | target | **NOT_SERVING 1.0, 0.0667/s** | NOT_SERVING 1.0, 0.0333/s | **0.0s** | 172.2s |
| `131320` | target | **NOT_SERVING 1.0, 0.0333/s** | NOT_SERVING 1.0, 0.0167/s | **0.0s** | 151.9s |

### What remains unknown, and why it is not guessed

*Why* the Charge series carried a 100% error share **before** the write is not established
from the files. Three explanations fit the same numbers and no offline read separates them:
a genuinely failing payment path; a fault left armed somewhere other than the flag
document; or the health probe's own identity. That last one is a measured property, not a
guess: `read_service_health` for a target builds
`sum(rate(<calls>{span_name='oteldemo.PaymentService/Charge'}[120s]))` with **no service and
no span-kind filter**, so payment's server spans and checkout's *client* spans for the same
operation are one series set - the blind spot D22/A15 records for diagnosis. "Payment is at
100% errors" can therefore be a caller-side reading.

Nothing here claims the payment path recovered, and nothing re-runs the scenario to
harvest information the database already holds.

### The correction

Implemented at `79d733e` (engine, tests and the shared fake) plus the read-only export
`scripts/diagnose-live-incident.py`.

- **Both pre-injection readings are taken before the write**, adjacent to it, and passed
  into `wait_for_fault` as required keyword arguments (`target_before`,
  `corroborating_before`). The signature has no defaults, because there is no premise
  without a baseline.
- `_fault_is_new` makes the predicate a **transition**: the reading must be above the
  telemetry bound *and* strictly higher than its own pre-injection reading. A missing
  baseline is not a baseline of zero, so an unreadable one keeps the harness waiting and
  ends in an honest `NOT_MEASURED` at the `fault-visibility` stage rather than in an
  invented premise. The rule the corroborating branch already used is now the rule for the
  fault target too.
- The record carries the claim: `fault_visible.target_before` alongside
  `corroborating_before`, `window_seconds` on every health snapshot, and a `_fault_reason`
  that names both sides of the rise.

**What the correction does not claim.** It is not a clock gate, and it deliberately does
not add one. The accepted reading still averages a 120s window that overlaps the write;
what makes it attributable is the transition, which is the strongest statement a single
120s average supports. Requiring an observation whose window begins *after* the write would
cost at least one full span-metric window per scenario for a guarantee the differential
already provides, so the limit is recorded here instead of hidden behind a sleep.

### Tests, falsification, and what was executed

Three tests were added and one was corrected (`tests/unit/test_live_lifecycle.py`), and the
shared fake now models the real shape - a demo shows a fault only while its flag is armed
(`fault_only_when_armed`), and `cleared()` is what a safe flag position reads:

- `test_the_pre_injection_readings_are_taken_before_the_fault_is_armed` pins the ordering
  through a full scenario: the caller's baseline must read 0.0 and the accepted probe 1.0,
  and the reason must name the pre-injection reading.
- `test_a_target_that_was_already_failing_is_not_the_armed_fault` pins the `131320` shape:
  a demo already at 100% errors is never accepted as the fault this run armed.
- `test_an_unreadable_baseline_is_not_a_baseline_of_zero` pins the hole where an absent
  baseline read as `0.0`.
- `test_a_caller_that_was_already_erroring_is_not_the_fault` keeps its meaning and now
  supplies both baselines, as the driver does.

**Falsification** (`reports/local/setup-timing-falsification.log`, untracked): with only the
engine reverted to `HEAD`, the ordering test fails - the old code records
`corroborating_before` *after* the write, reads 1.0 against 1.0 and never fires - and
`reports/local/setup-timing-defect-repro.py` accepts a pre-existing failure as the armed
fault with `attribution: target`, `elapsed_seconds: 0.0`, `error_rate: 1.0` and
`calls_per_second: 0.0167`: the live record's own numbers.

Executed in this session: offline `197 passed, 7 skipped` in both variants (three more tests
than the user's 194), `ruff check`, `ruff format --check`, `mypy` (41 source files). Not
run: the integration suite, the sensitivity harness and the simulated scenarios (all need
PostgreSQL, and this sandbox denies every socket - `psycopg.OperationalError: connection is
bad`, recorded and not escalated again), and the live path (no demo access from here). The
new diagnostic script was exercised only far enough to prove it degrades honestly when both
channels are unreachable; it has not yet read a real incident.

## The Payment fault's behaviour and a scoped premise (`9058671` measured, corrected at `e6232c2`) - the visibility check accepted an unattributable caller failure

### The user's measured run

Revision `9058671`, validation logs `reports/local/validation-20260924T233003/`,
live evidence `reports/local/live-scenarios-20260924T233003.json`, user log
directory `.local/user-live-checks-20260924T232852/`, incident
`inc_01M3AWN0DG6WDAD20EGGK6YA1G`. Every measurement below is the **user's**, taken in
a normal WSL terminal; nothing in this pass ran the stack.

- Offline `197 passed, 7 skipped` in both environment variants; integration
  `85 passed`; lint, formatting, `mypy`, the sensitivity harness and all 9 simulated
  scenarios clean.
- Live P1-01: `SAFE_ESCALATION` / `READY_FOR_HUMAN`, **0 approvals, 0 actions**,
  6 evidence records, 6 tool calls, steps 7/36, one model call, 541.9s.
- Cleanup: `paymentUnreachable` restored `on -> off`, read back `off`; the flag
  document in both diagnosis logs shows all six verified flags safe.
- Pre-injection readings healthy (payment `SERVING`, error 0.0, 0.100 Charge calls/s;
  checkout `SERVING`, error 0.0). The premise was accepted at the caller probe:
  checkout `PlaceOrder` error share **0.1766** at `elapsed_seconds` 361.0 against its
  own pre-injection 0.0, with the target probe still `SERVING` at 0.0.
- The controller's own reads: `read_service_health` `SERVING`, error 0.0;
  `read_dependency` `attributable: true`, `calls_per_second` 0.0125, `error_rate` 0.0.
  Its recorded diagnosis was *"payment-service is serving normally and the
  checkout-service to payment-service path is carrying successful calls"*, so it
  handed off without remediation.

### What the pinned source says the fault does

The pinned checkout lives in the local demo checkout (tag `3.1.0`, commit
`dedc0178`), and it says something the previous pass had only inferred from the
service probe:

```go
// src/checkout/main.go:573-589, tag 3.1.0
if flags.PaymentUnreachable.Value(ctx, openfeature.EvaluationContext{}) {
	badAddress := "badAddress:50051"
	c := mustCreateClient(badAddress)
	paymentService = pb.NewPaymentServiceClient(c)
}
paymentResp, err := paymentService.Charge(ctx, &pb.ChargeRequest{ ... })
```

So the anchored flag does **not** skip the Charge: checkout redirects it at an
unresolvable address and issues the same RPC, which fails in the caller
(`mustCreateClient` is a lazy `grpc.NewClient` with `otelgrpc.NewClientHandler()`,
`src/checkout/main.go:473-483`). The flag is evaluated with an empty context and a
`false` default (`src/checkout/flags/flags_gen.go:73-91`), it is an `off`/`on`
boolean with no targeting (`src/checkout/flags.json`, `src/flagd/demo.flagd.json`).
Source-derived expectations: the caller's own client span for
`oteldemo.PaymentService/Charge` is the failing operation and the attributable
signal; no Charge reaches payment while the armed variant is in effect, so
payment's own span carries no error; and **the flag document is not the
evaluation** - a document reading `on` does not prove a running service evaluated
it as true.

### Measured versus inferred, and what the evidence does not settle

The user's post-cleanup capture
(`.local/user-live-checks-20260924T232852/diagnose-after.log`) groups a 120s window
that ends at 07:40:14 SGT, i.e. inside the armed period (armed 07:34:12, cleared
07:40:14):

| Span | Emitter | Kind | Status | Rate |
| --- | --- | --- | --- | --- |
| `oteldemo.PaymentService/Charge` | `checkout` | `SPAN_KIND_CLIENT` | `STATUS_CODE_UNSET` | 0.0500/s |
| `oteldemo.PaymentService/Charge` | `payment` | `SPAN_KIND_SERVER` | `STATUS_CODE_UNSET` | 0.0500/s |
| `oteldemo.CheckoutService/PlaceOrder` | `checkout` | `SPAN_KIND_SERVER` | `STATUS_CODE_UNSET` | 0.0500/s |
| `oteldemo.CheckoutService/PlaceOrder` | `checkout` | `SPAN_KIND_SERVER` | `STATUS_CODE_ERROR` | 0.0109/s |
| `oteldemo.CheckoutService/PlaceOrder` | `frontend` | `SPAN_KIND_CLIENT` | `STATUS_CODE_UNSET` | 0.0500/s |
| `oteldemo.CheckoutService/PlaceOrder` | `frontend` | `SPAN_KIND_CLIENT` | `STATUS_CODE_ERROR` | 0.0109/s |

That is the mismatch in one table: **17.9% of PlaceOrder calls failed while the
Charge series on both sides stayed `UNSET`**, and payment served the path at
0.050/s. The same log records a four-minute hole in the span-metric series for both
operations (23:36:12Z-23:40:13Z) while container metrics kept updating.

One run earlier, the same fault did produce the source-derived shape: at
`b4d80e7` the attributed edge read **100.00% of 0.033 calls/s** and the payment
probe read `NOT_SERVING`/`error_rate` 1.0 (the second of those because an unscoped
operation selector was summing the caller's client spans into the callee's probe).

What remains **unsettled, and is not guessed**: why 17.9% of PlaceOrder calls failed
without a failing Charge attempt in the `233003` window. The candidates are that the
running service evaluated the flag as false for those requests (the document is not
the evaluation), that those requests failed before the Charge call for a reason
unrelated to payment, or that a failing Charge attempt produced no exported caller
span. Any one of them means the scenario could not claim an attributable Payment
failure in that window, which is what the controller correctly refused to claim.

### The two defects, and the corrections

**1. The premise accepted an unattributable failure.** P1-01's plan corroborated the
fault with `checkout-service`'s own RPCs, and the controller's read plan requires a
*generic parent span* to be insufficient evidence for a Payment fault. The harness
and the controller therefore disagreed about what establishes the fault: the run
armed on checkout's 17.66% and the controller could not attribute it. A plan may now
name `corroborating_dependency` (the caller whose own client call to the scenario's
service must show the fault), and when it is set a rising caller-side health reading
is recorded as context and is **never** sufficient. The premise stays differential:
the attributed share must be above the telemetry bound *and* above its own
pre-injection reading, taken before the write.

**2. The health predicate was not scoped to a service.** `_op_selector` built
`sum(rate(<metric>{span_name='<op>'}[Ws]))` with no service filter, so a verdict
about `payment-service` was decided by every service emitting that operation -
`payment` server spans *and* `checkout` client spans (the table above shows both in
one series set). `read_service_health` and `read_metrics(error_rate)` now resolve the
service label from the exposed schema (`service_name`/`service`), cache it per
operation, and scope the selector; the value records `service` and `service_scope`,
and where no service label is exposed the reading says it is *unscoped* rather than
presenting an aggregate as one service.

**The semantic change, stated plainly:** the health probe is now a statement about
the target's *own* spans only. It can no longer be made `NOT_SERVING` by the
caller's failures - which is what the `131320` run recorded as "payment-service ...
100% error share" - nor credited with the caller's successes. Whether the caller can
reach the dependency is `read_dependency`'s question, and recovery still requires
both (`gateway.verify_service` unchanged). The reasoning rules, budgets, waits,
selector windows, the fault and P1-01's objective are all unchanged.

### Tests, falsification, and what was executed

New coverage, all with fixtures that model the measured shape rather than the
adapter's assumptions (the shared fake now carries per-emitter error rates and a
`read_dependency` surface with `dependency_error_share` / `dependency_attributable`):

- `tests/unit/test_live_adapter.py`: health is scoped to the target's own spans; a
  scoped reading still reports `NOT_SERVING` when the target's own spans error; the
  `error_rate` metric read is scoped.
- `tests/unit/test_live_lifecycle.py`: a generic caller-side failure cannot arm the
  Payment premise (the `233003` shape); an unattributable edge cannot arm it; quiet,
  missing and unattributable edge readings are not faults; and the pre-injection
  ordering test now pins that the *attributed* reading is what satisfies the
  premise, with the generic caller rise recorded beside it.

**Falsification** (both untracked, in `reports/local/`): `health-scope-falsification.py`
runs the scoped adapter with `_service_scope` forced to answer "unscoped" and prints
`SERVING error_rate=0.0` (scoped) against `NOT_SERVING error_rate=0.5` (unscoped);
`payment-premise-scope-falsification.py` feeds the measured `233003` state to both
premise contracts and shows the caller-only rule **accepting** it as
`attribution: 'caller'` while the attributed rule refuses it.

Executed in this session: offline `203 passed, 7 skipped, 85 deselected` in **both**
environment variants (six more tests than the user's 197), `ruff check`,
`ruff format --check` (85 files), `mypy src/opsaccord` (41 source files), the focused
live unit tests (`59 passed`), and the new diagnostic script's failure path (it
degrades honestly, restores nothing it did not arm, and exits non-zero). **Not run here:** the
integration suite, the sensitivity harness and the simulated scenarios (they need
PostgreSQL and this sandbox denies every socket - re-measured as
`curl: (7) failed to open socket: Operation not permitted`), and any live path.
P1-01 has still never reached `RESOLVED`.

### The bounded diagnostic capture

`scripts/diagnose-payment-fault.py` exists because the next step must be a
measurement, not another blind scenario run. It is **a diagnostic capture, and says
so on every run**: it never runs a scenario, writes no incident and scores no
outcome. It refuses to start beside a running scenario driver or on a demo whose
verified fault flags are not safe; it reads the baseline (flag document, both health
probes, the attributed edge, the grouped Charge/PlaceOrder series, recent Charge
traces), arms **only** `paymentUnreachable` through the harness's own `arm_fault`
with the write timestamped, polls both readings side by side until its deadline,
restores the flag in a `finally` block with a read-back, and writes the capture to
`reports/local/`. Its verdict names `attributable_failure`,
`generic_caller_failure_only` (the `233003` signature), `no_traffic`,
`no_attributable_failure` or `no_observation`, and exits non-zero if the capture
failed or the flag could not be restored.

### What the next result decides

- `attributable_failure`: the caller's own client share rose above the bound while
  the flag was armed. P1-01 is demonstrable on this stack; re-run the focused live
  path on this revision and the premise, diagnosis and verification will all rest on
  the same attributed signal.
- `generic_caller_failure_only`: the demo degrades without the attributed evidence,
  as in `233003`. Then P1-01 cannot be demonstrated as written, and the discussion
  is about which adjustment is honest: an instrumentation/scenario change that
  produces an attributable Payment signal (for example capturing what the caller's
  own spans do show for the redirected attempt), or restating what the scenario
  demonstrates - never silently swapping the fault, which the handoff forbids.
- `no_traffic` / `no_observation`: the capture itself did not see the demo under
  load. That is a demo-state fact, not a Payment measurement.

This pass replaced that aggregation and vocabulary (see the next section). The
headline is now the strongest class observed while armed rather than the commonest
one, `no_traffic` split into `traffic_without_failure` and
`no_usable_observation`, and `no_attributable_failure` is no longer a class - it was
the name of a bucket, not of a reading.

## The payment fault-evidence pass (`723fe7b` measured) - the trail stops at the trace read, and the verdict reported the commonest reading

The corrections in this pass are committed as `56fab5a`; this section is its record.

### The user's measured run

Revision `723fe7b` (code tip `e6232c2`), diagnostic log
`.local/user-live-checks-20260925T005554/payment-fault-diagnostic.log`, capture
`reports/local/payment-fault-diagnostic-1790297762.json`, window 2026-09-25
00:56:03-01:05:48 UTC. Every measurement below is the **user's**, taken in a normal
WSL terminal. Nothing in this pass ran the stack, and the metrics capture was
deliberately **not** repeated.

- The baseline was healthy before the fault, `paymentUnreachable` was armed at
  00:56:03, and cleanup read back `off` with `restored: true` - the demo was left at
  its baseline.
- 30 polls: 12 `no_attributable_failure`, 9 `no_traffic`, 9
  `generic_caller_failure_only`; the stored `verdict` was `no_attributable_failure`.
- The pasted exit-status line was blank. That is **not** a measured exit code, and
  this pass claims none: the script version the user ran returned an integer and
  printed nothing after it, so the blank line is consistent with any status. The
  generated command was re-read and the script was then changed to record and print
  its status explicitly (below), so the next run's status is measured rather than
  inferred.
- The decisive poll is **00:58:04**: `PlaceOrder` read 0.0500 calls/s with an error
  share of **0.3333** *while* the attributed Charge series read 0.0334 calls/s on
  `checkout`'s client spans and 0.0167 calls/s on `payment`'s server spans, **both at
  error share 0.0**. The failures are real and simultaneous with successful Charge
  traffic; this is not the "no traffic" phase.
- The closing polls read `PlaceOrder` at error share **1.0** (0.0167 calls/s, every
  call failing) with the Charge client series at 0.0 calls/s - nine consecutive
  identical readings, which the span-metric export cadence explains: a 120s window
  that no new span has entered reports the same numbers for about three minutes.
- All six distinct `charge_traces` payloads the capture recorded had
  `error_spans: 0`, and every span in every one was `status: OK`.

### The source question, answered

The pinned checkout (local demo, tag `3.1.0`, commit `dedc0178`, image references
raised `3.0.0` -> `3.1.0` and `latest` -> `3.1.0` in `.env`; the flag document is
content-identical to upstream apart from a removed `$schema` key) answers the
mechanism question and does **not** explain the mismatch:

* `paymentUnreachable` is an `off`/`on` boolean, `defaultVariant off`, no targeting,
  evaluated with an empty context and a `false` default
  (`src/checkout/main.go:573`, `src/checkout/flags/flags_gen.go:73-91`,
  `src/checkout/flags.json`, `src/flagd/demo.flagd.json`);
* when it reads true, `chargeCard` does not skip the call - it swaps the client for
  one pointed at `badAddress:50051` and issues the **same** `Charge`
  (`src/checkout/main.go:573-589`);
* that client is built by `mustCreateClient`, a lazy `grpc.NewClient` with
  `otelgrpc.NewClientHandler()` (`src/checkout/main.go:473-483`; `grpc v1.83.2`,
  `otelgrpc v0.71.0`, Go 1.26.0).

**So the injected failure should appear in the selected Charge client series.** The
operation name, the emitting service and the client span kind are all unchanged by
the fault, and `otelgrpc` records RPC failure as span status on the client span. The
expected signal is a `oteldemo.PaymentService/Charge` **client** span from `checkout`
with `otel.status_code=ERROR`. It is not there, and the fact that successful Charge
traffic is measured *in the same window* is what makes this a real inconsistency
rather than a selector or window artefact. This is the same conclusion the `233003`
pass reached from metrics alone; it is now confirmed at source, including the parts
the previous pass had only inferred (the redirect rather than a skip, and the same
RPC on both paths).

Not source-confirmed here: whether `grpc.NewClient` against the unresolvable
`badAddress:50051` fails at construction or lazily on the first RPC. That is the one
mechanism question that would put the failure *before* the span, and it needs a Go
toolchain, which this sandbox does not have.

### What the traces show, and exactly what is unavailable

The capture's `read_traces` readings cannot settle it, and the reason is a defect in
our adapter rather than a property of the demo.

`LiveEnvironment._jaeger_traces` (`src/opsaccord/tools/live_environment.py:598-627`)
sends `service`, `operation`, `lookback` and `limit=20` and no `start`/`end`, so
Jaeger answers with its newest matching traces. The 30 polls produced **6 distinct
payloads**, 12 of them sharing one payload verbatim, and the payload carries no trace
ID - only `name`, `status` and `duration_ms` per span. Three consequences, all
measured or sourced rather than assumed:

1. a trace reading here is a statement about *recent* Charge traffic, never about the
   requested window, and the controller's plan asks for `window_seconds: 300`;
2. it cannot reconstruct an interval that has passed, so the 00:56-01:05 window
   cannot be re-read from the retained payloads;
3. Jaeger runs the in-memory store with `max_traces: ${env:MEMORY_MAX_TRACES}` and
   compose sets `MEMORY_MAX_TRACES=25000` (`src/jaeger/config.yml:43`,
   `compose.observability.yaml:29`), so the store is bounded rather than unbounded -
   but **which** trace left, and why, is not something this record shows. The
   in-memory backend is also documented by the demo's own telemetry tests as rotating
   ("the in-memory backend rotates traces and the aggregator output is unreliable
   within the warmup window", `test/telemetry/README.md`), and the `022150` capture
   measured a payload that changed only twice in 18 polls. None of that is a
   demonstration that an interval was *evicted*, and the sentence this pass replaced
   said it was. See "The trace-query pass (`6b7fe61` measured)" for the correction:
   the exporter now reports which causes it ruled out instead of naming one.

Jaeger itself is unreachable from this session - `AF_INET 127.0.0.1:9090` raises
`PermissionError [Errno 1] Operation not permitted` at socket construction, re-measured
in this pass - so no query was attempted here. Per the handoff's instruction for that
case, the bounded read-only exporter was built first:
`scripts/diagnose-payment-trace-path.py` takes an explicit `--start`/`--end` window (or
`--lookback-minutes`), reports the window it could actually see rather than the one it
was asked for, and classifies each retained trace as
`edge_span_present_and_failing`, `edge_span_absent_with_a_failure_recorded`,
`edge_span_absent_no_failure_recorded`, `edge_span_present_and_ok` or `no_root_span`,
with the failing span's sanitized attributes and error message. It issues HTTP GETs
only, arms no flag and runs no scenario.

*(This paragraph describes the exporter as the fault-evidence pass left it, including the
finding vocabulary of that commit. The recovered-trace analysis pass replaced the vocabulary:
an unqualified `edge_span_present_and_ok` no longer exists, because an OK edge span beside
failing spans elsewhere is now named as such, and the window asked for is reported beside an
observed range built from each span's own end.)*

**What is unavailable, precisely:** any trace-level evidence about the 00:56-01:05
window, because the capture's stored trace payloads are unbounded newest-N sets with
no trace IDs and the window's traces may already have been evicted by count. The
distinguishing evidence has to be collected while the fault is armed.

*(Corrected in "The trace-query pass (`6b7fe61` measured)": the payloads are unbounded
newest-N sets and had no trace IDs, which is exactly right - but the last clause named
eviction as the cause. It is one candidate, not a measurement, and the exporter no
longer claims it. The exporter also now records the window it *asked for* beside the range
it observed, rather than presenting one as the other.)*

### Three hypotheses, and the evidence that separates them

None is a conclusion, and no offline read available here separates them:

1. **The failing request never reached Charge.** Then `PlaceOrder` has a failing child
   span for a different dependency (or the failure is recorded on the parent itself),
   and the retained trace shows the edge span absent *with* a failure recorded. This
   is the hypothesis the exporter is built to confirm or refute.
2. **The Charge client span exists and fails, but without status.** Then the trace
   shows the edge span present with `STATUS_CODE_UNSET` and the error in
   attributes/events, and the metric series' zero error share is a status-recording
   artefact rather than an absence of attempts.
3. **The failing attempt produced no exported span at all** (for example a
   construction-time failure, or a dropped export). Then the edge span is absent
   *and* no failure is recorded anywhere in the trace, which is the one outcome that
   would make this demo's instrumentation genuinely insufficient for P1-01.

The fourth possibility in the handoff's list - that the Checkout errors concern another
dependency entirely - is hypothesis 1 with a specific child span named; the exporter
reports every failed span in the trace, so it distinguishes them.

### The reporting correction

The stored verdict came from `max(verdicts, key=verdicts.count)` over all polls. That
rule guarantees the wrong headline: the first polls of any capture read a window that
predates the injection (the export cadence makes that about three minutes), so the
least informative class wins by plurality. In this capture it did - 12 stale polls are
a plurality, and the old rule's headline was `traffic_without_failure`, which is the
"nothing failed here" class, while the *latest* reading was
`generic_caller_failure_only`.

The correction is in `src/opsaccord/tools/fault_evidence.py` (new) and is used by the
diagnostic: four disjoint classes, ordered by how much they license -
`attributable_failure` (the attributed Charge edge failing above the bound, and the
only class a Payment premise or recovery verdict may rest on),
`generic_caller_failure_only` (the caller failing without it - context, never
attribution), `traffic_without_failure` (traffic seen, nothing above the bound - not
health), `no_usable_observation` (no traffic, or an unreadable read - never health).
The headline is the **strongest** class observed while armed, with the counts, the
last reading and `attributable_observed` reported beside it, so no single poll is the
whole story and "attributable failure anywhere in the capture" is visible even when
it is not the most frequent reading. The four classes are also disjoint by
construction, which the old five names were not.

Two further reporting corrections, both asked for by the handoff:

* **recovery is now `assessed: false`.** The post-restore sample averages the window
  that contains the armed period, so a clean reading there is as consistent with the
  fault never having been in effect as with recovery. Cleanup success stays a separate
  fact (`restored`, the read-back text) and no reading in the capture is called
  recovery.
* **exit status is recorded and printed.** `0` capture completed and the flag was
  restored, `1` the capture failed, `3` it refused to start, `4` the flag could not be
  restored; the status is written into the capture and logged before returning, and the
  `--help` epilog documents all four, so the next run's status is measured.

### Tests, falsification, and what was executed

New coverage, with fixtures that model the measured readings rather than the adapter's
assumptions:

- `tests/unit/test_fault_evidence.py` (15 tests): the four classes; zero traffic and a
  below-bound share are both `no_usable_observation` / `traffic_without_failure` and
  never `attributable_failure`; an attributed edge read licenses attribution on its own
  but a generic parent-span failure never does; the **real 12/9/9 distribution** is
  replayed through both rules to pin that the commonest reading no longer wins; an
  empty capture and an unreadable read report no usable observation; and the trace
  summarizer's allow-list, redaction and cardinal-number stripping are pinned
  (including that a card number never survives sanitization).

**Falsification** (`reports/local/payment-verdict-falsification.py` and `.log`, both
untracked): the user's real capture is replayed through both rules. The recomputed
per-poll classes are identical to the stored ones (so the new vocabulary is a
re-wording and not a re-reading of the run), the old rule reports
`traffic_without_failure` while the new one reports `generic_caller_failure_only` with
counts `{attributable: 0, generic: 9, traffic: 12, no_usable: 9}` and
`last = generic_caller_failure_only`.

Executed in this session: offline `218 passed, 7 skipped, 85 deselected` in **both**
environment variants (15 more than the user's 203), `ruff check` clean,
`ruff format --check` (88 files) clean, `mypy src/opsaccord` (42 source files) clean,
the focused unit tests, and the new exporter's argument handling (`--help`),
its refusal to start without a Jaeger endpoint, and the falsification log above.
**Not run here:** the integration suite (its 82 errors are all connection failures at
psycopg/SQLAlchemy setup - `socket()` and `connect()` both raise
`PermissionError [Errno 1]` in this sandbox), the sensitivity harness, the simulated
scenarios, and any live path or Jaeger query. P1-01 has still never reached `RESOLVED`,
and Payment recovery has still never been demonstrated.

### The recommendation, and whether it fits existing scope

Two questions are separate, and only one of them is a scope decision:

1. **Our trace read mislabels its window - in scope, and recommended, but not
   implemented here on purpose.** `read_traces` reports an unbounded newest-N set as
   though it were `window_seconds`, and the controller consumes `error_spans` from it
   as a second independent signal for a critical service
   (`src/opsaccord/reasoning/deterministic.py:169`), so the mislabelling can both
   corroborate a fault that is not in the window and hide one that is. The minimal
   shape-independent fix is to pass an explicit `start`/`end` derived from the
   requested window and to report the window actually observed. It was not
   implemented in this pass because the *useful* shape of the fix depends on what the
   retained traces show: if the edge span is present with `STATUS_CODE_UNSET` then the
   read also needs to consider attributes/events rather than status alone, and if the
   attempt produces no span then windowing changes nothing and the honest fix is a
   stated retention/coverage limit instead. Implementing one of those three now would
   be a guess, which is what the two corrections above just stopped doing.
2. **Whether the pinned demo can carry P1-01 as written - a scope decision for
   discussion, not implemented.** If the retained traces show that a failing
   `PlaceOrder` has no Charge child span at all, then no selector, window or premise
   change can make the attributed Charge series the signal for this fault, and the
   honest options are an instrumentation change (record the redirected attempt where
   the controller can read it) or an explicitly restated scenario objective. The
   handoff forbids silently swapping the fault, and this pass did not: the pinned
   revision, the flag, the fault and P1-01's objective are unchanged, no budget, wait,
   retry or threshold was touched, and no failure was reported as health.

Because the capture's trace window cannot be reconstructed, the next user step
collects only the missing evidence: a shorter bounded re-capture of the fault, and the
Jaeger trace extract for that same window. It is **not** another ten minutes of the
same metrics.

## The trace-query pass (`6b7fe61` measured) - the request was well-formed, the read was not bounded, and the entry filter was an assumption

### The user's measured run

Log directory `.local/user-live-checks-20260925T022150/`, capture
`payment-fault-diagnostic.json`, trace extract
`reports/local/payment-trace-path-1790303263.json`, interval 02:21:59-02:27:43 UTC.
The diagnostic exited `0` and restored `paymentUnreachable` to `off` with a read-back.
Its 18 armed observations resolved to **6 `generic_caller_failure_only`, 6
`traffic_without_failure`, 6 `no_usable_observation`, 0 `attributable_failure`**, with
`verdict: generic_caller_failure_only`, `attributable_observed: false` and
`summary total 18`.

That is the previous pass's aggregation working live. Three of the four classes tied at
six readings each, and the *strongest* class won without a tie-break: the rule that
reported the commonest reading would have picked one of the three by dictionary order,
and by construction the least informative one. The capture is also the third live
measurement of the same underlying fact - checkout's `PlaceOrder` fails while the
attributed `Charge` edge reads a clean error share - so the Payment premise question is
unchanged by this pass and still needs a decision (see "Decisions for discussion").

The trace exporter exited `0` and returned **zero `PlaceOrder` traces**, and its log
suggested eviction. Two separate things are wrong there, and neither is settled by the
zero: one is our exporter's reasoning, the other is our adapter's contract.

### The request was well-formed; the entry filter was an assumption

Audited against the pinned stack, every mechanical part of the request checks out:

- **Endpoint and base path.** The exporter builds
  `http://127.0.0.1:51984/jaeger/ui/api/traces`, and the pinned Jaeger sets
  `jaeger_query.base_path: /jaeger/ui` (`src/jaeger/config.yml`), so the query API is
  the base path plus `/api/...`. The same run's `/jaeger/ui/api/services` answered with
  17 services, which the extract records - the path is not in question.
- **Service names.** `checkout` and `payment` are the store's own names, not registry
  aliases: both appear in that 17-service list and in the demo's own signal matrix.
- **Timestamps.** `start`/`end` are microseconds since the epoch, which is what the v1
  query API reads, and they are derived from the UTC interval on the command line.
- **Encoding and shape.** Parameters sorted, the `/` in the operation name
  percent-encoded as `%2F`, `limit` within bounds, one HTTP GET per stage, no
  credentials, nothing written.
- **Errors.** `/api/traces` used `raise_for_status`, so an HTTP error was a failure
  rather than an empty answer, and the run recorded `exit_status` either way.

The part that was *not* verified is the one that decided the answer:

- **`operation=oteldemo.CheckoutService/PlaceOrder` is an assumption.** No run has ever
  returned a span with that name. The pinned gateway collector rewrites every span name
  through `transform/sanitize_spans` -> `set_semconv_span_name("1.43.0",
  "original_span_name")` (`src/otel-collector/otelcol-config.yml:199-204`), so what
  Jaeger stores is the semantic-convention form, and the pinned revision's own registry
  already carries **two different** forms - `payment` is `oteldemo.PaymentService/Charge`
  while `cart` is `POST /oteldemo.CartService/EmptyCart` - because the form depends on
  the service's language and instrumentation. The demo's own telemetry tests never
  filter by operation for this reason: they ask *by service*, then walk the spans
  (`test/telemetry/test_traces.py`, `test/telemetry/test_traces_edges.py`).

So a zero under an operation filter cannot distinguish "the store holds nothing" from
"the filter does not match what the store holds". The exporter had no way to ask either
question, which is the defect this pass fixes.

### The read was not bounded, and the reported window was not the observed one

`LiveEnvironment.read_traces` computed `start, end = self._window(window_seconds)` and
reported those as the reading's `observation_window_start/end`, while the request it
actually sent carried `service`, `operation`, `lookback` and `limit=20` and **no
`start`/`end`**. `lookback` bounds the *search*; the reply is the newest matching
traces. The reading therefore described an interval it had never asked for, and
discarded the trace identity, the cap and any out-of-window span.

This is our adapter's own contract, and it matters because `DeterministicAdapter` reads
`error_spans` from this evidence as a second independent signal for a critical service
(`src/opsaccord/reasoning/deterministic.py`): a payload that is not scoped to the window
can both corroborate and hide.

The same capture measured the practical shape of it: the runtime `Charge` read returned
**only 2 distinct payloads across 18 polls** spanning about six minutes, 5 spans each,
every one `status: OK`, `error_spans: 0`. What that does *not* establish is why. With
the callee unreachable, checkout's client `Charge` span is exported but payment's server
span is never created, so a frozen payload is as consistent with the fault as with a
bounded store; the capture keeps no trace IDs and cannot tell the two apart. It is
**not** evidence of health either way, and the pass records it as unexplained.

### The corrections

1. **A shared `JaegerClient`** (`src/opsaccord/tools/live_environment.py`) with one
   honest contract for both callers: `traces()` always carries explicit microsecond
   bounds and takes the operation filter as *optional*, an HTTP or API error raises,
   and a reply without a `data` list raises instead of reading as "nothing found".
   `services()` and `operations()` complete the surface.
2. **`read_traces` honours its window.** It converts the same `_window(window_seconds)`
   it reports into `start`/`end` and asks for exactly that, then keeps the provenance
   the reading needs to be weighed: `service`, `trace_ids`, `traces_returned`,
   `trace_limit`, `truncated` (a full page is the server's cap, never a census) and
   `spans_outside_window` (bounds are requested, not guaranteed).
3. **The exporter measures instead of assuming.** It now runs a progressive read-only
   sequence: services, then the operations advertised *for the caller service*, then a
   bounded recent query **with no operation filter**, then the same query filtered by an
   operation a returned trace actually showed, then a **bounded retrieval check** that
   re-queries one returned trace inside its own timestamp window as a positive control,
   and only then the historical interval (plus an unfiltered replay of it when the
   filtered query is empty). Each stage records its requested window, observed range,
   count, trace IDs and cap, or its failure - never both.
4. **The eviction claim is gone.** An empty interval now reports which causes were ruled
   out and which remain open, and an `evidence` block separates command completion from
   evidence sufficiency, so a successful exit is never read as a sufficient answer. Exit
   codes are documented and now match behaviour: `0` every query completed, `1` at least
   one query failed to complete, `2` bad arguments, `3` refused to start (the documented
   `3` previously could not happen, because `parser.error` exits `2`).

Nothing about the fault, the flag, the budgets, the attribution criteria or the scenario
objective changed. The Payment scenario still requires the same attributable evidence,
and no selector, limit or wait was loosened.

### Tests, and what was executed here

24 new tests: 20 for the exporter (`tests/unit/test_trace_path_export.py`) and 4 for the
adapter contract (`tests/unit/test_live_adapter.py`), covering the explicit
microsecond bounds, the service/operation sent, advertised-and-observed operation
discovery, the bounded retrieval check, empty-versus-error, a capped page, out-of-window
spans, retained trace IDs and the refusal/argument exit codes. The fixtures are labelled
rather than guessed: request and reply shapes follow the query API the demo's own
telemetry tests consume plus the measured service list, while the operation names are
explicitly modelled, because no run has yet returned one from the live stack.

Executed in this session: **offline `242 passed, 7 skipped, 85 deselected`** in both
environment variants; `ruff check` clean; `ruff format --check` clean (89 files);
`mypy src/opsaccord` clean (42 files); and exit-code smoke checks (`3` with no endpoint,
`2` for `--limit 0`). Nothing here can reach the live stack - `AF_INET
127.0.0.1:51984` still raises `Operation not permitted` at socket construction, which
the new script now reports as four failed stages with `evidence.sufficiency:
not_sufficient` and exit `1`, instead of as an empty store. The integration suite needs
PostgreSQL and did not run.

### What the next read decides

The historical interval 02:21:59-02:27:43 has still not been read, and this pass does not
claim it was, nor that it was evicted. One read-only run answers the questions that are
actually open, in this order:

- whether traces are retrievable at all right now (the recent unfiltered query and the
  bounded retrieval check);
- **what Jaeger calls checkout's entry span** (the advertised operations, cross-checked
  against the operation names a returned trace shows) - which is what decides whether
  `LIVE_TARGETS["checkout-service"].operation` is right, and therefore whether the
  registry, the exporter or both need changing;
- whether the historical interval is still readable, and if not, whether that is a
  retention fact (a working query that cannot find that window) or an unproven query.

Payment recovery remains undemonstrated and P1-01 remains unverified on this revision.

## The recovered-trace analysis pass (`504fbd0` measured) - two traces were summarized by the wrong span and called sufficient

This pass was asked to reconstruct the two recovered traces from their raw spans. The saved
extract contains **no raw spans** - only the exporter's summaries - and this sandbox cannot
reach the Jaeger query API to fetch them (`AF_INET 127.0.0.1:51984` -> `[Errno 1] Operation
not permitted`, re-measured this pass). What follows is therefore what the *summary* plus the
pinned source and the exporter's own code establish, separated from what remains unknown, and
the code corrections the summary demonstrated.

Committed as `0a898b8` (the corrections above and their tests) and the records commit on top
of it, which is this section; `504fbd0` remains the revision the user's run measured.

### The user's measured run (attributed to the user)

Revision `504fbd0`; log directory `.local/user-live-checks-20260925T025435/`; extract
`reports/local/payment-trace-path-20260925T025436.json`; fault interval armed 02:21:59 and
restored 02:27:43 UTC. The read-only exporter exited `0`: 17 services visible, 8 operations
advertised for `checkout`, 40 recent unfiltered `checkout` traces, `oteldemo.CheckoutService/
PlaceOrder` confirmed inside a returned trace, the bounded retrieval check around
`c528d29a914bf9604512e4b4ce3fa21f` succeeding, and two historical traces retained for the
requested interval. Time unit check: the payload's `startTime` and `duration` are both
microseconds in the Jaeger v1 query API, and the run's own printed start instants
(02:24:41 and 02:24:50) match that reading of `started_at_us`, so the durations are
microseconds too - a 563873173 and an 826600400.

### What the summaries show, and what they do not

| Observation (reported by the run) | What it establishes |
| --- | --- |
| The span the exporter called the "root" is `oteldemo.CheckoutService/PlaceOrder` on **`frontend`** | Both `frontend` (client) and `checkout` (server) export that operation name; the exporter picked the longest span carrying the name, and the real root is above it. The `frontend` span's own parent is a span the run did not follow. |
| `checkout`'s server span appeared as the only "child" | Consistent with the same selection: children were computed for the frontend client span, so the trace was described from the caller's span instead of the measured attempt. |
| `child_of_root=False` for the Charge span | An artefact of that selection, not a statement about relatedness. A Charge client span is a direct child of the `checkout` server span, which is one hop below the frontend span. |
| Frontend durations 563873.2 ms and 826600.4 ms | Both exceed the 344 s armed interval by minutes, so a request that began at 02:24:41 cannot have been held by that fault for its whole duration: the spans must end after restoration, or the field is not the one the exporter assumed. Only the raw payload settles which. |
| Six and nine spans "reported failing", `failed: 0`, `ok: 2`, finding `edge_span_present_and_ok` | The failure count came from one span's direct children only. The traces are neither "successful" nor positioned against injection and restoration. |

Not established by these traces: whether Payment served a request while the fault was armed,
whether the Charge calls belong to the failing request branch, whether the two retained traces
are the failing requests at all, and anything about recovery. Two retained traces are not a
census of the interval, and a successful client span is not a served request - when the callee
is unreachable its own server span is never created.

### The claim this report retracts

The extract recorded `evidence.sufficiency: sufficient` with the reason "2 retained trace(s)
were summarized for the requested interval". That sentence is true about *retrieval* and was
read as a statement about *the subject*: the run had already summarised a trace whose own
failing-span list contradicted the word "successful" in its own log line. The earlier claim is
retracted here, and the field is now a ladder rather than a word (below).

### Confirmed defects corrected in this pass

1. **Root, attempt and ancestry were chosen by operation name and duration.** `summarize_trace`
   took every span whose `operationName` matched, kept the longest, called it the root, and
   reported a boolean for "is the edge its direct child". It now resolves the **root** as a span
   whose `CHILD_OF` parent is absent from the payload, the **attempt** as the measured
   operation's own `SPAN_KIND_SERVER` span, and the edge's **relationship** - `direct_child`,
   `descendant` at *n* hops, `ancestor`, `unrelated_branch`, or `missing_parent` - by walking
   parent references. Span and parent IDs are reported for every row, and a parent named but
   absent is recorded rather than silently treated as a root.
2. **A trace was called successful from one span.** `trace_failed` inspected the direct children
   of the chosen span, which is how "`failed: 0`, `ok: 2`" appeared beside nine failing spans.
   The counts are now `with_error_spans` / `without_error_spans` over the whole trace, and the
   finding vocabulary says when a successful edge span sits beside failures elsewhere.
3. **Status ignored every signal but `otel.status_code` and `error`.** A span whose framework
   wrote `rpc.grpc.status_code = 14`, an HTTP 5xx, an `error.type`/`exception.type` attribute or
   an exception event was reported `OK`. Status is now three-valued (`ERROR`/`OK`/`UNSET`) and
   carries the signal that decided it; `UNSET` is never a success. The runtime `read_traces`
   adapter had the same defect in its own `_span_failed` and now shares the classifier, keeping
   its requested-window contract and gaining `unset_status_spans` beside `error_spans`.
4. **The observed range mixed two spans' fields.** `trace_bounds` took the latest *start* and
   added the longest *duration* in the trace, which can name an instant at which nothing
   happened; the stage range is now `max(start + duration)` per span. Every reported span also
   carries its position relative to the window asked for - `before_window`, `starts_before_window`,
   `during_window`, `crosses_window_end`, `after_window`, `unpositionable` - so a call that ran
   past restoration is visible as that instead of as a duration to interpret.
5. **One word conflated five questions.** `evidence` now reports `query_completed`,
   `traces_retrieved`, `request_path_reconstructed` and a `sufficient_for` block for
   `request_path_review`, `dependency_attribution` and `recovery`, with the two a trace cannot
   reach fixed at `false` and their reasons stated. `sufficiency` is `sufficient_for_path_review`
   or `not_sufficient`; "sufficient" alone is no longer a value.

Nothing in the Payment fault, the scenario objective, the approval requirements, the budgets,
the retry limits or the recovery thresholds changed, and no selector, limit or wait was
loosened. The thresholds and scenario remain as `docs/phase1.md` defines them.

### Tests, falsification, and what was executed

New and updated coverage, using a reconstruction of the shape the extract reported rather than
captured raw spans (which are unavailable): frontend client and checkout server share one
operation name; the Charge call is a descendant rather than a child of the span chosen by name;
a successful branch beside failing spans elsewhere; a span crossing the restoration edge; an
`UNSET` status that is not a success; a gRPC status and an exception event as failure signals;
a named parent missing from the payload leaving the ancestry unresolved; a stage's observed end
built per span; and the evidence ladder never claiming attribution or recovery.

| Check | Exact command | Result |
| --- | --- | --- |
| Offline suite, operator shell stripped | `scripts/validate.sh --offline-only` stage 02 | **256 passed, 7 skipped, 85 deselected** |
| Offline suite, live settings force-exported | stage 03 | **256 passed, 7 skipped, 85 deselected** |
| Lint / format / types | stages 04-06 | `ruff check` clean; `ruff format --check` 89 files; `mypy src/opsaccord` clean, 42 files |
| Exit-code contract | `scripts/diagnose-payment-trace-path.py` without an endpoint, and with `--limit 0` | `3` refused to start; `2` bad arguments - unchanged |
| Failure path end to end | the same script against the blocked socket, endpoint file sourced | four failed stages, `query_completed=False`, `sufficiency: not_sufficient`, exit `1`; artifact `.local/sandbox-trace-path-check-20260925T040648/` - **a sandbox run, not a user run** |
| Integration suite | `pytest -q -m integration` | **not run** - no PostgreSQL in this sandbox |

### What the next read decides

One read-only request per trace ID, saving the sanitized spans, is what turns the table above
into the ancestry-and-timeline reconstruction the pass was asked for: which span is the root,
which is the attempt, which branch the Charge calls belong to, where each span's start and end
sit relative to 02:21:59 and 02:27:43, and which status signal each failing span actually
carried. No fault is injected, no flag is armed or cleared, and P1-01 is not re-run to obtain
it. Payment recovery remains undemonstrated and P1-01 remains unverified on this revision.

### Narrowing that read to the two named traces (`6ebbe48`)

That read has to be about those two ids and nothing else, because the previous page *was* one
interval's worth of traces and was read as though it were the answer. Two changes were made to
`scripts/diagnose-payment-trace-path.py` and the classifier it uses, and neither touches the
fault, the scenario, the budgets, the thresholds or the approval path.

- **`--trace-id` (repeatable) restricts the report to the named traces.** The interval is still
  queried with explicit microsecond bounds - the store is in memory and the query API has no
  id-only route this project uses, so a bounded window query is what "fetch this trace id" means
  here - but the historical query **drops the operation filter**, because a named trace is what
  the caller wants and an unverified operation name could only exclude it. The stage still
  records the whole page, so a narrowed report is never mistaken for a smaller page.
- **A named id the page did not hold is a reported miss, not an absence.** The three causes -
  rotation, an interval that does not cover the trace's start, and collector loss - are named as
  the run's own limit rather than resolved by it, and the run exits `1`, so a partial hand-off
  cannot read as a complete one. A run that answers every query but returns one of two named
  traces is not a successful run.
- **`summarize_trace` now returns the whole payload as a table, and the log prints it.** Each row
  carries both span ids, the service, operation and kind, both timestamps as UTC instants and raw
  microseconds, the duration, the status *and the signal that decided it*, the position in the
  window, and the relationship to the measured attempt. The named rows still answer the question;
  the table is what lets a reader disagree with the answer, because a retry, a sibling branch or
  the load generator's own span was otherwise absent from the extract entirely.

Checks executed on this tree: offline `262 passed, 7 skipped, 85 deselected` in both environment
variants, `ruff check` clean, `ruff format --check` (89 files) clean, `mypy src/opsaccord`
(42 files) clean - log directory `reports/local/validation-20260925T041844`. Six new tests cover
the filter, the missing-name report, the unfiltered historical query and the whole-payload table.
The new exit path was exercised end to end against this sandbox's blocked socket: four stages
failed, both named ids were listed as not returned, and the run exited `1`
(`.local/user-live-checks-20260925T041552/` - **a sandbox run, not a user run**). The integration
suite is still not executable here: `psycopg.OperationalError: connection is bad` against
`127.0.0.1:55432`, so it is reported as not run.

### The recovery read against the restarted stack (11:23 UTC) - the two traces are gone

The narrowed read has now run, four times within two minutes, from your terminal. The first
(11:22:31) used the endpoint file written at 02:54 and hit `[Errno 111] Connection refused` on
every Jaeger route - that file named port **51984**, and `scripts/demo.sh endpoints` reported
"the stack is not up", so the endpoint it carried was dead rather than the query being wrong.
The three runs after it (11:23:23, 11:23:33, 11:23:44) all answered, against a **new** port,
**65238**, written by `scripts/demo.sh endpoints` at 11:23:25.

That is a replaced Jaeger, not a rotated store. Between 11:23:23 and 11:23:44 the store
advertised **3, then 10, then 11 services** - a stack registering its services as it boots - and
at 11:23:44 `checkout` was not among them, with `0` operations advertised for it. Every stage
answered (`query_completed: true`, `failures: {}`) and every trace query returned `0`: no trace
in the 02:24:30-02:25:10 interval, and none in the whole two-hour recent window either. Jaeger's
in-memory store is volatile, so the instance that held `ce5e79a180be61a3c87ac161b474ef1d` and
`798301119dc69ac06ecc4704cf100bcc` no longer exists.

What that settles and what it costs:

- **The raw spans are permanently unavailable.** This was not retention inside a live store, so
  no read-only retry will bring them back, and the ancestry/timeline reconstruction of those two
  traces cannot be completed at all. The 563.9 s and 826.6 s spans' real end times are now
  unknowable; the report keeps them as reported values with unknown ends rather than guessing.
- **The miss was reported as a miss.** The 11:23:44 run exercised `--trace-id` live for the first
  time: two requested, none returned, both listed as not returned with the three causes named,
  and exit `1`. A run that had answered every query is precisely the run that would previously
  have been written up as "nothing was retained".
- **A capture now has to be fresh.** `checkout` was not yet registered in the store at 11:23:44
  and no `PlaceOrder` traffic had been exported, so a capture started then would have had no
  Payment path to observe. Any live answer to P1-01 has to wait for the restarted stack to
  register `checkout` and export `PlaceOrder` traffic, and then run with the fault armed.

Artifacts (this session's reading of your run, not a run by this session):
`.local/user-live-checks-20260925T112231/`, `.../112323/`, `.../112332/`, `.../112344/` and their
`reports/local/payment-trace-path-20260925T1122*.json` extracts.

**Follow-up, the readiness check at 11:26:54.** The same exporter, run read-only with no named ids
(`--lookback-minutes 15 --recent-minutes 15`), exited `0` with every stage answering and no checkpoint
to arm: 12 services visible, `checkout` **not among them**, `0` operations advertised for it, `0` recent
`checkout` traces, and `0` unfiltered `checkout` traces in the interval. Comparing the store's own list
with the 02:54 run's 17 services, exactly five are missing now: `checkout`, `email`, `quote`, `shipping`
and `flagd-ui` - and `checkout` is the one the other three hang off (they emit spans only when checkout
calls them). So the restarted stack is **not fully up**: no `checkout` span has reached the collector,
which means a capture taken now would have no Payment path to observe at all, whatever the fault state.
This is a stack-readiness question, not a trace question, and it is the precondition for anything live:
the trace half of P1-01 cannot be attempted until `checkout` registers in the store. Phase 1 remains
PARTIAL.

**Correction (baseline diagnosis pass): what the missing `checkout` spans do not prove, and the scope of
"gone".** Reading `checkout`'s absence from the 11:23 store as evidence that `checkout` was the root cause
was an overstatement: those reads landed while a **replacement Jaeger was still registering services**
(3, then 10, then 11 between 11:23:23 and 11:23:44, and five services still missing at 11:26:54), so the
store's coverage, not the service's health, was what changed. The distinction is the difference between
"the service has not exported yet" and "the service is faulty", and only the first is measured. Second,
the two traces are unrecoverable **from the current store** - the evidence supports a volatile in-memory
store on a replaced instance, and does not support the stronger claim that they exist nowhere. The
historical observations above are kept as they were made.

### The first live trace through the new classifier (11:28:33) - a corrected defect, and a confounded stack

The stack-recovery run found `checkout` registered: 13 services, **4 operations** advertised for it,
`oteldemo.CheckoutService/PlaceOrder` **observed in a returned trace** (so the registry's name is the
stored one, re-measured), the bounded retrieval check retrieving its anchor, and **one** trace in the
fifteen-minute interval - `290d12a9429910cab4800d4a44b79e43`, 11:26:51-11:27:56, **73 spans**. That is
the first live payload this pass's classifier has seen, and it did its job and showed one defect.

**Confirmed working on live data.** The trace root is resolved from references, not from the operation
name: it is the load generator's own `user_checkout_multi` span (`root candidates=1`), with the frontend's
identically named client span hanging below it. The status vocabulary read real signals the old code
discarded, including a `frontend-proxy/router frontend egress` span whose only failure marker is the
`error` tag. The whole payload is in the extract as a table (73 rows, `status counts {ERROR: 17, OK: 20,
UNSET: 36}`, every span positioned `during_window`), which is what made the defect below visible at all.

**The defect the live payload demonstrated (`09f8c56`).** The attempt search compared
`span.kind == "SPAN_KIND_SERVER"`, the OTLP enum name this repo's fixtures use. The live query API stores
the semantic-convention value, **`server`**, so no span matched, the search fell back to the longest span
carrying `oteldemo.CheckoutService/PlaceOrder` - the **frontend client** span - and reported it as the
measured attempt, demoting `checkout`'s server span to a child of it and placing the whole ancestry one
level too high (`attempt basis: the longest span carrying the measured operation below a parentless
span`). This is precisely the mis-description `0a898b8` set out to remove, in the spelling the live payload
uses; the fixtures could not have caught it. `span_kind_of()` now reduces both spellings to the word, and
the regression test is built from this trace's captured structure.

**What the trace also proves, and it is not about Payment.** This `PlaceOrder` failed **before** the
measured edge was reached - `edge_span_absent_with_a_failure_recorded`, no `Charge` span anywhere in the
73 - and the recorded reason is not payment: `checkout/oteldemo.ProductCatalogService/GetProduct` failed
with `error reading server preface: raw-read tcp 172.18.0.25:46450->172.18.0.20:3550: use of closed
network connection`, and the attempt records `failed to prepare order: failed to get product
#"L9ECAV7KIM"`. `scripts/demo.sh status` in the same run shows every container up except
`product-catalog`, which is **Up (unhealthy)**. So the current stack fails `PlaceOrder` for a reason that
has nothing to do with payment, and a Payment capture taken against it would measure the product catalog.
That is a stack defect to fix before any armed capture, not a trace-classification question, and it is
recorded here rather than worked around: no selector, limit, wait or scenario was changed.

Checks on this tree: offline `263 passed, 7 skipped, 85 deselected` in both variants, `ruff check`,
`ruff format --check` (89 files) and `mypy src/opsaccord` (42 files) clean - logs
`reports/local/validation-20260925T113048`. Reading of the live run: `.local/user-live-checks-20260925T112831/`
(`demo-status.log`, `trace-readiness.log`) and `reports/local/payment-trace-readiness-20260925T112831.json`.

**Corrections (baseline diagnosis pass).** `checkout.log` is empty in that run, and emptiness is not
evidence that the service was silent: the pinned revision sends the Go services' `slog` output over OTLP
and its `otel-config.yml` declares no stdout exporter, so their container logs are normally empty and the
collector's `debug` exporter holds what they emitted. Where an earlier note called this unexplained, the
cause is now measured from the pinned configuration. Separately, `query_completed: true` is not
application readiness - it says a read returned, `evidence sufficiency: not_sufficient` says the answer
cannot support a conclusion, and `docker ps` says what Docker's probe last saw. The three are different
claims and this report treats them as different.

## The baseline diagnosis pass (`5984cfc` code, this commit's records) - the catalog is the failing service, four records were corrected, and the diagnosis is bounded to one block

This pass did not inject a fault and did not run a scenario. It verified the retained evidence, corrected
four overstatements that evidence does not support, diagnosed the failing service from the pinned source,
fixed the demo helper's own defects, and wrote the single bounded evidence block that decides the repair.

**Whose measurements these are.** The starting evidence - the 11:28:31 `docker ps` dump, the 11:23-11:26
readiness reads, and the 73-span trace `290d12a9429910cab4800d4a44b79e43` - is the **user's own run in
their WSL terminal**; it is read here, not re-measured. Everything under "Checks executed in this pass" is
this session's, in a sandbox with no Docker engine and no network, and says so where it is not live
evidence. Nothing in this pass changed a fault, a budget, a selector, a wait or a scenario.

No repair was made, because the evidence needed to justify one has not been measured yet; the block is
what measures it, and it is deliberately repair-free.

### Corrections to the record (marked, not deleted)

1. **Missing Checkout traces did not prove Checkout was the root cause.** The 11:23 observations were
   taken while a **replacement Jaeger was booting** - the store advertised 3, then 10, then 11 services
   between 11:23:23 and 11:23:44, and five services were missing from the 02:54 list at 11:26:54. That
   is a store-registration fact about the instance, not a statement that `checkout` was faulty. The
   corrected reading is in the new note under "The recovery read against the restarted stack".
2. **The two historical traces are unrecoverable *from the current store*.** The evidence supports a
   volatile in-memory store on a replaced instance; it does not support "these traces exist nowhere".
3. **An empty log capture is not evidence that a service emitted no logs.** Measured cause, found this
   pass: the pinned revision routes the Go services' `slog` output over OTLP and `otel-config.yml`
   declares **no stdout exporter**, so their container logs are empty by design. `checkout.log` being
   0 bytes at 11:28:31 is therefore expected, and what those services emitted is in the **collector**,
   whose logs pipeline exports to the `debug` exporter. The E26 "Also odd" row and the vault copies of
   it now say this instead of "unexplained".
4. **Query completion is not application readiness.** New explicit rule, stated in the readiness section:
   `query_completed: true` says a read returned, `evidence sufficiency: not_sufficient` says what it
   returned cannot support a conclusion, and neither says the application is serving.
5. One claim is now qualified rather than repeated: "`scripts/demo.sh status` reports 25 containers with
   the stack healthy" (Environment arrangement, above) has **no retained artifact**. The only retained
   `docker ps` dump is 11:28:31, and it shows all containers up with **`product-catalog` unhealthy**, so
   the phrase is kept as a historical measurement and is not restated as a current property.

### What the pinned source says about `product-catalog`

Established by reading the checkout rather than by guessing, and separated by what each fact does and
does not prove:

| Fact | Source | What it supports |
| --- | --- | --- |
| `memory: 20M` with `GOMEMLIMIT=16MiB`, healthcheck `grpc_health_probe -addr=:3550`, `restart: unless-stopped` | `compose.yaml`, product-catalog | The identical limit pair the local override was written to fix for `checkout`, where it was measured as a Go GC loop (~700% CPU, 18.1MiB of 20MiB held, gRPC never accepting, 118 consecutive probe failures) |
| `initDatabase()` runs **before** `net.Listen`, and any DB error is `logger.Error` + `os.Exit(1)` | `src/product-catalog/main.go:130-134` | A database failure would exit the process and produce a restart loop, not a container that stays `Up (unhealthy)` for five minutes. `astronomy-db` is reported healthy |
| `triggerLockContentionLoop` ticks every 10s and only touches the database when the flag is on | `main.go:446-465` | No by-design busy loop; the ticker is inert with the flag off |
| `productCatalogFailure`'s targeting returns `"off"` for **every** product id, and all 18 flags default `off` | `src/flagd/demo.flagd.json` | The failure is not an armed flag, and that flag's targeting rule could not have armed it for the failing product either. It also fails pre-trace, inside the catalog's own flag client, which is consistent with the observed trace |
| `logger_provider` exports only to `otlp_grpc`; the collector's logs pipeline exports to `debug` | `otel-config.yml`, `otelcol-config.yaml` | Why the container log is empty and where the service's own log records actually are |

**The leading reading is therefore the same resource wedge the override already documents for `checkout`:**
a process that is alive and listening at the kernel level but never completes a gRPC handshake. The
retained trace is consistent with it - `error reading server preface ... use of closed network connection`
on `checkout -> product-catalog:3550` is a TCP connection that was accepted and then closed without a
server preface, which is not what a refused connection looks like.

It is recorded as the **leading reading, not a conclusion**, because one competing reading is not excluded
by anything measured so far: a startup or dependency delay that never clears. The measurement that decides
it is small and is in the block below:

- `restarts=0` and `oom_killed=false` with a health probe that reports "failed to connect ... within 1s"
  excludes an OOM loop and a crash loop;
- CPU near the core count with RSS held at the 20MiB limit over two samples is the documented signature
  of the GC loop, and would justify the same deviation for `product-catalog` that `checkout` already has;
- CPU near idle with RSS well below the limit would instead point at a dependency or listener problem,
  and would forbid the memory change - no selector, limit, wait or health check was changed this pass.

### The demo helper's defects, fixed

`scripts/demo.sh logs` could not be trusted to say what it appeared to say:

- `compose logs --tail N "${@:-}"` expands to **one empty service name** when no service is named, which
  compose rejects, so the documented bare `demo.sh logs` could not have worked;
- the same call inside a caller's `$( ... )` hides its exit status, so an empty capture and a failed
  capture were indistinguishable - and an empty capture is *not* silence (see correction 3);
- it is a snapshot, not a follow: no `-f` is passed and none is wanted, but that was undocumented.

Also fixed or added, each because this diagnosis needs it: `inspect` (per-container health, restart
count, exit code, OOM flag, configured limits, effective `GOMEMLIMIT`, and the probe's own output for a
container that is not healthy - `docker ps` shows none of those); `stats` (two CPU/memory samples, because
a single non-streaming sample can report 0.00% for a spinning process); `logs --since` as an opt-in window;
`PRODUCT_CATALOG` added to the health probe surface, which did not cover the failing service at all; and
`endpoints` now repeats the raw `compose port` call when resolution fails, so it reports the real cause
instead of always blaming a stopped stack. Nothing in the fault, budget, verification or scenario code
changed.

### Checks executed in this pass

- Focused tests `tests/unit/test_demo_script.py` (new, 8 tests) and `tests/unit/test_config_isolation.py`:
  **16 passed**. The new tests run the script against a stub docker binary; they pin the empty-argument
  bug, status propagation, the opt-in window, the two samples, the health-probe surface, and that a failed
  endpoint resolution names its cause.
- Offline suite `-m "not integration"`: **271 passed, 7 skipped, 85 deselected** in both environment
  variants (`263` before this pass's 8 new tests); `ruff check` clean, `ruff format --check` (90 files)
  clean, `mypy src/opsaccord` (42 files) clean, `bash -n scripts/demo.sh` clean - logs
  `reports/local/validation-20260925T114111` and `.../validation-20260925T114254`.
- The block itself: syntax-checked, then executed here twice - once with no reachable engine, where it
  stops at the endpoint refresh with the real `compose port` error, and once against a stub docker binary,
  where every step runs, each command reports its own status, and a failing flag read and trace leave the
  run intact (`reports/local/validation-20260925T114254/09-block-stop-path.log` and `.../10-block-full-stub-run.log`).
  These are **sandbox structure checks, not live evidence**.
- Not run, and not reported as results: any live scenario, the integration suite (no PostgreSQL/Docker in
  this session), and any measurement of `product-catalog` - the block below is the only thing that can
  produce that.

### The one next action (superseded: this block ran on 2026-09-25 11:46:57, and its readings are
recorded - with four corrections and a replacement block - in "The restart pass" at the end of this
report)

Paste the block below from the host. It refreshes the endpoints, moves a stale endpoints file aside
instead of trusting it, and collects in one run: per-container state (health, restarts, exit, OOM, limits,
effective `GOMEMLIMIT`), the health probe surface including `PRODUCT_CATALOG`, two CPU/memory samples,
bounded logs for the catalog, checkout and the collector, the live fault flags, and the current trace
evidence. It performs **no repair**: the reading above decides between the memory deviation and a
dependency investigation, and that decision needs these numbers first.

```bash
( set -euo pipefail
  cd /home/brentiburo/projects/OpsAccord
  if declare -F deactivate >/dev/null; then deactivate; fi
  source .venv/bin/activate
  export UV_PROJECT_ENVIRONMENT="$PWD/.venv"
  PYTHON="$PWD/.venv/bin/python"
  printf 'interpreter: %s\n' "$("$PYTHON" -c 'import sys; print(sys.executable)')"
  printf 'tested revision: %s on %s\n' "$(git rev-parse --short HEAD)" "$(git rev-parse --abbrev-ref HEAD)"

  TS="$(date -u +%Y%m%dT%H%M%S)"; LOGDIR=".local/user-catalog-diagnostics-$TS"; mkdir -p "$LOGDIR"
  printf 'evidence dir: %s\n' "$LOGDIR"

  run() { local name="$1"; shift
    set +e; { "$@"; } 2>&1 | tee "$LOGDIR/$name.log"; rc="${PIPESTATUS[0]}"; set -e
    printf '==== %s exited %s (log %s) ====\n' "$name" "$rc" "$LOGDIR/$name.log"; }

  # A stale endpoints file names ports that a replaced stack owned, and querying
  # those answers "connection refused" - which reads like a fault in the stack
  # rather than a stale file. It is moved aside, never deleted, and only a file
  # resolved by this run is used below.
  if [ -f .local/demo-endpoints.env ]; then mv .local/demo-endpoints.env ".local/demo-endpoints.env.pre-$TS"; fi
  run endpoints bash scripts/demo.sh endpoints
  if [ ! -f .local/demo-endpoints.env ]; then
    printf '\nSTOP: scripts/demo.sh endpoints could not resolve the stack; nothing further was run.\n'
    exit 1
  fi
  # The file is generated by the command above, so a wrong shape means something
  # other than a port lookup went wrong; sourcing it would then run the rest of the
  # block against nonsense or abort it with a confusing shell error.
  if [ "$(grep -c '^OPSACCORD_[A-Z_]*=' .local/demo-endpoints.env)" -ne 4 ]; then
    printf '\nSTOP: the refreshed endpoints file does not hold exactly the four expected settings; not sourcing it.\n'
    exit 1
  fi
  set -a; . .local/demo-endpoints.env; set +a

  run containers bash scripts/demo.sh inspect
  run health bash scripts/demo.sh health
  run stats bash scripts/demo.sh stats
  export OPSACCORD_DEMO_LOG_SINCE=20m
  run logs-catalog bash scripts/demo.sh logs product-catalog
  run logs-checkout bash scripts/demo.sh logs checkout
  run logs-collector bash scripts/demo.sh logs otel-collector
  unset OPSACCORD_DEMO_LOG_SINCE

  set +e
  curl -fsS --max-time 10 "$OPSACCORD_FLAG_CONTROL_BASE_URL/api/read" >"$LOGDIR/flags.json" 2>"$LOGDIR/flags.err"
  printf '==== flag read exited %s ====\n' "$?"
  set -e
  "$PYTHON" - "$LOGDIR/flags.json" <<'PY'
import json, sys
try:
    flags = json.load(open(sys.argv[1]))["flags"]
except Exception as exc:
    print(f"flags document unreadable: {exc}")
    raise SystemExit(0)
for name in sorted(flags):
    print(f"{name}: defaultVariant={flags[name].get('defaultVariant')}")
PY

  run trace "$PYTHON" scripts/diagnose-payment-trace-path.py --lookback-minutes 15 --recent-minutes 15 --out "reports/local/payment-trace-readiness-$TS.json"

  printf '\nDone. Evidence: %s/\n' "$LOGDIR"
  printf 'Also: reports/local/payment-trace-readiness-%s.json\n' "$TS"
  printf 'Send back: the product-catalog line from containers, the PRODUCT_CATALOG line from health,\n'
  printf 'the two stats samples, and logs-catalog.\n'
)
```

## Remaining work and discussion

**Blockers (environment, not code):**

- **Push and draft PR are blocked.** Re-attempted in the deadlock-correction pass
  (`git push -u origin phase1/baseline`, exit `128`), and still blocked for two
  independent reasons: the push fails
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
  concurrency, recovery, scenario and live checks outstanding. Re-measured in the
  configuration-isolation pass: `socket.socket(AF_INET)` raises
  `PermissionError [Errno 1]` at construction, and `AF_UNIX` `bind()`/`connect()`
  raise the same, so no local cluster can be reached by any client. The escalation
  route was attempted once for the integration suite in that pass, as the handoff
  directs, and was rejected before the command ran by the same
  `codex-auto-review` misconfiguration; it was not retried.
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
- Whether `tests/integration/test_live_demo.py` should carry the `integration` mark. It is opt-in
  through `OPSACCORD_LIVE_DEMO=1` and deliberately outside the integration mark. Measured: its seven
  tests are collected by `-m "not integration"` either way (104 collected, 78 deselected), so that
  variable alone decides whether they skip or execute - which is why the runner has to remove it.
  Adding the mark would move them out of the offline selection at the source, but it would also grow
  the integration suite from 78 tests to 85 collected, with seven skips when the opt-in is unset, and
  that suite has just been validated at 78.
- Whether the corrected control-ordering test should keep its `pg_locks` observation
  now that it is the only thing that proves the writer contended for the gate. It reads
  a system view (`pg_locks` is world-readable, so no grant is needed) and it is what
  turns a possibly-vacuous wait into a check that fails loudly. It is also the only part
  of this pass that has never executed, so if it misbehaves on your terminal the honest
  fallback is to drop the observation and accept the weaker wait - which the independent
  review already flagged as able to pass without contention.
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
- ~~**Whether to fix `read_traces`' window** by passing an explicit `start`/`end`
  derived from the requested `window_seconds` and reporting the window actually
  observed.~~ **Done in "The trace-query pass (`6b7fe61` measured)"** at `a686617`: the
  read carries explicit microsecond bounds and reports the window it asked for, and the
  reading now keeps the trace IDs, the page cap and any span outside the bounds as
  provenance. The second half of the question stands and is what the same pass made
  measurable: if the window holds nothing, the exporter says which causes it ruled out
  instead of naming one.
- **Whether the pinned demo can carry P1-01 as written**, if the traces show a
  failing `PlaceOrder` with no Charge child span: an instrumentation change that
  makes the redirected attempt visible, or an explicitly restated scenario
  objective. Deliberately not implemented - the fault, the flag, the budgets and the
  scenario objective are unchanged, and no failure was reported as health.

**One command block for the next run.** It is **read-only**: it starts nothing, arms
nothing, changes no flag and generates no traffic. It refreshes the resolved endpoint file
from the stack that is already running (`scripts/demo.sh endpoints` only reads published
ports; it refuses if the stack is down rather than starting it), then runs the trace
exporter once. A single invocation now performs the whole progressive sequence - services,
the operations advertised for checkout, a bounded recent query with no operation filter, a
query filtered by an operation a returned trace showed, a bounded retrieval check, and the
historical interval last - so it answers "can we read traces at all", "what is this span
called" and "is the 02:21:59-02:27:43 interval still there" together, without arming the
fault again. It runs in a subshell so the operator's interactive shell is neither closed by
a failing stage nor left in another directory, activates the project's own environment
explicitly, prints the interpreter and the revision it is testing, keeps the live settings
inside the subprocess, writes timestamped logs, preserves the failure status through `tee`,
and stops on the first failure.

```bash
(
  set -euo pipefail
  cd /home/brentiburo/projects/OpsAccord
  deactivate 2>/dev/null || true
  source .venv/bin/activate
  export UV_PROJECT_ENVIRONMENT="$PWD/.venv"
  printf 'interpreter: %s\n' "$(python -c 'import sys; print(sys.executable)')"
  printf 'revision: %s\n' "$(git rev-parse --short HEAD)"
  DIRTY="$(git status --porcelain)"
  if [ -n "$DIRTY" ]; then printf 'working tree:\n%s\n' "$DIRTY"; else printf 'working tree: clean\n'; fi
  LOGDIR=".local/user-live-checks-$(date -u +%Y%m%dT%H%M%S)"
  mkdir -p "$LOGDIR"
  printf 'log directory: %s\n' "$LOGDIR"
  bash scripts/demo.sh endpoints >>"$LOGDIR/endpoints.log" 2>&1 \
    || printf 'warning: could not refresh .local/demo-endpoints.env; see %s/endpoints.log\n' "$LOGDIR"
  test -f .local/demo-endpoints.env || { printf 'no .local/demo-endpoints.env; run: bash scripts/demo.sh endpoints\n'; exit 3; }
  set -a
  . .local/demo-endpoints.env
  set +a
  printf 'jaeger: %s\n' "$OPSACCORD_JAEGER_BASE_URL"
  TS="$(date -u +%Y%m%dT%H%M%S)"
  rc=0
  python scripts/diagnose-payment-trace-path.py \
    --recent-minutes 60 \
    --start 2026-09-25T02:21:59Z \
    --end 2026-09-25T02:27:43Z \
    --out "reports/local/payment-trace-path-$TS.json" \
    2>&1 | tee "$LOGDIR/payment-trace-path.log" || rc=$?
  printf 'trace-path exit status: %s\n' "$rc"
  printf 'extract: reports/local/payment-trace-path-%s.json\n' "$TS"
  printf 'log directory: %s\n' "$LOGDIR"
  exit "$rc"
)
```

Expected, in `$LOGDIR/payment-trace-path.log` and the extract:

- one line per stage recording what was asked for and what answered: services visible,
  the operations **advertised for `checkout`** (with whether the expected name is among
  them and which names contain `PlaceOrder`), the recent unfiltered query with the
  operation names a returned trace actually shows, the chosen operation and the basis for
  that choice, the bounded retrieval check with its `retrieved:` verdict, and the
  historical interval filtered and (only if empty) unfiltered.
- the extract under `stages.*` keeps each stage's `requested_window`, `observed_start`/
  `observed_end`, `count`, `trace_ids` and `truncated`, and `failures` holds any stage
  that did not complete - an unanswered query is never recorded as a zero-count answer.
- `evidence.sufficiency` says whether the run produced something usable, separately from
  the exit status: `0` means every query completed, **not** that the evidence was
  sufficient. If the interval is empty, `evidence.reason` names which causes were ruled
  out - and if the bounded retrieval check succeeded, that is the one case where the
  answer may be read as "the store no longer holds this interval", without claiming *why*.
- the decision this run settles: the advertised/observed operation names decide whether
  `LIVE_TARGETS["checkout-service"].operation` is correct. If it is not, the registry and
  the exporter are both wrong in the same way and that is a code change; if it is, the
  historical zero is a retention question and the fault's trace evidence has to be
  collected while it is armed.

*(The capture-and-extract pairing that stood here belonged to the fault-injection run.
This pass does not inject another fault, so the read-only block above replaces it; the
previous run's own expectations and exit codes are recorded with it in "The payment
fault-evidence pass (`723fe7b` measured)".)*

`scripts/validate.sh` remains the full gate (it starts and verifies PostgreSQL, runs the
offline suite, linter, formatter, `mypy`, the integration suite, the sensitivity harness
and the simulated scenarios, refuses to run if another live driver holds the demo flags,
and only then runs the live paths). It is not part of this next step: the trace evidence
answers the open question first, and running the whole gate would take the demo for
~20 minutes to learn the same thing.

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

---

## The restart pass: what the 2026-09-25 measurement settled (code `86735c1`)

Appended 2026-09-26 on top of everything above. The earlier sections are left as they were and
the corrections below are marked rather than written over.

### The measurement the previous block asked for

The user ran the read-only block from "The one next action" in their own terminal at **11:46:57 UTC
on 2026-09-25**, after the state the previous section describes. Evidence:
`.local/user-catalog-diagnostics-20260925T114656/`, with the trace extract at
`reports/local/payment-trace-readiness-20260925T114656.json`. Nothing here was produced by this
session; it is the user's measurement, read and recorded.

| Reading | What it measured | Source |
| --- | --- | --- |
| Every published port, the catalog's included | `ok` for all 14 endpoints, `PRODUCT_CATALOG product-catalog:3550 -> 65235`, plus `prometheus /-/healthy`, `flagd-ui /api/read`, `jaeger /jaeger/ui/api/services` | `health.log` |
| `product-catalog` CPU and resident set | **159.76% then 378.74% CPU; 11.3 MiB then 5.668 MiB of a 20 MiB cap** (56.50%, 28.34%); 28 then 33 pids. `checkout` 0.01% / 22.45 MiB of 256 MiB, `payment` 0.23% / 98.45 MiB of 140 MiB | `stats.log` |
| The catalog's own output | ~30 consecutive export timeouts, 11:27:58 to 11:46:49, roughly every 30-60 s, never recovering: `exporter export timeout: rpc error: code = DeadlineExceeded desc = context deadline exceeded`, `processor export timeout`, `failed to upload metrics: context deadline exceeded`, `traces export: processor export timeout` | `logs-catalog.log` |
| The collector | Up and exporting - the `debug` exporter logs `resource spans: 1, spans: 9` at 11:47:12. Its only recurring error is the `kafkametrics` receiver failing to resolve host `kafka` | `logs-collector.log` |
| Per-container state | **Not collected at all**, in 144 bytes: `demo: listing project containers exited 0`, `template parsing error: template: :1: function "trimPrefix" not defined`, `demo: inspect checkout failed` | `containers.log` |
| Retained traces | 7 traces for `checkout`, `query_completed: true`, `request_path_reconstructed: true`, exit 0, `sufficiency: sufficient_for_path_review`, 18 services visible, 123 spans in the first trace `{ERROR: 15, OK: 43, UNSET: 65}` | trace extract |
| The Payment edge | **Present and serving** in 6 of the 7: `payment / oteldemo.PaymentService/Charge / server / status OK` at 1.1-55.8 ms beside `checkout / same operation / client / status UNSET` at 2.5-90.0 ms. In the seventh no edge span was recorded and the attempt records a failure (`edge_span_absent_with_a_failure_recorded`) | trace extract |
| `GetProduct` (checkout -> catalog) | client spans of **37.2 s to 238.9 s**, 14 samples, no error status | trace extract |
| `PlaceOrder` | 144.7 s to 482.5 s across the 7 attempts | trace extract |
| Fault flags | all 18 `off`, including `paymentUnreachable`, `paymentFailure`, `productCatalogFailure`, `productCatalogLockContention` | `flags.json` |

### Four corrections this measurement forces

1. **The "resource wedge" reading for the catalog is not supported.** The previous section's leading
   reading was that `product-catalog` carries the same memory wedge the local override documents for
   `checkout`. The measurement says otherwise: the catalog's resident set is **28-56% of its cap**, not
   pinned at it. `D27` fixed this decider in advance - "RSS well below the limit ... **no** memory
   change" - so **no memory deviation is applied to the catalog**, and the same reasoning forbids
   reaching for one. The catalog's CPU is *not* idle, so the second row of that table did not match
   cleanly either, and `D27`'s table is now recorded as having been incomplete rather than quietly
   reinterpreted: the measurement matched neither row exactly.
2. **The catalog is no longer what blocks the Payment path on this revision.** The previous section
   records that `PlaceOrder` failed before the payment edge and that no `Charge` span existed. That was
   true of the one trace then retained. In these 7, the `Charge` edge is present in 6 with the payment
   server span recording `OK`. Recovery is still unverified (a retained span cannot show a service is
   serving now), but "no Charge span exists" is no longer a property of the current stack.
3. **`demo.sh health` proves reachability, not serving.** It printed `ok PRODUCT_CATALOG` while Docker
   reported the same container `unhealthy`. Both are correct: the probe is a TCP connect, which
   succeeds as soon as the listener exists. The surface must not be read as a health verdict, and the
   catalog's `Up (unhealthy)` is the stronger claim.
4. **The collector's `kafka` errors are not a fault to repair.** `kafkametrics` fails to resolve
   `kafka:9092` because that service belongs to `compose.full.yaml`, which this stack does not start,
   and `compose.observability.yaml` documents the consequence in its own words: the Kafka and
   PostgreSQL receivers "are harmless - they will simply fail to scrape since those services are not
   running". Restarting or reconfiguring the collector to silence them would be a change made to
   remove noise.

### What the numbers do point at, and what was changed

The catalog is **CPU-busy with a small heap** while its OTLP export to `otel-collector:4317` times out
continuously, its gRPC health probe never answers, and `GetProduct` takes 37-239 s. That is not the
ceiling signature; it is a process that is doing something expensive and not answering. The
measurement that separates *starved* from *blocked* is the runtime's own (`go_`/`process_` metrics)
plus the health probe's own text - and the second was **lost by a defect in the helper**, so this pass
fixed the helper before asking for another run:

* `cmd_inspect` opened with `{{trimPrefix "/" .Name}}`, and the installed Docker CLI answered
  `function "trimPrefix" not defined` for every container. The whole per-container reading - health,
  restarts, exit and OOM state, the limits, the effective `GOMEMLIMIT` and the probe's message - was
  lost while the step still reported exit 0. The slash is now removed in bash, and a test reads the
  script's own `--format` templates and fails on any template function outside a listed set. The stub
  docker binary cannot evaluate a Go template, which is exactly how this reached a live run.
* `scripts/diagnose-service-runtime.py` reads the Go runtime metrics the demo publishes to Prometheus
  for named services, discovery-driven so it does not depend on which instrumentation shape a service
  emits, and reports a metric it cannot find as absent rather than as zero.
* `demo.sh restart [services...]` was added: `compose restart` keeps each container and its state, so a
  wedged service can be given a fresh process without destroying the restart count and probe history a
  diagnosis reads. `stop` removes the containers and is therefore *not* the primitive for this.
* `docs/development.md` named `OPSACCORD_DEMO_CHECKOUT`, which no script reads (the helper reads
  `OPSACCORD_DEMO_DIR`); a test now fails if a documented `OPSACCORD_*` setting exists nowhere.

### Executed in this session, and what was not

| Check | Result | Log |
| --- | --- | --- |
| `pytest -q -m "not integration"`, scrubbed environment | **286 passed, 7 skipped, 85 deselected** | `reports/local/validation-20260926T003439/06-offline.log` |
| the same with live settings exported (stale ports) | **286 passed, 7 skipped, 85 deselected** | `.../07-offline-live-settings.log` |
| `ruff check`, `ruff format --check` (92 files), `mypy src/opsaccord` (42 files), `bash -n` | clean | `01`-`04` in the same directory |
| the block, against a stub engine | full flow runs; per-step statuses reported; a failing step is named and the block exits 1 | `.../08-block-full-stub-run.log` |
| the block, with no engine | stops before touching anything | `.../09-block-stop-path.log` |
| **Not run** | no live scenario, no integration suite, no PostgreSQL (none in this session) - and therefore **no P1-01 outcome and no recovery measurement** | - |

The stub runs are structure checks, not live evidence. The catalog is still `Up (unhealthy)` until a
run measures it, and Payment recovery remains unverified on this revision: Phase 1 stays **PARTIAL**.

### The one next action

Paste the block below from the host. It starts what is missing, records the state **as found**, then
restarts the two services whose state the 2026-09-25 capture put in question and records the state
again, so "a fresh process clears it" and "a fresh process fails the same way" are distinguishable
from one directory. It names every step's exit status, stops on a failed prerequisite, and exits
non-zero if any step failed.

```bash
( set -euo pipefail
  cd /home/brentiburo/projects/OpsAccord
  deactivate 2>/dev/null || true
  source .venv/bin/activate
  export UV_PROJECT_ENVIRONMENT="$PWD/.venv"
  PYTHON="$PWD/.venv/bin/python"
  DC="${OPSACCORD_DOCKER_BIN:-/mnt/c/Users/admin/AppData/Local/Programs/DockerDesktop/resources/bin/docker.exe}"
  printf 'interpreter: %s\n' "$("$PYTHON" -c 'import sys; print(sys.executable)')"
  printf 'tested revision: %s on %s\n' "$(git rev-parse --short HEAD)" "$(git rev-parse --abbrev-ref HEAD)"

  TS="$(date -u +%Y%m%dT%H%M%S)"; LOGDIR=".local/user-next-action-$TS"; mkdir -p "$LOGDIR"
  printf 'evidence dir: %s\n' "$LOGDIR"

  failed=()
  run() { local name="$1"; shift
    set +e; { "$@"; } 2>&1 | tee "$LOGDIR/$name.log"; rc="${PIPESTATUS[0]}"; set -e
    printf '==== %s exited %s (log %s) ====\n' "$name" "$rc" "$LOGDIR/$name.log"
    [ "$rc" -eq 0 ] || failed+=("$name"); }

  # 1. The engine is the one prerequisite this block cannot start for you.
  if ! "$DC" version --format '{{.Server.Version}}' >"$LOGDIR/docker-version.log" 2>&1; then
    printf '\nSTOP: the Docker engine did not answer. Start Docker Desktop, wait until it settles,\n'
    printf 'then run this block again. Nothing was started and nothing was changed.\n'
    exit 1
  fi
  printf 'docker engine: %s\n' "$(cat "$LOGDIR/docker-version.log")"

  # 2. PostgreSQL is project-local (no root, no Docker) and idempotent. The catalog
  #    still needs it for the next action even though it is not the current fault.
  run postgres bash scripts/pg.sh start

  # 3. Bring the pinned stack up. Idempotent: a container that is already running is
  #    left alone. It can print nothing for several minutes while it waits for health.
  printf 'starting the pinned demo stack (waits up to 900s for health; quiet until it ends)...\n'
  run stack bash scripts/demo.sh start

  # 4. Refresh the endpoints from the stack that is up now. A stale file names ports a
  #    replaced stack owned, and querying those answers "connection refused", which
  #    reads like a stack fault. It is moved aside, never deleted, and only a file
  #    resolved by this run is used. Sourcing it here sets these variables in this
  #    subshell only; they do not outlive the block.
  if [ -f .local/demo-endpoints.env ]; then mv .local/demo-endpoints.env ".local/demo-endpoints.env.pre-$TS"; fi
  run endpoints bash scripts/demo.sh endpoints
  if [ ! -f .local/demo-endpoints.env ]; then
    printf '\nSTOP: scripts/demo.sh endpoints could not resolve the stack; nothing further was run.\n'; exit 1
  fi
  if [ "$(grep -c '^OPSACCORD_[A-Z_]*=' .local/demo-endpoints.env)" -ne 4 ]; then
    printf '\nSTOP: the refreshed endpoints file does not hold exactly the four expected settings.\n'; exit 1
  fi
  set -a; . .local/demo-endpoints.env; set +a

  # 5. State as found, before anything is restarted. This is what separates "wedged
  #    since it started" from "a fresh process fails the same way" below.
  run containers-before bash scripts/demo.sh inspect
  run stats-before bash scripts/demo.sh stats
  export OPSACCORD_DEMO_LOG_SINCE=20m
  run logs-catalog-before bash scripts/demo.sh logs product-catalog
  unset OPSACCORD_DEMO_LOG_SINCE
  run runtime-before "$PYTHON" scripts/diagnose-service-runtime.py \
      --services product-catalog checkout payment \
      --out "reports/local/service-runtime-before-$TS.json"

  # 6. The restart the user asked for, and the smallest reversible version of it:
  #    `compose restart` keeps each container, its configuration and its state, and
  #    touches no volume and no image, so it cannot lose evidence - unlike `stop`,
  #    which removes the containers. It is applied to the two services whose state
  #    the 2026-09-25 capture put in question: the catalog (unhealthy, and slow in
  #    the retained traces) and the collector (its receiver, and the catalog's
  #    export target). Restarting a healthy container is harmless; both are named.
  run restart bash scripts/demo.sh restart product-catalog otel-collector
  run wait bash env OPSACCORD_DEMO_WAIT_SECONDS=300 bash scripts/demo.sh wait

  # 7. State after the restart, the same readings as step 5 plus the health surface,
  #    so "did a fresh process clear it" is answerable from one directory.
  run containers-after bash scripts/demo.sh inspect
  run health-after bash scripts/demo.sh health
  run stats-after bash scripts/demo.sh stats
  export OPSACCORD_DEMO_LOG_SINCE=5m
  run logs-catalog-after bash scripts/demo.sh logs product-catalog
  unset OPSACCORD_DEMO_LOG_SINCE
  run runtime-after "$PYTHON" scripts/diagnose-service-runtime.py \
      --services product-catalog checkout payment \
      --out "reports/local/service-runtime-after-$TS.json"

  # 8. What the fault flags say, so no reading above is attributed to an armed fault.
  set +e
  curl -fsS --max-time 10 "$OPSACCORD_FLAG_CONTROL_BASE_URL/api/read" >"$LOGDIR/flags.json" 2>"$LOGDIR/flags.err"
  flags_rc=$?
  set -e
  printf '==== flag read exited %s ====\n' "$flags_rc"
  set +e
  "$PYTHON" - "$LOGDIR/flags.json" <<'PY'
import json, sys
try:
    flags = json.load(open(sys.argv[1]))["flags"]
except Exception as exc:
    print(f"flags document unreadable: {exc}")
    raise SystemExit(0)
armed = {n: f.get("defaultVariant") for n, f in flags.items() if f.get("defaultVariant") not in (None, "off")}
print(f"{len(flags)} flags read; not off: {armed or '<none>'}")
PY
  set -e

  # 9. The retained traces, to say whether a fresh Checkout -> Payment path exists.
  run trace "$PYTHON" scripts/diagnose-payment-trace-path.py --lookback-minutes 15 --recent-minutes 15 \
      --out "reports/local/payment-trace-readiness-$TS.json"

  printf '\nDone. Evidence: %s/\n' "$LOGDIR"
  printf 'Also: reports/local/payment-trace-readiness-%s.json, reports/local/service-runtime-{before,after}-%s.json\n' "$TS" "$TS"
  if [ "${#failed[@]}" -gt 0 ]; then
    printf 'Steps that failed (their logs are above): %s\n' "${failed[*]}"
    printf 'Send back: the failed step names, the product-catalog and otel-collector lines from\n'
    printf 'containers-before/containers-after, the PRODUCT_CATALOG line from health-after, both\n'
    printf 'stats samples, and logs-catalog-before/after.\n'
    exit 1
  fi
  printf 'Send back: the product-catalog and otel-collector lines from containers-before and\n'
  printf 'containers-after, the PRODUCT_CATALOG line from health-after, both stats samples,\n'
  printf 'whether logs-catalog-after still shows DeadlineExceeded, and the service-runtime reads.\n'
)
```

One note on the local files: a sandbox check of the block wrote a stub-generated
`.local/demo-endpoints.env` by mistake. It was moved to
`.local/sandbox-checks/stub-endpoints-20260926/` with a README and never deleted. The path now holds a
real file resolved by the user's run (see "The restart pass, measured" below), and the block moves any
existing file aside as `.pre-<timestamp>` before resolving, so a stale file whose ports a replaced
stack owned can never be sourced.

## The restart pass, measured (`7f46e69`) - the restart cleared the CPU symptom, the helper ate the per-container state again, and Payment is still unverified

Appended 2026-09-26. Everything above is left as it was and the corrections below are marked rather
than written over.

### The user's measured run

The user ran the block from "The restart pass" in their own terminal at **02:57-02:58 UTC on
2026-09-26**, from revision `7f46e69`. Evidence: `.local/user-next-action-20260926T025708/`
(`containers-{before,after}.log`, `stats-{before,after}.log`, `restart.log`,
`logs-catalog-{before,after}.log`, `health-after.log`, `flags.json`, `runtime-{before,after}.log`,
`trace.log`, `endpoints.log`, `docker-version.log`), with
`reports/local/payment-trace-readiness-20260926T025708.json` and
`reports/local/service-runtime-{before,after}-20260926T025708.json`. Every step exited `0` and
`failed=()` was empty. Nothing in this section was produced by this session; the code changes, the
tests and this record are.

| Reading | What it measured | Source |
| --- | --- | --- |
| Engine and stack surface | Docker engine 29.7.2; the block brought up 25 containers and every published port answered `ok`, including `PRODUCT_CATALOG product-catalog:3550 -> 55172`, plus `prometheus /-/healthy`, `flagd-ui /api/read`, `jaeger /jaeger/ui/api/services` | `docker-version.log`, `health-after.log` |
| The catalog, as found | `cpu=286.48% mem=19.68MiB / 20MiB mem_pct=98.38% pids=22`, then `cpu=793.98% mem=14.81MiB / 20MiB mem_pct=74.04% pids=27` - i.e. **at its cap, on a container seconds old** | `stats-before.log` |
| The catalog, after the in-place restart | `cpu=2.66% mem=12.83MiB / 20MiB (64.14%) pids=21`, then `cpu=0.76% mem=17.91MiB / 20MiB (89.57%) pids=21` - CPU fell from 793.98% to 0.76% | `stats-after.log` |
| The catalog's own output, before | two `exporter export timeout: rpc error: code = DeadlineExceeded`, at 02:57:43 and 02:57:59 - **within ~30 s of the container starting** | `logs-catalog-before.log` |
| The catalog's own output, after (`--since 5m`) | the **same two lines and no later one**; the window is 5 minutes and it contains the restart | `logs-catalog-after.log` |
| Per-container state | **one container out of twenty-five, on both captures** - the defect below; `product-catalog` and `otel-collector`, the two services the block existed to read, are the two it did not collect | `containers-{before,after}.log` |
| Go runtime metrics in the store, before | 347 names advertised, 24 kept, 20 services report metrics; **0 of 24 have a value** for all three services | `runtime-before.log` |
| Go runtime metrics, after | `checkout` **7 of 24** (the `go_memory_*` / `go_goroutine_count` family); `product-catalog` **0 of 24**; `payment` **0 of 24** | `runtime-after.log` |
| The trace read | 17 services visible; the store advertises **1** operation for `checkout` and it is `oteldemo.CartService/GetCart` (client), not the entry operation the probe asks for; 1 recent unfiltered `checkout` trace (`cb41c59b21d6c832ee86d7da67605aae`); bounded retrieval check `retrieved=False`; the operation-filtered interval query returned **0**, so `traces_retrieved=0`, `request_path_reconstructed=False`, `sufficiency: not_sufficient` | `trace.log`, trace extract |
| Fault flags | `18 flags read; not off: <none>` | `flags.json` |

### The helper ate the per-container state again

`containers-before.log` and `containers-after.log` are each **268 bytes** and hold exactly one
container - `checkout` - while printing `demo: listing project containers exited 0` and exiting `0`.
That is the second run in a row in which the helper lost the evidence the run was made for, by a
different cause from the first.

`docker inspect` reads stdin, so
`while read -r name; do ... "$DOCKER_BIN" inspect ... "$name"; done <<<"$names"` hands the rest of
the here-string to the first `inspect` invocation, which consumes it; the loop then sees EOF and ends.
`cmd_pull` had the identical shape over its image list, so every image after the first was silently
skipped. Reproduced here with a stub whose `inspect` drains stdin: a stub that does not read stdin
prints all three containers, and a stub that does is handed only the first.

Both loops now read their list into an array before iterating (`mapfile`) and every `docker`/`compose`
call that could read stdin is given `</dev/null`. The regression test drives the stub **with**
stdin-draining enabled and asserts that every container is reported and every image pulled; both
assertions fail against the pre-fix script. A second test pins that a failing `pull` still reaches the
caller rather than being reported as the last image's status.

### A second defect the same run exposed: the advertised operation names were dict reprs

`JaegerClient.operations()` ran `sorted(str(name) for name in _data_list(...))` over the v1 reply,
whose entries are **objects** (`{"name": ..., "spanKind": ...}`), so it returned their repr. The
extract shows it verbatim: `names` as `["{'name': 'oteldemo.CartService/GetCart', 'spanKind':
'client'}"]` and `advertises_expected` as `false`.

Two consequences, both live in the same run. `ENTRY_OPERATION in advertised` was `False` however the
store spelled a name, so the "expected operation **advertised**, not observed" rung of the
entry-operation ladder could never fire and a name the store was willing to confirm would still have
been recorded as `unverified`. And `names_containing_alias` was matched against repr text rather than
names. Fixed by unwrapping `name` from the object, with the bare-string form older builds answer still
accepted. The fixture answered bare strings, which is exactly why no test caught it; the default
fixture now answers the live shape.

The trace log also printed a **count** of advertised operations rather than the names, which is what
made the mismatch invisible. It now prints each name and says plainly when none of them is the
operation the probe looks for.

### The runtime probe could not tell a broken export from a guessed name

`runtime-before` reported "0 of 24 runtime metrics have a value" for all three services while the
store advertised 347 names. Read alone, that cannot distinguish a service whose export path is broken
from one whose metrics are named something the discovery never asked about - the one question the
script exists to answer.

The script now asks the store directly, per service: `count by (__name__) ({service_name="..."})`
lists the metric names the store really holds for that service. An empty answer is then a statement
about the service, and any held name the discovery never asked about is printed as
`held but never asked about` - the vocabulary to build the next discovery from. The probe is additive
and a failed probe is recorded as a failure, so it cannot turn a failed read into an empty one.

What the three-service comparison already argues, without deciding it: `checkout` and
`product-catalog` are both Go services of this revision with the same instrumentation, and in the
**same run and the same query** the first returned 7 of 24 and the second 0 of 24. A name-shape
artefact would have to be service-specific to explain that; a broken export path on the catalog
explains it directly and matches its own `DeadlineExceeded` output. The probe in the next run settles
it.

### What this run does and does not settle

Settled:

- The restart was reversible and stayed in scope: `compose restart product-catalog otel-collector
  exited 0`, and `checkout` carries the same `started=2026-09-26T02:57:33.504438613Z` in both
  captures, so nothing outside the two named services was replaced and no volume or image was touched.
- A fresh process **does** clear the CPU symptom: 793.98% collapsed to 0.76-2.66%.
- No `DeadlineExceeded` line later than 02:57:59 appears in the post-restart 5-minute window.
- Nothing was injected: all 18 fault flags `off`.

Not settled, and not claimed:

- **Payment recovery remains unverified on this revision.** The store advertises exactly one operation
  for `checkout` and it is not the one the probe asks for, so the interval query returned nothing.
  There is no `Charge` evidence either way, and a zero under a filter that does not match is not an
  absent path.
- The catalog's **Docker health after the restart was never captured** - the defect above ate it.
  `health-after` is `demo.sh health`, a TCP connect, which proves a listener exists and not that the
  service is serving.
- Whether the catalog's export timeouts actually stopped, or the restarted process is simply younger
  than one export interval, is not decidable from two lines in a `--tail 80` window.
- The catalog's Go runtime metrics did not return after restarting **both** it and the collector,
  while `checkout`'s appeared after restarting the collector alone. That is the state the next run's
  probe reads.

### `D27`'s table is again not a partition

`D27` (the local decision log) records two rows - "CPU near the core count **with RSS at the limit**"
or "CPU near idle, RSS below" - and the 2026-09-25 11:46:57 reading matched neither, because CPU was high
*and* RSS was low (56.50%, 28.34%). This run adds a third observation of the same container: at ~30 s
of age its RSS is **98.38% and 74.04%** of the cap with 286.48% and 793.98% CPU; at ~23 minutes of age
it was 56.50% and 28.34% with 159.76% and 378.74%.

So the two rows do not partition the container's life: the same container sits at the cap early and
well below it later, which is what a Go heap held under `GOMEMLIMIT` does when the collector runs and
returns memory. A single RSS sample therefore cannot decide the memory question, and the deviation
stays **not applied** - this is not a re-reading of the earlier number. What the pair does support is
that neither observation is a stable state to repair against, and that the readings which persist
across both are the catalog's export timeouts and its never-answering health probe.

### Executed in this session, and what was not

Offline suite `291 passed, 7 skipped, 85 deselected` in **both** environment variants (`ruff check`,
`ruff format --check` over 92 files, `mypy src/opsaccord` over 42 files, `bash -n scripts/demo.sh` all
clean) at `reports/local/validation-20260926T030620`. Each new test was falsified against the pre-fix
code before it was kept: both `demo.sh` assertions and the operations-shape assertion fail there, the
latter producing the live repr verbatim. Those suites run against stubs, which is structure and not
live evidence.

**Not run:** no live scenario, no integration suite, no PostgreSQL, and no live stack from this
session - the sandbox reaches neither the Docker socket nor a loopback port, so the per-container
state, the corrected runtime probe and Payment recovery all stay with the user. **No P1-01 outcome is
reported**, and no recovery is claimed.

### The one next action

Read-only; the stack is already up and healthy, so nothing is started, restarted or armed. It runs the
integration suite this pass could not, then captures the per-container state that was lost, the
catalog's own numbers and output, the runtime probe and the retained traces.

```bash
( set -euo pipefail
  cd /home/brentiburo/projects/OpsAccord
  deactivate 2>/dev/null || true
  source .venv/bin/activate
  export UV_PROJECT_ENVIRONMENT="$PWD/.venv"
  PYTHON="$PWD/.venv/bin/python"
  printf 'interpreter: %s\n' "$("$PYTHON" -c 'import sys; print(sys.executable)')"
  printf 'tested revision: %s on %s\n' "$(git rev-parse --short HEAD)" "$(git rev-parse --abbrev-ref HEAD)"

  TS="$(date -u +%Y%m%dT%H%M%S)"; LOGDIR=".local/user-next-action-$TS"; mkdir -p "$LOGDIR"
  printf 'evidence dir: %s\n' "$LOGDIR"

  failed=()
  run() { local name="$1"; shift
    set +e; { "$@"; } 2>&1 | tee "$LOGDIR/$name.log"; rc="${PIPESTATUS[0]}"; set -e
    printf '==== %s exited %s (log %s) ====\n' "$name" "$rc" "$LOGDIR/$name.log"
    [ "$rc" -eq 0 ] || failed+=("$name"); }

  # 1. The stages this pass could not run: PostgreSQL, then the integration suite. Both
  #    are project-local and idempotent. The live settings are removed so an exported
  #    endpoint file cannot turn the suite into a live run against the demo stack.
  run postgres bash scripts/pg.sh start
  run integration env -u OPSACCORD_DEMO_MODE -u OPSACCORD_PROMETHEUS_BASE_URL \
      -u OPSACCORD_JAEGER_BASE_URL -u OPSACCORD_FLAG_CONTROL_BASE_URL -u OPSACCORD_LIVE_DEMO \
      "$PYTHON" -m pytest -q -m integration

  # 2. Refresh the endpoints from the stack that is up now. A stale file names ports a
  #    replaced stack owned, and querying those answers "connection refused", which reads
  #    like a stack fault. It is moved aside, never deleted, and only a file resolved by
  #    this run is used. Sourcing it sets these variables in this subshell only.
  if [ -f .local/demo-endpoints.env ]; then mv .local/demo-endpoints.env ".local/demo-endpoints.env.pre-$TS"; fi
  run endpoints bash scripts/demo.sh endpoints
  if [ ! -f .local/demo-endpoints.env ]; then
    printf '\nSTOP: scripts/demo.sh endpoints could not resolve the stack; nothing further was run.\n'; exit 1
  fi
  if [ "$(grep -c '^OPSACCORD_[A-Z_]*=' .local/demo-endpoints.env)" -ne 4 ]; then
    printf '\nSTOP: the refreshed endpoints file does not hold exactly the four expected settings.\n'; exit 1
  fi
  set -a; . .local/demo-endpoints.env; set +a

  # 3. The per-container state, now that the loop no longer consumes its own list. This is
  #    the reading the 2026-09-26 block was written for and did not get: every container,
  #    `product-catalog` and `otel-collector` included, not one out of twenty-five.
  run containers bash scripts/demo.sh inspect
  containers_seen="$(grep -cE '^[^ ]+ state=' "$LOGDIR/containers.log" 2>/dev/null || true)"
  case "$containers_seen" in ''|*[!0-9]*) containers_seen=0 ;; esac
  printf 'containers reported: %s\n' "$containers_seen"
  if [ "$containers_seen" -le 1 ]; then
    printf 'WARNING: that is fewer containers than the stack has, so the list is still being\n'
    printf 'consumed by the first inspect. Read nothing from containers.log; send it back as-is.\n'
  fi

  # 4. The catalog's own resource numbers and its own output, on the restarted process.
  run stats bash scripts/demo.sh stats
  export OPSACCORD_DEMO_LOG_SINCE=20m
  run logs-catalog bash scripts/demo.sh logs product-catalog
  unset OPSACCORD_DEMO_LOG_SINCE

  # 5. The runtime read, now with the store-side probe: it prints the metric names the store
  #    really holds per service, so an empty answer is a statement about the service rather
  #    than about the names this script guessed.
  run runtime "$PYTHON" scripts/diagnose-service-runtime.py \
      --services product-catalog checkout payment \
      --out "reports/local/service-runtime-$TS.json"

  # 6. The retained traces. The lines carrying the store's own operation names, and the
  #    evidence-sufficiency line, are the ones that decide the next step.
  run trace "$PYTHON" scripts/diagnose-payment-trace-path.py --lookback-minutes 15 --recent-minutes 15 \
      --out "reports/local/payment-trace-readiness-$TS.json"

  # 7. What the fault flags say, so no reading above is attributed to an armed fault.
  set +e
  curl -fsS --max-time 10 "$OPSACCORD_FLAG_CONTROL_BASE_URL/api/read" >"$LOGDIR/flags.json" 2>"$LOGDIR/flags.err"
  flags_rc=$?
  set -e
  printf '==== flag read exited %s ====\n' "$flags_rc"
  set +e
  "$PYTHON" - "$LOGDIR/flags.json" <<'PY'
import json, sys
try:
    flags = json.load(open(sys.argv[1]))["flags"]
except Exception as exc:
    print(f"flags document unreadable: {exc}")
    raise SystemExit(0)
armed = {n: f.get("defaultVariant") for n, f in flags.items() if f.get("defaultVariant") not in (None, "off")}
print(f"{len(flags)} flags read; not off: {armed or '<none>'}")
PY
  set -e

  printf '\nDone. Evidence: %s/\n' "$LOGDIR"
  printf 'Also: reports/local/service-runtime-%s.json and reports/local/payment-trace-readiness-%s.json\n' "$TS" "$TS"
  if [ "${#failed[@]}" -gt 0 ]; then
    printf 'Steps that failed (their logs are above): %s\n' "${failed[*]}"
    printf 'Send back: the failed step names, the containers log, the runtime log and the trace log.\n'
    exit 1
  fi
  printf 'Send back: the "containers reported" count and the product-catalog and otel-collector\n'
  printf 'lines from containers.log, both stats samples, the runtime log in full (its "held" and\n'
  printf '"never asked about" lines especially), and the "advertised:" lines plus the\n'
  printf 'evidence-sufficiency line from the trace log.\n'
)
```

Read that run as follows. A `containers reported` count of 1 means the list is still being consumed
and the capture is worthless - stop and send it back rather than reading it. The runtime log's
`held but never asked about` line is the vocabulary the next discovery needs, and a service that
holds **no** names at all is the attributable form of "its export path is broken".

**Phase 1 remains PARTIAL.** Its required live outcomes - a resolved Payment scenario and a measured
recovery - have not been demonstrated on this revision, and this pass did not attempt them.

## The resume pass (this commit) - the lifecycle hazard was real, its cause was mis-stated, and the guard now refuses instead of guessing

Resumed 2026-09-29 after the break. **No live command ran from this session.** This sandbox reaches
neither Docker nor a loopback port, so every lifecycle action below is the user's to run; what this
pass did is establish the state, fix the lifecycle hazard it was asked to fix, and pin the refusals in
tests that fail against the previous script. No new live outcome is reported here: the live readings
this project holds are still the user's, in `.local/user-live-checks-*`, unchanged and unextended.

### The state as found, before anything was touched (read-only)

| Item | Finding |
| --- | --- |
| Revision | `2e3ccbc` on `phase1/baseline`; `AGENTS.md` modified (the user's uncommitted edit, preserved) and `scripts/pg.sh` carrying this pass's work |
| Drivers | none running: no postmaster, no `validate.sh`, no `run_live_scenarios.sh`, no pytest |
| PostgreSQL | **stopped**, and provably so from a shell that can see it: `.local/pg/data/postmaster.pid` does not exist, `.local/pg/run/` is empty (no socket, no lock file), and `postgres.log` ends at `2026-09-26 11:07:58` with `FATAL: could not create any TCP/IP sockets` followed by `LOG: database system is shut down` |
| Docker | **cannot be inspected from here** (`UtilBindVsockAnyPort:309: socket failed`); that is an inability to inspect, not evidence that the engine or the containers are down |
| Sockets | every loopback connect from this shell is denied, so `scripts/pg.sh status` here exits **4** (*unknown*) by design - see below. A failed probe was never read as "down" |
| Endpoints | `.local/demo-endpoints.env` still names the 2026-09-26 stack (prometheus 9090, jaeger 49716, flagd 8080) and is stale if that stack was not restarted |
| Teardown | no `.local/user-teardown-*` directory exists, so the teardown block an earlier pass supplied was never run. All `.local/user-*` evidence directories are intact, and the quarantined `.local/sandbox-checks/` fixture is untouched |

### The incident, read from the log rather than from the earlier summary

```
2026-09-26 10:57:08.550 [44909] LOG:  database system is ready to accept connections
2026-09-26 11:04:13.002 [13]    FATAL: could not create any TCP/IP sockets        <- a start from this sandbox
2026-09-26 11:05:09.017 [44909] LOG:  could not open file "postmaster.pid": No such file or directory
2026-09-26 11:05:09.017 [44909] LOG:  performing immediate shutdown because data directory lock file is invalid
2026-09-26 11:05:09.020 [44909] LOG:  database system is shut down
2026-09-26 11:07:58.358 [24]    FATAL: could not create any TCP/IP sockets        <- a second attempt
```

**A correction to the previous pass's account.** It reported that the failed attempt "removed the pid
file". The log does not say that, and the difference is worth keeping: the *live* postmaster (PID
44909) found the lock file it owns missing or invalid and shut itself down. `pg_ctl start` - and the
postmaster itself, early in startup - writes `postmaster.pid` before the listen socket is created, so
the sandboxed attempt **replaced** the live server's lock file. The live server, finding a lock file
that no longer identified it, refused to continue and shut down immediately (not fast: the next start
replays WAL as after a crash). Same hazard, same outcome, but nothing "deleted" a file behind the
server's back - the supported tooling rewrote it.

**Why the obvious guard was not enough.** `pg_ctl status` decides liveness by reading `postmaster.pid`
and testing that PID with `kill(0)`. That test is namespace-sensitive: a shell that cannot see the
postmaster reads a *live* server as *stopped*, and the next `pg_ctl start` is then issued against a
data directory that already has an owner. The two checks disagreeing is the whole incident, and a
single failed probe could not have distinguished them. Two further shapes were the same hazard:
`cmd_stop` treated a failed `status` as "nothing to do", and `cmd_reset` was
`cmd_stop || true; rm -rf`, so the destructive path could reach `rm -rf` after a refused stop.

### The safeguards added to `scripts/pg.sh`

1. **`pg_state()`** decides liveness from two independent signals and never from one probe: the PID in
   `postmaster.pid` when `kill -0` can see it (in which case the port is not probed at all), otherwise
   a TCP probe of `127.0.0.1:$PG_PORT` that separates *refused* (`Connection refused` - definitively
   nothing listening) from *cannot probe* (anything else, including this sandbox's `Operation not
   permitted`). What the two cannot agree on is `unknown`.
2. **`pg_refuse_unknown()`** - `start` and `stop` refuse on `unknown`, naming the pid file, the port
   and the reason, instead of guessing. This is the demonstrated failure's direct fix: on
   2026-09-26 the sandboxed shell would now be refused before touching the cluster.
3. **`cmd_start`** serialises with `flock -n 9` on `$PG_HOME/start.lock` (released on process exit, so
   a crashed start cannot leave a lock behind) and re-checks the state *inside* the lock, because the
   attempt that held it may already have started the server. The post-start `SELECT 1` and
   `cmd_ensure_dbs` still propagate their failures.
4. **`cmd_stop`** requires `pg_ctl status` to identify a server in `$PG_DATA` before signalling it, so
   a host shell cannot signal an unrelated process sitting in a stale pid file; a stopped cluster is a
   no-op and an unknown one is a refusal.
5. **`cmd_status`** now distinguishes the two: `0` running, `3` stopped, **`4` unknown** (it used to
   return 3 for both).
6. **`cmd_reset`** no longer masks the stop with `|| true`: it stops first, re-reads the state, refuses
   unless the cluster is *stopped*, and only then runs `rm -rf`.
7. **No path writes, deletes, renames or fabricates a pid file**, there is no automatic stale-PID
   deletion, and every signal is sent through PostgreSQL's own tooling against the verified
   project-local data directory.

The script is also sourceable now (dispatch is guarded by `[ "${BASH_SOURCE[0]}" = "$0" ]`), which is
what lets the tests below replace the single socket attempt and still exercise the real classification,
refusals, locking and argv.

### A second defect, caught by the new tests before this fix was committed

The first version of `pg_state`/`port_reachable` toggled `set +e` / `set -e` around the socket attempt.
`errexit` is a shell-wide option, so restoring it inside a function that is about to return non-zero
aborts the caller that is reading that very status: `state="$(pg_state)"` came back empty and
`pg.sh start` died with **no message at all**. A *stopped* cluster could not be started. The new tests
caught it on their first run (`test_start_of_a_stopped_cluster_starts_once_and_verifies` failed with
`rc 1` and an empty stderr). Both sites now use `|| rc=$?`, which captures the status without touching
the option - the same idiom `cmd_endpoints` already used for the same reason. Had this been committed
untested it would have read on the host as "PostgreSQL will not start".

### Regression coverage: `tests/unit/test_pg_lifecycle.py`

Thirteen cases, all offline, each against a disposable fixture - its own temporary `PG_DATA`, `PG_RUN`,
`PGBIN` and port 55439, with `pg_ctl`, `psql`, `createdb` and `initdb` stubs that record their argv and
cannot touch a real cluster. The suite asserts, in the fixture's terms:

- a server this shell can *see* is never started again, and its port is never probed;
- a shell that cannot see the server refuses to start, sends no signal, and leaves the pid file byte
  for byte as it found it (the demonstrated failure);
- `status` separates stopped (3) from unknown (4);
- a start of a stopped cluster runs exactly one `pg_ctl start` against the fixture, verifies with
  `SELECT 1`, and fabricates no pid file;
- a failed `pg_ctl start`, and a successful start whose verification fails, are both propagated;
- a second start is refused while another holds the lock, with no start attempted;
- `stop` refuses when the port answers but `pg_ctl` cannot identify a server, and otherwise sends
  `-m fast` to the fixture's own data directory - or does nothing at all when stopped;
- `reset` cannot reach its delete without proving the cluster stopped - two ways, with a canary file
  left in the data directory as the witness;
- only a *refused* connection counts as nothing listening (0/1/2 for reachable/refused/cannot-probe);
- the script still dispatches when executed and dispatches nothing when sourced.

**Falsified first.** Pointed at `HEAD`'s copy of the script (`PG_SCRIPT_UNDER_TEST`), all thirteen cases
fail; against the fixed script all thirteen pass. The error text quoted in the report of the previous
pass's live run - `pg_ctl` reading a live cluster as stopped - is exactly what the fixture reproduces.

### Executed in this session

| Check | Result |
| --- | --- |
| `bash -n scripts/pg.sh` | clean |
| `tests/unit/test_pg_lifecycle.py` | **13 passed**; the same file against `HEAD`'s script: **13 failed** |
| Offline suite, `.venv/bin/python -m pytest -q -m "not integration"` | **304 passed, 7 skipped, 85 deselected** (was 291 passed before the 13 cases) |
| `ruff check .` / `ruff format --check .` | clean / 93 files already formatted |
| `mypy` | `Success: no issues found in 43 source files` |
| `bash scripts/pg.sh status` (this sandbox, real data directory) | `[pg] state unknown: ...`, exit **4**; no pid file created, `.local/pg/run/` still empty, `postgres.log` mtime unchanged at `2026-09-26 11:07:58` |

The last row is the hazard itself, exercised read-only: in this shell the new guard refuses, exits 4 and
touches nothing.

### Not run, and why

- **PostgreSQL recovery** - the cluster is confirmed stopped, but starting it requires a host shell
  that can open a socket, and this session has none. It is the first stage of the block below.
- **The demo baseline and the Checkout -> Payment capture** - no Docker access from here.
- **P1-01, and any recovery measurement** - unchanged from the previous pass. **Phase 1 remains
  PARTIAL.**
- **The integration suite** - needs the cluster; it is stage 2 of the block, and of the read-only block
  at the end of [the previous section](#the-one-next-action).

### The one next action (host shell, one block)

This is the minimal next step: establish the cluster's state, start it at most once, prove it is usable,
start only the demo services that are missing, and refresh the endpoints. It is not the evidence run -
that block (integration suite, per-container state, the catalog's numbers, the retained traces) is at
the end of the previous section and should follow once this one reports OK.

```bash
( set -euo pipefail
  cd /home/brentiburo/projects/OpsAccord
  deactivate 2>/dev/null || true
  source .venv/bin/activate
  export UV_PROJECT_ENVIRONMENT="$PWD/.venv"
  PYTHON="$PWD/.venv/bin/python"
  TS="$(date -u +%Y%m%dT%H%M%S)"; LOGDIR=".local/user-recovery-$TS"; mkdir -p "$LOGDIR"
  echo "interpreter: $("$PYTHON" -c 'import sys; print(sys.executable)')"
  echo "revision: $(git rev-parse --short HEAD) on $(git rev-parse --abbrev-ref HEAD)"
  echo "evidence: $LOGDIR"
  echo "--- 1. the cluster's state, established before anything is started ---"
  set +e
  bash scripts/pg.sh status 2>&1 | tee "$LOGDIR/01-pg-status.log"; pg_rc=${PIPESTATUS[0]}
  set -e
  echo "pg.sh status exited $pg_rc (0 running, 3 stopped, 4 unknown)"
  case "$pg_rc" in
    0) echo "already running: nothing is started" ;;
    3) echo "stopped, and this shell can see that: starting once"
       bash scripts/pg.sh start 2>&1 | tee "$LOGDIR/02-pg-start.log"
       tail -n 40 .local/pg/postgres.log | tee "$LOGDIR/03-pg-recovery.log" ;;
    *) echo "STOP: pg.sh status exited $pg_rc, so the cluster's state is not established and no start is safe from this shell. Send $LOGDIR/01-pg-status.log back." >&2
       exit 1 ;;
  esac
  echo "--- 2. what was recovered: connectivity, the three databases, real rows ---"
  bash scripts/pg.sh status 2>&1 | tee "$LOGDIR/04-pg-after.log"
  set -- "$(bash scripts/pg.sh url dev)" "$(bash scripts/pg.sh url test)" "$(bash scripts/pg.sh url scenario)"
  "$PYTHON" - "$LOGDIR" "$@" <<'PY' 2>&1 | tee "$LOGDIR/05-pg-verify.log"
import sys
from pathlib import Path
import psycopg
from opsaccord.config import to_libpq_dsn
from opsaccord.persistence.tables import Base
logdir = Path(sys.argv[1])
targets = dict(zip(("dev", "test", "scenario"), sys.argv[2:5], strict=False))
expected = set(Base.metadata.tables)
checkpoints = {"checkpoints", "checkpoint_blobs", "checkpoint_writes"}
failures, full_schema = [], 0
for name, url in targets.items():
    try:
        with psycopg.connect(to_libpq_dsn(url), connect_timeout=10) as conn:
            print(f"{name}: connected - {conn.execute('select version()').fetchone()[0].split(',')[0]}")
            tables = {row[0] for row in conn.execute("select tablename from pg_tables where schemaname='public'")}
            present = expected & tables
            print(f"{name}: {len(tables)} public tables; {len(present)}/{len(expected)} domain tables; checkpoints {bool(checkpoints & tables)}")
            if expected - present:
                print(f"{name}: missing domain tables: {sorted(expected - present)}")
            for table in ("incidents", "workflows", "tool_calls", "audit_events"):
                if table in tables:
                    print(f"{name}.{table}: {conn.execute(f'select count(*) from {table}').fetchone()[0]} rows")
            full_schema += expected <= tables
    except Exception as exc:
        failures.append(f"{name}: {exc}"); print(f"{name}: FAILED - {exc}")
print(f"databases reachable: {len(targets) - len(failures)}/{len(targets)}; holding the app's full schema: {full_schema}")
if failures or not full_schema:
    raise SystemExit(1)
print("pg verification: OK")
PY
  echo "--- 3. the demo stack: start only what is missing ---"
  if bash scripts/demo.sh health 2>&1 | tee "$LOGDIR/06-demo-health.log"; then
    echo "every documented endpoint answers: nothing is started"
  else
    echo "not every endpoint answers yet; 'compose up -d' fills in only what is missing (no 'down', no volumes)"
    bash scripts/demo.sh start 2>&1 | tee "$LOGDIR/07-demo-start.log"
    bash scripts/demo.sh health 2>&1 | tee "$LOGDIR/08-demo-health-after.log"
  fi
  echo "--- 4. endpoints, scoped to this subprocess ---"
  bash scripts/demo.sh endpoints 2>&1 | tee "$LOGDIR/09-endpoints.log"
  test -s .local/demo-endpoints.env
  set -a; . .local/demo-endpoints.env; set +a
  echo "--- 5. expected final states, asserted ---"
  bash scripts/pg.sh status >"$LOGDIR/10-pg-final.log" 2>&1 || { cat "$LOGDIR/10-pg-final.log" >&2; echo "STOP: the cluster does not report running" >&2; exit 1; }
  failed=()
  for probe in "prometheus|$OPSACCORD_PROMETHEUS_BASE_URL/-/healthy" "jaeger|$OPSACCORD_JAEGER_BASE_URL/api/services" "flagd|$OPSACCORD_FLAG_CONTROL_BASE_URL/api/read"; do
    name="${probe%%|*}"; url="${probe#*|}"
    if curl -fsS --max-time 10 "$url" -o "$LOGDIR/probe-$name.json" 2>"$LOGDIR/probe-$name.err"; then
      echo "ok   $name -> $url"
    else
      echo "FAIL $name -> $url ($(head -c 200 "$LOGDIR/probe-$name.err"))"; failed+=("$name")
    fi
  done
  for var in OPSACCORD_DEMO_MODE OPSACCORD_PROMETHEUS_BASE_URL OPSACCORD_JAEGER_BASE_URL OPSACCORD_FLAG_CONTROL_BASE_URL; do
    grep -q "^$var=" .local/demo-endpoints.env || { echo "STOP: $var missing from .local/demo-endpoints.env" >&2; exit 1; }
  done
  if [ "${#failed[@]}" -gt 0 ]; then echo "STOP: endpoints that did not answer: ${failed[*]}" >&2; exit 1; fi
  echo "OK: cluster running, three databases reachable, endpoints refreshed and answering"
  echo "Next: the read-only block at the end of the previous section of docs/reports/phase1-implementation.md."
  echo "Evidence: $LOGDIR"
)
```

## The baseline-capture pass (this commit, resuming on `c33b81e`) - the fault-free baseline had no entry point, so one was built and the live run is handed off

This pass resumed on `c33b81e` (the `scripts/pg.sh` lifecycle guard) and worked the task's parts 1-3
read-only, then ran into the same wall the previous pass did: **this shell cannot start PostgreSQL and
cannot reach Docker**, so the demo baseline and the Checkout -> Payment capture cannot be measured
from here. What it could do is remove the reason the capture could not be *attempted* at all, which is
a real gap in the tree rather than a limitation of the environment.

### Every live entry point arms a fault, so nothing could demonstrate a baseline

`src/opsaccord/scenarios/live.py` holds seven `LIVE_PLANS`, and every one injects a fault: P1-01, P1-05,
P1-06 and P1-07 arm `paymentUnreachable`, P1-02 arms `adHighCpu`, P1-03 arms `cartFailure=100%`, and
P1-04 makes telemetry unroutable. `scripts/validate.sh --live` runs those plans, so the documented live
path can only answer "what does the system do when something is broken" - it has no way to answer "does
the unfaulted path still complete a checkout". The trace exporter
(`scripts/diagnose-payment-trace-path.py`) is fault-oriented in the same way: it is at its best
describing a *failing* request, and its findings are about an absent or failing edge.

That is the gap this pass closes, because the task's part 5 asks precisely for the fault-free case with
raw evidence.

> **Corrected 2026-10-15.** The block above - and the one at the end of this section, which the user has
> **not** run - tells the reader that `compose up -d --no-build` "fills in only what is missing". That is
> not true of compose: it also **recreates** any container whose configuration or image differs from the
> one it would create, which is exactly the wrong thing to do to a container that is already running,
> because the running container, its restart count and its health history *are* the evidence a diagnosis
> reads. The corrected handoff replaces both, and is at the end of this report. The wording above is left
> as it was handed over.

### `scripts/capture-baseline-checkout.py` - read-only, and it refuses more than it accepts

The new capture takes an explicit `--start`/`--end` (or `--lookback-minutes`) and does three things:
it queries Jaeger for `checkout` traces in that interval, reconstructs each trace's request path from
the payload's own parent references via `opsaccord.tools.fault_evidence.summarize_trace`, and passes a
trace only when all of the following hold, each recorded separately with the evidence that decided it:

- **`checkout_entry_span`** - the entry operation resolved to its own *server* span, not the caller's
  identically named client span one level above it;
- **`product_catalog_reached`** - `product-catalog` **served** a `ProductCatalogService` call on the
  attempt's own path. A caller-side client span is recorded as exactly that: it says the call was made,
  not that the catalog answered;
- **`payment_charge_on_path`** - a `payment` *server* span for `oteldemo.PaymentService/Charge` whose
  relationship to the attempt is `direct_child` or `descendant`. A Charge span in an unrelated branch,
  or one whose ancestry is unresolved, is reported with that relationship named;
- **`explicit_success`** - the entry and edge spans both record `OK` with a named status source
  (`otel.status_code`, a gRPC code, and so on), and no span on the reconstructed path records `ERROR`.
  `UNSET` is reported as `UNSET`: a span that wrote no status is not a success;
- **`fault_flags_safe`** - the flag document read from flagd-ui holds every flag in `FLAG_SAFE_VARIANT`
  at its safe variant. A document that could not be read is reported as *unverified*, never as safe.

Refusals are distinguished rather than flattened: an empty interval, a failed query, an unreached edge,
a client-only edge span, an edge in another branch and an unread flag document each produce their own
named blocking reason, so the next run has something to act on. It writes sanitized **raw** spans -
`span_id`, `parent_span_id`, service, operation, span kind, start/end in microseconds, duration, status
and its source, message, relationship and hop count - to the JSON extract, and the same evidence as a
human-readable log; `--out` and `--log` are the only files it writes. It arms no flag, starts no
container, restarts nothing, and calls no service directly. What it will *not* claim is written into the
extract's `limits`: an empty query is not health, `UNSET` is not success, and one verified trace
describes the interval it was captured in - it is not sustained reliability and it is not a P1-01 result.

### Regression coverage: `tests/unit/test_baseline_checkout_capture.py`

Nineteen offline cases. The fixtures are built to be wrong one property at a time - a trace that never
reached the edge, one whose Payment span hangs off a branch the attempt never called, one whose Payment
span wrote no status, one where the catalog call exists only as the caller's client span, one whose
entry resolved to a client span, one with an `ERROR` span on the path, and a stale span outside the
window. Each must fail, must name the right reason, and must not report a verified baseline.

The falsification is explicit: **`test_the_gate_would_catch_a_relationship_blind_variant`** shows that
the *same* unrelated-branch fixture is rejected while the relationship requirement stands, and accepted
once that requirement is patched away - so the requirement is load-bearing rather than decorative. The
command's own contract is covered too: it refuses to start without a Jaeger endpoint (exit 3), rejects
an out-of-range limit (exit 2), writes both artefacts on a verified baseline, reports an empty interval
as `not_verified_no_trace_in_interval` rather than as health, records a failed query as a failure rather
than as an empty result, respects the queried window, and lets an armed flag block the verdict.

### Executed in this session

| Check | Result |
| --- | --- |
| `tests/unit/test_baseline_checkout_capture.py` | **19 passed** |
| Offline suite, `-m "not integration"` | **323 passed, 7 skipped, 85 deselected** (was 304 passed, 7 skipped, 85 deselected at `c33b81e`; +19 cases here) |
| `ruff check .` / `ruff format --check .` | clean / 95 files already formatted |
| `mypy src/opsaccord` | `Success: no issues found in 42 source files` |
| `mypy scripts/capture-baseline-checkout.py` | `Success: no issues found in 1 source file` |
| `bash -n` on the block below | clean |
| The capture against an unreachable endpoint (smoke) | exits 1, verdict `not_verified_query_failed`, records three query failures and the unread flag document, writes both artefacts |
| `bash scripts/pg.sh status` (this sandbox, real data directory) | `[pg] state unknown: ...`, exit **4**; the guard refuses, and nothing was started, stopped or written |

### Not run, and why

- **PostgreSQL recovery** - `pg.sh status` exits **4** here, and `4` is *unknown*, not *stopped*: it is
  the guard refusing to guess, and this shell cannot establish the cluster's state, so no start is safe
  from it. The recorded evidence does not establish that the cluster is stopped either - the last line of
  `.local/pg/postgres.log` is `2026-09-29 23:23:41.437 +08 [55881] LOG: received smart shutdown request`
  with no following `shut down` line, the pid file still holds PID 55881 with mode `-rw-------`, and
  `kill -0 55881` cannot see that process from here. A shutdown *request* with no completion line and a
  PID this shell cannot test is a conflict between two signals, not a shutdown. **This bullet said "the
  cluster is *stopped as far as the recorded evidence goes*" when it was written, and that reading was
  wrong**; the recovery/baseline-handoff pass at the end of this report corrects it. What the evidence
  supports is UNKNOWN, and whether crash recovery is even *required* is not established by any of it. If a
  start happens later, its own logs report whether recovery ran - and a successful start is not proof of
  complete data integrity.
- **Docker, the demo baseline and the Checkout -> Payment capture** - `docker` is not resolvable in this
  WSL distro at all (`The command 'docker' could not be found in this WSL 2 distro`), and every loopback
  connect is denied (`bash: /dev/tcp/127.0.0.1/55432: Operation not permitted`), so nothing live can be
  measured here.
- **P1-01 and any recovery measurement** - unchanged. **Phase 1 remains PARTIAL**: the fault-free
  baseline and the fault run are both still unmeasured on this revision.

### The one next action (host shell, one block)

Start Docker Desktop first - the block checks the engine before touching the demo, and the previous
attempt died on `the Docker engine did not answer`. This block preserves the stale pid file and the log
tail *before* anything starts, establishes the cluster's state and acts on `0`/`3`/`4`, verifies the
three databases and their real rows, starts only the demo services that are missing (never `down`, never
a volume), refreshes the endpoints rather than inheriting them, asserts the three HTTP endpoints answer,
and then runs the new capture and keeps its exit status as the block's own.

```bash
( set -euo pipefail
  cd /home/brentiburo/projects/OpsAccord
  deactivate 2>/dev/null || true
  source .venv/bin/activate
  export UV_PROJECT_ENVIRONMENT="$PWD/.venv"
  PYTHON="$PWD/.venv/bin/python"
  TS="$(date -u +%Y%m%dT%H%M%S)"; LOGDIR=".local/user-baseline-$TS"; mkdir -p "$LOGDIR"
  DOCKER_BIN="${OPSACCORD_DOCKER_BIN:-/mnt/c/Users/admin/AppData/Local/Programs/DockerDesktop/resources/bin/docker.exe}"
  echo "interpreter: $("$PYTHON" -c 'import sys; print(sys.executable)')"
  echo "tested revision: $(git rev-parse --short HEAD) on $(git rev-parse --abbrev-ref HEAD)"
  echo "evidence dir: $LOGDIR"
  echo "Docker Desktop must already be running: this block starts no desktop and stops if the engine does not answer."

  echo "--- 0. preserve what this shell sees before anything is started ---"
  git status --porcelain=v1 | tee "$LOGDIR/00-git-status.log"
  if [ -e .local/pg/data/postmaster.pid ]; then
    cp -p .local/pg/data/postmaster.pid "$LOGDIR/00-postmaster.pid.before"
    echo "preserved .local/pg/data/postmaster.pid to $LOGDIR/00-postmaster.pid.before"
  else
    echo "no .local/pg/data/postmaster.pid to preserve" | tee "$LOGDIR/00-postmaster.pid.before"
  fi
  { find .local/pg/data -maxdepth 1 -name 'postmaster.pid' -printf '%p %s bytes %TY-%Tm-%TdT%TH:%TM:%TS\n'; find .local/pg/run -maxdepth 1 -printf '%p\n'; } 2>/dev/null | tee "$LOGDIR/00-pid-and-socket.log"
  if [ -f .local/pg/postgres.log ]; then tail -n 60 .local/pg/postgres.log | tee "$LOGDIR/00-postgres-log-tail-before.log"; fi

  echo "--- 1. the cluster's state, established before anything is started ---"
  set +e
  bash scripts/pg.sh status 2>&1 | tee "$LOGDIR/01-pg-status.log"; pg_rc=${PIPESTATUS[0]}
  set -e
  echo "pg.sh status exited $pg_rc (0 running, 3 stopped, 4 unknown)"
  case "$pg_rc" in
    0) echo "already running: nothing is started; it is verified below" ;;
    3) echo "stopped, and this shell can see that: starting exactly once"
       bash scripts/pg.sh start 2>&1 | tee "$LOGDIR/02-pg-start.log"
       tail -n 40 .local/pg/postgres.log | tee "$LOGDIR/03-pg-recovery.log" ;;
    *) echo "STOP: pg.sh status exited $pg_rc, so this shell cannot establish the cluster's state and no start is safe from it. Send $LOGDIR/01-pg-status.log back." >&2
       exit 1 ;;
  esac

  echo "--- 2. what was recovered: connectivity, the three databases, real rows ---"
  bash scripts/pg.sh status 2>&1 | tee "$LOGDIR/04-pg-after.log"
  set -- "$(bash scripts/pg.sh url dev)" "$(bash scripts/pg.sh url test)" "$(bash scripts/pg.sh url scenario)"
  "$PYTHON" - "$LOGDIR" "$@" <<'PY' 2>&1 | tee "$LOGDIR/05-pg-verify.log"
import sys
from pathlib import Path
import psycopg
from opsaccord.config import to_libpq_dsn
from opsaccord.persistence.tables import Base
targets = dict(zip(("dev", "test", "scenario"), sys.argv[2:5], strict=False))
expected = set(Base.metadata.tables)
checkpoints = {"checkpoints", "checkpoint_blobs", "checkpoint_writes"}
failures, full_schema = [], 0
for name, url in targets.items():
    try:
        with psycopg.connect(to_libpq_dsn(url), connect_timeout=10) as conn:
            print(f"{name}: connected - {conn.execute('select version()').fetchone()[0].split(',')[0]}")
            tables = {row[0] for row in conn.execute("select tablename from pg_tables where schemaname='public'")}
            present = expected & tables
            print(f"{name}: {len(tables)} public tables; {len(present)}/{len(expected)} domain tables; checkpoints {bool(checkpoints & tables)}")
            if expected - present:
                print(f"{name}: missing domain tables: {sorted(expected - present)}")
            for table in ("incidents", "workflows", "tool_calls", "audit_events"):
                if table in tables:
                    print(f"{name}.{table}: {conn.execute(f'select count(*) from {table}').fetchone()[0]} rows")
            full_schema += expected <= tables
    except Exception as exc:
        failures.append(f"{name}: {exc}"); print(f"{name}: FAILED - {exc}")
print(f"databases reachable: {len(targets) - len(failures)}/{len(targets)}; holding the app's full schema: {full_schema}")
if failures or not full_schema:
    raise SystemExit(1)
print("pg verification: OK")
PY

  echo "--- 3. the demo stack: the engine first, then only what is missing ---"
  if [ ! -x "$DOCKER_BIN" ] || ! "$DOCKER_BIN" version --format '{{.Server.Version}}' >"$LOGDIR/06-docker-engine.log" 2>&1; then
    echo "STOP: the Docker engine did not answer, so nothing below was run. Start Docker Desktop, wait until it reports running, then re-run this whole block." >&2
    tail -n 20 "$LOGDIR/06-docker-engine.log" >&2
    exit 1
  fi
  set +e
  bash scripts/demo.sh health 2>&1 | tee "$LOGDIR/07-demo-health.log"; health_rc=${PIPESTATUS[0]}
  set -e
  if [ "$health_rc" -eq 0 ]; then
    echo "every documented endpoint answers: no service is started, none is restarted"
  else
    echo "not every endpoint answers ($health_rc): 'compose up -d --no-build' fills in only what is missing (no 'down', no volumes, no limit changes)"
    bash scripts/demo.sh start 2>&1 | tee "$LOGDIR/08-demo-start.log"
    bash scripts/demo.sh health 2>&1 | tee "$LOGDIR/09-demo-health-after.log"
  fi
  bash scripts/demo.sh inspect 2>&1 | tee "$LOGDIR/10-demo-inspect.log"
  if grep -E 'load-generator|checkout|payment|product-catalog' "$LOGDIR/10-demo-inspect.log" >"$LOGDIR/10b-key-containers.log"; then
    cat "$LOGDIR/10b-key-containers.log"
  else
    echo "none of load-generator/checkout/payment/product-catalog appears in the inspect output (see 10-demo-inspect.log)"
  fi

  echo "--- 4. endpoints, refreshed rather than inherited ---"
  bash scripts/demo.sh endpoints 2>&1 | tee "$LOGDIR/11-endpoints.log"
  test -s .local/demo-endpoints.env
  for var in OPSACCORD_DEMO_MODE OPSACCORD_PROMETHEUS_BASE_URL OPSACCORD_JAEGER_BASE_URL OPSACCORD_FLAG_CONTROL_BASE_URL; do
    grep -q "^$var=" .local/demo-endpoints.env || { echo "STOP: $var is missing from the refreshed endpoint file" >&2; exit 1; }
  done
  set -a; . .local/demo-endpoints.env; set +a
  failed=()
  for probe in "prometheus|$OPSACCORD_PROMETHEUS_BASE_URL/-/healthy" "jaeger|$OPSACCORD_JAEGER_BASE_URL/api/services" "flagd|$OPSACCORD_FLAG_CONTROL_BASE_URL/api/read"; do
    name="${probe%%|*}"; url="${probe#*|}"
    if curl -fsS --max-time 10 "$url" -o "$LOGDIR/probe-$name.json" 2>"$LOGDIR/probe-$name.err"; then
      echo "ok   $name -> $url"
    else
      echo "FAIL $name -> $url ($(head -c 200 "$LOGDIR/probe-$name.err"))"; failed+=("$name")
    fi
  done
  if [ "${#failed[@]}" -gt 0 ]; then echo "STOP: endpoints that did not answer: ${failed[*]}" >&2; exit 1; fi

  echo "--- 5. the baseline: one fault-free Checkout -> Payment request, from Jaeger ---"
  set +e
  "$PYTHON" scripts/capture-baseline-checkout.py \
      --lookback-minutes 30 \
      --out "reports/local/baseline-checkout-$TS.json" \
      --log "reports/local/baseline-checkout-$TS.log" 2>&1 | tee "$LOGDIR/12-baseline-capture.log"
  cap_rc=${PIPESTATUS[0]}
  set -e
  echo "capture exit: $cap_rc (0 verified, 1 not verified, 3 refused to start)"
  echo "extract: reports/local/baseline-checkout-$TS.json"
  echo "capture log: reports/local/baseline-checkout-$TS.log"
  if [ "$cap_rc" -ne 0 ]; then
    echo "Not verified on this run. The capture names which property failed; if it says the interval held no trace, the load generator's row is in 10b-key-containers.log. Send $LOGDIR back with the two reports/local files rather than re-running blind." >&2
  fi
  echo "evidence: $LOGDIR"
  exit "$cap_rc"
)
```

> **Superseded 2026-10-15 by the recovery/baseline-handoff pass at the end of this report.** The user has
> **not** run the block above. The pass below corrects three things about it before it is run: the cluster's
> state was overstated (UNKNOWN, not *stopped*), the capture had no way to tell a fresh request from a
> historical one, and the demo startup it prescribed could recreate running containers.

## The recovery/baseline-handoff pass (this commit) - the cluster state was overstated, the baseline had no freshness, and the handoff is corrected before the user runs it

This pass resumed on `1e0cc1e`. **It ran no live command, and claims no live outcome.** This shell reaches
neither Docker nor a loopback port - unchanged since the resume pass - so `scripts/pg.sh status` still exits
**4** here and nothing about the demo can be measured from it. The user has **not** run the block supplied
at `1e0cc1e`, so no host-side recovery or baseline result is attributed to them; the live readings this
project holds remain the ones already recorded under `.local/user-*`, and those are the user's.

Read-only inspection of this revision: `phase1/baseline` at `1e0cc1e`, the user's `AGENTS.md` edit preserved,
no live-scenario driver running. `.local/pg/data/postmaster.pid` is 163 bytes with mtime
`2026-09-29T23:23:41.437`; `.local/pg/postgres.log` ends at that same instant with `received smart shutdown
request` and no following `shut down` line; `bash scripts/pg.sh status` exits **4**. `bash scripts/demo.sh
ensure` refuses with `the Docker engine did not answer` and touches nothing. `.local/demo-endpoints.env`
still names the 2026-09-26 stack and is stale if that stack was never restarted.

### 1. The cluster's state is UNKNOWN, and `ensure` gives the three states three actions

The previous report's "Not run" bullet called the cluster *stopped* and the next start "recovery-boundary
recovery". The evidence supports neither: a shutdown *request* with no completion line, a pid file whose
owner this shell cannot see, and a probe this shell cannot make are three signals that do not agree, and
`pg.sh` reports exactly that - `unknown`, exit **4**. That bullet is corrected in place and the earlier
reading is kept, because the chronology is the point: the wrong reading was made, and then corrected.

What was added is the entry point the handoff needs, so the decision is not left to a reader of prose:

| Command | State it reads | What it does |
| --- | --- | --- |
| `scripts/pg.sh ensure` | `running` | verifies connectivity and the three databases; starts nothing |
| | `stopped` | `cmd_start` **once** - with its own checks, its `flock` and its post-start verification |
| | `unknown` | refuses with exit **4** and touches nothing: no start, no stop, no `pg_ctl` call at all |

Its refusals were verified read-only in this sandbox rather than against the real cluster: `bash scripts/pg.sh
ensure` prints `refusing to ensure ...` and exits **4**, creates no pid file, and leaves `.local/pg/run/`
and `postgres.log`'s mtime exactly as it found them. No path writes, deletes or renames a pid file, and
`reset` still cannot reach its delete without first proving the cluster stopped.

### 2. The baseline capture now observes *forwards*, and refuses a historical request

The capture added at `1e0cc1e` accepted any trace in a 30-minute window and read the flags once, at present
day. Those two facts cannot be combined into "this request happened under these flags": the reading was
taken at a different time from the request, and nothing distinguished a request that began after the reading
from one already in flight when it was made. The gate now has a second half:

| Requirement | How the capture meets it |
| --- | --- |
| Flags read before the interval | `read_flags()` is called *before* the boundary is fixed, and the reading carries the instant it was taken |
| A UTC observation-start boundary | recorded as `observation_start` (microsecond precision) and `observation_start_us` |
| A **new** Checkout attempt | the measured attempt's own `started_at_us` must be `>=` the boundary; a request that began earlier is refused by name (`attempt_after_boundary`) however healthy it is |
| Catalog and Payment on *that* attempt | the ancestry walk in `summarize_trace`: the catalog's `server` span and Payment's `server` span must be `direct_child`/`descendant`/`same_span` of the attempt |
| Explicit success | the attempt and the Charge span both `OK` with a named status source and no `ERROR` on the reconstructed path; `UNSET` is reported as `UNSET` |
| Bounded ingestion polling | repeated bounded requests (explicit `limit`, explicit `start`/`end`, 10s timeout) until a documented overall deadline (default 180s), both reported under `polling` |
| Flags re-read, refusal on change | a second reading is taken when the first payload arrives (or at the end, if none did); a relevant flag that moved, became unsafe or could not be read either time blocks the verdict |
| Sanitized spans, promptly | every observed payload's sanitized spans and both timestamps go into the extract, which is written before the rendered log |
| A competing driver | `/proc/<pid>/cmdline` is scanned for another `run-live-scenarios` driver, the *method* is recorded, and a driver that is found blocks the verdict |

The flag guarantee is now stated for what it is. `flag_guarantee` in the extract says in words that two
readings bracket the interval, that they cannot exclude a change which began and ended inside it, that they
are not continuous observation, and that a competing writer is checked separately - so "the flags are safe"
is never read as "the flags were safe throughout". An empty store, a missing edge, an ambiguous status, an
incomplete ancestry and a deadline that expired are five different named outcomes, and none of them is a
verification. The historical window was **not** widened to compensate for any of them.

### 3. `compose up` recreates containers, so demo startup reads the state first

`demo.sh start` is `compose up -d` plus a health wait, and compose does not only fill in what is missing: it
**recreates** a container whose configuration or image differs from the one it would create. The claim in
the two earlier blocks that it "fills in only what is missing" is corrected above and in the runbook.

`scripts/demo.sh ensure` replaces the blanket call for this workflow's purpose. It reads this project's own
containers - filtered by the compose project label, so no other Docker project is touched - and takes one of
three actions:

| Container state | Action |
| --- | --- |
| none in the project | `start`: the pinned stack, then the health wait |
| some not running | `compose up -d --no-build <those services>` - **service** names taken from each container's own `com.docker.compose.service` label, never its container name, because upstream pins `container_name:` on several services and the two differ. The message says out loud that a named service whose configuration or image differs is recreated |
| all running, some unhealthy | nothing is started, stopped or recreated: the health probe's own output and a bounded log tail are printed and the command exits **1**, because a repair cannot be chosen from a failed *endpoint* probe alone |

The Docker executable is now exported as `OPSACCORD_DOCKER_BIN` before any project script is called, so
`demo.sh` resolves the same binary the block probed the engine with instead of falling back to its default.

### 4. Executed in this session

| Check | Result |
| --- | --- |
| `tests/unit/test_pg_lifecycle.py` | **16 passed** (13 before; +3 for `ensure`) |
| `tests/unit/test_demo_script.py` | **18 passed** (14 before; +4 for `ensure`) |
| `tests/unit/test_baseline_checkout_capture.py` | **35 passed** (19 before; rewritten for the freshness gate) |
| Offline suite, `-m "not integration"` | **346 passed, 7 skipped, 85 deselected** (323 passed at `1e0cc1e`) |
| `ruff check .` / `ruff format --check .` | clean / 95 files already formatted |
| `mypy src/opsaccord` | `Success: no issues found in 42 source files` |
| `mypy scripts/capture-baseline-checkout.py` | `Success: no issues found in 1 source file` |
| `bash -n scripts/pg.sh scripts/demo.sh` and the block below | clean |
| Falsification: `PG_SCRIPT_UNDER_TEST` at `HEAD`'s `pg.sh` | all three new `ensure` cases **fail** (usage text, exit 1 - the command does not exist there); against the fixed script all 16 pass |
| `bash scripts/pg.sh ensure` (this sandbox) | `refusing to ensure ...`, exit **4**; no `pg_ctl` call, no pid file written, `postgres.log` mtime unchanged |
| `bash scripts/demo.sh ensure` (this sandbox) | `the Docker engine did not answer`, exit **1**; nothing started |

The new cases are one requirement each: an unknown cluster state starts nothing (no `pg_ctl` call at all,
the pid file byte-identical); a running-but-unhealthy container produces its health output and a bounded log
tail and triggers **no** `up`/`down`/`restart`; a healthy project is left completely alone; a historical
successful trace cannot satisfy the baseline while a fresh, correctly linked one can; a flag that moved,
went unsafe or could not be read blocks the verdict whichever reading failed; no traffic ends at the
deadline as `not_verified_no_trace_in_interval`; and a 500 from the store is recorded as a query failure
rather than as an empty result.

**These are offline tests with stubbed probes, and they are not host-side validation.** The clock is
injected, the store is an `httpx.MockTransport`, `pg_ctl`/`psql`/`docker` are recording stubs, and
`demo.sh health`'s `curl`/`timeout` are stubbed too, because this sandbox denies loopback sockets. They show
the *decisions* these scripts make; they do not show that PostgreSQL recovers, or that a live checkout
reaches Payment. That is what the block below is for, and the user has not run it.

### Not run, and why

- **PostgreSQL recovery** - the state is UNKNOWN to this shell (exit 4), so nothing may be started or stopped
  from here. Whether recovery is *required* is likewise unestablished; a later start's own logs are what
  would show it, and a successful start would still not be proof of complete data integrity.
- **Docker, the demo stack and the live Checkout -> Payment baseline** - `docker` is not resolvable in this
  WSL distro and every loopback connect is denied, so no live measurement exists for this revision.
- **The integration suite** - needs the live stack; not run here.
- **P1-01** - untouched. No fault was armed and no scenario ran. **Phase 1 remains PARTIAL**: the fault-free
  baseline and the fault run are both still unmeasured on this revision.

### The one next action (host shell, one block)

Start Docker Desktop first: the block probes the engine before it touches the demo and stops if it does not
answer. It preserves the pid file, the log tail and the current endpoint file *before* any mutation,
establishes the cluster's state and hands the decision to `pg.sh ensure` (which starts at most once, and only
on a proven stop), verifies the three databases and their real rows, reads the project's container state and
hands that decision to `demo.sh ensure` - stopping with the collected evidence if a *running* container is
unhealthy rather than recreating it - refreshes the endpoints and stops if one does not answer, then runs
the fresh capture and keeps its exit status as the block's own.

```bash
( set -euo pipefail
  cd /home/brentiburo/projects/OpsAccord
  deactivate 2>/dev/null || true
  source .venv/bin/activate
  export UV_PROJECT_ENVIRONMENT="$PWD/.venv"
  PYTHON="$PWD/.venv/bin/python"
  TS="$(date -u +%Y%m%dT%H%M%S)"; LOGDIR=".local/user-baseline-$TS"; mkdir -p "$LOGDIR"
  export OPSACCORD_DOCKER_BIN="${OPSACCORD_DOCKER_BIN:-/mnt/c/Users/admin/AppData/Local/Programs/DockerDesktop/resources/bin/docker.exe}"
  echo "interpreter: $("$PYTHON" -c 'import sys; print(sys.executable)')"
  echo "tested revision: $(git rev-parse --short HEAD) on $(git rev-parse --abbrev-ref HEAD)"
  echo "evidence dir: $LOGDIR"
  echo "Docker Desktop must already be running: this block starts no desktop and stops if the engine does not answer."

  echo "--- 0. preserve what this shell sees, before anything is started ---"
  git status --porcelain=v1 | tee "$LOGDIR/00-git-status.log"
  if [ -e .local/pg/data/postmaster.pid ]; then
    cp -p .local/pg/data/postmaster.pid "$LOGDIR/00-postmaster.pid.before"
    echo "preserved .local/pg/data/postmaster.pid"
  else
    echo "no .local/pg/data/postmaster.pid to preserve"
  fi
  find .local/pg/data -maxdepth 1 -name 'postmaster.pid' -printf '%p %s bytes %TY-%Tm-%TdT%TH:%TM:%TS\n' | tee "$LOGDIR/00-pidfile.log"
  if [ -f .local/pg/postgres.log ]; then
    cp -p .local/pg/postgres.log "$LOGDIR/00-postgres.log.before"
    tail -n 40 .local/pg/postgres.log | tee "$LOGDIR/00-postgres-log-tail-before.log"
  fi
  if [ -f .local/demo-endpoints.env ]; then
    cp -p .local/demo-endpoints.env "$LOGDIR/00-demo-endpoints.env.before"
    echo "preserved the endpoint file as it stands"
  else
    echo "no .local/demo-endpoints.env yet"
  fi

  echo "--- 1. the cluster's state, established before any lifecycle command ---"
  set +e
  bash scripts/pg.sh status 2>&1 | tee "$LOGDIR/01-pg-status.log"; pg_rc=${PIPESTATUS[0]}
  set -e
  echo "pg.sh status exited $pg_rc (0 running, 3 stopped, 4 unknown)"
  case "$pg_rc" in
    0) echo "running: ensure verifies it and starts nothing"
       bash scripts/pg.sh ensure 2>&1 | tee "$LOGDIR/02-pg-ensure.log" ;;
    3) echo "provably stopped: ensure starts it once"
       bash scripts/pg.sh ensure 2>&1 | tee "$LOGDIR/02-pg-ensure.log"
       echo "--- what that start logged; a clean start is not proof of data integrity ---"
       tail -n 40 .local/pg/postgres.log | tee "$LOGDIR/03-pg-recovery.log" ;;
    *) echo "STOP: pg.sh status exited $pg_rc (unknown), so this shell cannot establish the cluster's state and no start or stop is safe from it. Send $LOGDIR back." >&2
       exit 1 ;;
  esac

  echo "--- 2. connectivity, the three databases, and their real rows ---"
  bash scripts/pg.sh status 2>&1 | tee "$LOGDIR/04-pg-after.log"
  set -- "$(bash scripts/pg.sh url dev)" "$(bash scripts/pg.sh url test)" "$(bash scripts/pg.sh url scenario)"
  "$PYTHON" - "$LOGDIR" "$@" <<'PY' 2>&1 | tee "$LOGDIR/05-pg-verify.log"
import sys
from pathlib import Path
import psycopg
from opsaccord.config import to_libpq_dsn
from opsaccord.persistence.tables import Base
targets = dict(zip(("dev", "test", "scenario"), sys.argv[2:5], strict=False))
expected = set(Base.metadata.tables)
checkpoints = {"checkpoints", "checkpoint_blobs", "checkpoint_writes"}
failures, full_schema = [], 0
for name, url in targets.items():
    try:
        with psycopg.connect(to_libpq_dsn(url), connect_timeout=10) as conn:
            print(f"{name}: connected - {conn.execute('select version()').fetchone()[0].split(',')[0]}")
            tables = {row[0] for row in conn.execute("select tablename from pg_tables where schemaname='public'")}
            present = expected & tables
            print(f"{name}: {len(tables)} public tables; {len(present)}/{len(expected)} domain tables; checkpoints {bool(checkpoints & tables)}")
            if expected - present:
                print(f"{name}: missing domain tables: {sorted(expected - present)}")
            for table in ("incidents", "workflows", "tool_calls", "audit_events"):
                if table in tables:
                    print(f"{name}.{table}: {conn.execute(f'select count(*) from {table}').fetchone()[0]} rows")
            full_schema += expected <= tables
    except Exception as exc:
        failures.append(f"{name}: {exc}"); print(f"{name}: FAILED - {exc}")
print(f"databases reachable: {len(targets) - len(failures)}/{len(targets)}; holding the app's full schema: {full_schema}")
if failures or not full_schema:
    raise SystemExit(1)
print("pg verification: OK")
PY

  echo "--- 3. the engine, then this project's own container state ---"
  set +e
  "$OPSACCORD_DOCKER_BIN" version --format '{{.Server.Version}}' >"$LOGDIR/06-docker-engine.log" 2>&1
  engine_rc=$?
  set -e
  cat "$LOGDIR/06-docker-engine.log"
  if [ "$engine_rc" -ne 0 ]; then
    echo "STOP: the Docker engine did not answer, so nothing below was run. Start Docker Desktop, wait until it reports running, then re-run this whole block." >&2
    exit 1
  fi
  set +e
  bash scripts/demo.sh ensure 2>&1 | tee "$LOGDIR/07-demo-ensure.log"; ensure_rc=${PIPESTATUS[0]}
  set -e
  echo "demo.sh ensure exited $ensure_rc"
  bash scripts/demo.sh inspect 2>&1 | tee "$LOGDIR/08-demo-inspect.log"
  if grep -E 'load-generator|checkout|payment|product-catalog' "$LOGDIR/08-demo-inspect.log" >"$LOGDIR/08b-key-containers.log"; then
    cat "$LOGDIR/08b-key-containers.log"
  else
    echo "none of load-generator/checkout/payment/product-catalog appears in the inspect output (see 08-demo-inspect.log)"
  fi
  if [ "$ensure_rc" -ne 0 ]; then
    echo "STOP: demo.sh ensure exited $ensure_rc. Either a running container is unhealthy - its health output and a bounded log tail are in $LOGDIR/07-demo-ensure.log - or the start it chose did not answer. No repair is guessed here: send $LOGDIR back and the next action will be chosen from that evidence." >&2
    exit 1
  fi

  echo "--- 4. endpoints, refreshed rather than inherited ---"
  bash scripts/demo.sh endpoints 2>&1 | tee "$LOGDIR/09-endpoints.log"
  test -s .local/demo-endpoints.env
  for var in OPSACCORD_DEMO_MODE OPSACCORD_PROMETHEUS_BASE_URL OPSACCORD_JAEGER_BASE_URL OPSACCORD_FLAG_CONTROL_BASE_URL; do
    grep -q "^$var=" .local/demo-endpoints.env || { echo "STOP: $var is missing from the refreshed endpoint file" >&2; exit 1; }
  done
  set -a; . .local/demo-endpoints.env; set +a
  failed=()
  for probe in "prometheus|$OPSACCORD_PROMETHEUS_BASE_URL/-/healthy" "jaeger|$OPSACCORD_JAEGER_BASE_URL/api/services" "flagd|$OPSACCORD_FLAG_CONTROL_BASE_URL/api/read"; do
    name="${probe%%|*}"; url="${probe#*|}"
    if curl -fsS --max-time 10 "$url" -o "$LOGDIR/probe-$name.json" 2>"$LOGDIR/probe-$name.err"; then
      echo "ok   $name -> $url"
    else
      echo "FAIL $name -> $url ($(head -c 200 "$LOGDIR/probe-$name.err"))"; failed+=("$name")
    fi
  done
  if [ "${#failed[@]}" -gt 0 ]; then echo "STOP: endpoints that did not answer: ${failed[*]}" >&2; exit 1; fi

  echo "--- 5. the fresh baseline: a request that begins after this point, not one already in flight ---"
  set +e
  "$PYTHON" scripts/capture-baseline-checkout.py \
      --deadline-seconds 180 --poll-interval-seconds 10 \
      --out "reports/local/baseline-checkout-$TS.json" \
      --log "reports/local/baseline-checkout-$TS.log" 2>&1 | tee "$LOGDIR/10-baseline-capture.log"
  cap_rc=${PIPESTATUS[0]}
  set -e
  echo "capture exit: $cap_rc (0 verified, 1 not verified, 3 refused to start)"
  echo "extract: reports/local/baseline-checkout-$TS.json"
  echo "capture log: reports/local/baseline-checkout-$TS.log"
  if [ "$cap_rc" -ne 0 ]; then
    echo "Not verified on this run. The capture names which property failed and keeps the raw spans it did see: if it reports no trace arrived, the load generator's row is in $LOGDIR/08b-key-containers.log. Send $LOGDIR back with the two reports/local files rather than re-running blind." >&2
  fi
  echo "evidence: $LOGDIR"
  exit "$cap_rc"
)
```

## The live baseline-handoff pass (`6662628` handed off; the user ran it on 2026-10-15) - the cluster recovered, but the helper dropped six of twenty-five containers and that failure cost the run its remaining stages

Appended 2026-10-15. Everything above is left as it was; the corrections below are marked rather than
written over. The user ran the block from "The recovery/baseline-handoff pass" in their own terminal from
revision `6662628`; the code changes, the tests and this record are this pass's.

### The user's measured run (attributed to the user)

Run at **2026-10-15 15:40:20 +08** from `6662628` on `phase1/baseline`. Evidence:
`.local/user-baseline-20261015T074020/` (**stages 0-3 only**) and the live log `.local/pg/postgres.log`.
The user's `AGENTS.md` edit was still the only working-tree change when the block began.

| Reading | What it measured | Source |
| --- | --- | --- |
| Preserved before any mutation | `M AGENTS.md`; `postmaster.pid` 163 bytes, mtime `2026-09-29T23:23:41.437`; the endpoint file still the 2026-09-26 one | `00-*.log`, `00-demo-endpoints.env.before` |
| The cluster's state, as found | `[pg] not running` - **exit 3**, a proven stop, so `ensure` started it **once** | `01-pg-status.log` |
| The start | `pg_ctl: another server might be running; trying to start server anyway` -> `server started` -> `[pg] running on 127.0.0.1:55432 (dbs: opsaccord, opsaccord_scenarios, opsaccord_test)` | `02-pg-ensure.log` |
| **Recovery, which did run** | `database system was interrupted; last known up at 2026-09-29 10:31:49 +08`; **`database system was not properly shut down; automatic recovery in progress`**; `redo starts at 0/1A96AF70`; `redo done at 0/1A96B040`; `checkpoint complete` (wrote 0 buffers); `database system is ready to accept connections` (PID 87113) | `03-pg-recovery.log`, `.local/pg/postgres.log` |
| Connectivity and rows | **3/3** databases, **12/12** domain tables each, checkpoints present; `dev` 39/39/512/985, `test` 1/1/7/19, `scenario` 117/117/781/2081 rows; `pg verification: OK` | `05-pg-verify.log` |
| The engine | Docker `29.7.2` | `06-docker-engine.log` |
| Container state, as found | 25 containers in the project, **24 of them `exited`**; `ensure` started those 24 and nothing else; every documented endpoint answered, including `PRODUCT_CATALOG product-catalog:3550 -> 51876` and `PAYMENT payment:50051 -> 51872`; `demo.sh ensure exited 0` | `07-demo-ensure.log` |
| The catalog, moments after that start | `health=starting`, `memory_limit=20971520`, `GOMEMLIMIT=16MiB`, and its last three health checks all `ExitCode -1`, `Health check exceeded timeout (5s)` at 07:40:46, 07:40:57 and 07:41:07 UTC; the only `SERVING` checks in its log are the two from 2026-09-26 04:02 (the container was **started**, not recreated, so its health log spans both eras) | `08-demo-inspect.log` |
| Per-container state | **19 rows for 25 containers**: `cart`, `otel-collector`, `jaeger`, `flagd`, `grafana` and `prometheus` produced none - see the defect below | `08-demo-inspect.log` |
| Where the run stopped | after `08-demo-inspect.log`: no `08b-key-containers.log`, no `09-endpoints.log`, no `reports/local/baseline-checkout-*` | the evidence directory |

The **UNKNOWN** reading the previous pass insisted on is not withdrawn by this. From *this* sandbox the
cluster's state was genuinely undeterminable (`bash scripts/pg.sh status` still exits **4** here), and the
host's own shell - which can see the pid file's process and open the socket - is what resolved it to
`stopped` (**3**). That distinction is what made the resolution possible without a guess, and the two
readings are consistent rather than contradictory.

**What the recovery lines establish, and what they do not.** The cluster *was* left not-properly-shut-down:
the `received smart shutdown request` of 2026-09-29 23:23:41 has no matching `shut down` line, so the
request never completed. Recovery therefore ran, from exactly the redo position the last clean checkpoint
(2026-09-29 10:31:49 +08, `redo lsn=0/1A96AF70`) had recorded, and ended with a checkpoint that wrote
nothing. That is a short, clean redo. It is **not** proof of complete data integrity and not evidence that
nothing was lost; it shows the cluster reached a consistent state, which is what the three databases and
their row counts then confirm.

### The defect: `demo.sh inspect` dropped every container the CLI would not render

`08-demo-inspect.log` holds **19 rows for 25 containers**. For each of the other six the live CLI answered,
verbatim:

```
template parsing error: template: :1:52: executing "" at <.State.Health>: map has no entry for key "Health"
demo: inspect <name> failed
```

`cmd_inspect` printed that to stderr, printed **no row**, set `rc=1` and continued to the next name. The
six are exactly the six containers that have no healthcheck - a container with no row reads exactly like a
container that is not there, which is the same class of loss as the 2026-09-25 run (`trimPrefix` undefined)
and the 2026-09-26 run (stdin consumed by the first `inspect`, 1 container of 25). It is the third run to
lose per-container evidence, by a third distinct cause.

**The trigger is per-container, and this evidence does not explain it.** `cmd_ensure` reads
`.State.Health` with the same field form and was **accepted** for all 25 containers, including those same
six, minutes earlier in the same run (`cart exited none cart`). So the rejection is not simply "the field
form is rejected"; what differs between the two commands includes the containers' state (the six were
`exited` then and `running` now), so a state-dependent difference in the JSON is the likeliest
explanation - and it is **not** verified here. What is established is the observation, and that the
correction removes any dependence on the CLI's missing-key policy, so the trigger no longer has to be
known for the row to survive.

### The correction

* **All four optional-key reads use `index` and bind to a variable** - `.State.Health`,
  `.Config.Healthcheck` and `.Config.Labels` in `cmd_inspect` and `cmd_ensure`, and the probe-log read.
  `index` is a builtin and is not subject to the missing-key policy, and the fields read *from* the bound
  variable are safe because the variable is non-empty only when the key was there.
* **`cmd_inspect` always emits a row.** On refusal it prints the failure to stderr, sets `rc=1`, re-runs a
  brief `{{.Name}} state={{.State.Status}}` template and prints the two fields it can still claim, marked
  `per_container_detail=missing`; if even that fails it says so instead of vanishing. Dropping a row is how
  the 2026-09-26 captures came to report one container of twenty-five while exiting 0, so the row is no
  longer optional and the non-zero status still reports the degradation.
* **A guard test forbids the three dotted optional accesses** in any `--format` template in the script, so
  the next one is caught offline rather than by a live run that quietly loses containers.

The stub gained a branch that answers the **verbatim** live error and the degraded template, so the
fallback is exercised rather than assumed. The regression tests are pointed at the script through
`DEMO_SCRIPT_UNDER_TEST`, the same falsification hook `test_pg_lifecycle.py` uses: against `HEAD`'s
`demo.sh` both new tests **fail** (the container's row is absent), and against the fixed script all 20
pass. The stub stands in for "the CLI refuses this container's rich row"; it does not model the per-container
rule the live evidence leaves unexplained.

The correction also protects the *next* `ensure`, where those six containers will be **running**:
`cmd_ensure`'s old read would refuse the whole command (`inspect <name> failed, so its state is unknown and
nothing is started`) rather than merely omit a row, if the CLI's rejection is state-dependent as this run
suggests it may be.

### The block no longer treats an unrenderable container as fatal

The run did not stop because of the six missing rows alone: stage 3 ran
`bash scripts/demo.sh inspect 2>&1 | tee ...` as a bare pipeline under `set -euo pipefail`, `demo.sh
inspect` exits **1** when a row cannot be rendered (deliberately, since the 2026-09-26 pass), and `pipefail`
therefore made the pipeline non-zero, so `set -e` aborted the block immediately after
`08-demo-inspect.log` was written. That is why `08b`, `09` and the capture do not exist.

That was an unintended interaction rather than a designed stop: the block's own `08b` step already prints a
**warning** when the key containers are missing from the inspect output and continues. The block now
captures that status explicitly (`inspect_rc=${PIPESTATUS[0]}`, exactly as it already does for `ensure_rc`)
and says out loud that it is not fatal, because the capture below reads Jaeger's and flagd's own APIs and
does not depend on that snapshot. Both copies of the block (this report and the runbook) carry the same
text.

### Checks executed in this pass

| Check | Result |
| --- | --- |
| `tests/unit/test_demo_script.py` | **20 passed** (18 before; +2 for the row that survives a refusal and the dotted-access guard) |
| The three touched files | **71 passed** (`test_demo_script`, `test_pg_lifecycle`, `test_baseline_checkout_capture`) |
| Offline suite, `-m "not integration"` | **348 passed, 7 skipped, 85 deselected** (346 before) |
| `ruff check .` / `ruff format --check .` | clean / 95 files already formatted |
| `mypy src/opsaccord` | `Success: no issues found in 42 source files` |
| `bash -n scripts/demo.sh scripts/pg.sh` | clean |
| Both copies of the next-action block | extracted and `bash -n` clean; the two copies are byte-identical |
| Falsification: `DEMO_SCRIPT_UNDER_TEST` at `HEAD`'s `demo.sh` | both new tests **fail** - `test_a_container_the_cli_will_not_render_still_gets_a_row` fails at its first assertion because the row is absent, and `test_optional_inspect_keys_are_read_with_index_not_with_a_field` reports `\.State\.Health` in two templates; against the fixed script all 20 pass |
| `bash scripts/pg.sh status` (this sandbox) | exit **4** - unchanged; this shell still cannot establish the cluster's state |
| `bash scripts/demo.sh ensure` (this sandbox) | `the Docker engine did not answer`, exit **1**; nothing started |

**These are offline tests with a stubbed CLI, and they are not host-side validation.** They pin the
decisions the helper makes when a container's rich row is refused; they do not reproduce the live
per-container trigger, and no live reading in this section was produced here - every live number above is
the user's.

### Not run, and why

- **The integration suite** - needs the live stack; not run here.
- **PostgreSQL recovery, the demo stack and the fresh Checkout -> Payment capture** - this sandbox reaches
  neither Docker nor a loopback port (`pg.sh status` exits 4, `demo.sh ensure` refuses). The recovery that
  *did* run is the user's, read from their logs and from `.local/pg/postgres.log`.
- **P1-01, and the fault-free baseline** - both untouched. No fault was armed and no scenario ran. **Phase 1
  remains PARTIAL**: the fault-free baseline and the fault run are both still unmeasured on this revision.

### The one next action (host shell, one block)

**A precondition now holds that did not before: the cluster was started and verified on 2026-10-15 15:40 +08,
so `pg.sh status` should now exit 0 and `pg.sh ensure` should only verify it; the demo containers were
started by the same run.** The block is kept whole rather than shortened, because stages 0-3 are what
preserve the prior evidence and read the state before anything is touched - and because it adapts: a
`running` cluster is verified and nothing is started. Start Docker Desktop first; the block stops if the
engine does not answer. It refreshes the endpoints before the capture, and it now reports `demo.sh inspect`'s
status without letting it end the run.

```bash
( set -euo pipefail
  cd /home/brentiburo/projects/OpsAccord
  deactivate 2>/dev/null || true
  source .venv/bin/activate
  export UV_PROJECT_ENVIRONMENT="$PWD/.venv"
  PYTHON="$PWD/.venv/bin/python"
  TS="$(date -u +%Y%m%dT%H%M%S)"; LOGDIR=".local/user-baseline-$TS"; mkdir -p "$LOGDIR"
  export OPSACCORD_DOCKER_BIN="${OPSACCORD_DOCKER_BIN:-/mnt/c/Users/admin/AppData/Local/Programs/DockerDesktop/resources/bin/docker.exe}"
  echo "interpreter: $("$PYTHON" -c 'import sys; print(sys.executable)')"
  echo "tested revision: $(git rev-parse --short HEAD) on $(git rev-parse --abbrev-ref HEAD)"
  echo "evidence dir: $LOGDIR"
  echo "Docker Desktop must already be running: this block starts no desktop and stops if the engine does not answer."
  echo "The cluster was started and verified on 2026-10-15 15:40 +08, so stage 1 should report it running and start nothing."

  echo "--- 0. preserve what this shell sees, before anything is started ---"
  git status --porcelain=v1 | tee "$LOGDIR/00-git-status.log"
  if [ -e .local/pg/data/postmaster.pid ]; then
    cp -p .local/pg/data/postmaster.pid "$LOGDIR/00-postmaster.pid.before"
    echo "preserved .local/pg/data/postmaster.pid"
  else
    echo "no .local/pg/data/postmaster.pid to preserve"
  fi
  find .local/pg/data -maxdepth 1 -name 'postmaster.pid' -printf '%p %s bytes %TY-%Tm-%TdT%TH:%TM:%TS\n' | tee "$LOGDIR/00-pidfile.log"
  if [ -f .local/pg/postgres.log ]; then
    cp -p .local/pg/postgres.log "$LOGDIR/00-postgres.log.before"
    tail -n 40 .local/pg/postgres.log | tee "$LOGDIR/00-postgres-log-tail-before.log"
  fi
  if [ -f .local/demo-endpoints.env ]; then
    cp -p .local/demo-endpoints.env "$LOGDIR/00-demo-endpoints.env.before"
    echo "preserved the endpoint file as it stands"
  else
    echo "no .local/demo-endpoints.env yet"
  fi

  echo "--- 1. the cluster's state, established before any lifecycle command ---"
  set +e
  bash scripts/pg.sh status 2>&1 | tee "$LOGDIR/01-pg-status.log"; pg_rc=${PIPESTATUS[0]}
  set -e
  echo "pg.sh status exited $pg_rc (0 running, 3 stopped, 4 unknown)"
  case "$pg_rc" in
    0) echo "running: ensure verifies it and starts nothing"
       bash scripts/pg.sh ensure 2>&1 | tee "$LOGDIR/02-pg-ensure.log" ;;
    3) echo "provably stopped: ensure starts it once"
       bash scripts/pg.sh ensure 2>&1 | tee "$LOGDIR/02-pg-ensure.log"
       echo "--- what that start logged; a clean start is not proof of data integrity ---"
       tail -n 40 .local/pg/postgres.log | tee "$LOGDIR/03-pg-recovery.log" ;;
    *) echo "STOP: pg.sh status exited $pg_rc (unknown), so this shell cannot establish the cluster's state and no start or stop is safe from it. Send $LOGDIR back." >&2
       exit 1 ;;
  esac

  echo "--- 2. connectivity, the three databases, and their real rows ---"
  bash scripts/pg.sh status 2>&1 | tee "$LOGDIR/04-pg-after.log"
  set -- "$(bash scripts/pg.sh url dev)" "$(bash scripts/pg.sh url test)" "$(bash scripts/pg.sh url scenario)"
  "$PYTHON" - "$LOGDIR" "$@" <<'PY' 2>&1 | tee "$LOGDIR/05-pg-verify.log"
import sys
from pathlib import Path
import psycopg
from opsaccord.config import to_libpq_dsn
from opsaccord.persistence.tables import Base
targets = dict(zip(("dev", "test", "scenario"), sys.argv[2:5], strict=False))
expected = set(Base.metadata.tables)
checkpoints = {"checkpoints", "checkpoint_blobs", "checkpoint_writes"}
failures, full_schema = [], 0
for name, url in targets.items():
    try:
        with psycopg.connect(to_libpq_dsn(url), connect_timeout=10) as conn:
            print(f"{name}: connected - {conn.execute('select version()').fetchone()[0].split(',')[0]}")
            tables = {row[0] for row in conn.execute("select tablename from pg_tables where schemaname='public'")}
            present = expected & tables
            print(f"{name}: {len(tables)} public tables; {len(present)}/{len(expected)} domain tables; checkpoints {bool(checkpoints & tables)}")
            if expected - present:
                print(f"{name}: missing domain tables: {sorted(expected - present)}")
            for table in ("incidents", "workflows", "tool_calls", "audit_events"):
                if table in tables:
                    print(f"{name}.{table}: {conn.execute(f'select count(*) from {table}').fetchone()[0]} rows")
            full_schema += expected <= tables
    except Exception as exc:
        failures.append(f"{name}: {exc}"); print(f"{name}: FAILED - {exc}")
print(f"databases reachable: {len(targets) - len(failures)}/{len(targets)}; holding the app's full schema: {full_schema}")
if failures or not full_schema:
    raise SystemExit(1)
print("pg verification: OK")
PY

  echo "--- 3. the engine, then this project's own container state ---"
  set +e
  "$OPSACCORD_DOCKER_BIN" version --format '{{.Server.Version}}' >"$LOGDIR/06-docker-engine.log" 2>&1
  engine_rc=$?
  set -e
  cat "$LOGDIR/06-docker-engine.log"
  if [ "$engine_rc" -ne 0 ]; then
    echo "STOP: the Docker engine did not answer, so nothing below was run. Start Docker Desktop, wait until it reports running, then re-run this whole block." >&2
    exit 1
  fi
  set +e
  bash scripts/demo.sh ensure 2>&1 | tee "$LOGDIR/07-demo-ensure.log"; ensure_rc=${PIPESTATUS[0]}
  set -e
  echo "demo.sh ensure exited $ensure_rc"
  set +e
  bash scripts/demo.sh inspect 2>&1 | tee "$LOGDIR/08-demo-inspect.log"; inspect_rc=${PIPESTATUS[0]}
  set -e
  echo "demo.sh inspect exited $inspect_rc (0 every container reported its full state; 1 at least one container was reported without its per-container detail)"
  if [ "$inspect_rc" -ne 0 ]; then
    echo "note: that is not fatal here - the capture below reads Jaeger's and flagd's own APIs, not this snapshot - but read 08-demo-inspect.log beside the capture when you send the evidence back." >&2
  fi
  if grep -E 'load-generator|checkout|payment|product-catalog' "$LOGDIR/08-demo-inspect.log" >"$LOGDIR/08b-key-containers.log"; then
    cat "$LOGDIR/08b-key-containers.log"
  else
    echo "none of load-generator/checkout/payment/product-catalog appears in the inspect output (see 08-demo-inspect.log)"
  fi
  if [ "$ensure_rc" -ne 0 ]; then
    echo "STOP: demo.sh ensure exited $ensure_rc. Either a running container is unhealthy - its health output and a bounded log tail are in $LOGDIR/07-demo-ensure.log - or the start it chose did not answer. No repair is guessed here: send $LOGDIR back and the next action will be chosen from that evidence." >&2
    exit 1
  fi

  echo "--- 4. endpoints, refreshed rather than inherited ---"
  bash scripts/demo.sh endpoints 2>&1 | tee "$LOGDIR/09-endpoints.log"
  test -s .local/demo-endpoints.env
  for var in OPSACCORD_DEMO_MODE OPSACCORD_PROMETHEUS_BASE_URL OPSACCORD_JAEGER_BASE_URL OPSACCORD_FLAG_CONTROL_BASE_URL; do
    grep -q "^$var=" .local/demo-endpoints.env || { echo "STOP: $var is missing from the refreshed endpoint file" >&2; exit 1; }
  done
  set -a; . .local/demo-endpoints.env; set +a
  failed=()
  for probe in "prometheus|$OPSACCORD_PROMETHEUS_BASE_URL/-/healthy" "jaeger|$OPSACCORD_JAEGER_BASE_URL/api/services" "flagd|$OPSACCORD_FLAG_CONTROL_BASE_URL/api/read"; do
    name="${probe%%|*}"; url="${probe#*|}"
    if curl -fsS --max-time 10 "$url" -o "$LOGDIR/probe-$name.json" 2>"$LOGDIR/probe-$name.err"; then
      echo "ok   $name -> $url"
    else
      echo "FAIL $name -> $url ($(head -c 200 "$LOGDIR/probe-$name.err"))"; failed+=("$name")
    fi
  done
  if [ "${#failed[@]}" -gt 0 ]; then echo "STOP: endpoints that did not answer: ${failed[*]}" >&2; exit 1; fi

  echo "--- 5. the fresh baseline: a request that begins after this point, not one already in flight ---"
  set +e
  "$PYTHON" scripts/capture-baseline-checkout.py \
      --deadline-seconds 180 --poll-interval-seconds 10 \
      --out "reports/local/baseline-checkout-$TS.json" \
      --log "reports/local/baseline-checkout-$TS.log" 2>&1 | tee "$LOGDIR/10-baseline-capture.log"
  cap_rc=${PIPESTATUS[0]}
  set -e
  echo "capture exit: $cap_rc (0 verified, 1 not verified, 3 refused to start)"
  echo "extract: reports/local/baseline-checkout-$TS.json"
  echo "capture log: reports/local/baseline-checkout-$TS.log"
  if [ "$cap_rc" -ne 0 ]; then
    echo "Not verified on this run. The capture names which property failed and keeps the raw spans it did see: if it reports no trace arrived, the load generator's row is in $LOGDIR/08b-key-containers.log. Send $LOGDIR back with the two reports/local files rather than re-running blind." >&2
  fi
  echo "evidence: $LOGDIR"
  exit "$cap_rc"
)
```

## The second live baseline-handoff run (`b0ca505` handed off; the user ran it on 2026-10-15) - the inspect fix held against the live CLI, and the run stopped by design at an unhealthy `product-catalog`

Appended 2026-10-15. Everything above is left as it was. The user ran the block from "The live
baseline-handoff pass" a second time, from revision `b0ca505`; the code change, its tests and this record
are this pass's, and every live reading below is the **user's**.

### The user's measured run (attributed to the user)

Run at **2026-10-15 16:08 +08** from `b0ca505` on `phase1/baseline`, on the host. Evidence:
`.local/user-baseline-20261015T080803/` (**stages 0-3 only**) plus the console tail the user pasted back.
The user's `AGENTS.md` edit was still the only working-tree change when the block began.

| Reading | What it measured | Source |
| --- | --- | --- |
| Preserved before any mutation | `M AGENTS.md`; `.local/pg/data/postmaster.pid` 163 bytes, mtime `2026-10-15T15:40:20.5738821710` - run 1's own start, untouched; the endpoint file still the 2026-09-26 one | `00-*.log`, `00-demo-endpoints.env.before` |
| The cluster's state, as found | `[pg] running on 127.0.0.1:55432` - **exit 0** - so `pg.sh ensure` answered `[pg] already running` and started nothing. This is the precondition the previous handoff predicted: the state run 1 left behind is readable from the host shell, and stage 1's three-way case took the `running` branch | `01-pg-status.log`, `02-pg-ensure.log` |
| Connectivity and rows | **3/3** databases reachable, **12/12** domain tables each, checkpoints present; `dev` 39/39/512/985, `test` 1/1/7/19, `scenario` 117/117/781/2081; `pg verification: OK` | `05-pg-verify.log` |
| The engine | Docker `29.7.2` | `06-docker-engine.log` |
| The stack, as found | 25 containers, **all of them running** - run 1 started them and nothing has stopped since - so `ensure` started, stopped and recreated nothing, and its 25 rows include all six healthcheck-less containers (`cart running none cart`) with no refusal | `07-demo-ensure.log` |
| **`demo.sh inspect`, against the live CLI** | **exit 0, 25 rows, every row with its full per-container detail** - including `cart`, `otel-collector`, `jaeger`, `flagd`, `grafana` and `prometheus`, the six the previous run lost, now rendering `health=none ... healthcheck=none` (with `GOMEMLIMIT=160MiB` / `60MiB` where the service sets one) | `08-demo-inspect.log` |
| The catalog | `state=running health=unhealthy restarts=0 exit=0 oom_killed=false memory_limit=20971520 nano_cpus=0 started=2026-10-15T07:40:41.431897294Z healthcheck=["CMD","/bin/grpc_health_probe","-addr=:3550"]`, `GOMEMLIMIT=16MiB` | `08b-key-containers.log` |
| The catalog's health probes | five entries spanning `08:07:14`-`08:08:14` UTC: one `ExitCode 2` (`timeout: failed to connect service ":3550" within 1s`), then four `ExitCode -1` (`Health check exceeded timeout (5s)`) | `07-demo-ensure.log` |
| The catalog's own output | ~40 `exporter export timeout` / `processor export timeout` / `traces export` / `failed to upload metrics` lines, every one `DeadlineExceeded`, from `07:41:04` to `08:07:36` - i.e. continuously since the process started, about 27 minutes | `07-demo-ensure.log` |
| Where the run stopped | `demo.sh ensure` exited **1** and the block stopped by design, *after* `08-demo-inspect.log` and `08b-key-containers.log`: no `09-endpoints.log`, no `10-baseline-capture.log`, no `reports/local/baseline-checkout-*` | the evidence directory |

### The `b0ca505` fix is now live-validated

The previous section could only say that the row surviving a CLI refusal was pinned against a stub. This
run is the live answer, and it is stronger than the fallback path it guarded:

* `demo.sh inspect` exited **0 with all 25 rows** and full detail on every one. The fallback was never
  reached, so `per_container_detail=missing` appears nowhere: the six containers that produced no row at
  all in run 1 now render like the other nineteen.
* Both commands that read optional keys (`inspect` and `ensure`) read `.State.Health`,
  `.Config.Healthcheck` and `.Config.Labels` through `index`, and both were accepted for all 25
  containers, including the six, now that those six are **running**. The second half of
  [A26](../04%20Known%20Issues.md)'s open question - whether `ensure` would still read their state once
  they were running - is answered yes.
* The live *trigger* is still unexplained, and this run does not explain it either: the previous run's
  contrast (the field form accepted by `ensure` for those six while stopped, rejected by `inspect` while
  running) cannot be revisited, because the fixed script no longer reads them with a field form at all.
  That is the intended shape of the fix - the row no longer depends on knowing why the CLI refused.

### The stop was the designed one, not a defect

Run 1 aborted through an unintended `set -e`/`pipefail` interaction. This run stopped because the block
says so: `ensure` exits **1** when a running container is unhealthy (the `D31`/`D32` behaviour written at
`6662628`), and `07-demo-ensure.log` records what that refusal is for - nothing started, stopped or
recreated, the probe's own text, and a bounded log tail, ending in
`demo: STOP: an unhealthy running container needs this evidence before a repair is chosen`. One was a
defect; this is the block working.

It is also why stage 4 and the capture did not run. The fault-free baseline needs `PlaceOrder` on the
checkout -> payment path, and the capture requires `product_catalog_reached` - a catalog **server** span on
the attempt's path, not a caller's own client span - so a wedged catalog is a property of the path itself
rather than an incidental condition. **P1-01 was not run, and the fault-free baseline remains unmeasured
on this revision; Phase 1 stays PARTIAL.**

### What the catalog evidence does and does not settle

Measured this run, for the instance started at `07:40:41` UTC: the container is **alive and listening**
(`restarts=0`, `oom_killed=false`, and the endpoint probe for its port answers), it has been **exceeding
its own 5 s health-probe timeout continuously**, and its OTLP exports have been failing with
`DeadlineExceeded` since its first minute. So the half of D27's first row that excludes an OOM loop and a
crash loop is confirmed for *this* instance.

What is **not** measured for this instance is the rest of that row: there is no CPU/resident-set sample
pair, so neither "CPU near the core count with RSS held at the 20 MiB cap" nor "CPU near idle with RSS
well below it" can be read from this run. The two samples on file ([E28](../03%20Experiment%20Log.md):
56.50% and 28.34% of the cap, with 159.76% and 378.74% CPU) belong to the 2026-09-25 instance; they are
what refused the memory deviation for `product-catalog` in the first place, and that refusal stands - it
is not overturned by anything measured here.

One reading *is* new, and it is new because it was **retained** rather than because anything was sampled.
The container was **started**, not recreated, so its own health log still holds its previous era's last
two probes: both `ExitCode 0`, both `status: SERVING`, at `2026-09-26T04:02:30Z` and `04:02:35Z` - about
an hour **after** the `02:57:33` in-place restart [E29](../03%20Experiment%20Log.md) measured.
[D29](decisions/D29-a-restart-is-allowed-only-after-the-state-it-replaces-is-captured.md)'s revisit
condition asked for exactly that reading, and E29 could not capture it (the helper ate the per-container
state that run). It is a ring buffer of five entries, so it dates a recovery and not its extent; what it
does not support is "the restart did nothing".

### The correction this pass makes

`cmd_ensure`, when it refuses over a running-but-unhealthy container, now also collects the **two bounded
`docker stats` samples** for it, beside the health output and the log tail. This run is the reason: it
stopped exactly as designed and handed back the probe's text and the logs, but not the numbers
[D27](decisions/D27-a-repair-waits-for-the-measurement-that-names-its-cause.md) requires before a repair,
so the decision had to cost a second host run - and the state those numbers describe (a process wedged
*now*, with its restart counter and its health history intact) cannot be recovered afterwards. The
samples are read-only and the refusal is unchanged: still exit **1**, still nothing started, stopped or
recreated, and a failed sample pair is reported as a failed sample (`the sample pair exited N`) without
changing the verdict. See [D33](decisions/D33-a-stop-hands-back-the-measurement.md).

### Checks executed in this pass

| Check | Result |
| --- | --- |
| `tests/unit/test_demo_script.py` | **21 passed** (20 before; +1 for a sample pair that fails without changing the verdict, and the refusal test now asserts the pair was taken) |
| The three touched files | **72 passed** (`test_demo_script`, `test_pg_lifecycle`, `test_baseline_checkout_capture`) |
| Offline suite, `-m "not integration"` | **349 passed, 7 skipped, 85 deselected** in **both** environment variants (348 before) |
| `ruff check .` / `ruff format --check .` / `mypy src/opsaccord` | `All checks passed!` / 95 files already formatted / no issues in 42 source files |
| `bash -n` on `scripts/demo.sh` and on both copies of the block | clean, and the two copies are byte-identical (extracted and `diff`ed) |
| Falsification through `DEMO_SCRIPT_UNDER_TEST` | against `HEAD`'s `demo.sh`, exactly the two tests this pass touches **fail** - the refusal test at `the sample pair exited 0`, the new one at `the sample pair exited 1` - while the other 19 pass |
| `bash scripts/pg.sh status` (this sandbox) | exit **4** - unchanged; this shell still cannot establish the cluster's state |
| `bash scripts/demo.sh ensure` (this sandbox) | `the Docker engine did not answer`, exit **1**; nothing started |

**These are offline tests with a stubbed CLI, and they are not host-side validation.** They pin that the
stop collects the measurement, and that a failed measurement does not change the verdict; they do not
reproduce the live wedge, and no live reading in this section was produced here.

### Not run, and why

- **The integration suite** - needs the live stack; not run here.
- **The demo stack, the endpoint refresh and the fresh Checkout -> Payment capture** - this sandbox reaches
  neither Docker nor a loopback port. Nothing was started, restarted, stopped or armed.
- **P1-01** - untouched, and it cannot pass while `PlaceOrder`'s path needs the catalog to serve. **Phase 1
  remains PARTIAL.**

### The one next action (host shell, one block)

Docker Desktop must already be up, and the cluster and the 25 containers are already running, so stage 1
should verify and start nothing. The block preserves the as-found state first. When `ensure` refuses, the
measurement that names the cause is taken **before** anything is restarted; then only `product-catalog`
and `otel-collector` are restarted in place - the scope
[D29](decisions/D29-a-restart-is-allowed-only-after-the-state-it-replaces-is-captured.md) allows, and the
one whose evidence ([E28](../03%20Experiment%20Log.md), [E29](../03%20Experiment%20Log.md), and the
retained health log above) shows the catalog serving again - the same readings are taken again, and only
if the catalog then reports **healthy** from its own `grpc_health_probe` does the block refresh the
endpoints and run the checkout capture. A container outside that named pair stops the run instead of
widening the restart.

```bash
( set -euo pipefail
  cd /home/brentiburo/projects/OpsAccord
  deactivate 2>/dev/null || true
  source .venv/bin/activate
  export UV_PROJECT_ENVIRONMENT="$PWD/.venv"
  PYTHON="$PWD/.venv/bin/python"
  TS="$(date -u +%Y%m%dT%H%M%S)"; LOGDIR=".local/user-baseline-$TS"; mkdir -p "$LOGDIR"
  export OPSACCORD_DOCKER_BIN="${OPSACCORD_DOCKER_BIN:-/mnt/c/Users/admin/AppData/Local/Programs/DockerDesktop/resources/bin/docker.exe}"
  echo "interpreter: $("$PYTHON" -c 'import sys; print(sys.executable)')"
  echo "tested revision: $(git rev-parse --short HEAD) on $(git rev-parse --abbrev-ref HEAD)"
  echo "evidence dir: $LOGDIR"
  echo "Docker Desktop must already be running: this block starts no desktop and stops if the engine does not answer."
  echo "The cluster and all 25 demo containers were already running when this block was written; stage 1 should verify and start nothing."

  echo "--- 0. preserve what this shell sees, before anything is started ---"
  git status --porcelain=v1 | tee "$LOGDIR/00-git-status.log"
  if [ -e .local/pg/data/postmaster.pid ]; then
    cp -p .local/pg/data/postmaster.pid "$LOGDIR/00-postmaster.pid.before"
    echo "preserved .local/pg/data/postmaster.pid"
  else
    echo "no .local/pg/data/postmaster.pid to preserve"
  fi
  find .local/pg/data -maxdepth 1 -name 'postmaster.pid' -printf '%p %s bytes %TY-%Tm-%TdT%TH:%TM:%TS\n' | tee "$LOGDIR/00-pidfile.log"
  if [ -f .local/pg/postgres.log ]; then
    cp -p .local/pg/postgres.log "$LOGDIR/00-postgres.log.before"
    tail -n 40 .local/pg/postgres.log | tee "$LOGDIR/00-postgres-log-tail-before.log"
  fi
  if [ -f .local/demo-endpoints.env ]; then
    cp -p .local/demo-endpoints.env "$LOGDIR/00-demo-endpoints.env.before"
    echo "preserved the endpoint file as it stands"
  else
    echo "no .local/demo-endpoints.env yet"
  fi

  echo "--- 1. the cluster's state, established before any lifecycle command ---"
  set +e
  bash scripts/pg.sh status 2>&1 | tee "$LOGDIR/01-pg-status.log"; pg_rc=${PIPESTATUS[0]}
  set -e
  echo "pg.sh status exited $pg_rc (0 running, 3 stopped, 4 unknown)"
  case "$pg_rc" in
    0) echo "running: ensure verifies it and starts nothing"
       bash scripts/pg.sh ensure 2>&1 | tee "$LOGDIR/02-pg-ensure.log" ;;
    3) echo "provably stopped: ensure starts it once"
       bash scripts/pg.sh ensure 2>&1 | tee "$LOGDIR/02-pg-ensure.log"
       echo "--- what that start logged; a clean start is not proof of data integrity ---"
       tail -n 40 .local/pg/postgres.log | tee "$LOGDIR/03-pg-recovery.log" ;;
    *) echo "STOP: pg.sh status exited $pg_rc (unknown), so this shell cannot establish the cluster's state and no start or stop is safe from it. Send $LOGDIR back." >&2
       exit 1 ;;
  esac

  echo "--- 2. connectivity, the three databases, and their real rows ---"
  bash scripts/pg.sh status 2>&1 | tee "$LOGDIR/04-pg-after.log"
  set -- "$(bash scripts/pg.sh url dev)" "$(bash scripts/pg.sh url test)" "$(bash scripts/pg.sh url scenario)"
  "$PYTHON" - "$LOGDIR" "$@" <<'PY' 2>&1 | tee "$LOGDIR/05-pg-verify.log"
import sys
from pathlib import Path
import psycopg
from opsaccord.config import to_libpq_dsn
from opsaccord.persistence.tables import Base
targets = dict(zip(("dev", "test", "scenario"), sys.argv[2:5], strict=False))
expected = set(Base.metadata.tables)
checkpoints = {"checkpoints", "checkpoint_blobs", "checkpoint_writes"}
failures, full_schema = [], 0
for name, url in targets.items():
    try:
        with psycopg.connect(to_libpq_dsn(url), connect_timeout=10) as conn:
            print(f"{name}: connected - {conn.execute('select version()').fetchone()[0].split(',')[0]}")
            tables = {row[0] for row in conn.execute("select tablename from pg_tables where schemaname='public'")}
            present = expected & tables
            print(f"{name}: {len(tables)} public tables; {len(present)}/{len(expected)} domain tables; checkpoints {bool(checkpoints & tables)}")
            if expected - present:
                print(f"{name}: missing domain tables: {sorted(expected - present)}")
            for table in ("incidents", "workflows", "tool_calls", "audit_events"):
                if table in tables:
                    print(f"{name}.{table}: {conn.execute(f'select count(*) from {table}').fetchone()[0]} rows")
            full_schema += expected <= tables
    except Exception as exc:
        failures.append(f"{name}: {exc}"); print(f"{name}: FAILED - {exc}")
print(f"databases reachable: {len(targets) - len(failures)}/{len(targets)}; holding the app's full schema: {full_schema}")
if failures or not full_schema:
    raise SystemExit(1)
print("pg verification: OK")
PY

  echo "--- 3. the engine, then this project's own container state ---"
  set +e
  "$OPSACCORD_DOCKER_BIN" version --format '{{.Server.Version}}' >"$LOGDIR/06-docker-engine.log" 2>&1
  engine_rc=$?
  set -e
  cat "$LOGDIR/06-docker-engine.log"
  if [ "$engine_rc" -ne 0 ]; then
    echo "STOP: the Docker engine did not answer, so nothing below was run. Start Docker Desktop, wait until it reports running, then re-run this whole block." >&2
    exit 1
  fi
  set +e
  bash scripts/demo.sh ensure 2>&1 | tee "$LOGDIR/07-demo-ensure.log"; ensure_rc=${PIPESTATUS[0]}
  bash scripts/demo.sh inspect 2>&1 | tee "$LOGDIR/08-demo-inspect.log"; inspect_rc=${PIPESTATUS[0]}
  set -e
  echo "demo.sh ensure exited $ensure_rc (0 nothing to do; 1 a running container is unhealthy, and its health output, bounded log tail and the two CPU/memory samples are in $LOGDIR/07-demo-ensure.log)"
  echo "demo.sh inspect exited $inspect_rc (0 every container reported its full state; 1 at least one was reported without its per-container detail)"
  if [ "$inspect_rc" -ne 0 ]; then
    echo "note: that is not fatal here - the capture below reads Jaeger's and flagd's own APIs, not this snapshot - but read 08-demo-inspect.log beside the capture when you send the evidence back." >&2
  fi
  if grep -E 'load-generator|checkout|payment|product-catalog' "$LOGDIR/08-demo-inspect.log" >"$LOGDIR/08b-key-containers.log"; then
    cat "$LOGDIR/08b-key-containers.log"
  else
    echo "none of load-generator/checkout/payment/product-catalog appears in the inspect output (see 08-demo-inspect.log)"
  fi

  if [ "$ensure_rc" -eq 0 ]; then
    echo "every container is running and healthy, so no restart is taken and the block goes straight to the endpoints"
  else
    echo "--- 3b. which container(s), and whether the in-place restart is the scoped action ---"
    unhealthy_names="$(sed -n 's/^demo: [0-9][0-9]* container(s) are running but not healthy: //p' "$LOGDIR/07-demo-ensure.log" | tr '\n' ' ')"
    echo "unhealthy: ${unhealthy_names:-<none named>}"
    if [ -z "$unhealthy_names" ]; then
      echo "STOP: demo.sh ensure refused but named no unhealthy container, so the subject of a restart is not established. Send $LOGDIR back." >&2
      exit 1
    fi
    unexpected=""
    for name in $unhealthy_names; do
      case "$name" in
        product-catalog|otel-collector) ;;
        *) unexpected="$unexpected $name" ;;
      esac
    done
    if [ -n "$unexpected" ]; then
      echo "STOP: unhealthy container(s) outside D29's named pair:$unexpected. This pass scopes an in-place restart to product-catalog and otel-collector only, so nothing is restarted and nothing else is widened. Send $LOGDIR back." >&2
      exit 1
    fi
    echo "the as-found readings are already written (07, 08, 08b), so the restart below cannot erase them."
    echo "this is not a repair of a named cause: the memory deviation was refused by measurement (E28/D27), and what a restart replaces is a wedged process (D29)."
    bash scripts/demo.sh restart product-catalog otel-collector 2>&1 | tee "$LOGDIR/08c-restart.log"
    echo "--- 3c. the same readings again, bounded: is the catalog serving now? ---"
    catalog_health=unknown
    for attempt in 1 2 3 4 5 6 7 8 9 10 11 12; do
      catalog_health="$("$OPSACCORD_DOCKER_BIN" inspect --format '{{$h := index .State "Health"}}{{if $h}}{{$h.Status}}{{else}}none{{end}}' product-catalog </dev/null)"
      echo "reading $attempt of 12: product-catalog health=$catalog_health"
      if [ "$catalog_health" = healthy ]; then break; fi
      sleep 10
    done
    set +e
    bash scripts/demo.sh stats product-catalog 2>&1 | tee "$LOGDIR/08e-stats-after.log"; stats_rc=${PIPESTATUS[0]}
    bash scripts/demo.sh inspect 2>&1 | tee "$LOGDIR/08d-inspect-after.log"; after_rc=${PIPESTATUS[0]}
    set -e
    echo "after the restart: demo.sh stats exited $stats_rc, demo.sh inspect exited $after_rc (1 is the degraded-row status, not fatal here)"
    if [ "$catalog_health" != healthy ]; then
      echo "STOP: product-catalog did not report healthy within the bounded 120 s window after an in-place restart, so the baseline cannot be captured against a serving catalog. The before/after samples are in 07 and 08e, the before/after per-container state in 08 and 08d, the restart in 08c. Send $LOGDIR back." >&2
      exit 1
    fi
    echo "product-catalog reports healthy from its own grpc_health_probe, which is a serving reading - the endpoint probe alone only ever proved a listener"
  fi

  echo "--- 4. endpoints, refreshed rather than inherited ---"
  bash scripts/demo.sh endpoints 2>&1 | tee "$LOGDIR/09-endpoints.log"
  test -s .local/demo-endpoints.env
  for var in OPSACCORD_DEMO_MODE OPSACCORD_PROMETHEUS_BASE_URL OPSACCORD_JAEGER_BASE_URL OPSACCORD_FLAG_CONTROL_BASE_URL; do
    grep -q "^$var=" .local/demo-endpoints.env || { echo "STOP: $var is missing from the refreshed endpoint file" >&2; exit 1; }
  done
  set -a; . .local/demo-endpoints.env; set +a
  failed=()
  for probe in "prometheus|$OPSACCORD_PROMETHEUS_BASE_URL/-/healthy" "jaeger|$OPSACCORD_JAEGER_BASE_URL/api/services" "flagd|$OPSACCORD_FLAG_CONTROL_BASE_URL/api/read"; do
    name="${probe%%|*}"; url="${probe#*|}"
    if curl -fsS --max-time 10 "$url" -o "$LOGDIR/probe-$name.json" 2>"$LOGDIR/probe-$name.err"; then
      echo "ok   $name -> $url"
    else
      echo "FAIL $name -> $url ($(head -c 200 "$LOGDIR/probe-$name.err"))"; failed+=("$name")
    fi
  done
  if [ "${#failed[@]}" -gt 0 ]; then echo "STOP: endpoints that did not answer: ${failed[*]}" >&2; exit 1; fi

  echo "--- 5. the fresh baseline: a request that begins after this point, not one already in flight ---"
  set +e
  "$PYTHON" scripts/capture-baseline-checkout.py \
      --deadline-seconds 180 --poll-interval-seconds 10 \
      --out "reports/local/baseline-checkout-$TS.json" \
      --log "reports/local/baseline-checkout-$TS.log" 2>&1 | tee "$LOGDIR/10-baseline-capture.log"
  cap_rc=${PIPESTATUS[0]}
  set -e
  echo "capture exit: $cap_rc (0 verified, 1 not verified, 3 refused to start)"
  echo "extract: reports/local/baseline-checkout-$TS.json"
  echo "capture log: reports/local/baseline-checkout-$TS.log"
  if [ "$cap_rc" -ne 0 ]; then
    echo "Not verified on this run. The capture names which property failed and keeps the raw spans it did see: if it reports no trace arrived, the load generator's row is in $LOGDIR/08b-key-containers.log. Send $LOGDIR back with the two reports/local files rather than re-running blind." >&2
  fi
  echo "evidence: $LOGDIR"
  exit "$cap_rc"
)
```
