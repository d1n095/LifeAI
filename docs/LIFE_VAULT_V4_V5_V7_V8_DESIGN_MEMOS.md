# Life Vault — Design Decision Memos: V4 (ownerless callers), V5 (classification schema), V7 (history replay), V8 (ledger provenance)

**Status: DESIGN OPTIONS ONLY. No migration, no schema change, no code in this document's own
PR. No `owner_id` is invented anywhere below.** Written per the founder's own night-run
directive as the required secondary deliverable, grounded in the actual current codebase (not
aspiration) — every claim below was independently traced against real files, most via a
dedicated read-only investigation pass, not inferred from names. See
`docs/LIFE_VAULT_EGRESS_CONTROL.md` for the live ranked-blockers tracker this memo feeds
recommendations into; that document remains the authoritative status tracker, not this one.

---

## V4 — The two genuinely ownerless `chat_with_fallback()` callers

### The problem, precisely

`app/mainai_execution/lesson_conflicts.py:80`'s `detect_conflict()` and
`app/agent_orchestration.py:189,252`'s `dispatch_task()`/`review_task()` call
`chat_with_fallback(db, messages)` with **no** `owner_id`/`purpose`/etc kwargs at all. Because
`app/providers/registry.py`'s gate is only entered `if owner_id is not None`, these two callers
skip the egress gate **completely** today — not narrowly, not partially, zero policy inspection
of any kind on whatever gets sent.

This is not an oversight the way the other V4 callers were (those had a real owner sitting one
hop away and just weren't wired yet — closed in PR #175). These two are structurally different:

- **`EngineeringLesson`** (`app/models/mainai_execution.py:302-337`) has no `owner_id`,
  `goal_id`, `task_id`, or `project_id` column. Its `source_ref` field is a free-text string,
  not a foreign key. Its own docstring states this is deliberate: *"Deliberately NOT
  RLS-protected — founder-wide project/system knowledge, not per-owner personal data, same
  reasoning as ProjectNote itself."*
- **`AgentTask`** (`app/models/agent_task.py:55-78`) has one nullable FK,
  `source_note_id → project_notes.id`. Traced one hop further: `ProjectNote`
  (`app/models/project_memory.py:52-86`) **also** has no `owner_id` — its own docstring: *"Not
  RLS-protected — like `provider_config`/`provider_verification_checks`... founder-wide project
  state, not per-user data, in a founder-only system."* The one FK `AgentTask` does have
  terminates in another ownerless table. There is no owner anywhere in the chain for either
  caller, not a missing wire-up.

Both tables' own authors already made the same call the founder's original V4 framing
anticipated: these are genuinely global, cross-project, founder-wide knowledge stores, not
per-owner personal data. Inventing an `owner_id` column on either table to make V4's plumbing
uniform would misrepresent what the data actually is — a lesson learned from Project A's
failure is exactly supposed to generalize to Project B; scoping it to a single owner would be
architecturally backwards, not just extra work.

### An existing pattern worth naming

`app/founder.py` already defines `FOUNDER_USER_ID = uuid.UUID(int=1)` — a fixed, non-secret
sentinel value `app/deps.py`'s `require_founder()` checks the authenticated user's id against
directly, specifically because it's stronger than a role check (a bad migration granting
`role=founder` to a second row still wouldn't match this fixed id). This is a real,
already-load-bearing "system/founder singleton" pattern elsewhere in this exact codebase, not a
new concept this memo would be introducing.

### Options

**Option A — Attribute to the founder-singleton sentinel (`FOUNDER_USER_ID`).**
Pass `owner_id=FOUNDER_USER_ID, purpose="lesson_conflict_detection"` /
`purpose="agent_task_dispatch"` (etc.) at both call sites, reusing the exact same constant
`require_founder()` already trusts elsewhere.
- *Pro:* Zero schema change. Immediately closes the V4 gate gap — both calls become fully
  policy-inspected and ledger-recorded today. MainAI 0.1 genuinely is single-founder-only, so
  this is not inventing an identity — it's stating a fact that is currently true of the running
  system, using a constant the codebase has already committed to as the founder's identity
  elsewhere.
- *Con:* Doesn't generalize if the project ever becomes multi-founder/multi-tenant — must be
  documented explicitly as a single-tenant assumption tied to `FOUNDER_USER_ID`'s own existing
  scope, not a general "ownership" claim about `EngineeringLesson`/`AgentTask` rows.
- *Recommended.* Smallest, most honest option — it doesn't add a new concept, it uses one this
  codebase already trusts for exactly this kind of "there is currently exactly one possible
  owner" situation.

**Option B — A distinct `SYSTEM_OWNER_ID` sentinel, separate from `FOUNDER_USER_ID`.**
Same mechanism as A, but a new constant, so the disclosure ledger can later distinguish "founder
personally initiated this disclosure" (real `FOUNDER_USER_ID` chat/planning calls) from
"system-initiated disclosure about founder-wide self-improvement state" (lesson-conflict/
agent-orchestration calls).
- *Pro:* Cleaner semantic split for V8's audit question — "what did provider X learn about MY
  active work" vs. "what did provider X learn while MainAI reasoned about its own lesson
  history" are genuinely different questions a founder might want answered separately.
- *Con:* A second sentinel needs its own decision (a reserved UUID? a real placeholder `users`
  row? does RLS need to know about it?) — real design surface for a distinction that may not
  matter in practice while the system is single-founder anyway.
- Worth revisiting once V8's ledger-provenance work is live and the founder has a concrete need
  to filter "system self-reflection" disclosures out of "my own project" disclosures — not
  urgent now.

**Option C — Add a real `owner_id` column to `EngineeringLesson`/`AgentTask`.**
Explicitly **not recommended** and included only because the founder's own instruction asked
for 2-3 options considered, not to bias toward it. Both tables' own docstrings already state
they are intentionally founder-wide/cross-project, not per-owner — adding `owner_id` here would
contradict the tables' own documented purpose (a lesson from Project A genuinely should apply
to Project B) unless the founder explicitly wants to change that data model, which is a much
bigger decision than V4's scope.

### Recommendation

**Option A.** It closes the actual gap (zero-inspection today) with no schema change, reuses an
identity pattern this codebase has already committed to, and is honestly scoped as a
single-tenant-system fact rather than an invented ownership claim — provided the wiring PR that
eventually implements this documents the single-tenant assumption explicitly (a one-line
comment at each call site pointing at this memo is enough), so a future multi-founder change
doesn't silently inherit a stale assumption.

---

## V5 — Sensitivity/egress classification schema

### What exists today (verified directly, not from the doc's summary)

- `enforce_egress_policy()` (`app/egress_policy/service.py:105-196`) has exactly **two**
  `decision` values, `"allowed"`/`"denied"` — enforced by a DB CHECK constraint (migration 0062
  line 51). No REDACT/SUMMARIZE_LOCALLY/ASK_MAINAI/REQUIRE_FOUNDER_APPROVAL categories exist in
  running code today.
- Deny triggers on exactly two things: a literal `"NEVER_EGRESS:"` substring or an SSH
  private-key header appearing anywhere in the (recursively-scanned) payload
  (`_contains_never_egress_marker()`, lines 58-65) — a fixed two-string marker scan, not a
  classification lookup. Otherwise the payload is passed through `operator._redact_value()`
  (the pre-existing regex secret scrubber) and allowed.
- **There is no per-object/per-field classification hook anywhere in this function** — not even
  a stub parameter. The gate is entirely content-string-based today, blind to which
  `Document`/`DocumentChunk`/message the content came from.
- `Document.classification` (`app/models/document.py:178-180`) is the existing
  `KnowledgeClassification` enum — `vision/architecture/decisions/history/security/general`, a
  **topic** taxonomy, not a sensitivity tier (this was already flagged correctly in
  `LIFE_VAULT_EGRESS_CONTROL.md` — confirmed by direct read, not just trusting that note).
- `DocumentChunk` (`app/models/document_chunk.py`) has a real, working `owner_id` FK + RLS
  policy (`document_chunks_isolation` in `app/rls.py`) — but **no classification/sensitivity
  field of any kind.**

### Design questions, answered as concrete options

**Where does classification attach — object or field/chunk level?**
Two-level model: `Document.sensitivity_class` (required at ingest; the "whole document" default)
+ `DocumentChunk.sensitivity_class` (nullable; inherits the parent Document's value unless
explicitly overridden). A document mostly about architecture with one appendix containing a
live API key needs the appendix's *chunk* to carry a stricter class than the rest of the
document — a single document-level field can't express that, but a bare chunk-only field loses
the "classify once at import, don't force per-chunk manual review for the common case" ergonomic
that most documents need. **Conflict rule: always take the MAX (most restrictive) of
document-level and chunk-level** — a chunk can only ever raise the effective class above its
parent document's floor, never lower it below.

**Inheritance rules for derived data (summaries, embeddings, extracted claims)?**
Derived content inherits the source's classification by default. Downgrading requires an
explicit, recorded declassification action (see below) — matches the invariant already stated
in `LIFE_VAULT_EGRESS_CONTROL.md`'s architecture-map section: "summarizing VAULT content does
not auto-downgrade it."

**Declassification authority?**
Founder only, never automatic, never model-driven — a direct application of this project's own
already-established "FOUNDER AUTHORITY CANNOT BE CREATED FROM MODEL OUTPUT" doctrine to
information disclosure instead of execution authority. Recorded as an append-only event (the
same shape as `authorize_execution_scope()`'s own revocation-audit pattern elsewhere in this
codebase), not a bare column update — so "why was this downgraded, and by whom" is always
answerable later, matching V8's own audit goal.

**Default when a row has no classification yet (existing rows, migration path)?**
**Must not silently become PUBLIC** — this was an explicit requirement in the founder's
directive, and it's the right one: this is the founder's own private document store, and an
unclassified row is far more likely to be sensitive-by-default than not. Recommend the
migration default be **CONFIDENTIAL** for every existing row at migration time (not the floor
of the ladder, not the ceiling) — a conservative-but-not-maximal default that still lets
`enforce_egress_policy()` function immediately without a blanket refusal of all existing content,
while requiring an explicit founder or classifier action to ever lower it. A one-time backfill
job the founder can run later to actually review/reclassify existing rows is a natural follow-up,
not part of this memo's scope.

**How do RAG chunks inherit source sensitivity?**
Per the two-level model above: `DocumentChunk.sensitivity_class` defaults to
`Document.sensitivity_class` at chunking time (chunking already reads the parent `Document` row,
so this is a copy at creation time, not a live join), with the explicit per-chunk override
column available for the appendix-with-a-secret case.

**How does chat history inherit mixed sensitivity across a multi-turn conversation?**
This is the direct seam into **V7** below: model a conversation's replay-sensitivity floor as
the **max of every classification that has ever contributed to any turn in the window** — once
a VAULT-classified chunk has been surfaced once, the conversation's floor rises to at least
VAULT for all *subsequent* re-inspection, and never silently drops back down just because a
later turn's fresh retrieval happens to be less sensitive. This is a conservative,
correct-by-construction rule, not an optimization — see V7 for why it must never become a
shortcut that skips re-inspection.

**The `IP_PROTECTED` flag.**
Orthogonal boolean, independent of the PUBLIC..NEVER_EGRESS ladder — a `PUBLIC`-tier idea for a
blog post could still be `IP_PROTECTED` (a genuine business idea) even though it isn't
"sensitive" in the security sense. The gate should treat `IP_PROTECTED` as its own denial/
redaction reason-code, targeted specifically at the "coding agent needs a technical requirement,
not the underlying business rationale" distinction the original Vault architecture memo named
explicitly.

### Where classification enforcement composes with V8

`enforce_egress_policy()` is purely content-based today; adding real classification support
means it needs an optional "source classification" input alongside the content itself,
populated by each call site from whatever object-level context it already has (chat.py's
`hits` → `document_id`s; `ingest.py`'s `document` object directly; `library.py`'s
query-embedding call → nothing, structurally, since it runs before any document has matched
anything). **This is the same data V8's `source_refs` plumbing needs.** Recommend designing
V5's classification-lookup and V8's source-linkage as one shared plumbing change at each call
site (pass source object ids once; use them for both the classification lookup and the ledger's
provenance record) rather than building two separate mechanisms that both need to know "which
object is this content actually from."

---

## V7 — Chat history replay: avoiding needless re-disclosure without an authorization cache

### What actually happens today (verified line-by-line, not assumed from the architecture doc)

`app/routers/chat.py:_attempt_assistant_reply()`:
- `history` = last 20 `MessageModel` rows for the conversation (status `succeeded`, excluding
  the current message), ordered `(created_at, id)` ascending (lines ~181-197).
- `messages` = `[system] + [history...] + [new user message]` — **one single combined list**,
  not a separate "history channel" (lines 214-216).
- This exact list is passed whole to `chat_with_fallback(db, messages, owner_id=..., ...)`
  (lines 221-227), which inside `registry.py` (lines 191-209) re-serializes and re-hashes the
  **entire** payload — all 20 history turns included — through `enforce_egress_policy()` **on
  every single call, per provider-fallback-chain attempt** (i.e. a same-turn provider-A→B
  fallback re-evaluates the full history independently for each provider, never reusing a
  decision from provider A's attempt).
- **No caching layer exists anywhere near the provider/egress path** (`grep -rn
  "lru_cache|@cache" app/providers/ app/egress_policy/ app/routers/chat.py` → zero hits). Redis
  exists in this codebase, but only for rate-limiting/lease/locking, nowhere near egress. There
  is currently nothing that could accidentally function as an authorization cache.

### The actual risk this closes vs. what's already safe

The founder's original V7 concern conflated two different risks. Tracing the real code
separates them cleanly:

1. **Stale-authorization risk (already NOT present).** Because every call re-serializes and
   re-inspects the FULL current payload with no cached decision anywhere, there is no path today
   where a prior turn's "allowed" verdict gets silently reused without a fresh check. The
   `"PREVIOUSLY ALLOWED != CURRENTLY ALLOWED"` invariant is already upheld structurally, by the
   simple fact that nothing is cached — not because of any dedicated V7 mechanism.
2. **Compounding-exposure risk (real, and still open).** A chunk that was disclosed once in turn
   3 gets re-sent as literal raw text in turns 4, 5, 6... every one an independent, full,
   ledger-recorded disclosure event. This is safe (each one is freshly checked) but not
   *minimized* — the same content leaves the process repeatedly, and (per V8, since the ledger
   already records every call) the founder's own audit trail will show this compounding
   directly today, even before any V7 mechanism exists.

### Options

**Option A — Do nothing structurally right now; rely on the ledger to make compounding
visible.** Current behavior is conservative-safe by construction (point 1 above). Since V1's
gate already ledger-records every repeated disclosure as its own event (confirmed via V8's
findings below — nothing here is silently hidden), the founder can already SEE the compounding
in the audit trail once V8's source-linkage work lands, and decide whether the token-cost/
exposure-surface tradeoff is worth building a minimization feature for.
- *Recommended for now* — it's zero new complexity, zero new risk, and the thing it would
  optimize (repeated literal disclosure) is not actually a currently-unsafe gap, just an
  un-minimized one.

**Option B — Build a "disclosure fingerprint" minimization layer**, if/when the founder decides
compounding exposure or token cost is a live problem worth solving. Sketch, not a spec: after
`enforce_egress_policy()` freshly ALLOWS a piece of content (every single time, no shortcut),
the SEND step may substitute a short reference for the raw text ONLY IF, freshly re-checked at
send time (never cached-and-trusted): (1) the same provider+model as the prior disclosure of
this exact content (a provider switch always re-discloses in full — "PROVIDER SWITCH NEVER
INHERITS A PRIOR DECISION"); (2) the same content fingerprint was already allowed to this exact
provider/model earlier in this same conversation; (3) the source's classification has not
changed since; (4) no founder revoke/scope-narrowing action has happened since; (5) the current
call's purpose matches the original disclosure's purpose. Critically, `enforce_egress_policy()`
itself still runs on the full substituted payload every time — a DENY at that fresh check still
denies the whole call exactly as if no fingerprint existed. This narrows *what bytes get sent*
for an already-fresh-approved disclosure; it never widens or skips the approval step.
- *Not recommended now* — five independently-fresh-checked conditions is real complexity to get
  right, and the risk it closes (compounding exposure, not stale authorization) is lower-severity
  than most of the founder's other named priorities. Worth building only once V5's classification
  and V8's provenance linkage exist (Option B needs both to check "has classification changed"
  and "which prior event was this").

### Recommendation

**Option A now, Option B later if the founder decides compounding exposure/cost is worth
addressing** — and only after V5 and V8 land, since B structurally depends on both.

---

## V8 — Ledger provenance: linking disclosure events back to their real source

### What's captured today (verified against migration 0062 and `app/egress_policy/service.py`
directly)

`provider_disclosure_events` columns: `owner_id, provider, model, purpose, requested_by,
task_id, goal_id, job_id, decision, reason, redaction_categories (jsonb), attempted_content_hash,
sent_content_hash, created_at`. `task_id`/`goal_id`/`job_id` are **plain nullable UUIDs with no
FK constraint at all** (migration 0062 lines 42-44 — bare `uuid`, not `uuid REFERENCES`).
Confirmed directly: `chat.py`, `rag/ingest.py`'s `embed_with_policy()` call, and
`routers/library.py` all pass none of the three — for every RAG/chat call site today, all three
columns are `NULL`. **There is no `document_id`/`project_id`/`conversation_id` linkage of any
kind.** The table can truthfully answer "did provider X ever receive ANYTHING from owner Y, and
when" but cannot answer the founder's own stated requirement, "what has provider X ever received
about project/document Y."

### What's actually in scope at each real call site (so the design is grounded, not speculative)

- **`chat.py`** — `hits` (a `list[dict]`, each with `document_id`, `chunk_id`, `title`, `text`,
  `score`) is computed via `retrieve_context()` **before** the gate call, so the full list of
  contributing document/chunk ids is available in scope at gate time. `conversation` (the
  `Conversation` row) is also already in scope.
- **`rag/ingest.py`** — the single `document` object (with `.id`) is directly in scope; the
  source here is singular, not a list.
- **`routers/library.py`** — the gate call here guards the **query embedding**, which happens
  **before** `hybrid_search()` runs. At gate time there is no candidate/result document set in
  scope yet — structurally, "which document does this concern" has no answer, because the query
  hasn't matched anything yet. A provenance design must represent this honestly as "no specific
  source, this disclosure is a raw query," not force a fabricated document reference.

### Options

**Option A — `source_refs jsonb` column on `provider_disclosure_events`.** Array of
`{type: "document"|"chunk", id: uuid}` entries, populated at each call site from data already
in scope: chat.py → one entry per contributing `document_id`/`chunk_id` in `hits`; ingest.py →
a single-element array wrapping `document.id`; library.py's query-embedding call → an empty
array, honestly representing "no document source for this event." Matches the pattern this same
table already uses for `redaction_categories jsonb`. A GIN index on `source_refs` keeps "find
every disclosure that ever touched document Y" queries fast without needing a join, and without
having to predict every future source type up front.

**Option B — Normalized link table**,
`provider_disclosure_event_sources(event_id, source_type, source_id)`. Real FK-able, supports a
proper join/index without jsonb containment queries, and could later gain a `source_type`
-specific FK constraint (e.g. `FK → documents.id` when `source_type='document'`). Costs more
schema surface (a new table + migration) and turns each disclosure's provenance write into an
N-row insert instead of one jsonb append.

**Option C — A narrower `document_ids uuid[]` array column** (native Postgres array, not
jsonb), since `Document` is by far the dominant source type across every currently-known call
site. Simpler than a fully general `source_refs`, but defers "what about a genuinely different
source type" (e.g. a future calendar/contact-derived disclosure) to another migration later if
one ever shows up.

### An additional open question, not in the original founder framing

**Conversation-level linkage.** None of task_id/goal_id/job_id/`source_refs` capture *which
conversation* a chat disclosure happened within — a distinct, real audit question from "which
document(s) did it disclose from" ("what has provider X learned in conversation Y" vs. "what has
provider X learned about document Y" are both things a founder might reasonably want to ask
separately). `conversation` is already in scope at chat.py's call site at zero extra cost to
fetch. Recommend a single nullable `conversation_id` column (not jsonb — exactly one value per
event, unlike sources) if the founder wants this; flagging it here as an open decision rather
than assuming yes, since it wasn't in the original V8 framing.

### Recommendation

**Option A (`source_refs jsonb`)** — most flexible, reuses a pattern this table already has
precedent for, doesn't force a join for the common "sources for event X" read, and a GIN index
keeps the reverse lookup fast without pre-committing to every possible future source type.
Populate it from exactly the in-scope data documented above — never raw content, only reference
ids, matching this ledger's own existing hash-only-content discipline. Pair with a founder
decision on the `conversation_id` question above, and build the actual population plumbing
together with V5's classification-lookup call-site changes (same call sites, same "what object
is this from" data needed by both).
