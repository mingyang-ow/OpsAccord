# Pre-fix sensitivity harness

Evidence that the two safety defects closed in `b508a0f` are real: the same tests
fail on the pre-fix revision `5cbcce2` **because of the defects** and pass on the
fixed revision.

Run it with:

```bash
scripts/pg.sh start                 # the dedicated *_test database must be reachable
scripts/prefix-sensitivity.sh
```

Exit codes: `0` as expected, `1` an unexpected result, `2` PostgreSQL was not
reachable so nothing was decided. Evidence lands in
`reports/local/prefix-sensitivity-<ref>.log` (gitignored).

## Why this file exists instead of reusing `tests/integration/test_dispatch_safety.py`

That suite can only run against the fixed revision. It imports
`opsaccord.persistence.locks` and passes `commit_context=` to
`repositories.claim_action`; at `5cbcce2` those do not exist, so it fails to
*import* there. An import failure is not a defect reproduction, so this harness is
written to be portable:

| Adaptation | Why |
| --- | --- |
| The advisory-lock key is the literal `(11296, 1)` | The registry module it comes from was added by the fix |
| `repositories.claim_action` is monkeypatched, never called with new arguments | The `commit_context` parameter was added by the fix |
| The faulty adapter is defined in the harness | The demo adapter's `action_response_error` fault switch was added by the fix |
| Only public repository functions present in both revisions are used | Keeps the tested production behaviour the old revision's own code |

`scripts/prefix-sensitivity.sh` materialises each revision read-only with
`git archive` into a scratch directory and copies this file into that copy's
`tests/prefix_sensitivity/`. The shared checkout, the current branch, and demo
databases are never modified; only the pytest session's dedicated `*_test`
database is rebuilt.

## What each test proves

| Test | On `5cbcce2` | Proves |
| --- | --- | --- |
| `test_a_committed_global_freeze_blocks_a_later_authorization` | passes | Control: the harness itself is sound, and an already-committed global freeze blocks a later authorization in both revisions |
| `test_a_global_freeze_cannot_commit_inside_the_authorization_window` | fails | Defect A2: the global `add_constraint` took no lock, so the freeze committed between the dispatcher's control read and its claim and the dispatch went out anyway. Real concurrent PostgreSQL transactions, coordinated by a bounded wait rather than by a sleep |
| `test_an_error_after_the_change_landed_stays_uncertain` | fails | Defect A1: the general handler recorded `FAILED` / `CONFIRMED_NOT_APPLIED` for an error raised after the change had already been applied |

Each failing test collects *all* of its observations and reports them in one
assertion message, so a failure names both the missing lock and the freeze that
landed inside the window.
