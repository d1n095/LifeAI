# Founder Alpha — Blueprint draft (items 8/9)

Items 1–7 of Founder Alpha are already verified end-to-end by #334's real-Ollama run
(41666998428 @ 82138cc): sign-in, model-backed chat, conversation persistence across restart,
document upload + immutable original preservation, indexing + retrieval with source references,
cross-owner isolation. Founder-only original download is #329/#330. The remaining gap was items
8/9 — generating an initial **Blueprint draft** from the Founder's indexed materials.

This adds that gap as composition over existing infrastructure (RAG retrieval for
source-grounding + the active local model for synthesis + a new owner-scoped store) — no new
retrieval/model/archive system.

## What it does
`app/blueprint/service.py::generate_blueprint_draft` takes retrieved source hits (e.g. from
`app.rag.vector_store.search`) + a synthesizer (the active local model in prod/CI; a
deterministic stub in local composition tests) and persists a `BlueprintDraft`:

- **Versioned** — each regeneration for the same `title` is a new immutable `version`.
- **Source-traceable** — `source_document_ids` / `source_refs` point at the immutable original
  Documents the retrieval returned; an ungrounded draft (no sources) is refused.
- **Draft pending Founder approval** — created `status="draft"`; only an explicit
  `approve_blueprint_draft` (a Founder action) moves it to `approved`. Life never self-approves.
- **Separated from originals** — the row is an AI interpretation only; generation never reads or
  mutates original bytes (`provenance.originals_mutated == False`).
- **Owner-isolated** — RLS by `owner_id` (app/rls.py), like document_chunks / knowledge_versions.

## Migration
`0070_blueprint_drafts` — additive, non-destructive, reversible; branches from this lineage's
single head `0069_embedding_space_identity`. (Merge note: PR #326's own `0069` also branches
from `0068`; whichever of #333/#326 merges first, `0070.down_revision` is re-chained onto the
resulting single head at merge time — standard per the repo merge-rule. No data/logic conflict.)

## Executed evidence (real Postgres)
`tests/backend/test_blueprint_draft.py` — 3 passed (migration 0070 applied + RLS): versioning;
source-traceability; draft-pending-approval + explicit Founder approval; originals-not-mutated;
ungrounded-refused; cross-owner RLS isolation. Migration verified single-head + reversible
(upgrade→downgrade→upgrade). The real-local-model synthesis (items 7→8 together) runs in the
#334-style Ollama CI; a stub synthesizer is used for the local composition tests (same split as
#336), never claimed as the real-model PASS.

## Status / boundaries
Built on #333 (`claude/embedding-space-local-model` @ fdf3647). **Independent review required —
author does not self-certify.** No GitHub writes, no merge, no paid providers, no STRATO, no
destructive migration; `first_cycle_go=false`. Depends on #333 (and the #334-verified RAG path).
