# Phase 1 optional second review (Claude Code)

This file records an **optional second review** requested under the updated
handoff. It is a review of the implementation, not an approval, and it is kept
separate from the implementation agent's own self-checks (`docs/phase1.md`
completion gates, the offline suite, the live suite). No review result is used as
an acceptance gate; final review and acceptance happen with the user in the
planning conversation.

## Reviewer and scope

| Item | Value |
| --- | --- |
| Tool | Claude Code CLI, `2.1.267` (`claude -p`, headless, non-interactive) |
| Model reported by the harness | `claude-opus-5[1m]` (canonical `claude-opus-5`), 35 turns, 21,532 thinking tokens |
| Auth mode | subscription (`authMethod: claude.ai`, `apiProvider: firstParty`, `subscriptionType: pro`) - **no API key, no separately billed usage** |
| Usage reported by the harness | 40 input / 31,814 output tokens, 2.2M cache reads, ~8.3 min; the harness's list-price equivalent is $3.73, which is not a charge on a subscription |
| Reviewed revision | `b4b75aa` (diff base `3168bcf`), branch `phase1/baseline` |
| Reviewed state | committed tree only; the working tree was clean at review time |
| Access granted | read-only: `Read`, `Grep`, `Glob`, and a `Bash` allow-list of `git diff/show/log/status` |
| Deliverable | findings only, with `file:line` and a reproduction, in a fixed format |

The prompt asked the reviewer to check eight invariants and to check four named
risk points: no model-controlled execution, approval binding and revalidation,
atomic claim, crash recovery, fresh verification, budgets and retries, untrusted
text, live-vs-simulated provenance, the request/claim/dispatch/record ordering,
the verification settling loop, whether a live scenario can pass without
exercising the demo, and whether human controls live in the backend.

## Outcome

Eleven findings, all of which the implementation agent reproduced in the code
before accepting them. **All eleven were confirmed as real and fixed**, each with
a regression test that fails on the pre-fix code (verified by reverting the source
fixes and re-running: 14 tests fail without them). Nothing was accepted on the
reviewer's word alone, and no finding was dismissed without checking it.

Severity is the reviewer's; the disposition and evidence are the implementation
agent's.

### 1. `DISPATCHING` did not block `RESOLVED` - high - FIXED

`src/opsaccord/workflow/controller.py:787` (`_route_after_verify`). The gate
filtered in-flight actions to `ActionStatus.UNCERTAIN`, so an action left in
`DISPATCHING` - the outcome of a real side effect was never recorded - did not
prevent resolution, contradicting the method's own docstring. The reviewer's
reproduction: kill a worker between `apply_remediation` and the recording block,
restart, `advance()`; the claim fails, fresh evidence is healthy, and the route
returns `resolved`.

Confirmed: `repo.list_in_flight_actions` already returns `DISPATCHING` and
`UNCERTAIN`, so only the filter dropped it. Fixed by treating every unsettled
status as blocking. Evidence: `test_an_open_side_effect_blocks_resolution_even_after_real_recovery`;
with the filter reverted to the old predicate the same test observes `resolved`.

### 2. An autonomous remediation was not bound to the incident's service - high - FIXED

`src/opsaccord/tools/gateway.py:276` passed `incident_service=workflow.incident_id`
(an `inc_...` id) into `PolicyRequest`, and `policy/engine.py` never read the
field. Untrusted runbook text could therefore steer an approval-free,
allow-listed remediation onto a *different* allow-listed service than the
incident under investigation; P1-09 only covered the non-allow-listed direction.

Confirmed: the field is declared but unused, and the read path
(`gateway.py:145`) had the same defect. Fixed by passing the incident's real
service and by refusing to pre-authorise a cross-service side effect - it now
requires an exact human approval (`CROSS_SERVICE_REMEDIATION_REQUIRES_APPROVAL`).
Evidence: `test_autonomous_remediation_may_not_target_a_different_service`,
`test_the_incidents_own_allow_listed_target_stays_autonomous`.

### 3. Verification freshness degenerated when no action completed - medium - FIXED

`src/opsaccord/tools/gateway.py:516` set `collected_after_action = True` whenever
`action_completed_at is None`, so "fresh evidence after the action" became "any
evidence". Combined with `_execute` ignoring `execution.executed is False`, a
dispatch that was refused, never claimable, or left in `DISPATCHING` could still
produce `VERIFICATION_SUCCESS` and `RESOLVED` with nothing applied in that run.

Confirmed and fixed: freshness now requires a completed action to be fresh with
respect to. This also closes finding 3's route to `RESOLVED` without touching the
restart-with-a-settled-action path that the Phase 1 recovery gate needs. Evidence:
`test_verification_without_a_completed_action_is_never_a_recovery` (negative and
positive control).

### 4. A budget error after dispatch discarded the ledger row - medium - FIXED

`src/opsaccord/tools/gateway.py:400`. `record_tool_call`, `consume_step` and
`finish_action` ran in one transaction, and `consume_step` raises
`BudgetExhausted` - which rolled back the tool-call row and skipped
`finish_action`, leaving an applied change with no record and the action stuck in
`DISPATCHING`, with the exception escaping the `_execute` node.

Confirmed and fixed: the action's final status and the tool-call ledger are
persisted first, and the budget consumption is now tolerant, reporting the
uncounted step in the execution detail instead of losing the record. Evidence:
`test_a_budget_error_after_dispatch_never_erases_the_ledger` (fault-injected).

### 5. Revalidation was a hand-rolled subset of policy, with a race - medium - FIXED

`src/opsaccord/tools/gateway.py:422`. `_revalidate` checked takeover, terminal
status, four constraint kinds, rejected intent and pending approval, but not
service posture, the target/argument allow-lists, the service/target binding, or
the hard budget wall; it also read the workflow without a row lock, so under READ
COMMITTED a takeover or constraint committed between that read and the claim was
invisible.

Confirmed and fixed in both halves: the full deterministic engine now runs
immediately before the claim, and the revalidate+claim pair holds the incident
row lock, which `set_takeover`, `add_constraint` and `set_takeover`'s workflow
lock now share. Evidence: `test_a_posture_change_between_approval_and_dispatch_stops_the_side_effect`,
`test_an_exhausted_hard_cap_stops_the_side_effect_at_dispatch`, and
`test_a_human_control_that_commits_during_dispatch_is_not_ignored` - the last one
reproduces the race with two threads and, without the lock, observes the side
effect going out (`executed=True`) despite the human takeover.

### 6. Budget exhaustion during verification was fatal - medium - FIXED

`src/opsaccord/tools/gateway.py:508` caught only `PolicyBlocked`, so a
`BudgetExhausted` from `consume_step` escaped `verify_service` and the `_verify`
node: no verification row, workflow left in `VERIFYING` after a side effect, and
an aborted run instead of a handoff.

Confirmed and fixed: both read paths now treat an unaffordable verification as
inconclusive, which routes to handoff. Evidence:
`test_verification_that_cannot_afford_a_step_is_inconclusive_not_fatal`.

### 7. The soft-budget extension was inert - medium - FIXED

`src/opsaccord/persistence/repositories.py:552`. `grant_extension` incremented
`extensions_granted` but nothing consulted it, so a granted extension permitted
no work: the soft budget behaved as a hard wall, `BudgetState.can_extend` was
dead, the `BUDGET_SOFT_EXHAUSTED` verdict could never be satisfied, and the audit
event plus the run note recorded something with no effect.

Confirmed and fixed: an extension now buys a bounded allowance
(`EXTENSION_STEP_ALLOWANCE = 5`), used by both `consume_step` and
`BudgetState.soft_exhausted` so policy and the wall agree. Evidence:
`test_a_granted_extension_actually_buys_more_soft_budget`.

### 8. Live actions were persisted as simulated - medium - FIXED

`src/opsaccord/persistence/repositories.py:826` hardcoded `is_simulated=True` in
`create_action` and nothing corrected it, so an action taken against the live
stack was stored as fake while its tool-call row said otherwise.

Confirmed and fixed: provenance is now a parameter of `create_action`, stamped
from the adapter by the gateway. Evidence:
`test_actions_taken_through_a_live_adapter_are_not_persisted_as_simulated`.

### 9. The simulated adapter claimed certainty it could not have - medium - FIXED

`src/opsaccord/tools/demo_environment.py:324` returned `CONFIRMED_NOT_APPLIED`
for an idempotency key it had no record of, conflating "no record" with "the
change did not happen" (the live adapter correctly answers `UNKNOWN`). The
simulated adapter is what the acceptance suite scores, so the one crash case
where the outcome is genuinely unknown was reconciled to a confident `FAILED`
instead of remaining `UNCERTAIN` and ending in a handoff.

Confirmed and fixed. The pre-existing test
`test_crash_after_claim_before_dispatch_is_not_blindly_retried` asserted the
false certainty, which is how the behaviour survived; it now asserts `UNCERTAIN`,
`UNKNOWN`, no re-dispatch and no resolution, and
`test_reconciliation_says_unknown_when_the_adapter_has_no_record` pins the
adapter directly.

### 10. The live driver asserted its own provenance - medium - FIXED

`src/opsaccord/scenarios/live.py:487` set `LiveResult.simulated=False` as a
constant, so a live run with zero successful live reads could still be reported
as live (P1-04's plan deliberately points at an unroutable host).

Confirmed and fixed: provenance is now derived from the evidence the run actually
collected, with an explicit reason recorded when simulator evidence appears or
no evidence was collected at all. Evidence:
`test_live_provenance_is_read_out_of_the_evidence`.

### 11. The read path bypassed the typed argument gate - low - FIXED

`src/opsaccord/tools/gateway.py:119`. `read()` never called
`spec.validate_arguments`, and `_invoke_read` dereferenced `arguments["target"]`
independently of the `target` that policy evaluated, so the approved target and
the executed target were two different values.

Confirmed: no current caller diverges, but the registry is stated to be the single
gate for arguments. Fixed by validating through the registry and requiring the
two targets to agree, which now fails loudly instead of diverging. Evidence:
`test_a_read_target_must_match_its_validated_arguments`,
`test_a_read_argument_outside_the_typed_catalog_is_refused`.

## What the reviewer verified as correct

Recorded because a review that only lists defects hides the rest. The reviewer
also confirmed, by reading: no shell, HTTP or container primitive exists on the
`Environment` protocol; `apply_remediation` arguments are pinned globally and per
target, with `cart-service`/`checkout-service` mapped to an empty tuple that
rejects every value; a `shell_exec` proposal dies before policy and becomes a
handoff; numeric telemetry is coerced defensively; `run`/`resume`/`advance` all
funnel through one entry point that returns early on a terminal workflow and
derives its entry node from persisted status rather than from the caller;
`consume_step` locks the workflow row and enforces the soft gate and hard wall in
one place; the rejection of a human approval is bound to intent identity; the
settling loop calls `self.read` exclusively and never touches `claim_action`,
`apply_remediation` or `finish_action`, so it cannot repeat a side effect; human
controls and takeover are enforced in the API/policy layer with the Streamlit
console holding no authority; and `LiveEnvironment` has no simulator fallback.

## Limitations of this review

- **Nothing was executed by the reviewer.** `Bash` was restricted to read-only
  git, and one command (`git -C <path> diff --stat`) was denied by the
  permission mode, so the reviewer could not confirm diff statistics or history
  and worked from the files at `b4b75aa`. It ran no tests, no migrations and no
  live stack.
- Concurrency findings were analytical. The reviewer could not itself exercise
  the competing-worker claim race or the control-versus-claim window. The
  implementation agent then reproduced the latter deterministically (finding 5).
- Findings 4, 6 and 7 depend on budget arithmetic the reviewer computed by hand
  from `config.py` defaults rather than from an instrumented run.
- The reviewer did not read the tests (except the adversarial module), the
  migrations in full, `persistence/tables.py` in full, `reasoning/base.py`,
  `tools/factory.py` or `scripts/`, and it did not audit this report or the
  scenario JSON against its own findings.
- The review covers `b4b75aa`. The fixes in this document were written after it
  and are covered by the offline suite, the scenario suite and the live suite -
  not by the reviewer.

## Follow-up review of the safety pass: not run

The planning conversation asked for an optional focused review of the safety pass
that followed `5cbcce2` (the control gate and the uncertain-outcome contract). **It
did not run.** The subscription-backed Claude Code harness is installed on this
machine - `claude --version` reports `2.1.267 (Claude Code)`, and the user settings
select the subscription model with no API key configured - but four bounded
non-interactive attempts across two sessions (`claude -p ... --output-format text`
with timeouts of 90s, 120s, and 45s with `--debug` in the implementation session;
120s re-attempted in the delivery session, which exited `124` after the timeout
with a captured output of exactly 0 bytes) each hung and produced no stdout and no
stderr.
That is consistent with the same sandbox restriction that blocks PostgreSQL: every
socket operation raises `PermissionError [Errno 1]`, so the harness cannot reach
its API. Escalating outside the sandbox is rejected by the misconfigured automatic
approval review, which is the same failure recorded in the implementation report.

Consequences, stated plainly:

- The fixes in `5102dbe` and the safety pass on top of `5cbcce2` have **no
  independent review**. Their evidence is the implementation agent's own checks
  (`docs/reports/phase1-implementation.md`, "Safety pass after `5cbcce2`").
- The changed areas a review should look at first, if one is run later: the lock
  scope and ordering in `src/opsaccord/persistence/locks.py` and
  `repositories.add_constraint`/`set_takeover`, the authorization window in
  `ToolGateway.execute_action`, the classification in
  `tools/contracts.classify_dispatch_failure`, the live adapter's
  `action_status` inference, and whether
  `tests/integration/test_dispatch_safety.py` actually establishes the guarantees
  it claims. Two of its tests - the probe that must find the control gate held
  across revalidate-and-claim, and the freeze writer that must block while the gate
  is held - are *written to fail on the pre-fix code*. That is the implementation
  agent's design argument, not an observation: PostgreSQL cannot run in this
  sandbox, so neither the two pre-fix failures nor the post-fix passes were
  executed. They should be re-run against a database before the claim is treated
  as established.
- No independent approval was obtained, and none is implied by anything in this
  document.

### Update, validation pass

Claude was not retried, per the handoff ("do not retry Claude in this pass"). The
optional external review therefore remains **unavailable** rather than failed, and
still does not exist for `5102dbe` or `b508a0f`.

For the two pre-fix-sensitive tests named above, a route that does not depend on the
fixed revision's own test file now exists:
`scripts/prefix-sensitivity.sh`, with the harness in
`scripts/prefix_sensitivity/`. It is written to import and run against `5cbcce2`
(no dependency on the module the fix added), so its failures there are behavioural
rather than import errors. It was verified offline only as far as collection; its
execution, like every database-backed check in this pass, is outstanding and is
listed with exact commands in `docs/reports/phase1-implementation.md`,
"Validation attempt after `b508a0f`".

### Update, deadlock-correction pass

Claude was not retried again, and it could not have run: the invocation was denied
before it started by the harness's own broken approval review
(*"The supported API model names are deepseek-flash, deepseek-v4-pro, but you passed
codex-auto-review"*), and this sandbox denies socket creation outright. **The optional
external review still does not exist**, for `5102dbe`, `b508a0f`, `cb60242`, `7b66f11` or
`7becf98`. Nothing in this file is a review of them.

What did happen is recorded in `docs/reports/phase1-implementation.md`, "Independent
agent review (not the external reviewer, and not Claude)": a separate agent instance, a
different model (`gpt-5.5-codex`), read-only, reviewing `7b66f11`. It confirmed the
production lock-order audit, found no production defect, and found two real defects in
the tests this pass had just written - a guard that could not detect the very inversion
it was written to prevent, and a cleanup path that would have replaced an assertion
failure with a `join()` error. Both were fixed in `7becf98`, and the guard's failure on
the inverted composition is demonstrated in
`reports/local/lock-order-guard-falsification.log`.

### Update, configuration-isolation pass

No Claude attempt was made: the handoff for this pass says not to retry unavailable external
reviewers, and no independent review is required for a focused correction. **The optional external
review still does not exist**, for any revision on this branch.

The provider restriction is now measured twice rather than once. Two bounded attempts, in this pass,
to start a reviewer sub-agent with an explicit model override were refused with the same error:
`gpt-6-luna`, and then `gpt-6-sol`. Both messages say the same thing - *"The supported API model names
are deepseek-flash, deepseek-v4-pro, but you passed &lt;name&gt;"* - which makes the cause the provider's
model allow-list, not a single mistyped name. It is the same misconfiguration that rejects
`codex-auto-review` and therefore every escalated command, so one defect in the harness accounts for
the blocked escalations and the unselectable reviewer alike (see the known-issues note, `B5`).

What did run is recorded in `docs/reports/phase1-implementation.md`, "Independent agent review of the
configuration-isolation pass": a fresh-context reviewer sub-agent on the harness default model,
read-only, given `fe16796^..5294575` and told to falsify the pass. It confirmed the pass's core claims
- the changed tests really do fail against a leaked shell, the six new tests are non-vacuous, the
corrected counts reproduce, and `src/` is untouched - and found no production defect.

It did falsify two guarantees in the first version of the isolation fix, and both are real:

- the scrub compared names case-sensitively while `Settings` matches them case-insensitively, so
  `opsaccord_demo_mode=live` bypassed it (reproduced: the isolated defaults test failed, and
  `scripts/validate.sh --offline-only` exited 1 with three failures);
- the fixture cleared `os.environ` but not the `lru_cache` on `get_settings()`, so a value read from
  the leaked shell outlived the scrub.

It also found two narrower gaps: the competing-driver guard ran minutes before the command it
protects, and the drift-guard regex would have missed `export OPSACCORD_NEW=1` or an indented
assignment. All four are fixed at `7f8c71e` with new tests pinning the first two;
`reports/local/config-isolation-guard-falsification.log` shows those tests failing when the fix is
removed and passing when it is restored. This is the second time in this branch's history that a
fresh-context review found defects in the pass's own tests rather than in the production code, which
is the reason to keep running it even when it is not a completion gate.

That is a second pair of eyes on this pass's work, and it is still **not** the
independent external review this repository's earlier instructions wanted. The
distinction is kept because it is the difference between evidence and reassurance.

### Update, live-run-recovery pass

No Claude attempt was made. The handoff for this pass says in as many words: *"Do not retry
unavailable model reviewers in this pass. No GPT-6 reviewer setup is required."* The GPT-6 Luna
reviewer is not merely unavailable - it is **retired** (`1296b23`, recorded in
`.local/obsidian/OpsAccord/decisions/D14-development-workflow-and-retired-reviewer.md`, untracked), the repository contains no
`.codex/agents/luna_reviewer.toml`, and this environment's only configured provider is
`deepseek`. Preparing that reviewer therefore meant verifying that it does not exist and using the
recorded fallback, not registering one. **The optional external review still does not exist**, for
any revision on this branch.

What did run is a **fresh-context reviewer sub-agent on the harness default model**, read-only,
briefed to falsify the live-run-recovery pass rather than to read it. It changed no files. It
reported **9 findings with reproductions**, reported in full in
`docs/reports/phase1-implementation.md`, "Independent agent review of the live-run-recovery pass",
and all 9 are fixed and pinned. The two high-severity ones were failures a live operator would have
felt:

- an evidence-write failure raised out of the suite lifecycle, **skipping the fault restore** and
  escaping with a fault still armed;
- an arm write that was applied but never **acknowledged** left the flag armed while the record
  claimed nothing needed restoring.

The seven remaining findings were a mislabelled failure stage with a dropped observation/cleanup, an
absent flag document read as a verified clean baseline, a corroboration signal that was not
differential, an in-progress record saying `INTERRUPTED`, a missing competing-driver guard in
`scripts/run_live_scenarios.sh`, lost interrupt status when Ctrl+C met a failed cleanup, and six
labelling/provenance defects. Each was confirmed in the code before being fixed, and
`reports/local/live-recovery-falsification.log` shows the new tests failing when the fixes are
removed and passing when they are restored.

This is a third independent-brief agent review on this branch, and it is still **not** the optional
external review. It is the third time an independent review found defects in the pass that wrote
them - this time in production code rather than only in tests - which is the argument for keeping
the fallback even though it is not a completion gate.
