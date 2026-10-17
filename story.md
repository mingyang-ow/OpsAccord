# OpsAccord - the story so far

A running record of the decisions behind this project: what was chosen, what was
rejected, and why. Newest entries are at the bottom. Exact commands and measurements live
in [docs/reports/](docs/reports/); this file is the reasoning, written to be picked up
again after time away.

## What this project is

OpsAccord is an incident-response agent for a microservice shop: the OpenTelemetry
Astronomy Shop demo, pinned at `3.1.0` and run locally. It reads metrics and traces,
diagnoses a fault, proposes a fix, and asks a human before running anything that
matters. The thesis it is built to demonstrate: **a language model may propose, but it
never decides.** A deterministic policy engine, a typed tool gateway and exact human
approvals decide what actually runs, and PostgreSQL records every step.

It is a learning and portfolio project, meant to be public.

## Phase 1 decisions (2026-09-23 to 2026-10-17)

These are reconstructed from the architecture doc and the Phase 1 report, which holds the
detail behind each one.

- **Models propose, the controller decides.** No model output writes state or calls a
  tool directly. Every proposal goes through typed validation, then the policy engine,
  then the gateway. *Why:* the risky part of an operations agent is acting, not
  reasoning, so authority belongs in code that can be tested exhaustively.
- **A deterministic reasoning adapter by default.** Phase 1 runs with no model
  credentials. A paid OpenAI-compatible adapter exists but is off unless paid calls are
  enabled with a budget. *Why:* the safety machinery had to be proven before a
  non-deterministic component was added, and the tests must not cost money or vary
  between runs.
- **PostgreSQL as the source of truth, with LangGraph checkpoints for continuity.**
  There is an append-only audit trail, and approval consumption and execution are
  claimed in one transaction under an advisory lock. *Why:* process restarts at the
  approval wait and around dispatch must never repeat a side effect, and in-memory state
  or SQLite cannot show that under real concurrency.
- **An unknown outcome hands off; it is never treated as "not applied".** After a
  dispatch error, the action stays uncertain until reconciliation finds the desired
  state. *Why:* guessing "not applied" is how an agent ends up running a fix twice.
- **Payment faults are attributed on the caller's own client spans.** The fault is
  attributed on checkout's `Charge` call, not on payment's own probe. *Why:*
  `paymentUnreachable` breaks the call path while the payment server stays healthy, so a
  target-only check would diagnose the wrong service, and a wrong service is
  "verified" by the same mistake.
- **"No calls" is never "faulted" or "recovered".** Quiet telemetry makes a verdict
  inconclusive and leads to a handoff. *Why:* the demo's traffic is bursty, and a quiet
  window proves nothing either way.
- **Live evidence is never mixed with simulated evidence.** Every record carries its
  provenance, and a live result containing simulator evidence counts as a defect.
- **Phase 1 shipped as a merge commit, not a squash** (PR #1, `36f8cbf`, tagged
  `phase1`). *Why:* the 72 descriptive commits are the project's audit trail of how each
  live defect was found and fixed.
- **CI creates its own demo database** (`11d5b43`). *Why:* the isolation test proves that
  resetting the test database leaves a separate demo database alone. CI never provided
  that database, so the test reached for a local one that does not exist on a runner. No
  test was changed.

## 2026-10-17 - making the live gate less flaky

**Problem.** The first single seven-path run measured 5/7. An unhealthy
`product-catalog` failed every order before it reached payment, so P1-01 had no `Charge`
call to measure and P1-07 had none to verify. The controller behaved correctly: it
refused to conclude. The cost was a wasted 35-minute run, and the live gate depended on
the demo's luck.

**Decision.** Two changes, both in the harness and neither in the agent:

1. **A storefront preflight** that reads the frontend's catalog-call failure share and
   refuses to start (exit 2, with the recovery command) at 50% or more.
2. **Bounded synthetic orders** during the Payment plans: one every 15 s, at most 60,
   through the shop's public API, recorded on every result as `synthetic_traffic`.

**Alternatives rejected:**

- *Raise the load generator's user count.* This changes the demo's configuration, still
  gives no guarantee, and does nothing about an unhealthy catalog.
- *Let the harness restart unhealthy containers itself.* That would give an automated
  path Docker mutation in the middle of a measurement, and a restart replaces the state
  being measured. The preflight tells the operator instead.
- *Longer timeouts.* A dead catalog never recovers by waiting, and they make every bad
  run longer.

**Still open:** why `product-catalog` becomes unhealthy (`restarts=0`,
`oom_killed=false`, a 20 MB memory limit). The earlier measurement argued against a
memory-limit fix, so this needs new numbers before anything changes.

**Validated:** offline, integration and simulated checks all pass. The change has not
yet been run live; a seven-path run with the driver on is the evidence still needed.

## 2026-10-17 - going public

**Decision (pending the user's pick):** the repository will be made public as a
portfolio project. Before that it needs a licence (MIT recommended: short, permissive,
standard for portfolio work) and a pass so a stranger can read it: a README with a
diagram and a demo capture, and a short Phase 1 summary in front of the 5,000-line
report.

## 2026-10-17 - Phase 2: what to start with

**Recommendation:** start by putting a real model in the loop on the scenarios that
already exist, in simulated mode, with a small hard budget. Only then add new
scenarios, chosen by what that evaluation reveals.

**Why this first:**

- The project's thesis, "the model proposes, policy decides", has so far only been
  exercised with a deterministic stand-in. Running a real model through the same nine
  scenarios, including the two prompt-injection cases, is the first time the thesis is
  actually tested.
- It needs no new infrastructure: the adapter, the budget accounting and the scenario
  scoring already exist, and simulated mode is cheap and repeatable.
- It produces the artifact that reads best in a portfolio: a table of what the model
  proposed, what policy blocked, outcome per scenario, latency and cost - compared with
  the deterministic baseline.
- Its findings choose the next scenarios on evidence. For example, if the model often
  targets the wrong service, approval mismatch (#12) goes next; if it acts on stale
  diagnoses, "approved action becomes unnecessary" (#9).

**Considered and deferred:**

- *More benchmark scenarios first.* This is more of the same deterministic proof, so
  each new scenario adds less to the story.
- *Multi-model routing.* This is interesting, but it is premature until one model's
  behaviour is measured.
- *Production integrations.* These are out of scope for a portfolio project and would
  weaken the "isolated, safe" framing.
