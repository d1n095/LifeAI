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

The suite now contains 49 tests. `COMPLETE` is only possible when the caller declares expected
source classes and every one is covered without a failure/truncation; otherwise coverage is
`KNOWN_PARTIAL` or `UNKNOWN`. Structured polarity/value disagreement produces a contradiction
*candidate*, never a verified contradiction.

Remaining production P0s are deliberately outside this branch: real RLS-backed adapter attacks;
encrypted snapshot placement and mandatory trusted `base_dir`; durable generation/locking policy
with crash recovery; source-registry-backed locator existence/ownership validation; deletion
tombstone propagation from canonical stores; and authorization-safe API serialization. Remaining
P1s: morphology/entity resolution beyond bounded deterministic matching, reviewed alias-management
UI/workflow, scalable FTS/vector candidate generation, near-duplicate document/OCR detection,
large-corpus benchmarks, and model-assisted query interpretation as non-authoritative proposals.
