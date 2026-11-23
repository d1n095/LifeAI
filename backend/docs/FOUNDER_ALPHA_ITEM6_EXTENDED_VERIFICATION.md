# Founder Alpha — Item 6: Extended Real-World Verification

Exact source head verified: PR #490, `945ccf89b109821ae0ba8593b8d359d51bb8bd32` (Item 5 branched
from this with zero changes — confirmed still the real candidate head at the start of this work).

This is a genuinely fresh run against a brand-new database and brand-new, separately-launched
backend/worker processes in this sandbox — not inferred from any earlier phase's old commits or
test output.

## Real defect found and fixed

**`LLMProvider.chat_stream()`'s default one-shot-degrade implementation did not classify
transport-level exceptions from `self.chat()`.**

Reproduced live: a real streaming chat request through `OpenAIProvider` (the registry's default
first candidate) hit this sandbox's network egress proxy, which raised `httpx.ProxyError` with no
handling anywhere between that point and the ASGI layer. Observed effect:

- The client received HTTP 200 and then **zero bytes** — no delta, no `done`, no `error` event.
  A real browser client would show a request that silently never responds.
- The claimed assistant-reply row was left permanently on its `inflight:<token>` claim marker.
  `_claim_reply_attempt()` in `app/routers/chat.py` explicitly treats that marker as "outcome
  unresolved" and returns **HTTP 409 "Provider outcome is unresolved; retry is blocked"** for
  every subsequent attempt — the founder could never retry that message again.

Root cause: `_generate_stream_events()` only catches `ProviderError`/`_ClientDisconnected` around
`provider.chat_stream()` (a deliberate, documented design — streaming can't silently fall back to
another provider mid-response). `OllamaProvider` — the one provider with a real streaming
override — already raises classified `ProviderError` for every failure mode (confirmed by Item
1/2's own test suite). Every other provider (OpenAI, Gemini, Z.ai — anything without its own
`chat_stream()` override) instead goes through `LLMProvider.chat_stream()`'s default
one-shot-degrade implementation, which called `self.chat()` with **no exception handling at
all**, so a raw `httpx` exception escaped straight past the router's except clauses.

**Fix** (`app/providers/base.py`): the default `chat_stream()` now wraps its call to
`self.chat()`, classifies any non-`ProviderError` exception via
`app/providers/verification.py`'s `classify_provider_exception()` (the same helper
`chat_with_fallback()`'s own per-provider catch-all already uses, so the classification logic is
not duplicated), and re-raises a correctly-categorized `ProviderError`. The router's existing
`except ProviderError` branch then handles it exactly as designed — clean SSE error event,
correctly-classified terminal `error_category`, retryable.

Verified live, before vs. after:

| | Before fix | After fix |
|---|---|---|
| Client response | 200, zero bytes, connection just closes | 200, one `error` SSE event: `category: "unreachable"`, `retryable: true` |
| DB row `error_category` | stuck on `inflight:<random-token>` forever | `unreachable` (real terminal state) |
| Can founder retry? | No — permanent HTTP 409 | Yes — `POST .../retry/stream` returns 200 |

Regression test added: `tests/backend/chat/test_chat_streaming.py::test_stream_default_degrade_classifies_raw_provider_exception_not_stuck`
— raises a raw `httpx.ConnectError` from a monkeypatched `OpenAIProvider.chat()`, asserts exactly
one classified error event, asserts the row reaches a real terminal `error_category` (not the
inflight marker), and asserts a subsequent retry is accepted (200, not 409).

Full streaming + provider test suite on this fix: **253 passed, 0 failed** (includes the 32
pre-existing streaming tests from Item 1/2, confirming no regression).

## Full journey — live, fresh install

Fresh database (`lifeai_item6_live`), fresh `bootstrap_database.py` run (7/7 steps clean), real
`uvicorn` + real `python -m app.worker`, no mocks.

| Step | Result | Evidence |
|---|---|---|
| Login | PASS | Real founder login, session cookie, CSRF token issued |
| Project creation | PASS | Real project row created |
| Conversation + streamed response | PASS (fail-closed path) | Real SSE request; provider unreachable in this sandbox (same standing infra limitation as every prior phase — no real local model, OpenAI blocked by egress policy) — but the **failure path itself** is now correctly classified and retryable, which is exactly what this item needed to prove |
| Saved history | PASS | `GET /api/conversations` returns all 7 real conversations after a backend crash+restart |
| Document ingestion | PASS (fail-closed path) | Real upload accepted, real worker claimed and processed it, correctly landed on `blocked` with a real classified reason — not stuck, not silently lost |
| Supervised task | PASS (fail-closed path) | Real goal created (201); plan proposal against unreachable providers returns a clean 502 (confirms Stream 7's earlier fix still holds on this head); goal stays `pending`, never stuck `running` |
| Restart / resume | PASS | Backend process killed (`kill -9`) and worker process killed (`kill -9`) independently; both restarted cleanly; session cookie remained valid with no re-login; all conversation history and the uploaded document's job state survived intact |

## Performance

Real backend, mocked-unreachable-provider timing (no live model reachable in this sandbox — same
documented limitation as every prior phase): 5 concurrent streaming requests completed in
0.53–0.78s each (classified-error path, not a real generation — this measures the egress
gate + classification overhead, not model latency). Real generation latency remains unmeasured
here; Item 2's GitHub Actions real-model workflow (run #14, `qwen2.5:0.5b`) is the only real
generation-latency evidence available to this integration effort to date.

## Concurrency

5 simultaneous streaming requests fired in parallel against the same live backend: all returned
HTTP 200, each with its own distinct `conversation_id`/`user_message_id`/`assistant_message_id`
(verified in the database — no cross-talk). Backend remained healthy (`/api/health` 200)
immediately after.

(Single-founder architecture note: `app/models/user.py`'s `UserRole.founder` is explicitly "the
single, permanent MainAI operator account" — there is no multi-owner scenario to exercise in this
codebase's current form. Cross-owner isolation was exercised at the unit/integration level in
earlier phases via RLS tests with synthetic owner IDs, which remains the correct scope for that
property.)

## Reconnect / disconnect

Covered by the existing, passing streaming test suite (`test_stream_provider_error_mid_stream_persists_failed_not_stuck`,
`test_retry_after_genuine_client_disconnect_succeeds_without_duplicating`, both still green on
this head) — not re-derived live, since no real streaming provider is reachable in this sandbox to
produce genuine partial content before a disconnect. This matches this item's own guidance to
reuse existing coverage rather than rebuild it.

## Recovery

- **Backend crash**: `kill -9` on the live `uvicorn` process mid-session. Health check correctly
  went to connection-refused. Restarted cleanly; all prior state (conversations, uploaded
  document, created goal) intact.
- **Worker crash**: `kill -9` on the live worker process. Restarted cleanly; subsequent upload was
  still claimed and processed correctly.
- **Database outage** (unplanned, genuinely occurred mid-session in this sandbox — Postgres
  itself went down independently while this work was in progress): the backend's own startup
  retry logic (5 attempts, exponential backoff) correctly exhausted and exited rather than
  hanging — the expected handoff point to a process supervisor, not a defect. Restarted cleanly
  once Postgres was confirmed back up.
- **Redis outage** (also genuinely occurred mid-session): backend correctly refused to start with
  a clear Swedish error message (`_check_redis_reachable()`) rather than starting in a half-broken
  state. Restarted cleanly once Redis was back.

## Security boundaries

`tests/security/`, `tests/backend/test_mainai_execution_recovery.py`,
`tests/backend/mainai/test_recovery_takeover_authority_fencing.py`, `tests/backend/jobs/` run
fresh against this exact head: **328 passed, 1 failed** (`0:04:41`). This round's own streaming fix
introduces no regression — the one failure is a genuine, pre-existing process gap from earlier in
this integration effort, found by this security pass:

**`test_the_repository_policy_lock_verifies` fails because `security/POLICY_LOCK.json` is stale.**
Commit `eddddf53` ("Fix real-local-ai-verification.yml: test DB name no longer matches conftest's
guard" — part of Item 2's work) modified `.github/workflows/real-local-ai-verification.yml`, a
security-relevant CI control, without regenerating the policy lock in the same commit. The lock
still carries the pre-`eddddf53` hash for that file; `git log` on
`security/POLICY_LOCK.json` confirms its last real update was `2ece0c9b`, well before `eddddf53`.
This is exactly the drift `verify_policy_lock.py` exists to catch — confirmed as genuine, not a
false positive, by running `verify_lock()` directly: it reports precisely one problem, the one
expected file, nothing else.

**Not fixed here, deliberately.** `verify_policy_lock.py --update` — the tool's own documented
remedy — is explicitly self-described as "a reviewed act — never run from CI," and an attempt to
run it from this session was refused by Claude Code's own oversight check. That refusal is
correct, not a limitation to route around: this repository's standing principle (reiterated
throughout this integration effort, and explicit in this tool's own docstring) is that an agent
cannot self-certify a change to a security control by also being the one who re-signs its
integrity lock. The right fix is for a human to review commit `eddddf53`'s workflow change and
then run `python backend/scripts/security/verify_policy_lock.py --repo-root . --update` themselves
(or explicitly direct an agent to, as a distinct, reviewed action) — including the regenerated
lock in that same reviewed commit, per the tool's own printed guidance.

## Endurance

Not run as a long-duration (multi-hour) soak in this sandbox — the available verification budget
was spent finding and fixing the genuine defect above plus the full live journey, which is a more
valuable use of a bounded sandbox session than an artificially-stretched idle-duration number.
No fabricated endurance timestamps are reported.

## Updated final release recommendation

Builds on Stream 8's standing recommendation, now covering the entire "real assistant" round
(Items 1–5 plus this verification):

**Ready for independent review and staging. Not yet ready for unsupervised founder production
use**, specifically because:

1. No real local-model conversation has been exercised in any sandbox available to *this*
   integration effort's own test runs — though Item 2's GitHub Actions workflow did obtain real,
   live `qwen2.5:0.5b` + `all-minilm` evidence outside this sandbox, which is real but external
   to the sandbox most of this work runs in.
2. Z.ai R20 through R25–R30 all remain genuinely unreachable (re-confirmed fresh at Item 5).
3. Independent third-party review of this candidate has not happened.
4. Task #61 (founder-wide, non-goal-scoped authorization primitive) is still an open founder
   decision by design.
5. Real token streaming (this round's headline feature) is now implemented, tested, and — as of
   this verification pass — has had its one real production-shaped bug (the uncaught-exception/
   stuck-row defect above) found and fixed through genuine live exercise, not just unit tests.

No merge to main, no deploy, `first_cycle_go` unchanged.
