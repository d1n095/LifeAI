# Independent examination — #291 (FO-7 base fetch, FO-6 salvage, rehearsal)

- **Candidate:** PR #291 (draft), branch `cursor/first-cycle-execution-completion-91c1`.
- **Examined SHA:** `0b1871bf63e4c9b6d0d32633af097b281e644679`. Two commits on frozen #290 `6c98814`: `dd15271` (production) and `0b1871b` (records).
- **Head and CI:** confirmed through the unauthenticated public API. The commit was fetched anonymously (isolated git config, empty credential helper, public HTTPS).
- **Examiner:** Claude, read-only.
  - Hermetic git and synthetic credentials; credential suites sandboxed.
  - No paid calls, merge, deploy or Recall.
  - **Local evidence only.**

## Verdict: RETURN_TO_BUILDER — FO-7 closed; FO-6 (P1, re-rated) not closed

| Item | Result |
|---|---|
| **FO-7** (approved-base fetch at worktree creation) | **CLOSED.** `fetch_origin_base_sha` types network, timeout and integrity failures and verifies `FETCH_HEAD == base_sha`. FO-7x (exact function) and FO-7 (end to end: terminal, zero paid, one job) pass; both failed on #290. FO-7n passes. |
| **FO-6** (bare RuntimeError / crash at the push boundary on an envelope-governed Path A goal) | **NOT CLOSED.** The #291 classifier change (a `finalized` checkpoint proves the push; local HEAD ≠ base counts as a commit) leaves the governed path unchanged: governed recovery declines the takeover, the task becomes `retryable_failed`, the retry tick re-dispatches and the paid code agent re-runs. Result: **3 jobs, 3 code-agent calls, task stranded `ready`.** FO-6x, RL-1..3, FO-6b and `untyped_boundary_exception` all fail, identically to #288–#290. See `../grant_contract_v1/INTEGRATION_CONTRACTS.md` §3 for the corrected evidence and root cause. |
| Exact-SHA CI | all 12 required checks + aggregate `success` in push and pull_request suites |
| Everything else (full `cursor` profile) | FO 18 origins, LR-1/2, RS-0..3, FC-1..3, CS-1..3 (concurrent STOP vs draft PR), ER-4 (erasure after a full cycle), credential canary 6/6, sandboxed harness clean, review/STOP, task authority, spend, FC-4: all pass. **788 regression tests passed, 0 failed.** |
| Transport contract (Codex-owned, listed) | TC-0/TC-2 pass; **TC-1** (global credential helper consulted) and **TC-3** (`GIT_ASKPASS` invoked) fail on production `_run_git` |

**Required for FO-6:**
- At the publication boundary (push and open_pr), map **every** exception to a typed fail-closed error, including a bare `RuntimeError`.
- For a dead job on a governed goal whose last durable checkpoint is `work_result`/committed but not `finalized`:
  - observe the remote ref (spec: never replay);
  - finalize without re-running the code agent, or fail closed;
  - never use the generic retry, and never leave the task `ready`.
- Gate: FO-6x, RL-1..3, FO-6b.

## Reproduce

```
git worktree add --detach ../wt291 0b1871bf63e4c9b6d0d32633af097b281e644679
REDIS_URL=redis://127.0.0.1:6390/0 probes/first_cycle_acceptance_v1/run_acceptance.sh cursor ../wt291
```
