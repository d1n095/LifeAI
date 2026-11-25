# Context Atlas identity vs PR #501

PR #501 adds one acceptance test,
`backend/tests/founder_acceptance/test_founder_acceptance_suite.py::test_scenario_12_document_contradiction_citation_survives_restart`.
It proves the **declared contradiction** path over persisted claims, not byte identity.

## Reuse these existing functions

- `backend/app/models/document.py` — `Document.checksum` is already the sha256 of the
  original bytes. It is the authoritative exact-identity value; do not add a second hash
  column or identity table.
- `backend/app/rag/library_import.py` — `_import_one_file()` and
  `_resume_incomplete_document()` already use `Document.checksum` plus the
  content-addressed `storage_key` for import idempotency and crash recovery.
- `backend/app/rag/trust.py` — the only reader that interprets recorded
  `duplicates`/`contradicts` edges for retrieval.
- `backend/app/rag/claim_graph.py` — `record_claim_relationship()` is the one production
  writer for a declared `contradicts` edge. PR #501 calls it directly.
- `backend/app/rag/claim_graph.py` — `trace_evidence_chain()` exposes both sides of a
  declared contradiction for citation.
- `backend/app/rag/epistemics.py` — `epistemic_status_counts_sql()` and
  `format_epistemic_instructions()` carry the "present the conflict, never pick a winner"
  contract into retrieval.

## Context Atlas identity contract

`backend/app/context_atlas/identity.py`:

- `fingerprint(data)` hashes and normalizes **in-memory bytes** only.
- `compare_sources(left, right)` classifies two payloads as exact, normalized,
  one-character edit, near-duplicate, similar or distinct.
- Only `exact_byte_identical` is authoritative and only it proposes a `duplicates` edge.
- It stores nothing, writes no relationship, reads no claim, and preserves both originals.
- `proposed_relationship` is a review hint, not a dedup decision.

## What differs from PR #501

| Context Atlas identity | PR #501 contradiction scenario |
| --- | --- |
| Two byte payloads | Two persisted `Document`/`KnowledgeClaim` rows |
| No owner, DB session or RLS | Owner-scoped rows and RLS |
| Exact byte identity is authoritative | Contradiction is a human/declared relationship |
| Never writes an edge | Writes through `record_claim_relationship()` |
| Similarity is a review candidate | Conflict is surfaced, never auto-adjudicated |

## Do not duplicate

- Do not add another exact-identity hash or a second document-identity table; use
  `Document.checksum`.
- Do not make `compare_sources()` write `duplicates` or `contradicts` edges.
- Do not infer semantic contradictions from token similarity. A contradiction remains a
  declared act through the existing claim-graph writer.
