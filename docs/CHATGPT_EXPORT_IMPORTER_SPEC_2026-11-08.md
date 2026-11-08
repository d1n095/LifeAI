# ChatGPT Conversation-Export Importer — Implementation-Ready Specification

> **Status: SPECIFICATION ONLY — not implemented.** This is the next knowledge-bootstrap
> implementation candidate, preserved per the 2026-11-08 ingestion-readiness finding
> (`docs/FOUNDER_KNOWLEDGE_INGESTION_READINESS_2026-11-08.md`): the historical corpus includes
> 500–1000+ ChatGPT conversations, but the current importer supports only `.pdf/.docx/.txt/.md/.zip`
> and has **no `conversations.json` handler**. **No feature branch is started**, and no ingestion
> code is modified, while release-blocking CI remediation (#323 npm-audit, #344 E2E runner pin)
> remains unmerged. The existing **78-pass ingestion evidence is preserved** (unchanged). `first_cycle_go=false`.

## 1. Goal

Let Life ingest an OpenAI "Export data" archive (a ZIP containing `conversations.json`) into the
existing Library as **immutable, provenance-tracked, owner-isolated source units**, reusing the
current durable-worker / pgvector / Founder-Knowledge-Studio pipeline — **without** building a
parallel importer and **without** relaxing any existing safety control.

## 2. Input (treat as untrusted; validate, never assume)

- Delivery: the OpenAI export is a **ZIP**; `conversations.json` is one entry (often alongside
  `user.json`, `message_feedback.json`, media files). The importer must therefore ride on the
  **existing hardened ZIP path** (`app/rag/zip_import.py`: traversal + zip-bomb + nesting limits).
- `conversations.json` is (at time of writing) a JSON **array** of conversation objects; each has a
  `title`, `create_time`, and a `mapping` of `node_id → { message, parent, children }`, where a
  `message` carries `author.role` (`user`/`assistant`/`system`/`tool`) and `content.parts`.
  **The exact schema is provider-controlled and has changed over time** — the importer MUST parse
  defensively (schema-version detection, every field optional-until-validated, unknown fields
  ignored, never `KeyError`-crash) and **fail closed per-conversation** on anything it cannot
  confidently interpret, recording that conversation as `skipped`/`failed` with a reason, never
  silently dropping content.
- `.json` must be added to the supported-type set **only via this dedicated, security-reviewed
  path** — not as a generic "index any JSON" capability.

## 3. Mapping to the existing model

| Export concept | Life target (reuse) |
|---|---|
| One conversation | One `Document` (source), `storage_key` = content-addressed original bytes of that conversation (immutable), `DocumentSource.zip_import`. |
| The whole export ZIP | One `ImportJob` / `source_import_batch` (bounded, resumable, idempotent). |
| Each message node (linearized from the `mapping` tree along the active leaf→root path) | A source unit / chunk via the existing `chunking.py` (respect `MAX_CHUNK_CHARS`), tagged with `author.role` + `create_time` for provenance. |
| Export-level checksums | Per-file `sha256` + the existing nested-archive **provenance chain** (`zip_import.py`). |

**Linearization rule:** reconstruct each conversation's *current* thread (the mapping is a tree with
branches/regenerations); import the active path, and record branch points in provenance rather than
flattening ambiguously. Regenerated/alternate branches are preserved as provenance, not silently merged.

## 4. Security acceptance criteria (ALL must hold; each needs a test)

1. **Immutable original preservation** — the raw conversation bytes are stored content-addressed
   before any DB row; never mutated. (Reuse `storage_key` + `acquire_storage_key_lock`.)
2. **Checksum & provenance** — every imported unit traces to `{export_sha256 → conversation_id →
   node_id}`; the import manifest records them.
3. **Idempotency** — re-importing the same export (or overlapping exports) produces `duplicate`,
   never double-indexes (reuse checksum-idempotency + the distributed job lock).
4. **Bounded resource consumption** — per-conversation and per-export caps on count, bytes, and
   node depth; a pathological export cannot exhaust memory/time. Must include an explicit
   **max conversations / max total decompressed size** budget, enforced like the ZIP budgets.
5. **Decompression-bomb / traversal protection** — inherited from `zip_import.py`; the JSON parser
   must additionally bound total parsed size and nesting (a 2 GB `conversations.json` must be
   rejected, not loaded whole).
6. **No code/markup execution from content** — message text is data only; never evaluated, never
   rendered as HTML/script; no URL in content is auto-fetched.
7. **No unauthorized external AI egress** — embedding goes through the existing egress gate
   (`EgressDeniedError`); a `NEVER_EGRESS`-marked import is refused before any provider call.
8. **Owner isolation** — every chunk is `owner_id`-scoped under `document_chunks_isolation` RLS;
   one Founder's export can never land in another owner's scope.
9. **Explicit Founder approval** — ingestion is authorized (Founder Knowledge Studio); nothing is
   promoted to a Blueprint source without the existing Founder-approval gate.
10. **Fail-closed on malformed input** — a corrupt/unknown-schema conversation is recorded as a
    per-item failure with a safe reason; the batch continues (retryable), the job never crashes,
    and no partial/garbage document is published.
11. **PII minimization** — do not copy `user.json` identity fields into indexed content; import
    conversation text only. No secrets/credentials found in content are surfaced in logs.

## 5. Test plan (synthetic fixtures only — never real exports)

- Unit: defensive parser against (a) current-schema sample, (b) an older-schema sample, (c) a
  malformed/truncated sample → fail-closed, (d) a branchy `mapping` (regenerations) → active-path +
  provenance.
- Integration (reuse the rag test harness; see the 78-pass suite): export-as-ZIP round trip →
  immutable original, checksum/provenance, idempotent re-import, owner isolation, egress-denied path,
  bounded-resource rejection of an oversized synthetic export, resumable after a simulated transient
  failure.
- Negative: a ZIP whose `conversations.json` is a decompression bomb / 2 GB → rejected without OOM.

## 6. Out of scope / sequencing

- **Do not implement now.** Start the feature branch only **after** the release-blocking CI
  remediation (#323, #344) is resolved, and after security review of this spec.
- Reuse-first: no new ingestion engine; extend `library_import.py` + `zip_import.py` behind the new
  `.json`/ChatGPT-export path. Blueprint promotion remains gated by the existing Founder-approval and
  the Codex(#360)/Copilot(#361)-owned egress remediation.

*Builder (Claude Code). Specification-only; review-pending. No code changed; 78-pass ingestion evidence preserved.*
