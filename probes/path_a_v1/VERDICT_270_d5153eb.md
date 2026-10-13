# Independent examination — Path A SPEND-8/9/10 remediation PR #270

Exact SHA: `d5153eb1fa1727427a24a983c005b9b88b718b2c` (`cursor/mainai-execution-path-a-spend-8-91c1`, parent `66c2204` = #269).
Examiner: Claude, read-only. Detached worktree, verified unchanged after every run. No live provider call: `provider.chat` or the provider's low-level `_post` is monkeypatched; everything else is real `billed_execution_chat` + `chat_with_fallback`. The production spend gate is forced on.

## Verdict: RETURN_TO_BUILDER — SPEND-9 and SPEND-10 fixed; SPEND-8 still open (three narrow facets)

What changed in `billed_chat.py`, `registry.py` and the openai/deepseek/openrouter adapters:
- the code refuses before calling when `ceil(chars/3)` exceeds the reserved prompt tokens;
- `max_tokens` equal to the completion hold is passed to the provider;
- one reservation now authorizes exactly one provider/model attempt (`authorized[:1]`);
- the chain is passed per call (`chat_with_fallback(..., chain=...)`) instead of mutating the registry.

The original SPEND-8, 9 and 10 probes now HOLD. But the Founder's dollar ceiling can still be exceeded by real spend, because the cost hold and the token holds are not reconciled. The prompt estimate is also not an upper bound, and Gemini ignores the completion cap.

| # | Required property | Result | Evidence |
|---|---|---|---|
| 1 | Prompt cost cannot exceed the reservation before the call | **GAP (SPEND-8b)** | The original 80k-char prompt is now refused with 0 paid requests. But 20,000 emoji (80,000 UTF-8 bytes, ≥ 20,000 tokens) estimate to `ceil(chars/3)=6,667 ≤ 8,000`, so the request is **paid 3/3 times** and refused afterwards as over-hold. |
| 2 | Completion capped at the provider boundary | **GAP (SPEND-8c)** for Gemini | openai, deepseek and openrouter receive `max_tokens=4000`; anthropic already honoured `max_tokens`. **`GeminiProvider.chat` ignores it**: payload keys `['contents']`, no `generationConfig.maxOutputTokens`. Gemini is priced, so a Founder can allowlist it. |
| 3 | Real paid cost cannot exceed the grant through reservation mismatch | **GAP — P1 (SPEND-8a)** | Grant `max_cost_usd=0.01`, `max_cost_per_request_usd=0.001`. Every answer stays *inside* the token holds (prompt 7,000/8,000; completion 4,000 = `max_tokens`). Result: **10 paid requests, all refused as over-hold; ledger $0.0100; real cost $0.0345 = 3.45× the grant.** The cost hold is `min(per_request, remaining)`, but the token holds (8k/4k) are not shrunk to match it. The same mismatch hits the last call of any grant whose remaining budget is below the token-hold cost. |
| 4 | One reservation = one provider/model attempt | HOLDS | `reserved_chain = authorized[:1]`. SPEND-9: first attempt times out → 1 invocation, counted, no fallback. |
| 5 | `max_requests=1` → at most one paid request, fallback and error paths included | HOLDS | SPEND-6 (1 paid) and SPEND-9 (1 invocation). |
| 6 | No fallback creates an unreserved paid call | HOLDS | Same as 4. A later retry takes a new reservation. |
| 7 | Restrictions passed per call, no process-global mutation | HOLDS | `_constrain_chat_chain` was removed; SPEND-10: after overlapping calls the registry function is the original. |
| 8 | Concurrent billable and ordinary chat do not leak state | HOLDS | SPEND-10b: while a Path A call (grant `gpt-4o-mini` only) is in flight, an ordinary `chat_with_fallback` gets the full chain (`gpt-4o`) with `max_tokens=None`, and Path A gets `gpt-4o-mini` with `max_tokens=4000`. The registry is untouched. |
| 9 | Every paid request durably counted | HOLDS (count); cost under-counted per 8a/8c | SPEND-7, 7b, 7c HOLD. Over-hold answers are still settled at the hold (the DB refuses more), so a mismatched hold under-counts real dollars. |
| 10 | SPEND-1..7, CTX, TEST, recovery intact | HOLDS | CTX-1/2, TEST-1, SPEND-1..7/7b/7c HOLD. Builder suites (30 files, including all `test_mainai_execution_*` recovery/salvage/takeover/replan/retry, provider_spend, providers, path_routing, supervisor): **391 passed**. The #269 E2E/scope probes give the same results as at #269 (E2E-A' HOLDS; SCOPE-1/CAP-1/FC-2b/ROUTE-4 still open there, unchanged by #270). CI at d5153eb: all product checks pass; only the inherited npm audit and its aggregate fail. |

## P1 SPEND-8a — cost hold and token holds are not reconciled
`_bound_reservation` caps the cost hold at `min(max_cost_per_request_usd, remaining_budget)`, but leaves the token holds at the defaults or grant maxima. The pre-call check compares tokens only.

**Fix (in `billed_execution_chat`, before reserving):**
- Compute `prompt_ub` (see 8b).
- Refuse if `estimate_cost(provider, model, prompt_ub, 1) > hold_cost`.
- Otherwise set `max_tokens = min(hold_completion, floor((hold_cost − input_cost(prompt_ub)) / output_rate_per_token))`. Refuse if the result is ≤ 0.

That makes `estimate_cost(actual) ≤ hold_cost ≤ remaining budget` by construction for priced models.

## P1 SPEND-8b — `ceil(chars/3)` is not an upper bound
Non-ASCII text (emoji, CJK, and likely dense base64/hex) tokenizes at more than one token per three characters. Use the UTF-8 byte length plus a per-message overhead as the upper bound: byte-level BPE tokens never cover less than one byte. Regression: the 20,000-emoji prompt makes 0 provider calls.

## P1 SPEND-8c — Gemini has no completion cap; thinking tokens not counted
`GeminiProvider.chat` should send `generationConfig.maxOutputTokens = max_tokens` (and a bounded `thinkingConfig.thinkingBudget` for 2.5 models). Its usage normalization drops `thoughtsTokenCount`, which Google bills as output. Count it as completion tokens; otherwise the ledger under-counts even accepted answers.

Probe: Gemini reports 9,000 candidates + 6,000 thoughts tokens. That is ≈ $0.0045 real against a $0.0018 hold, and the payload carried no cap.

The alternative is to refuse non-capping providers in `billed_execution_chat`: fail closed unless the adapter is known to honour `max_tokens`.

## Required regressions for the next candidate
- 8a: per-request cap below the token-hold cost → real cost ≤ `max_cost_usd`, and `max_tokens` is shrunk or the call refused.
- 8b: the 20,000-emoji prompt is refused with 0 provider calls.
- 8c: the Gemini payload carries `maxOutputTokens`, and `thoughtsTokenCount` is counted.
- Keep SPEND-1..10, 10b, CTX, TEST.

## Reproduce
```
git worktree add --detach ../wt270 d5153eb1fa1727427a24a983c005b9b88b718b2c
REDIS_URL=redis://127.0.0.1:<port>/0 probes/path_a_v1/run.sh ../wt270 test_path_a_268.py test_path_a_270.py   # local Postgres :5433 + Redis
REDIS_URL=... probes/e2e_v1/run.sh ../wt270 test_path_a_269.py -k "not e2e_d"
```
