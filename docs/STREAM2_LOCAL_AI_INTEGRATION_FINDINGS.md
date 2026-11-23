# Stream 2 — Real Local AI Integration: Findings

Date: 2026-11-23
Base: PR #480, `claude/founder-alpha-canonical-candidate`, head `d0de9efc9393d215f2dc84c6336e2e41e1064aca`
Branch: `claude/founder-alpha-stream2-local-ai`

## Summary

Every code-level item on this stream's checklist was already correctly implemented and is
covered by passing tests. No application code changes were made — there was nothing real to
fix. One genuine architecture gap was found and is flagged below rather than invented around.
The real-model network block is re-confirmed with precise, reproducible evidence.

## Checklist, item by item

| Item | Status | Where |
|---|---|---|
| Model discovery/readiness checks | Already correct | `app/providers/verification.py`: `verify_provider()`/`ensure_verified()`/`verify_now()` — a real minimal probe call, not just a key-presence check; cached with a bounded TTL; local providers get a longer timeout budget for cold model loads (`LOCAL_PROVIDER_NAMES`, `local_verification_timeout_seconds`) |
| Real chat completion | Already correct, but **not streaming** — see gap below | `app/providers/ollama_provider.py::chat()` |
| Conversation persistence | Already correct | `app/routers/chat.py::_attempt_assistant_reply()` persists via the same path for both initial send and retry |
| Provider failure classification | Already correctly granular | `_http_error_category()` (unsupported vs unreachable), `classify_provider_exception()` (invalid_key/rate_limited/unreachable/unsupported/not_configured) |
| Cancellation and timeout | Timeout: yes (bounded per-call timeout, `num_predict` mapped from `max_tokens`). Client-disconnect cancellation: not applicable — there is no long-lived streaming connection to cancel (see gap below); a non-streaming call is already timeout-bounded | `ollama_provider.py::chat()` |
| Local-only routing enforcement | Already correct | `app/providers/registry.py::chat_with_fallback()` — filters to `_LOCAL_PROVIDERS` before any attempt, raises `ProviderError(category="local_only_mode_no_local_provider")` on an empty chain, unconditional (ignores `owner_id`) |
| Recovery after model restart | Already correct | `app/worker.py::_requeue_blocked_jobs()` — re-verifies the active provider each poll cycle and flips `blocked` imports back to `pending` automatically; covered in `tests/backend/providers/test_provider_verification.py` |
| Model capability validation | Already correct | `verify_provider()` checks chat vs embedding support per role (`role == "chat"` vs the embed branch); a provider that can chat but not embed is `unsupported` for that role, not silently accepted |
| Embedding generation + pgvector compatibility | Already correct | `ollama_provider.py::embed()`/`_embed_legacy()` (batch + legacy single-call fallback for pre-0.1.36 servers), native-width vectors zero-padded by `app/rag/embedding_space.py` rather than truncated |
| Frontend model-status indicators | Already correct | `frontend/app/(shell)/chat/page.tsx` renders `context_status` (including `awaiting_provider`/`blocked_provider`) from the backend's real `ContextStatusOut`, not a client-side guess |

## Real gap found: no token-by-token streaming exists

The directive named "real chat completion and streaming" as one item. These are not one
feature — they're two, and only the first exists. `OllamaProvider.chat()` hardcodes
`"stream": False` and `app/routers/chat.py`'s `POST /chat` endpoint returns one complete
`ChatMessageOut` per call; there is no SSE/WebSocket endpoint and no chunked response
anywhere in the backend. The frontend's only `ReadableStream` reference
(`frontend/app/api/[...path]/route.ts`) is a generic request-body proxy, unrelated to chat
rendering.

This is an **existing architectural choice, not a regression and not something broken by
recent work** — every PR in this integration effort (#468–#480) built on top of it unchanged.
Implementing real token streaming (backend SSE/WebSocket endpoint, provider-level streaming
support, frontend incremental rendering) is a genuine feature addition spanning three layers,
not a bug fix, and is out of scope for a single bounded verification/repair stream — attempting
it here risked a half-finished implementation smuggled into what's meant to be a focused
fix pass. Flagging it for an explicit, separately-scoped decision rather than inventing it.

## Real-model network block: precise re-confirmation

Re-verified fresh in this sandbox (not assumed from earlier phases):

- `registry.ollama.ai` / `ollama.com`: DNS `gaierror: Name or service not known` — not in this
  sandbox's resolvable set at all (distinct from a proxy policy denial).
- `github.com` (considered as an alternate binary source, e.g. a GitHub release asset):
  DNS resolves, but the HTTPS request gets **403 from the egress proxy** — per
  `/root/.ccr/README.md`, this specifically means "destination host/path not allowed for this
  session," not a GitHub-side block, and the README's own policy is "do not retry or route
  around it — report the blocked host."
- `registry-1.docker.io` (Docker Hub, as a path to `ollama/ollama:latest`): DNS resolves and
  HTTPS actually connects (401 from the registry API — real connectivity, needs auth for the
  manifest, as expected unauthenticated). **Moot anyway**: `docker info` shows no daemon
  running in this sandbox (`Cannot connect to the Docker daemon at unix:///var/run/docker.sock`),
  and starting one is outside this sandbox's permissions.
- No `ollama` binary is pre-installed, no server is listening on `11434`, and no cached model
  weights exist anywhere searched.

Conclusion: a real local-model conversation genuinely cannot be exercised in this sandbox.
This is the same boundary Phases C/D/H already found, now with host-by-host evidence. Per the
dispatch policy, this is not repeatedly retried — it's characterized once, precisely, and
reported. Everything up to that boundary (routing, auth, egress policy, fail-closed behavior,
persistence, status reporting) is verified; the boundary itself is accurately reported as
BLOCKED, not worked around or faked.

## Test evidence

- `tests/backend/providers/` (Ollama fail-closed, embed batch, routing security hardening): 71
  passed, 0 failed.
- `tests/backend/providers/ + tests/backend/chat/ + tests/backend/rag/test_embedding_space.py`
  (full combined run, fresh Postgres 16 + Redis, this exact head): **333 passed, 0 failed**
  (260.82s).

## Recommendation

If real local-model evidence is required before founder use, the only paths are: (a) a sandbox/
environment with the Ollama registry host(s) and/or GitHub release-asset downloads allowlisted,
or (b) a pre-baked image that already bundles a small model's weights. This fork does not
request either itself — that's an environment-level decision for the orchestrating session.
