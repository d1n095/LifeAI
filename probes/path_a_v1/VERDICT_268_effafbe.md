# Independent examination — Path A SPEND-7 remediation PR #268

Exact SHA: `effafbebd98a8afeeca4d37b027506ed16811900` (`cursor/mainai-execution-path-a-p1-spend-7-91c1`, stacked on #266 @ `dba9390`).
Examiner: Claude, read-only. A detached worktree was used and left unchanged (verified by the probe runner). No live AI call, GitHub write, or agent launch.
The audited diff is `dba9390..effafbe`: `billed_chat.py` rewritten; the demo/cancellation tests now name their target file; a SPEND-7 regression was added.

## Verdict: RETURN_TO_BUILDER — two P1s (narrow, same file), one P2

SPEND-7 itself is fixed. Every paid answer, including refused, unpriced, over-hold, settlement-error, and provider-exception cases, is now reserved first, never released, and committed on an independent session, so it survives the job rollback. Two P1s remain:

- **SPEND-8:** the reservation does not bound the paid request, so real spend exceeds the Founder's dollar ceiling.
- **SPEND-9:** provider fallback inside the authorized chain makes extra paid requests under a single reservation.

| # | Required property | Result | Evidence |
|---|---|---|---|
| 1 | Spend reserved before every paid provider request | **GAP (SPEND-9)** | Reserve is committed before `chat_with_fallback`, but with ≥2 authorized chain entries one reservation covers N provider invocations. `max_requests=1` → invocations `['gpt-4o-mini','gpt-4o']`, 1 usage row (and it records the reserved model, not the model that answered). |
| 2 | No paid request outside the authorized provider/model | HOLDS | The chain is filtered to the grant allowlist before the call; `_constrain_chat_chain` limits fallback; an off-allowlist answer is refused and counted (SPEND-2/7). |
| 3 | Every paid request durably counted (refused, unpriced, oversized, settle error, retries/rollback) | HOLDS for request count; **GAP for cost (SPEND-8)** | SPEND-7: (paid, counted) = outside-allowlist (2,2), over-hold (2,2), unpriced 0 paid (refused at reserve). SPEND-7b: both settle attempts fail → rows stay `reserved` and still count; the ceiling stops call 3. SPEND-7c: provider exception after send → settled at the hold. Over-hold answers are counted only up to the hold, because the DB `settle_provider_spend_usage` refuses actuals above the hold. The real cost is recorded only in `evidence`. |
| 4 | Counted usage survives task/job rollback | HOLDS | The probes roll back the job session after every refusal (mirroring `run_task_execution_job`); the rows persist. |
| 5 | Request/cost ceilings prevent later calls | Requests HOLD (SPEND-6, 7b, 7c); **cost GAP (SPEND-8)** | The cost ceiling stops further calls, but only after real spend has already exceeded it (below). |
| 6 | No regression of SPEND-1..6, CTX, TEST, recovery | HOLDS | Original probes: CTX-1/2, TEST-1, SPEND-1..6 all HOLD. Builder suites (27 files: all `test_mainai_execution_*` including recovery/salvage/takeover/replan/retry/ci_wait, plus github_client, agent_orchestration, provider_spend_api, execution_envelopes_api): **371 passed**. |
| 7 | The two previously failing demo tests are green | HOLDS | `test_mainai_execution_demos` and the `cancellation` mid-flight demo pass locally and in CI (`Backend — unit/integration tests` = pass at effafbe). The fix names the target file in the task description. This is valid under CTX but does not resolve FC-2 (the AI planner still emits no `target_files`). |

CI at effafbe: every product check passes. The only failures are `Frontend — npm audit` and its aggregate, both inherited (FC-4).

## P1 SPEND-8 — the hold does not bound the paid request
`billed_execution_chat` reserves a fixed hold (the default 8,000 prompt and 4,000 completion tokens, or the grant's limits), but:
- it sends the prompt without comparing its size to the hold;
- it calls the provider without `max_tokens` set to the completion hold;
- an over-hold answer is settled at the hold, because the DB refuses more.

Path A's own code-agent context allows 80,000 characters (`code_agent_context._MAX_CHARS`), roughly 20–25k tokens. A realistic `repo_edit` prompt is therefore **knowably** over the default hold before any money is spent, yet it is paid, refused, and paid again on every retry.

Probe (real `chat_with_fallback` path, production gate on): grant `max_cost_usd=0.05`, 80,001-char prompt, the answer reports 25,000 prompt and 2,048 completion tokens.
Result: **14 paid requests, 0 accepted; the ledger counted $0.0500; real cost $0.0697 (1.39×, and 0.0197 USD above the Founder's ceiling)**. Every refusal was predictable before the call. The overrun ratio grows with the prompt size and the model price.

**Minimal fix (in `billed_chat.py`):**
- Before reserving or calling, estimate prompt tokens conservatively (e.g. `ceil(chars/3)`). If the estimate exceeds the hold's prompt tokens, refuse with `ExecutionSpendError` and spend nothing.
- Pass `max_tokens=<hold completion tokens>` to the provider, so the completion cannot exceed the hold.

## P1 SPEND-9 — fallback makes unreserved paid requests
`_authorized_chain` keeps every allowlisted chain entry, and `chat_with_fallback` tries them in turn under one reservation:
- a first request that times out after sending (and may be billed), followed by a second provider request, consumes one counted request;
- the second invocation is a paid request with no reservation of its own, which breaks property 1 and the `max_requests` ceiling;
- the hold is priced for `authorized[0]`, so a pricier fallback model is under-held.

**Minimal fix:** invoke only the reserved pair (`authorized[:1]`), i.e. no fallback inside one reservation. Alternatively, reserve each attempt separately. Record the answering provider/model on the row.

## P2 SPEND-10 — process-wide monkeypatch of the provider registry
`_constrain_chat_chain` swaps the module global `provider_registry.resolve_chat_chain` and restores it LIFO by capture. With two overlapping calls in one process (e.g. two plan requests in the API server), B captures A's lambda as the "original" and restores it after A exits. **The registry stays pinned to goal A's allowlisted chain for every later chat caller in the process**, ordinary user chat included. While a call is in flight, unrelated chat calls also see that goal's chain.
- Probe result: `restored=False; leaked chain=['gpt-4o-mini']`.
- This narrows rather than widens the chain, so it causes no spend escape, but it is a correctness and availability defect.
- **Fix:** pass the chain explicitly (e.g. a `chain=` parameter on `chat_with_fallback`) instead of mutating module state. The SPEND-9 fix (a single pair) makes this simple.

## Minor (non-blocking)
- On a provider exception, `attempted` is lost: `chat_with_fallback` does not return it when raising, so the evidence shows `[]`.
- Unchanged from #266: the hard-coded `tests/backend/test_ok.py` default, and the test conftest disabling the spend gate for unlisted modules. The examiner probes force the production default explicitly.

## Required regressions for the next candidate
- SPEND-8: a prompt larger than the hold makes 0 provider calls, and real cost ≤ `max_cost_usd`; `max_tokens` is passed.
- SPEND-9: `max_requests=1` with a two-entry allowlist makes ≤ 1 provider invocation.
- SPEND-10: after overlapping calls, `registry.resolve_chat_chain` is the original function.
- Keep SPEND-1..7, 7b, 7c, CTX, TEST, and the demo suites.

## Reproduce
```
git worktree add --detach ../wt268 effafbebd98a8afeeca4d37b027506ed16811900
REDIS_URL=redis://127.0.0.1:<port>/0 probes/path_a_v1/run.sh ../wt268 test_path_a_268.py   # local Postgres :5433 + Redis
```
`test_path_a_268.py` adds SPEND-7b/7c/8/9/10. It sets `mainai_execution_require_provider_spend=True` because the target's test conftest turns it off for unlisted modules. Without that, post-exhaustion calls take the unbilled path and the numbers are invalid.
