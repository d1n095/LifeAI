# Universal Personal Recall — isolated foundation

Branch: `codex/universal-personal-recall`  
Baseline: certification candidate `818dfb7` (unchanged)

## Architecture map

`PersonalSourceAdapter` → owner-scoped canonical `PersonalKnowledgeItem` → lifecycle filtering
→ exact/entity/fuzzy/optional-semantic scoring → strong-identity deduplication → version,
supersession and contradiction reconciliation → inspectable `RecallResponse`.

The canonical item is a read projection, not a new source of truth. It carries locators and
references rather than requiring duplicated raw content. Existing `memory_source_units`,
`message_source_units`, `document_source_units`, `KnowledgeVersion`, `SourceRelationship`,
founder memory, project entities and intent objects are intended adapter inputs.

## Contracts

- Query intents: broad recall, latest state, timeline, change/decision history, source/file
  lookup, contradiction search, version comparison, why-changed and evidence display.
- Scores remain separate: exact, semantic, entity, temporal and aggregate relevance.
- Semantic score alone cannot establish subject identity.
- `MENTION`, `IDEA`, `ASSUMPTION`, `CLAIM`, `DECISION`, `PLAN`, `IMPLEMENTATION`,
  `VERIFIED_RESULT`, `REJECTED`, `SUPERSEDED`, and `UNKNOWN` remain distinct.
- `PARTIAL`/`STALE` indexing produces an explicit completeness warning. Deleted and failed
  items cannot be returned as current.
- Provenance always includes source class, durable source id and an openable local locator.
- Retrieval is local-only and emits no telemetry.

## Integration boundary

This foundation is deliberately **not wired** into chat, API routes, workers, migrations,
provider calls, or Claude's V2 identity/recovery implementation. Store-specific SQLAlchemy
adapters, vector-store scoring, OCR/extraction workers and UI source-opening are future
integration points. Any database adapter must use existing RLS and owner keys; the defensive
owner check in the aggregator is a second boundary, not a replacement for RLS.

## Known limitations and risks

P0 before production wiring: implement and attack real owner-scoped adapters; make snapshot
storage use the product's encrypted/local storage policy; validate every locator before open;
define deletion propagation and rebuild atomicity for persisted snapshots.

P1: Swedish/English query rules are deterministic but intentionally small; optional semantic
scoring has no embedding implementation here; version reasoning depends on explicit edges;
subject clustering is lexical; large-corpus pagination/ranking benchmarks remain; OCR and
near-duplicate image/PDF detection remain adapter responsibilities.

## Adversarial hardening round (2026-09-04)

The second round reproduced and fixed these bug classes in the isolated kernel:

- substring matches could establish subject identity (`HAp`/`app`, `fluor`/`fluorid`);
- aliases were global observations with no owner/project/domain/time or verification scope;
- recency could outrank truth, future dates received maximum recency, and any `valid_until`
  value was treated as already expired;
- missing and cyclic supersession targets were silent, as were concurrent current candidates;
- source authority and epistemic state were preserved but did not influence ranking;
- contradictions without explicit edges had no bounded candidate representation;
- stale hashes could collapse provenance without verification;
- snapshots had no owner, generation, checksum, permission, size, path, symlink or concurrent
  save guard;
- broad recall could imply completeness without a coverage contract;
- locators had no inert validation boundary;
- personal raw text could appear in default object representations;
- item count, result count, text, alias registry and contradiction candidate work were unbounded.

That hardening stage contained 54 tests. `COMPLETE` is only possible when the caller declares expected
source classes and every one is covered without a failure/truncation; otherwise coverage is
`KNOWN_PARTIAL` or `UNKNOWN`. Structured polarity/value disagreement produces a contradiction
*candidate*, never a verified contradiction.

The follow-up hardening pass made trusted snapshot-root policy mandatory, replaced stale-file
locking with kernel advisory locks, bound locator validation to a canonical owner-scoped source
registry, and added owner-gated local-client serialization that omits raw query/content by default.

The real-adapter pass below closes the RLS, canonical tombstone and local serialization items.
Authenticated snapshot encryption and future route-level authorization remain production P0s.
Remaining P1s: morphology/entity resolution beyond bounded deterministic matching, reviewed alias-management
UI/workflow, scalable FTS/vector candidate generation, near-duplicate document/OCR detection,
large-corpus benchmarks, and model-assisted query interpretation as non-authoritative proposals.

## Real read-only adapters (2026-09-05)

`app.personal_recall.sqlalchemy_adapters` now projects the canonical LifeAI models without a
new table or write path:

- conversations/messages, preserving user vs assistant authority;
- documents and document chunks, excluding soft-deleted sources;
- active `memory_source_units`, excluding revoked/purged lifecycle rows;
- knowledge versions and source supersession/contradiction relationships;
- founder-memory, project-entity and life-problem decision records where canonical rows exist.

Every adapter is constructor-bound to one authorized owner and every SQL statement repeats
that owner predicate even under RLS. Real PostgreSQL tests migrate a fresh database, use the
restricted `mainai_app` role, seed Alice and Bob data in every covered source class, then bind
the database session to Alice while deliberately querying Bob. All five adapter families
return zero rows under that attack. A seeded `allt om tandkrämsrecept` scenario returns all
five expected source classes while excluding Bob's content, a soft-deleted document and a
revoked memory source.

`SQLAlchemySourceRegistry` rechecks canonical owner, existence, lifecycle and exact
chunk/version locator immediately before open. `synchronize_authoritative_sources()` marks
snapshot entries deleted and scrubs their personal content after a complete canonical refresh;
it refuses to infer deletion when an adapter fails, truncates or lacks declared coverage.
Owner-authorized local serialization is exercised against the real adapter response.

The real-adapter milestone contained 62 passing tests: 56 provider/DB-independent
tests plus 6 tests against real PostgreSQL migrations and RLS. No live chat/API/worker wiring
was added. Authenticated snapshot encryption remains P0: no suitable reviewed AEAD dependency
is currently present, so this branch deliberately does not invent cryptography or shell out to
an unauthenticated cipher.

## MainAI integration bridge

The isolated bridge exposes `PersonalRecallService` without registering HTTP routes. Its
authorization contract binds the canonical owner to an already authenticated request identity,
session JTI/issue time/expiry, allowed source classes, project and conversation scopes,
current/history grants, disclosure level, and separate locator-open permission. Canonical account
revocation and JTI state are re-read both before retrieval and immediately before disclosure/open.

MainAI receives a structured evidence handoff retaining provenance, source authority,
verification, decision/current/history state, contradictions, why-matched explanations, aggregate
coverage, and per-source coverage. Context hints can narrow retrieval but cannot grant authority.
Source opening returns only an inert validated locator and a bounded authorized snippet; it never
executes a source. No HTTP/chat route is wired.

`RecallSnapshotProtector` defines the future V2 key-hierarchy seam. The only implementation in
this branch is explicitly test-only, deterministic, and labelled as non-encryption; production
snapshot protection remains blocked on reviewed AEAD/key-hierarchy integration.

The complete suite now contains 79 passing tests, including real migrated-PostgreSQL checks for
canonical session epochs, JTI revocation, and a foreign-owner authority attempt under the
restricted runtime role.
