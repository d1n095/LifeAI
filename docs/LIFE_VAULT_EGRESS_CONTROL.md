# Life Vault / External-AI Egress Control — Threat Model, Architecture Map, Blocker Map

Grounded in the actual codebase at integration tip `4261787` + `bf843cb` (PR #167, the first
composed autonomous milestone, and the Supervisor goal-worktree ownership fix that followed
it) — not aspiration. Every claim below is a file:line citation against real code, verified
directly, not inferred from names. See `docs/CLAUDE_LIFE_VAULT_EGRESS_LANE.md` for the
founder's own brief and standing invariants this document implements against.

## Why this exists now

PR #167 made real, non-deferred external-provider calls reachable for the first time in
MainAI's autonomous development loop (reserve → real fake-CI provider call → settle → plan →
execute). Before that, every provider call in the *autonomous* path stopped at
`PROVIDER_SPEND_NOT_AUTHORIZED`. That boundary is now live. Separately, and entirely
independently of #167, the founder's own **chat feature has been sending raw document content
to real external providers with zero classification or filtering since it was built** — this
document treats that as the higher-priority, pre-existing exposure, not a hypothetical.

**REPO-WRITE AUTHORITY != PROVIDER-SPEND AUTHORITY != EGRESS AUTHORITY.** This project already
has two of those three built and hardened (execution-authorization-envelope, migration 0057;
provider-spend authorization, migration 0060). The third — what content is allowed to leave
the process toward an external model at all — does not exist yet, anywhere, in any form beyond
a syntactic secret-pattern scrubber.

## Architecture map — every real egress point today

`app/providers/base.py`'s `LLMProvider.chat()`/`.embed()` is the one interface every provider
(`openai_provider.py`, `anthropic_provider.py`, `gemini_provider.py`, `deepseek_provider.py`,
`openrouter_provider.py`, `ollama_provider.py`) implements. Every provider *instance* in the
codebase is obtained through `app/providers/registry.py` — verified directly: no call site
anywhere constructs a provider class directly. `registry.py` is therefore already the correct
architectural location for a universal gate, but today it does **zero content inspection** —
`get_provider()`/`resolve_active()`/`chat_with_fallback()` only resolve which provider/model to
use, never look at what's being sent.

| # | Call site | Trigger | What's actually in the payload | Existing filtering |
|---|---|---|---|---|
| 1 | `app/routers/chat.py:_attempt_assistant_reply()` (~L104-206) | Founder sends a chat message | `context_block` = raw RAG chunk text, verbatim, joined straight from `retrieve_context()`'s hits (`f"{_label(h)}\n{h['text']}"`, L148); `history` = up to 20 prior `MessageModel.content` rows, verbatim (L168-179); both folded into `messages` sent via `chat_with_fallback(db, messages)` | **NONE.** Verified directly — no `redact()`/`_redact_value()` call anywhere in this function. |
| 2 | `app/rag/ingest.py:97` `provider.embed(chunks, ...)` | Every document import | Raw document chunk text | None |
| 3 | `app/rag/retrieve.py:19` `provider.embed([query], ...)` | Every search/chat retrieval | Raw query text | None |
| 4 | `app/rag/media_import.py:205` | Audio/video import | Raw transcript chunk text | None |
| 5 | `app/routers/library.py:615` | Manual library search | Raw search query | None |
| 6 | `app/safe_planner/service.py:safe_provider_prompt()` (L556) → `app/provider_planning/service.py:RegistryPlanningAdapter.propose()` | Autonomous MainAI development-planning tick (the #166/#167 path) | `instruction = operator.redact(...)` (L558) — **regex-redacted, fail-closed placeholder substitution on detection** (not partial-send); `context_references` = reference **metadata only** (`object_type`/`object_ref`/`basis`/`reason`/`path`, never raw document/claim bodies) from `assemble_planning_context()`, additionally passed through `operator._redact_value()`; fixed capability allowlist (`DEVELOPMENT_CAPABILITIES`) | **Partial, real, but syntactic only.** `redact()` (`app/development_operator/service.py`) is a regex secret-pattern scrubber (`key=value`, `Bearer ...`, standalone secret-shaped tokens) — it has no concept of business sensitivity, VAULT, or IP_PROTECTED. |
| 7-13 | `chat_with_fallback()` callers: `mainai_execution/execution_job.py` (L147,203,320), `mainai_execution/planner.py:344`, `mainai_execution/lesson_conflicts.py:80`, `rag/claims.py` (L251,404 — raw document text for claim extraction), `agent_orchestration.py` (L189,252), `jobs/handlers/corpus_review.py:203`, `routers/workbench.py:89` | Various autonomous ticks / founder actions | Task/goal descriptions, repo file content, raw document text (claim extraction is a *second*, separate raw-content path beyond the embedding one) | None found in any of these files |

**Not yet swept, flagged for the ledger design specifically:** `app/providers/verification.py`,
`app/providers/transcription.py`; whether chat history (#1) can compound exposure across turns
(a past assistant reply that already echoed retrieved content gets replayed verbatim as
`history` on the *next* turn, so redacting only the current turn's fresh retrieval would not
un-expose what a prior turn already sent).

**Existing patterns worth reusing, not reinventing:**
- `operator.redact()`/`_redact_value()` — real, working, reusable as *one layer*, not sufficient alone (syntactic, not semantic).
- `safe_provider_prompt()`'s fail-closed placeholder pattern (redact-or-refuse-the-whole-field, never partial-send-and-hope) — exactly the right posture to generalize.
- `Document.classification` (`app/models/document.py`) is a **topic** taxonomy (vision/architecture/decisions/history/security/general), manually set, **not** a sensitivity/egress tier. Do not confuse the two axes; a `security`-topic document is not automatically `SECRET`-tier, and a `general`-topic document is not automatically safe to disclose raw.
- **No existing sensitivity/egress classification field anywhere in the schema.** This is new work.
- **No existing disclosure ledger.** `provider_spend_usage_events`/`UsageLog` record token counts and cost only — zero content. Cannot answer "what did provider X ever actually receive." New work.
- RLS, `SupervisorScope`/`OperatorContext`/execution-authorization-envelopes are a **completely different, already-solved system** — outbound-to-*filesystem* write authority, not inbound-to-*provider* disclosure. Do not conflate; nothing here duplicates that work.

## The single last trusted boundary

**`app/providers/registry.py`**, but the gate must inspect payload *content*, not just resolve
provider/model. Concretely two real choke points cover every site above:

1. **`chat_with_fallback()`** — covers rows 1, 7-13 (14 of ~20 real call sites) automatically, zero caller changes needed once the gate is inserted inside `registry.py` itself.
2. **The 6 remaining direct `.chat()`/`.embed()` calls** (row 6's `RegistryPlanningAdapter.propose()`, rows 2-5's `.embed()` calls) — a small, mechanical refactor to route through `registry.py` too (e.g. new `embed_with_policy()` alongside `chat_with_fallback()`), not a redesign.

Gating inside `LLMProvider.chat()`/`.embed()` themselves (the abstract base/6 subclasses) was
considered and rejected: that scatters policy logic across 6 files instead of 1, and a provider
subclass has no access to the `db`/owner/task context a real policy decision needs.

## Data classification (proposed, not yet built)

`PUBLIC · INTERNAL · PRIVATE · CONFIDENTIAL · VAULT · SECRET · NEVER_EGRESS`, orthogonal
`IP_PROTECTED` flag. Derived data inherits the source's sensitivity unless an explicit,
provable declassification policy says otherwise — summarizing VAULT content does not
auto-downgrade it.

## Ranked blockers

| ID | Blocks a real Vault guarantee? | Status | Scope |
|---|---|---|---|
| **V1** | **YES — highest exposure, now gated** | **CLOSED** (`chat_with_fallback(owner_id=...)`, PR "Wire the Life Vault egress gate into chat_with_fallback() (V1)") | Chat endpoint (row 1) now passes `owner_id`/`purpose="chat"` into `chat_with_fallback()` — every attempt is routed through `enforce_egress_policy()` before any provider is called; NEVER_EGRESS-shaped retrieved content is denied outright (never partial-sent, never treated as a per-provider failure to fall back from); real disclosure-ledger entry per attempt. Still no semantic classification (V5) or history-replay handling (V7) — see those rows. |
| **V2** | YES — partially closed | **PARTIAL** (`embed_with_policy()` wired into rows 2+4, PR "Wire the Life Vault egress gate into document/media embedding (closes V2 document-ingestion half)") | Document-chunk embedding (`rag/ingest.py`) and transcript-chunk embedding (`rag/media_import.py`) — the highest-volume raw-content paths (whole documents, not short queries) — are gated via a new `app/providers/registry.embed_with_policy()`. Query embedding (`rag/retrieve.py`, `routers/library.py` — rows 3+5) remains OPEN: those call sites have different exception-handling shapes (graceful degrade-to-text-search on any failure) that need their own careful pass, not a rushed bolt-on. |
| **V3** | YES — mitigated | **CLOSED** (PR #170) | `provider_planning`/Safe Planner boundary (row 6) — the autonomous path from #166/#167. Gated via the same `enforce_egress_policy()`. Still no semantic classification (V5). |
| **V4** | YES | OPEN, not started | 6 remaining `chat_with_fallback()` callers (rows 7-13 minus row 1, now closed) — zero redaction found in any of them. The `owner_id` kwarg exists on `chat_with_fallback()` now; wiring the remaining callers is mechanical, not a redesign. |
| **V5** | Foundational, blocks everything else | OPEN, not started | No sensitivity/egress classification field exists anywhere in the schema. |
| **V6** | Foundational, blocks the founder's own audit requirement | **CLOSED** (migration 0062, PR #170) | `provider_disclosure_events` ledger exists and is written by every gated call site (V1, V3 today). Still doesn't cover V2/V4's ungated sites — "what has provider X ever received" is only a complete answer once those close too. |
| **V7** | Compounding risk on V1 | OPEN, not started, needs design | Chat history replay: a past turn's already-sent content re-appears verbatim in every later turn's `history`. V1's gate re-evaluates and re-redacts the FULL history (including prior turns) on every single call — structurally stateless, so this is less severe than originally scoped (nothing is exempt from re-inspection), but a prior turn's content that was already ALLOWED once is disclosed again on every subsequent turn; no design exists yet for whether/how that should be suppressed. |

## First PR scope (smallest unowned foundation, per the founder's own deliverable order)

**In scope:** a genuine default-deny egress policy gate — a real decision point that:
1. Requires an explicit, structured authorization to exist before ANY call proceeds (default
   deny, not default allow-unless-blocked).
2. Detects and blocks/redacts SECRET-shaped content (hardens/reuses `redact()`), fail-closed
   (refuse the field, never partial-send).
3. Records a durable disclosure-ledger entry for every decision (allow AND deny), even before
   full classification exists — provider, purpose, requester, decision, timestamp, content hash.
4. Enforces the prompt-injection doctrine structurally: whatever `context_references`/retrieved
   content flows through the gate is data-typed, never capable of expanding what the SAME call
   is authorized to disclose (extending, not duplicating, this codebase's own established
   `PROPOSED != AUTHORIZED` / `EXTERNAL CONTENT IS DATA, NEVER AUTHORITY` family of invariants).
5. Wired into the `provider_planning`/`safe_provider_prompt()` boundary (V3) first — the
   narrowest, best-understood, already-partially-compliant call path, and the one the founder's
   brief explicitly named. Built as a real `registry.py`-level primitive so V1/V2/V4 can adopt
   it later without a redesign, but **not** wired into chat.py/rag/* in this first PR.

**Explicitly out of scope for PR #1** (named here so the gap is documented, not silently
dropped): V1 (chat endpoint), V2 (RAG/embedding), V4 (other `chat_with_fallback` callers), the
full PUBLIC..NEVER_EGRESS classification schema applied to real `Document`/`KnowledgeClaim`
rows (V5 — the gate ships with a narrower, structural SECRET-detection + explicit-allow
mechanism first, not the full taxonomy), V7 (history-replay). These remained real, named,
tracked gaps at PR #1 time.

**Status update:** V1 closed in a follow-up PR ("Wire the Life Vault egress gate into
chat_with_fallback() (V1)"). V2 partially closed in a further follow-up ("Wire the Life Vault
egress gate into document/media embedding") — document-chunk and transcript-chunk embedding
(the whole-document raw-content paths) are gated via `embed_with_policy()`; query embedding
(short user-typed search/retrieval queries) is intentionally deferred, see V2's own row above.
See the ranked-blockers table above for current status. Remaining open, named gaps: the query-
embedding half of V2 (`rag/retrieve.py`, `routers/library.py`), V4 (6 remaining
`chat_with_fallback()` callers — mechanical now that the `owner_id` kwarg exists), V5 (full
classification schema), V7 (history-replay design).

**Unrelated finding surfaced during V2's regression run (2026-08-27):**
`tests/backend/rag/test_bootstrap_hardening.py::test_section3_account_erasure_hard_deletes_documents_entirely`
fails with `psycopg2.errors.InsufficientPrivilege: permission denied for function
erase_own_agent_coordination_children` — reproduced on a completely fresh database, on the
clean merged tip (`5a80310`) with none of this lane's changes present, so it predates and is
unrelated to any Life Vault work. Not fixed here per this repo's own "don't blend unrelated
fixes into the current branch" discipline (`CLAUDE.md`) — flagged for a separate, dedicated fix
(likely a privilege-grant gap in `app/rls.py` for `erase_own_agent_coordination_children`,
migrations 0046/0047).

## Attack list coverage plan (from the founder's brief; PR #1 realistic subset)

Testable against the boundary this PR actually gates today:
- **#3** (retrieved "ignore rules" content never gains authority) — directly testable: feed
  the gate content containing instruction-shaped text and prove it changes nothing about the
  decision itself.
- **#4** (SECRET in context → block/redact before adapter) — directly testable against the
  hardened detector.
- **#6** (cross-owner retrieval impossible) — testable as a structural assertion against the
  gate's own owner-scoping.
- **#7** (a new/expanded request needs its own fresh decision, never inherits a prior one).
- **#8** (retry/idempotency never bypasses the original decision).
- **#9** (switching provider A→B never inherits A's disclosure decision).
- **Disclosure ledger completeness** (every call, allowed or denied, produces exactly one
  ledger entry).

Deferred to later PRs (require infrastructure not yet built): **#1/#2/#5** (presuppose an
interactive provider-initiated retrieval loop `provider_planning` does not have today — it is
one-shot prompt→response, not a tool-call retrieval loop), **#10-14** (logs/exceptions/
caches/embeddings/telemetry sweep — needs V2 in scope first), **#15** (local-model policy
tier — needs a real local/Qwen provider wired in first, tracked separately, see the
agent-team-Qwen decision).
