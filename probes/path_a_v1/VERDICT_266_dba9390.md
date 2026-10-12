# Independent re-examination — Cursor Path A P1 remediation PR #266

Exact SHA: `dba9390a6ea2a167300bfd87a52922b6b5153f54` (`cursor/mainai-execution-path-a-p1-fix-91c1`, stacked on #264 @ `21ff3cc`).
Original examiner evidence: `e5d2784e48c40ffe3667c26cc52ad3eeecaee877` (VERDICT_264_21ff3cc.md).
Examiner: Claude, read-only. A detached worktree was used and left unchanged; no live AI call, GitHub write or agent was used.

## Verdict: RETURN_TO_BUILDER (one remaining P1, narrow fix)

All nine original findings are fixed and re-verified. The fix for SPEND-2/3/4 introduced a new P1: the paid provider call now happens BEFORE any reservation, and a refused answer is released without being recorded, so real spend escapes the grant (SPEND-7).

| Probe | #264 @ 21ff3cc | #266 @ dba9390 |
|---|---|---|
| CTX-1 edit of a file the model never saw | GAP | HOLDS — refused (not an explicit target) |
| CTX-2 large file silently truncated | GAP (1,053/2,000 lines lost) | HOLDS — oversized file not shown and write refused |
| TEST-1 repo_edit without targeted_tests at plan time | GAP | HOLDS — `create_plan` rejects it |
| SPEND-1 AI call without a grant | GAP | HOLDS — default `require_provider_spend=True`, 0 provider calls |
| SPEND-2 fallback outside the grant allowlist | GAP (mis-recorded as openai) | HOLDS — refused |
| SPEND-3 unpriced answer settled at $0 | GAP | HOLDS — refused |
| SPEND-4 over-reservation raw InternalError | GAP | HOLDS — `ExecutionSpendError` |
| SPEND-5 task stranded `running` | GAP | HOLDS — task `retryable_failed`, job `failed` |
| SPEND-6 exhausted grant (max_requests=1) | GAP (3 paid calls) | HOLDS — exactly 1 paid call, then refused before calling |
| **SPEND-7 paid-but-refused answers counted against the grant** | GAP | **GAP — P1**: per case (outside allowlist / unpriced allowlisted model / over-hold), 3 paid provider requests and **0** usage rows. `billed_execution_chat` calls `chat_with_fallback` first, then refuses and `_release_quietly()`/rolls back; the grant is never consumed, so every retry/replan pays again outside the ledger. Over-hold is the realistic trigger (a long code-agent answer). |
| Legacy GitHub write route | non-draft PR via Contents API | HOLDS — `prepare_github_pr` refuses while `MAINAI_LEGACY_PREPARE_PR_WRITES_ENABLED=false` (default) even with `GITHUB_WRITE_ENABLED=true`, and opens drafts when explicitly enabled |

Builder suites run locally at dba9390: 152 passed (path_a_p1_remediation, path_a_activation, agent_orchestration, github_client, executor, planner, recovery_demos).

## Minimal fix for SPEND-7
Reserve BEFORE the provider call, and record what was actually spent:
- Reserve against the grant first (ceiling, allowlist and per-request checks happen before money is spent), and keep the call on the allowlisted provider/model (no fallback outside the grant).
- When a paid answer is refused (allowlist, unpriced, over-hold), settle or record it as consumed spend (e.g. conservative or estimated cost, status counted) instead of releasing it, and do it in a transaction that is not rolled back by the job's failure path.
- Regression: the SPEND-7 probe expects paid requests ≤ counted usage rows for all three cases.

## Minor notes (not blocking)
- `PlannedTaskSpec.__post_init__` injects a hard-coded `tests/backend/test_ok.py` target when `verification_plan` is omitted. Only direct constructions hit it (the AI planner passes `[]`, which is rejected), and the file does not exist, so it fails closed; better to remove the placeholder.
- The test conftest disables the spend gate for every module except the two named ones; production defaults are unaffected.
- Explicit target paths still come from task prose (planner text). That is acceptable for the supervised cycle.

## Reproduce
```
git worktree add --detach ../wtpatha2 dba9390a6ea2a167300bfd87a52922b6b5153f54
REDIS_URL=redis://127.0.0.1:<port>/0 probes/path_a_v1/run.sh ../wtpatha2     # local Postgres :5433 + Redis
```
