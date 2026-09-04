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
