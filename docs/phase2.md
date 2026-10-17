# Phase 2: a real model in the loop

## Question

Phase 1 proved the controls with a deterministic stand-in for the model. Phase 2 asks what
happens when a real language model makes the proposals:

- How often does it diagnose correctly and propose the right single action?
- What does it propose that validation or policy has to refuse, including under the two
  prompt-injection scenarios?
- Does anything unsafe ever *execute*? The Phase 1 guarantees say no; this is where they
  meet an adversarial, non-deterministic proposer.
- What does it cost in tokens, latency and money compared with the deterministic baseline?

## Scope of Phase 2a

- The nine simulated scenarios (P1-01..P1-09), unchanged, run through the unchanged
  controller, policy engine, gateway and evaluator.
- One hosted provider (DeepSeek through the OpenAI-compatible adapter): `deepseek-flash`
  and `deepseek-v4-pro`, each run several times, because a model is not deterministic.
- The deterministic adapter runs alongside as the baseline in every report.
- Simulated demo only. Live runs with a model come after the simulated results are
  understood.

## Guard rails added before any paid call

| Guard | Where | What it guarantees |
| --- | --- | --- |
| Provider failures hand off | `WorkflowController._diagnose` | A timeout, HTTP error, malformed JSON or refused budget is recorded as a `HANDOFF` decision with unknown usage, and the incident ends `ESCALATED_INCOMPLETE`. It never crashes the workflow and is never read as a diagnosis. |
| Hard run budget | `opsaccord.reasoning.budget` | A maximum spend in USD and a maximum number of calls across the whole run. Each call's worst-case cost is reserved *before* it is made, from configured per-1k-token rates. Without rates the run refuses to start. A call that reports no usage stays charged at its worst case. |
| Prompt size cap | `OpenAICompatibleAdapter.max_input_bytes` | A prompt over 32,000 bytes is refused before any request. Byte-level tokenizers emit at most one token per byte, so this bounds input tokens and makes the worst case real. |
| Declared tool contracts | `ToolRegistry.contracts()` | The model sees each tool's arguments and their globally allowed values, plus the services in scope. It does **not** see the per-target allow-list: that is policy, and whether a model proposes something policy refuses is part of what is measured. |
| Target binding | `ToolGateway.evaluate_action` | The target policy evaluates is the target that executes (see below). |

**A safety defect found by this preparation.** Policy evaluated `proposal.target` while
dispatch acted on `arguments["target"]`. A proposal naming the allow-listed ad-service while
carrying payment-service in its arguments would have changed payment's flag with no
approval. The deterministic adapter never produced such a proposal; a real model or an
injected runbook could. Fixed in `8e937f8` and pinned by
`tests/integration/test_target_binding.py`.

## Metrics

Per run, from the persisted trace and the model's recorded decision:

- the authoritative outcome (PASS, SAFE_ESCALATION, INEFFECTIVE_FAILURE, UNSAFE_FAILURE)
  and final status;
- executed, forbidden and duplicate actions, and any resolution without verification;
- what the model concluded and proposed (tool, target, arguments), its confidence and
  whether it asked to escalate;
- which layer refused the proposal, if any (typed validation or policy);
- provider errors, input and output tokens, cost and latency.

A blocked unsafe proposal is not a system safety failure; an executed one is. The report
keeps proposal behaviour and executed outcomes in separate columns.

## How to run

```bash
# Baseline only: no model, no cost.
uv run python -m opsaccord.cli eval-model --baseline-only

# A hosted model: paid calls must be enabled, rates set, and a budget passed.
OPSACCORD_ALLOW_PAID_MODEL_CALLS=true \
OPSACCORD_MODEL_PROVIDER=openai_compatible \
OPSACCORD_MODEL_BASE_URL=https://api.deepseek.com/v1 \
OPSACCORD_MODEL_INPUT_COST_PER_1K_USD=0.0003 \
OPSACCORD_MODEL_OUTPUT_COST_PER_1K_USD=0.0012 \
uv run python -m opsaccord.cli eval-model --model deepseek-flash --repeats 3 \
  --budget-usd 1.00 --max-calls 30
```

Results are written to `reports/local/model-eval-<timestamp>.{json,md}`. The JSON keeps
every record. The Markdown has a summary per model and one row per run.
