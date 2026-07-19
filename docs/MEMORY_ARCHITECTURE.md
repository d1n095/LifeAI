# Memory Architecture — MainAI's complete memory system

**Scope:** deep-dive for `docs/MAINAI_ARCHITECTURE.md` §6 (Memory architecture), designed to
scale to millions of users. That document's §6 gives the four-layer summary already built
today; this document is the full system — every memory kind the product needs, how they
relate to each other (they are not eight independent systems), and the concrete storage,
indexing, and lifecycle mechanics that keep it fast and affordable at scale. Cross-references
`docs/LIFE_LIBRARY_PLAN.md` (the `KnowledgeObject` model this document's records generalize),
`docs/AI_PROVIDER_ARCHITECTURE.md` (embedding capability, data residency), and
`docs/MAINAI_ARCHITECTURE.md` §9/§10 (permission model, Trust Engine) rather than duplicating
them. Pure architecture — nothing here is implemented beyond what "Idag" states.

---

## 1. The unified model — two axes, not eight systems

Reading the required list — short-term, long-term, episodic, semantic, project,
organization, user, shared — as eight separate subsystems is the mistake this design avoids.
They are **two orthogonal axes** applied to one underlying record type. Every memory record
has both:

- a **class** (how it behaves over time and how it's queried): `WORKING` (short-term),
  `EPISODIC`, `SEMANTIC`, `STRUCTURED` — `EPISODIC`+`SEMANTIC`+`STRUCTURED` together are what
  "long-term memory" means: durable, survives past a session, as opposed to `WORKING`, which
  never is.
- a **scope** (who can see it): `USER`, `PROJECT`, `ORGANIZATION`, `SHARED`.

A "project's semantic memory" is `scope=PROJECT, class=SEMANTIC`. A "user's short-term working
memory" is `scope=USER, class=WORKING`. There is one storage substrate, one retrieval API, one
trust/provenance/versioning mechanism — applied consistently across a 4×4 grid instead of
sixteen bespoke implementations. This is the same design instinct as
`docs/AI_PROVIDER_ARCHITECTURE.md`'s capability-based interfaces: orthogonal properties,
not a combinatorial explosion of special cases.

```python
class MemoryScope(str, Enum):
    USER = "user"                  # RLS: owner_id — private to one person
    PROJECT = "project"            # visible to project members (docs/MAINAI_ARCHITECTURE.md §9 target)
    ORGANIZATION = "organization"  # visible org-wide — today's de facto scope for Document/Project
    SHARED = "shared"              # cross-organization / public — always opt-in, never implicit

class MemoryClass(str, Enum):
    WORKING = "working"        # short-term, ephemeral, never the source of truth
    EPISODIC = "episodic"      # time-ordered, append-only — events, not facts
    SEMANTIC = "semantic"      # embedding-indexed, retrieved by similarity — facts, not events
    STRUCTURED = "structured"  # explicit typed key/value or entity data — today's CompanyInfo/Project/Task
```

**The record type** — a direct specialization of `KnowledgeObject`
(`docs/LIFE_LIBRARY_PLAN.md` §3) for the specific purpose of "something MainAI remembers and
may retrieve later," not a competing abstraction:

```python
class MemoryRecord(BaseModel):
    id: UUID
    scope: MemoryScope
    scope_ref: UUID | None          # user_id | project_id | organization_id | null for SHARED
    memory_class: MemoryClass
    content: MemoryContent          # text, plus an optional structured payload for STRUCTURED
    embedding: Vector | None        # populated for SEMANTIC; optional for EPISODIC (hybrid search)
    provenance: Provenance          # §7
    trust: TrustRecord              # §6
    version_of: UUID | None         # §8 — a chain, never a mutation
    superseded_by: UUID | None
    lifecycle: MemoryLifecycle      # §10
    approval: ApprovalState | None  # §11
    created_at: datetime
    created_by: UUID | None         # null for system-generated records (e.g. auto-summaries)
```

The grid this produces, and what already exists for each cell:

| | WORKING | EPISODIC | SEMANTIC | STRUCTURED |
|---|---|---|---|---|
| **USER** | Idag: chat "last 20 messages" (recomputed, not cached — §2) | Idag: `Conversation`/`Message` | Idag: `DocumentChunk` (RLS: `owner_id`) | Målarkitektur: personal facts/preferences (§5) |
| **PROJECT** | Målarkitektur | Målarkitektur: project activity timeline (§5) | Målarkitektur: project-scoped document subset (§5) | Idag (partial): `Project`/`Task` fields — not yet a general memory record |
| **ORGANIZATION** | — (no working memory at org scope; working memory is always a single actor's context) | Målarkitektur: org-wide activity log | Idag (degenerate case): `Document` is de facto org-scoped since there's no `Organization` model yet | Idag: `CompanyInfo` (global key/value — a single-organization special case) |
| **SHARED** | — | Målarkitektur | Målarkitektur: opt-in cross-org knowledge (§5) | Målarkitektur |

---

## 2. Short-term (working) memory

**Idag:** the "last 20 messages" window `app/routers/chat.py` builds per request is
recomputed from `Message` on every single chat turn — correct, but at scale this is a
Postgres read on the hot path of every request.

**Målarkitektur:** working memory becomes an explicit, cached class, not an implicit query
pattern:

- **Storage:** Redis (already in the stack for rate limiting — `app/limiter.py`), keyed by
  `(scope=USER, conversation_id)`, TTL matched to conversation inactivity (e.g. 30 minutes of
  silence expires the cache entry — cheap to rebuild from `EPISODIC` storage on a cache miss,
  so the TTL can be aggressive).
- **Never authoritative:** a working-memory entry is always reconstructable from durable
  `EPISODIC` storage — this is the property that makes it safe to cache aggressively and
  evict without any data-loss risk, and safe to lose entirely on a Redis restart.
- **Write path:** append-only — a new message pushes onto the cached window and evicts the
  oldest entry past the window size, rather than re-querying Postgres for the whole window
  again. This is the concrete mechanism that removes a synchronous Postgres read from the hot
  path at scale (§12).
- **Not embedded, not indexed** — working memory is never a target of semantic search; it's
  read by exactly one consumer (the conversation that owns it) via a direct key lookup, never
  a similarity query.

---

## 3. Long-term memory — the umbrella, not a fourth thing

"Long-term memory" is not a separate storage mechanism alongside episodic/semantic/
structured — it's the property that `EPISODIC`, `SEMANTIC`, and `STRUCTURED` all share
(durable, survives past a session, backed by Postgres) as opposed to `WORKING` (ephemeral,
Redis, ok to lose). Every section below (§4, §5, and every scope in §5) that isn't §2 is
describing a kind of long-term memory. This is stated explicitly here because treating
"long-term" as its own bullet point (as a naive reading of the requirements list would) leads
to a fifth redundant storage mechanism that duplicates episodic/semantic/structured instead of
correctly being the union of them.

---

## 4. Episodic vs. semantic memory — the two long-term retrieval patterns

These are genuinely different access patterns and deserve genuinely different indexes — this
is the one place a class distinction maps to a real infrastructure difference, not just a
label.

### Episodic memory

**Idag:** `Conversation`/`Message` — time-ordered, append-only, queried by conversation_id +
chronological range, never by similarity.

**Målarkitektur:** generalizes beyond chat messages to **every significant event** in a given
scope — document uploaded, task completed, provider fallback engaged, trust level dropped
below threshold, a fact superseded (§8), an approval granted (§11). One `EPISODIC` stream per
scope, not a chat-specific table plus a separate ad-hoc event log for everything else. Indexed
for **time-range queries** (Postgres BRIN index on `created_at`, far cheaper to maintain than
a B-tree at the row volumes millions of users produce — episodic data is append-only and
naturally time-clustered, exactly what BRIN is built for) and secondarily by `scope_ref` for
"everything that happened in project X."

### Semantic memory

**Idag:** `DocumentChunk` + pgvector HNSW index (`backend/alembic/versions/0004_...py`) —
`scope=USER` only today (RLS: `owner_id`), queried by embedding similarity via
`app/rag/vector_store.py`.

**Målarkitektur:** the same mechanism, extended to all four scopes (§5) — a project's semantic
memory, an organization's, and (opt-in) shared/cross-org semantic memory all use the identical
chunk+embedding+HNSW pattern, differentiated only by `scope`/`scope_ref` and the RLS policy
that scope implies. See §12 for why this must become **per-scope partitioned indexes**, not
one global HNSW index over every tenant's vectors, once volume is real.

---

## 5. The four scopes

### User memory

**Idag:** `Conversation` (episodic) + `DocumentChunk` (semantic), both strictly RLS-isolated
by `owner_id` (`app/rls.py`) — the strongest isolation level in the system, matching
`docs/MAINAI_ARCHITECTURE.md` §9's framing of conversations/chunks as never shared.

**Målarkitektur:** add a `STRUCTURED` layer — personal facts and preferences ("prefers concise
answers," "works in the Gothenburg office," "timezone") extracted from conversation over
time, promoted into durable structured memory the same way §11's human-approval-gated
promotion works for other scopes (though promotion INTO a user's own private memory, by that
same user, is exactly the case §11 explicitly does **not** require approval for — the user is
both the actor and the sole audience).

### Project memory

**Målarkitektur, ties to an entity (`Project`) that already exists but has no memory concept
attached to it yet:**

- `EPISODIC`: a project's activity timeline — every task state change, document added,
  decision recorded — queryable as "what happened on this project, in order."
- `SEMANTIC`: the subset of an organization's documents actually relevant to this project,
  scoped so that a chat happening in a project context retrieves project-relevant chunks
  first (§13's retrieval algorithm) without needing every org member's unrelated documents in
  the candidate set.
- `STRUCTURED`: decisions, specs, requirements — explicit facts about the project, not
  free-text chunks (e.g. "target launch date," "assigned owner") — structured because these
  are exactly the facts a query-by-key ("what's the launch date?") should hit deterministically
  rather than probabilistically via similarity search.
- Membership (who is "in" a project, and therefore has read access to `scope=PROJECT` memory)
  is the resource-level collaborator list already sketched as a target in
  `docs/MAINAI_ARCHITECTURE.md` §9.

### Organization memory

**Idag (degenerate single-tenant case):** `CompanyInfo` (structured key/value) and
`Document`/`Project`/`Task` (shared, unprotected by RLS) are already, in effect, organization-
scoped memory — there is just no `Organization` entity yet to formally scope them to, so
"organization" today means "everyone in this MainAI installation."

**Målarkitektur:** requires `Organization` (`docs/MAINAI_ARCHITECTURE.md` §4's target model)
to exist as a real entity before this scope is anything more than the current degenerate case.
Once it does: `CompanyInfo` becomes `organization_id`-scoped `STRUCTURED` memory,
`Document`/`DocumentChunk` (for org-shared uploads, as distinct from user-private ones —
`docs/MAINAI_ARCHITECTURE.md` §9's open tension between `Document` and `DocumentChunk`
isolation is exactly resolved by this: a document uploaded with `scope=ORGANIZATION` gets
chunks with `scope=ORGANIZATION`, not `scope=USER`, so deletion/access-control questions have
one consistent scope instead of the mismatch flagged there) becomes `SEMANTIC` organization
memory, and a new org-wide `EPISODIC` stream captures cross-project activity for admin
visibility (feeds the audit trail, §14).

### Shared memory

**Målarkitektur, the newest and most tightly gated scope.** Two distinct kinds of "shared":

1. **Cross-organization / public** — knowledge one organization explicitly publishes for
   others to use, or a MainAI-curated base knowledge layer every tenant can draw on. Always
   an explicit "publish" action by a human with authority to do so (never an implicit
   consequence of, e.g., an organization's default visibility setting) — this is a hard
   multi-tenant boundary and treated with the same rigor as `docs/MAINAI_ARCHITECTURE.md`
   §8's loopback-only backend isolation: the default is closed, opening it is a deliberate,
   audited act (§14), not a fallback state.
2. **Cross-user, within an org, narrower than the whole organization** — a document shared
   with three specific people, not the whole company. Modeled not as a fifth scope but as
   `scope=USER` (or `PROJECT`) memory with an explicit `collaborators: list[UUID]` grant list
   (`docs/MAINAI_ARCHITECTURE.md` §9's resource-level permission target) — "shared with
   specific people" is a permission on user/project memory, not a separate memory class.

---

## 6. Trust scoring

**Authoritative source:** `docs/MAINAI_ARCHITECTURE.md` §10 (Trust Engine) defines how
confidence is computed and how it constrains a model's response. This section defines how
that trust attaches to and persists on an individual `MemoryRecord`, which the Trust Engine
consumes as one of its input signals (specifically `source_authority` and
`cross_source_agreement` in that document's multi-signal target design).

```python
class TrustRecord(BaseModel):
    retrieval_relevance: float | None     # similarity score at the moment of last retrieval —
                                           # a property of a QUERY, recomputed each time, not stored durably
    source_authority: float               # 0-1, derived from provenance (§7): an admin-uploaded,
                                           # "official" document scores higher than an unreviewed
                                           # conversation extract — computed at write/promotion time
    corroboration_count: int              # how many independent records assert the same fact —
                                           # incremented by conflict resolution (§9) when records agree
                                           # rather than conflict
    human_approved: bool                  # true if this record passed §11's approval gate
    last_validated_at: datetime | None    # when trust was last recomputed (source_authority can
                                           # change if the underlying source is edited/retracted)
    decayed_score: float                  # composite, recency-adjusted — see memory aging (§10)
```

**Idag:** `app/rag/trust.py`'s `assess_confidence()` computes confidence purely from
retrieval similarity at query time — it does not persist anything onto the retrieved records.
**Målarkitektur:** `source_authority`/`corroboration_count`/`human_approved` are computed once
at write/promotion/approval time and stored on the record, so a retrieval doesn't have to
recompute a source's trustworthiness from scratch on every query — only
`retrieval_relevance` (inherently query-specific) is computed per-request; the rest is a
cheap read of an already-materialized column, which is what makes trust-weighted retrieval
affordable at scale (§12).

---

## 7. Source tracking (provenance)

```python
class Provenance(BaseModel):
    origin_type: Literal["upload", "conversation_extract", "crawl", "generated", "api_import"]
    origin_ref: str                # a Document id, a Message id, a URL, a generation job id —
                                    # shape depends on origin_type
    extraction_method: str | None  # "pdf-text", "ocr", "manual-note", "llm-summary" — how this
                                    # record's content was produced from its origin, if not a direct copy
    checksum: str                  # sha256 of content, for dedup and tamper-evidence
    imported_by: UUID | None
    imported_at: datetime
    derived_from: list[UUID] = []  # OTHER MemoryRecord ids this one was derived from — e.g. a
                                    # generated summary's derived_from lists the source chunks it
                                    # actually cited, enabling per-claim citation
                                    # (docs/MAINAI_ARCHITECTURE.md §10's "per-påstående trust" target)
```

`derived_from` is the concrete field that makes chain-of-custody real: a `GeneratedProduct`
(`docs/LIFE_LIBRARY_PLAN.md` §3) doesn't just have provenance pointing at "the knowledge
base" abstractly — it points at the exact `MemoryRecord` ids its content was actually
generated from, so "show me the source for this specific sentence" is a direct lookup, not a
re-run of retrieval hoping to reproduce the same result.

---

## 8. Version history

Every `MemoryRecord` is immutable once created. A correction or update creates a **new**
record with `version_of` pointing at the record it supersedes; the old record gets
`superseded_by` set to the new record's id. Nothing is ever mutated in place, and nothing is
deleted by an edit — only by explicit erasure (account deletion's existing transactional-
delete pattern, `docs/AUTH_THREAT_MODEL.md`-adjacent, extends directly to memory records
owned by a deleted account).

**Why this matters beyond audit hygiene:** it's what makes §9 (conflict resolution) and §14
(audit trail) possible at all — you cannot resolve a conflict between "what the record says
now" and "what it said when a decision was made" if updates overwrite history. `docs/
LIFE_LIBRARY_PLAN.md` §3 already establishes this pattern for `KnowledgeObject`; this document
applies it uniformly to every memory record, not just documents.

**Idag:** no `MemoryRecord` abstraction exists yet, so no general versioning exists either —
the closest today is `RefreshToken` rotation (`docs/AUTH_THREAT_MODEL.md`), which is the same
"chain, never mutate" instinct applied to a completely different domain (session tokens), a
useful precedent that this design generalizes rather than a mechanism to reuse directly.

---

## 9. Conflict resolution

**The concrete problem:** two `MemoryRecord`s — possibly different scopes, different sources
— assert contradictory facts ("the office is in Gothenburg" vs. "the office is in
Stockholm"). What happens?

```
1. DETECTION — a semantic-similarity match between two records' content ABOVE a threshold,
   combined with an opposite-polarity signal (a genuinely hard NLP sub-problem — flagged
   explicitly as a target capability requiring its own model/heuristic, not claimed solved
   here) produces a Conflict record linking the two MemoryRecord ids.

2. AUTOMATIC RESOLUTION (attempted first, for low-stakes/high-confidence cases):
   - source_authority-weighted: the higher-trust (§6) record wins by default.
   - recency-weighted: for memory_class/content flagged as VOLATILE (e.g. "current headcount"
     — a StructuredFieldPolicy on the record type declares this), the newer record wins
     regardless of authority, since being current is the whole point of a volatile fact.
   - For memory flagged IMMUTABLE (e.g. a legal filing date), automatic resolution never
     overrides an existing record — any conflicting new record is held for human review
     regardless of its trust score.

3. ESCALATION — when neither rule resolves confidently (comparable trust scores, no
   volatility/immutability policy set, or the conflict involves an ORGANIZATION/SHARED-scope
   record where the blast radius of getting it wrong is large), the conflict is held as
   PENDING and routed into §11's human approval queue instead of auto-resolving.

4. OUTCOME recorded: the losing record gets `superseded_by` pointing at the winner (§8), and
   BOTH records keep a durable link to the Conflict record itself — the fact that a conflict
   existed and how it was resolved is retained, not erased once resolved (feeds §14).
```

This is the operational mechanism behind `docs/MAINAI_ARCHITECTURE.md` §10's
`cross_source_agreement` Trust Engine signal — that signal reads the *rate* of conflicts vs.
corroborations (§6) for a given source over time, not just a single query's result.

---

## 10. Memory aging

Aging is what keeps the "hot," fully-indexed working set bounded per tenant regardless of how
much history accumulates — the single most important mechanism for §12's scale requirement,
not a housekeeping afterthought.

```python
class MemoryLifecycleStage(str, Enum):
    HOT = "hot"            # recently created/accessed — fully indexed AND cached
    WARM = "warm"          # durably indexed (HNSW/BRIN) but not cache-resident
    COLD = "cold"          # archived — metadata stays in the primary DB, content may move to
                            # cheaper object storage (§12), still queryable but with higher latency
    EXPIRED = "expired"    # soft-deleted per retention policy — visible only to audit (§14) until
                            # the compliance retention window elapses, then hard-deleted

class MemoryLifecycle(BaseModel):
    stage: MemoryLifecycleStage
    last_accessed_at: datetime
    access_count: int
    aging_policy: AgingPolicy   # which rule below applies to this record
```

**Aging rules differ by `memory_class` — this is deliberate, not an oversight:**

- `EPISODIC` ages by **elapsed time since creation** — a conversation from two years ago is
  cold regardless of how interesting it was, because episodic memory's value is chronological
  context, which fades uniformly.
- `SEMANTIC` ages by **elapsed time since last retrieved**, not creation — a fact created a
  year ago that's still being retrieved weekly stays HOT; a fact created yesterday that's
  never retrieved again cools quickly. This access-based (not creation-based) rule is what
  correctly keeps genuinely useful old knowledge hot instead of penalizing it for age alone.
- `STRUCTURED` (organization facts, project specs) generally does **not** auto-age — it
  requires explicit supersession (§8/§9) rather than fading from disuse, since a structured
  fact being "not recently queried" doesn't mean it stopped being true.
- `WORKING` memory is never in this lifecycle at all — it's Redis-TTL-governed (§2), outside
  the durable-storage aging system entirely.

Downgrading HOT→WARM→COLD is a background job, not a request-path operation — it reads
`last_accessed_at`/`access_count` and moves records in batches, the same operational shape as
the existing scheduled cleanup job (`app/cleanup.py`, `docs/OPERATIONS.md`) that already
purges expired tokens on a schedule with an advisory lock preventing concurrent runs across
replicas.

---

## 11. Human approval

**Not blanket "everything needs approval"** — that wouldn't scale past a few hundred users,
let alone millions; approval requirement is **proportional to blast radius and trust**, a
concrete policy table rather than a single on/off switch:

| Action | Approval required? |
|---|---|
| User promotes their own conversation extract into their own `USER`-scope memory | No — actor and sole audience are the same person |
| Auto-extraction promotes a fact into `PROJECT`-scope memory, `source_authority` above threshold | No — logged (§14), but not gated, since project members can already see/correct it |
| Promotion into `ORGANIZATION`-scope memory from a low-`source_authority` origin (e.g. an unreviewed conversation extract, not an admin-uploaded document) | **Yes** — visible to everyone in the org; a wrong fact here has organization-wide blast radius |
| Any promotion into `SHARED` (cross-organization) memory | **Yes, always**, by someone with explicit publish authority — no trust score is high enough to skip this, since the blast radius crosses a tenant boundary |
| Conflict resolution (§9) that couldn't auto-resolve | **Yes** — routed to the approval queue by construction |
| A `STRUCTURED` fact flagged `IMMUTABLE` (§9) being contradicted | **Yes**, regardless of the new record's trust score |

```python
class ApprovalState(BaseModel):
    status: Literal["not_required", "pending", "approved", "rejected"]
    requested_at: datetime | None
    decided_by: UUID | None
    decided_at: datetime | None
    reason: str | None   # required on rejection — never a silent drop
```

A record with `status="pending"` is **not** retrievable at its target scope yet — it exists,
but is invisible to queries at that scope until approved, exactly mirroring how
`docs/MAINAI_ARCHITECTURE.md` §9's RLS-as-last-line principle works: a pending promotion
simply doesn't match any scope's retrieval policy until approval flips it, not a separate
enforcement mechanism bolted on top.

---

## 12. Audit trail

The audit trail is not a ninth memory system bolted on for compliance — it **is** a
specific, always-present `EPISODIC` stream (§4), scoped to match what it's auditing (a
`USER`-scope action produces a `USER`-scope audit episode readable by that user; an
`ORGANIZATION`-scope approval produces an org-visible audit episode readable by org admins),
extending the existing `AuditLog` pattern (`docs/MAINAI_ARCHITECTURE.md` §4/§8) rather than
introducing a new logging mechanism.

Every one of these produces an audit episode, unconditionally, regardless of whether the
underlying action itself needed approval:

- Record created / versioned / superseded (§8)
- Trust score changed (§6)
- Conflict detected / resolved, and how (§9)
- Lifecycle stage transition (§10) — visible for compliance ("when did this become archived,"
  relevant for data-retention audits)
- Approval requested / granted / rejected, and by whom (§11)
- Cross-scope promotion (a fact moving from `USER`→`ORGANIZATION` or `ORGANIZATION`→`SHARED`)
  — always logged even in the no-approval-required cases, since "who saw this become visible
  to whom, when" is exactly the question an audit trail exists to answer regardless of
  whether the promotion itself was gated.

Audit episodes are themselves immutable `EPISODIC` records (§4/§8) — an audit trail that could
be edited after the fact is not an audit trail, so the same "never mutate, only append"
discipline that governs every other memory record applies here with zero exceptions.

---

## 13. Retrieval algorithm — how a query actually pulls from multiple scopes at once

A single chat turn (or any memory-consuming operation) generally needs candidates from more
than one scope simultaneously — a project conversation should draw on the user's own memory,
the project's memory, AND the organization's, trust-weighted together, not four separate
uncombined searches:

```
1. SCOPE RESOLUTION — given the acting user + current project (if any) + their organization,
   determine which scope_refs are even eligible: {user_id}, {project_id if in a project
   context}, {organization_id}, plus any explicitly-granted SHARED sources. This list, not
   RLS alone, is the first filter — RLS (docs/MAINAI_ARCHITECTURE.md §9) is still the
   non-bypassable enforcement layer underneath it, defense in depth as always.

2. PER-SCOPE CANDIDATE FETCH — bounded top-K semantic search (§4) within EACH eligible scope
   independently (this is what makes §12's per-scope-partitioned index design work — each
   fetch hits one partition, never a cross-tenant scan).

3. AGING/TRUST FILTER — drop candidates below the caller's minimum trust threshold (§6) or in
   MemoryLifecycleStage.EXPIRED (§10); COLD candidates are included but flagged as
   higher-latency-to-fully-hydrate if their content isn't in the primary store (§12).

4. WEIGHTED MERGE — combine the per-scope candidate lists into one ranked set, weighted by:
   retrieval_relevance × decayed_trust_score × a scope-priority multiplier (project context
   generally outweighs org-wide for a project-specific question, configurable, not hardcoded).

5. CONTEXT ASSEMBLY — top-K of the merged, weighted list becomes the retrieved context handed
   to the Trust Engine (docs/MAINAI_ARCHITECTURE.md §10) for confidence assessment and prompt
   construction, exactly as app/rag/retrieve.py's retrieve_context() does today for the
   single-scope (USER-only) case — this generalizes that function's contract, not replaces it.
```

**Idag:** step 2 only ever runs against one scope (`USER`) because no other scope exists yet
— `retrieve_context(db, owner_id, query, top_k)`'s signature is exactly step 2's degenerate
single-scope case. Målarkitektur extends the signature to accept a resolved scope list (step
1's output) and performs steps 2–4 as described.

---

## 14. Scale: designing for millions of users

Every mechanism above is scale-relevant by construction (aging bounds working-set size, scope
partitioning bounds query blast radius, working-memory caching removes hot-path DB reads) —
this section makes the remaining infrastructure decisions explicit.

### Storage partitioning

Postgres table partitioning (native declarative partitioning, not application-level sharding)
on `scope_ref` — practically, by `organization_id` (or `user_id` for the rare cross-org-less
deployment) — for every `MemoryRecord`-backed table. This bounds:

- **Vacuum/index-maintenance cost** per partition instead of one unbounded table across every
  tenant combined — the single biggest operational risk of a single giant table at "millions
  of users" scale.
- **Query blast radius** — §13 step 2's per-scope fetch touches one partition, never scans
  rows belonging to other tenants, which is both a performance property and a defense-in-depth
  property (a query that somehow lost its scope filter still can't return another tenant's
  partition without an explicit cross-partition scan, an unusual and auditable query shape).

### Vector index at scale — per-partition HNSW, not one global index

pgvector's HNSW index build/query cost degrades as the indexed set grows into the hundreds of
millions of vectors combined across every tenant — the wrong architecture for "millions of
users" is one global HNSW index everyone's semantic memory lives in. **Each partition (§
above) gets its own HNSW index**, matching Postgres's native per-partition indexing — an
organization with 50,000 chunks gets a fast, small index; it never pays the cost of an index
sized for the platform's total volume across every other tenant. This is the direct
architectural answer to "how does pgvector, which already works today at MVP scale, keep
working at enterprise scale": partition first, index per-partition, never let one tenant's
query cost depend on another tenant's data volume.

### Caching / read path

- **Working memory (§2):** Redis, as described — removes the highest-frequency read
  (recent conversation window) from Postgres entirely.
- **HOT semantic/episodic candidates (§10):** an LRU cache of recent retrieval results,
  keyed by (scope, query-embedding-bucket) — a cache, not a source of truth, invalidated on
  any write to the underlying partition.
- **COLD tier (§10):** optionally moved to cheaper object storage (content body only —
  metadata, embeddings, and trust/provenance stay in Postgres for queryability) once a
  record has been WARM→COLD for long enough that per-GB storage cost matters more than
  retrieval latency — a standard hot/warm/cold tiering decision, not a novel mechanism.

### Write path

- `WORKING` writes: Redis, synchronous, cheap.
- `EPISODIC` writes (every chat message, every event): high-frequency, append-only — the
  natural candidate for `docs/MAINAI_ARCHITECTURE.md` §5's target message-queue evolution
  (Redis Streams, already a dependency in the stack) once write volume justifies moving off
  the synchronous request-path insert that's correct and sufficient at MVP scale today.
- `SEMANTIC` writes (embedding + chunk write): already asynchronous today
  (`BackgroundTasks`, `docs/MAINAI_ARCHITECTURE.md` §5) — the target evolution to a real queue
  worker is identical to that document's existing target for document ingestion generally;
  memory promotion is just another producer into the same queue, not a separate pipeline.
- `STRUCTURED`/approval/conflict writes: low-frequency by nature (these are gated, reviewed,
  or explicitly authored — never a hot-path bulk operation), so they stay synchronous
  indefinitely; no scale pressure ever forces them onto a queue.

### Embedding cost containment

Only `SEMANTIC`-class content is ever embedded — `WORKING` memory (§2) and raw `EPISODIC`
events (§4) are never sent to an embedding provider unless/until explicitly promoted into
semantic memory. This bounds embedding-API cost growth to "how much content users actually
promote into durable, searchable knowledge," not "every message anyone ever sends," which
would scale embedding cost linearly with total platform activity instead of with genuinely
valuable retained knowledge.

### Data residency at global scale

`docs/AI_PROVIDER_ARCHITECTURE.md` §8's `DataResidencyPolicy` (per-organization compliance
routing for AI provider calls) extends directly to memory storage location: an organization
with a data-residency requirement gets its partitions (see above) physically hosted in the
required region — the same partitioning mechanism that bounds cost also bounds geography,
without a second, separate multi-region mechanism.

---

## 15. Migration path — additive, not a rewrite

| Phase | What changes | What doesn't |
|---|---|---|
| **0 (idag)** | `Conversation`/`Message` (episodic, USER scope), `DocumentChunk`+pgvector (semantic, USER scope), `CompanyInfo` (structured, degenerate ORGANIZATION scope), `AuditLog`, single global HNSW index, chat-time-only "last 20 messages" working memory | — |
| **1** | Introduce the `MemoryRecord`/`Provenance`/`TrustRecord`/`MemoryLifecycle` types (§1, §6, §7, §10) as a schema layer OVER today's tables (a view/adapter, not a migration that moves data) — existing `Conversation`/`Message`/`DocumentChunk` rows are read AS `MemoryRecord`s of the appropriate class/scope without changing their physical storage yet | Physical schema, existing routers |
| **2** | Redis-backed working-memory cache (§2) — a pure performance change, transparent to callers of the existing chat context-building code | Chat behavior, response content |
| **3** | `Organization` entity lands (`docs/MAINAI_ARCHITECTURE.md` §4 target — not started until a real second-tenant driver exists, per that document's own stated avoidance of speculative build) — `PROJECT`/`ORGANIZATION`/`SHARED` scopes become real instead of degenerate; table partitioning (§12) introduced at the same time, since it's far cheaper to partition from the start of multi-tenancy than to retrofit | `USER`-scope behavior (unaffected) |
| **4** | Version history (§8) and conflict resolution (§9) as an explicit mechanism — builds directly on phase 3's multiple real scopes, since single-scope conflicts are rare enough today not to need automated resolution | Everything built in phases 1–3 |
| **5** | Human approval workflow (§11) and the full audit-episode mechanism (§14) — policy table starts conservative (more actions gated than strictly necessary) and relaxes as corroboration/trust data (§6/§9) accumulates enough history to justify auto-resolution | Approval-exempt paths (already safe by construction — user's own USER-scope promotions) |
| **6** | Memory aging background job (§10) and cold-tier storage migration (§12) — the scale-critical phase, deferred until retained-history volume is large enough that HOT-forever storage would actually be expensive, not before | Retrieval correctness (aging only affects performance/cost, never which facts exist) |

Every phase ships independently and is a strict superset of what exists — consistent with
`docs/AI_PROVIDER_ARCHITECTURE.md` §11's migration philosophy: nothing here is a placeholder,
each phase is its own deliverable, and the "byggt kontra designat" gap this document describes
is exactly why `docs/MAINAI_ARCHITECTURE.md` §6's closing table says "designat, inte byggt."
