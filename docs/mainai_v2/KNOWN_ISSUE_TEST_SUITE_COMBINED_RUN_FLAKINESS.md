# Known Issue — Non-Deterministic Errors on Large Combined `pytest` Runs

**Status:** OPEN, unresolved, evidence-classified. Out of scope for the MainAI Coverage/
Workforce/Capability-Learning candidate that discovered it -- not caused by that candidate's
own code (see evidence below). Do not fix speculatively inside that candidate's branch.

## Observed

Running the broad regression command below against `tests/backend/mainai/`:

```
pytest tests/backend/mainai/ -k "mainai_vision or mainai_research or mainai_cognitive_ops or
mainai_coverage or mainai_workforce or project_entit or resource_intelligence or
work_candidate or capability_reality or agent_coordination or agent_dispatch or
multi_agent" -q
```

(~505-510 tests selected) produced, on one occasion: **183 passed, 322 errors**. Every
subsequent attempt to reproduce the exact same command against a fresh test database has
produced a clean result:

| Attempt | Result |
|---|---|
| 1 (original) | 183 passed, 322 errors |
| 2 (rerun, same command, fresh DB) | 506 passed, 0 errors |
| 3 (rerun, same command, fresh DB, `--tb=line`) | 506 passed, 0 errors |
| 4 (rerun, same command, fresh DB, with live `pg_stat_activity` sampling every 2s throughout) | 506 passed, 0 errors |

**Reproduction rate: 1 failure in 4 identical attempts (25%).**

## What was ruled out, with direct evidence

**PostgreSQL connection-pool exhaustion** was the leading hypothesis (this project's
`tests/conftest.py::_clean_tables` fixture is `autouse=True` and creates + disposes a brand
new `sqlalchemy.create_engine()` on every single test, which could plausibly cause transient
connection-count pressure across a ~500-test run). This was tested directly, not assumed:

- `SHOW max_connections;` on the local Postgres 16 instance: **100**.
- `pg_stat_activity` was sampled every 2 seconds for the full duration of attempt 4 (a clean,
  506-passed run) via a separate polling process. **Peak concurrent connections observed: 12**
  (baseline idle was 6-9). This is nowhere near the 100-connection ceiling.

**Connection-pool exhaustion is therefore ruled out by direct measurement**, at least for a
clean run. It remains conceivable (but unproven) that the original failing run had a
different, unmeasured connection profile -- but there is no evidence for this, and the
hypothesis should not be asserted as the cause without support.

## What was NOT ruled out (isolation results)

Two independent, non-overlapping-enough sub-selections of the same combined `-k` query were
each run to completion and were both clean:

- The subset matching the *previous* round's proven-clean selector (no
  `agent_coordination`/`agent_dispatch`/`multi_agent`), now including this round's own new
  `mainai_coverage`/`mainai_workforce` tests: **452 passed, 0 errors**.
- The subset matching only `agent_coordination`/`agent_dispatch`/`multi_agent` plus the
  pre-existing package keywords (no `mainai_vision`/`mainai_research`/`mainai_cognitive_ops`/
  `mainai_coverage`/`mainai_workforce`): **274 passed, 0 errors**.
- Three individual files most likely to interact under load
  (`test_work_candidates.py`, `test_agent_dispatch_foundation.py`,
  `test_multi_agent_work_coordination.py`) run together directly: **68 passed, 0 errors**.

None of these narrower runs reproduce the original failure, and none of the code built across
this or the prior four MainAI V2 rounds (`mainai_vision`, `mainai_research`,
`mainai_cognitive_ops`, `mainai_coverage`, `mainai_workforce`) appears in any of the isolated
sub-runs' errors, because those sub-runs never showed any errors at all.

## Classification

**LOW-CONFIDENCE, NON-REPRODUCING, ENVIRONMENTAL/TIMING-TRANSIENT.** Candidate explanations
that remain plausible but unconfirmed:

- A rare timing-dependent interaction in one of the explicitly concurrency/expiry-focused
  tests in `test_multi_agent_work_coordination.py` (`test_lease_takeover_fences_old_worker`,
  `test_takeover_concurrency_race`, both of which manipulate lease `expires_at` timestamps
  directly) that only manifests under specific host-scheduling timing not present in any
  rerun.
- Transient host-level resource contention on the machine this session ran on (other
  processes, OS scheduling jitter) at the exact moment of the original run, unrelated to any
  Postgres or application-level resource.

**NOT attributable to any code in the MainAI Coverage & Omission Intelligence / Dynamic
Workforce Orchestration / Capability Learning candidate** (branch
`claude/mainai-v2-coverage-workforce-capability`) or any of the four prior MainAI V2 rounds --
confirmed by two independent clean isolation sub-runs and three further clean full
reproductions of the exact original command.

## Blast radius

Unknown/unconfirmed. If real, it would affect any CI or local run combining a large enough
subset of `tests/backend/mainai/` in one process -- not specific to any one package. Given the
25% observed reproduction rate over 4 attempts and full clean reruns otherwise, it does not
appear to block normal development (isolated package-level test runs, which is the normal
workflow for every MainAI V2 round in this session, are unaffected).

## Recommended next step (not performed here — out of scope)

If this recurs: capture a `pytest -p no:cacheprovider --tb=long -x` run with `pg_stat_activity`
AND `SELECT * FROM pg_locks` sampled at a higher frequency (every 250ms) around the failure
window, and check for `psycopg2.OperationalError`/deadlock-specific error text in the actual
(currently unavailable, since the original run's failure detail was not captured before this
investigation began) traceback. Do not attempt a speculative fix without first capturing that
traceback.
