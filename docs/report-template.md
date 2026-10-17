# Implementation report

## Identity and status

Repository, branch, commit, base commit, clean/dirty state, WSL distribution, coding model/provider, and overall status: READY_FOR_DISCUSSION / PARTIAL / BLOCKED. Use READY_FOR_DISCUSSION only when every technical acceptance gate passed; it is not user acceptance and it is not permission to merge.

## Delivered behavior

List working user-visible paths and important architectural choices. Distinguish implemented behavior from planned interfaces and placeholders.

## Validation evidence

| Check/scenario | Exact command | Result | Evidence path | Simulated or live |
| --- | --- | --- | --- | --- |

Include passed/failed/skipped counts, migration/restart results, action deduplication evidence, approval and constraint checks, and the upstream demo revision. Explain every skip. Include application-model usage/cost if run, otherwise mark not run.

## Optional external review, findings, and risk

State plainly whether any optional second review ran. If it did, record the tool and model actually used, the reviewed commit and any dirty diff, scope, findings with severity and file:line, reproductions, fixes, and limitations, and keep it clearly separate from the implementation agent's self-checks. If it did not run, write "Optional external review not run" and record the fallback reason. List every unresolved finding, known limitation, and unmet gate, including which evidence is simulated rather than live. Never turn unavailable review into approval.

## Remaining work and discussion

List blockers, deviations, remaining Phase 1 gates, and decisions for the user. Provide exact startup, demonstration, verification, and teardown commands. Include PR URL if one was opened. Stop for discussion rather than starting Phase 2.
