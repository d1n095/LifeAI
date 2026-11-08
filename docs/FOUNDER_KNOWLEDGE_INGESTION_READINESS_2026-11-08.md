# Founder Knowledge Ingestion Readiness — Capability Matrix (2026-11-08)

> **Purpose.** Assess whether the existing Library / durable-worker / pgvector / Founder
> Knowledge Studio / Blueprint ingestion architecture can safely process the Founder's
> historical corpus, and state exactly **what already works (with test evidence)**, **what is
> unverified**, and **what truly needs implementation**. Assessment only — no frozen candidate
> is modified; `first_cycle_go=false`.

## Evidence basis

- **Code:** `backend/app/rag/{ingest,library_import,zip_import,chunking,extract,claims,retrieve,vector_store,media_import}.py`, `backend/app/routers/library.py`, `backend/app/models/{import_job,source_import_batch}.py`.
- **Executable tests re-run this session** (py3.12 venv + local Postgres): the subset below returned **78 passed, 1 skipped in ~35s**:
  `test_zip_import_security.py`, `test_zip_import_capacity.py`, `test_rag_ingest_egress.py`, `test_chunking.py`, `test_library_import.py`, `test_library_import_job_lock_fail_closed.py`, `test_claim_interpretation_proposal_capture.py`.
  Other cited tests exist in the repo's suite but were not re-run this session (noted as *repo-covered*).

## Part 1 — Required safety properties

| Property | Status | Evidence |
|---|---|---|
| Immutable original-source preservation | **WORKS (verified)** | Content-addressed `storage_key`; "bytes exist before any DB row" (`library_import.py:169`); `acquire_storage_key_lock` reference-locking. Tests: `test_store_bytes_with_reference_lock_*` (passed this session). |
| Checksum & provenance tracking | **WORKS (verified)** | Per-file `hashlib.sha256` + a nesting **provenance chain** (`chain_prefix` of filename+checksum through nested archives, `zip_import.py:343–375`); manifest checksums. Test: `test_manifest_checksum_mismatch_rejects_that_file_only` (passed). |
| Idempotent imports | **WORKS (verified)** | Checksum-idempotency marks a re-import `duplicate` (`library_import.py:92`); distributed job lock. Tests: `test_concurrent_duplicate_import_is_protected_by_the_distributed_lock`, `test_permanent_failure_is_not_retried` (passed). |
| Bounded resource consumption | **WORKS (unit) / UNVERIFIED (corpus scale)** | `MAX_CHUNK_CHARS=6000` (`chunking.py:12`); ZIP capacity budgets. Tests: `test_chunking`, `test_zip_import_capacity` (passed). Full-corpus (1000+ conversations) memory/time behavior not load-tested — see Part 2 #5. |
| Archive traversal & decompression-bomb protection | **WORKS (verified)** | `ZipSecurityError` on path traversal / zip bomb / too-many-files; `MAX_NESTING_DEPTH`; cumulative decompressed-size budget "never reset per level" (`zip_import.py:9,24,53`). Tests: `test_zip_import_security`, `test_zip_security_violation_fails_the_job_cleanly_not_a_crash` (passed). |
| Retryable failures | **WORKS (verified)** | Auto-resume for ImportJob-backed imports (`ingest.py:67`); `awaiting_provider`/`blocked_provider` park; bounded attempts. Tests: `test_a_genuinely_job_level_transient_error_exhausts_attempts_and_fails`, `test_permanent_failure_is_not_retried` (passed); media provider-pause *repo-covered*. |
| Separation: source truth / interpretation / proposed-Blueprint | **WORKS (verified, partial scope)** | `KnowledgeClaim` interpretation layer is distinct from the immutable source; proposal capture is separate. Test: `test_claim_interpretation_proposal_capture` (passed). Blueprint-requirement separation is the subject of the Mission-3 gap analysis. |
| No unauthorized external AI egress | **WORKS (verified)** | `EgressDeniedError` gate in `ingest.py:107`, `retrieve.py:21`, `media_import.py:215` (Life Vault / External-AI Egress Control V2). Test: `test_rag_ingest_egress` (passed). |
| Owner isolation & explicit Founder approval | **WORKS (owner isolation verified) / approval present** | `owner_id` required for chunks + `document_chunks_isolation` RLS (`ingest.py:19–28`, `vector_store.py`). Job-lock fail-closed: `test_library_import_job_lock_fail_closed` (passed). Founder approval exists via Founder Knowledge Studio + `blueprint_approve_draft` (Founder-only, DB-enforced) — *repo-covered*. |

## Part 2 — Corpus-type capability (Mission-2 items 1–8)

| Corpus input | Status | Evidence / gap |
|---|---|---|
| 1. Historical **ChatGPT conversation exports** | **GAP — NEEDS IMPLEMENTATION** | No ChatGPT/`conversations.json` handler exists (grep: none), and **`.json` is not a supported extension** — supported set is `.pdf/.docx/.txt/.md/.zip` (`library_import.py:55–58`). A raw export would be rejected as unsupported. The corpus holds 500–1000+ such conversations (`MAINAI_CONTEXT_BUNDLE.md:53`). Requires a dedicated, security-reviewed importer (parse the export's message tree → per-conversation source units) — a feature, not a small fix. |
| 2. Existing Life architecture documents (`.md`) | **WORKS** | `.md` supported; extraction is plain-text. |
| 3. **Markdown / TXT / PDF / DOCX** | **WORKS (txt/md/pdf) / PARTIAL (docx completeness)** | `.txt/.md/.pdf/.docx` supported; `extract.py` handles `.pdf` and `.docx`. DOCX table/nested-table/header completeness is the subject of **PR #327 (open, not merged)** — until merged, DOCX extraction completeness is **unverified** for complex documents. |
| 4. **ZIP archives & nested folders** | **WORKS (verified)** | Recursive nested-archive extraction with depth + size + count limits (`zip_import.py`). Tests passed (Part 1). |
| 5. **Large multi-document imports** | **PARTIAL / UNVERIFIED at scale** | `source_import_batch` + `corpus_batch.py` provide batch orchestration (`test_source_import_batches` *repo-covered*). Per-file resource limits exist, but the **full-corpus** run (1000+ conversations + 19 docs) has no load/soak evidence — memory, time, and provider-pause behavior at that volume are unverified. |
| 6. **Duplicate & conflicting requirements** | **PARTIAL** | Exact-content duplicates: **WORKS** (checksum dedup → `duplicate`). **Semantic** duplicate / contradiction detection: **GAP** — not an ingestion capability today (covered in the Mission-3 Blueprint-source gap analysis). |
| 7. **Superseded architectural decisions** | **PARTIAL** | The reconciliation machinery exists at the memory layer (`LIFE_FOUNDER_MEMORY.md`: `status=superseded`, `supersedes_note_id`, never silent overwrite) and in `founder_vault` (supersede-not-overwrite). But **ingestion does not auto-detect supersession** — it preserves each source immutably; supersession is a later, authority-gated decision, not an import-time one. |
| 8. Historical docs with **outdated implementation assumptions** | **WORKS by design (preserve, don't correct)** | Ingestion preserves the original immutably and never edits it; outdated assumptions are reconciled later via provenance + supersede, not at import. This is the correct conservative behavior for a source-of-truth store. |

## Part 3 — Exact capability summary

**Already works (code + tests, verified this session unless noted):** immutable original preservation; checksum + provenance (incl. nested-archive chains); idempotent imports + distributed lock; decompression-bomb / path-traversal protection; retryable/resumable failures with provider-pause; egress control; owner RLS isolation; source-vs-interpretation separation; `.md/.txt/.pdf/.docx/.zip` ingestion; nested-ZIP traversal.

**Unverified (exists but not proven for this corpus):** full-corpus-scale resource behavior (1000+ items); DOCX completeness for complex documents (pending PR #327); approval-gated end-to-end run of the *actual* historical corpus.

**Truly needs implementation:**
1. **ChatGPT-export importer** (`conversations.json` / `.json` type) — the single biggest ingestion gap for the historical corpus.
2. **Semantic duplicate / contradiction detection** across requirements (Mission-3 territory).
3. **Corpus-scale load evidence** (a soak import of a representative large batch).

## Part 4 — Isolated-test opportunity (identified, not yet taken)

A genuinely unowned, low-risk regression test would **assert fail-closed handling of an unsupported type** (e.g. a raw `conversations.json`) so the current "reject, don't silently mishandle" behavior can't regress before the real importer exists. This is a candidate for a narrow follow-up (its own branch); it is **not** bundled into this documentation change. No ChatGPT-importer feature is started here (out of scope; security review required; must respect egress + owner isolation + Founder approval).

## Boundaries

Assessment only. No modification to frozen #352 or its outstanding security gates; no change to the ingestion code in this deliverable. Synthetic reasoning + the repo's own tests only — no private corpus content ingested or reproduced. `first_cycle_go=false`.

*Builder (Claude Code). Documentation-only; review-pending.*
