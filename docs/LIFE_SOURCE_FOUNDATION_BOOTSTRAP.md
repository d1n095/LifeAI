# Life — Source Foundation Bootstrap (Implementation Proposal)

**Status: PROPOSAL ONLY. No code written.** This is the founder-requested minimal slice that
breaks the bootstrap circularity flagged after reviewing `docs/LIFE_CANONICAL_ARCHITECTURE.md`
(the Provisional Canonical Architecture / Bootstrap Map, 2026-08-11): Life needs to read the
founder's full corpus to know exactly what to build, but reading that corpus systematically and
provably requires a secure, deterministic, AI-independent intake foundation first. This document
is that foundation's design — nothing more. It does **not** include Founder HQ, Life App,
Business/Commerce, domain modules, or any further MainAI capability. Its only job: make it
possible to get the *entire* founder corpus into Life, correctly, provably, and resumably,
so the *next* pass can do the real, final Canonical Life Architecture Recovery from actual
source truth instead of from `docs/` alone.

## Why this is small on purpose

Every piece of infrastructure below already exists in some form. This proposal's job is to
identify the smallest set of additions that connects them into one intake path, not to design a
new system. Section A makes this explicit before anything else, because the biggest risk in a
proposal like this is scope creep disguised as thoroughness.

---

## A. Existing components reused

| Component | Role in the bootstrap | Change needed |
|---|---|---|
| `LocalFilesystemStorage` (`app/storage/local_fs.py`) | Immutable, content-addressed original storage | Extend enforcement, not replace (§D) |
| `documents`, `document_chunks` | Document-shaped sources | Unchanged |
| `memory_source_units`, `document_source_units`, `memory_source_lifecycle_events` (S1A) | Universal, owner-scoped, `SECURITY DEFINER`-gated provenance layer | Unchanged — this bootstrap's new source types (§G) become new subtype tables in the *same* pattern, not a parallel one |
| `knowledge_import_jobs` (`ImportJob`) | Per-file/per-ZIP upload tracking, partial/blocked/cancelled status, resumable | Unchanged — stays scoped to file/ZIP uploads exactly as `MAINAI_PROJECT_UNDERSTANDING_PLAN.md` §6.12 already decided it should |
| `mainai_jobs`/`mainai_job_events`/`mainai_job_proposals`, `app/jobs/` (lease, heartbeat, retry) | Durable, crash-recoverable, lease-fenced job runtime — proven via V0.1-V0.3's crash-recovery demo matrix | Add new `job_kind`s (§H), no runtime changes |
| `app/rag/zip_import.py` | Nested/encrypted/zip-bomb-safe archive extraction, magic-byte verification, shared budget | Unchanged; `ALLOWED_EXTENSIONS` extended, not the safety model (§F) |
| `app/rag/extract.py` | Deterministic text extraction (PDF/DOCX/HTML today) | Extended with new format branches (§F), same dispatch pattern |
| `app/rag/backfill/` (`memory_source.py`, `memory_source_run.py`, `message_sequence.py`) | Proven, tested backfill pattern (batch-limited, resumable, idempotent) | Reused directly for the new S1C backfill (§G) |
| `messages`, `messages.sequence_number` (S1B) | Ordered, RLS-isolated (migration 0031) message storage | Unchanged — this bootstrap's ChatGPT importer writes *into* it |
| `provider_verification_checks` (P1) | Pre-flight provider health, `awaiting_provider`/`blocked_provider` states | Unchanged, referenced by §O |

**Nothing above is replaced.** The bootstrap's actual new surface area is small: one new
schema layer (a corpus manifest, §E), one new source subtype (messages as `memory_source_units`,
i.e. S1C — already designed, not invented here), a small number of new parsers (§F/§G), and one
concrete privilege-hardening step for blob storage (§D).

---

## B. Exact schema changes required

All additive, all EXPAND-only in this pass (no destructive change, per the founder's §24 and
§15 prohibitions):

1. **`source_import_batches`** (new) — the corpus manifest, §E.
2. **`message_source_units`** (S1C, already designed in `MAINAI_PROJECT_UNDERSTANDING_PLAN.md`
   §4.8/§8, not built) — `Message` as its own `memory_source_units` subtype, same exclusive-arc/
   composite-FK/immutability pattern as `document_source_units`.
3. **`chatgpt_export_imports`** (new, small) — the structural mapping table between one ChatGPT
   export archive and the `conversations`/`messages` rows it produced (§G); exists so a partial
   or re-run import can be verified/reconciled without re-parsing the whole archive.
4. **`documents.source_import_batch_id`** (new, nullable FK) — links an ingested document back
   to the batch it arrived in, for §P's completeness proof.
5. No changes to `memory_source_units`'s own columns — the exclusive-arc pattern already
   supports adding a new subtype without touching the parent table (proven by S1A's own design
   note on this exact extensibility property).

## C. Exact storage changes required

**None to the storage engine's write/read/dedup logic.** `LocalFilesystemStorage` already does
everything the founder's UPLOAD→hash→immutable-store chain requires (§1 of
`LIFE_SOURCE_VAULT_AND_MEMORY_ARCHITECTURE.md`). The only change is a privilege boundary, not a
mechanism — see §D.

## D. Immutability enforcement — the one genuine gap

This is the P0 invariant the founder named explicitly, and the one place this proposal adds a
real new control rather than reusing an existing one.

**Today:** `LocalFilesystemStorage`'s immutability is a *code convention* — content-addressed
naming plus atomic temp-file→fsync→rename means there is no code path that edits a blob in
place, but nothing stops a bug or a future code path from calling `unlink()`/`open(..., "w")`
directly against a blob the reference-counting purge logic doesn't know about.

**Proposed enforcement, layered (matches the S1A privilege model exactly, not a new
mechanism):**

1. **OS-level:** run the storage root's write path through a narrow, dedicated filesystem user/
   permission set (blob files `0444` after write-and-fsync, directory `0555` except during the
   atomic-rename window) — a concrete, realistic control given local-filesystem storage, and
   explicitly the honest boundary: this is enforced by the OS, not by Postgres, and a process
   running as the same OS user as the writer could still bypass it. **This trust boundary is
   named here explicitly, not hidden** — the same discipline the founder's mandate itself
   demanded ("designa det säkraste realistiska enforcement-lagret och dokumentera exakt vilken
   trust boundary som återstår").
2. **Application-level:** a single write path (`app/storage/local_fs.py`'s existing `write`/
   `write_stream`) remains the *only* code allowed to create a blob; the existing AST-allowlist
   test pattern (`test_storage_local_fs.py`'s established discipline, already proven for
   write-path centralization) is extended to also forbid any `os.remove`/`Path.unlink`/`open`
   call against the storage root outside `app/storage/purge.py`'s own reference-counted delete
   — this is a test-suite change, not a runtime change, and follows a pattern this codebase
   already trusts.
3. **DB-level (the part that's genuinely free — S1A already built it):** `memory_source_units`'s
   `content_text`/`content_hash` (the DB-side record of the original) already can't be
   `UPDATE`d/`DELETE`d directly by `mainai_app` — only through
   `transition_own_memory_source(..., 'purged', ...)`, which is itself owner-checked and
   auditable. This bootstrap extends the *same* revoke/`SECURITY DEFINER` pattern to
   `documents.storage_key`/`file_path` (today unprotected columns that name a blob) — narrow,
   additive, no new mechanism invented.
4. **Verification:** a scheduled job (same shape as `app/cleanup.py`'s existing advisory-lock-
   guarded jobs) re-hashes a sample of stored blobs against their recorded `content_hash` and
   raises an audit event on any mismatch — tamper *detection* as the last line, complementing
   (not replacing) prevention.

## E. Import batch / corpus manifest model

```
source_import_batches
  id, owner_id
  label                      -- e.g. "Founder Corpus Import 2026-XX-XX"
  source_description          -- free text, e.g. "FKP_curated + Life_OS_Claude_Handoff"
  status                       -- discovering | importing | completed | partial | failed
  discovered_files, discovered_archives, discovered_conversations,
  discovered_messages, discovered_attachments, discovered_bytes
  stored_originals_done, stored_originals_total
  parsed_done, parsed_total
  duplicate_count, failed_count, unsupported_count, semantic_pending_count
  created_at, completed_at

source_import_batch_failures
  batch_id, source_ref, reason, retryable (bool), created_at
```

This is the object that answers the founder's §6 requirement directly and provably: "Founder
Corpus Import 2026-XX-XX: discovered N files/N archives/N conversations/N messages/N
attachments/N bytes; stored originals N/N; parsed N/N; duplicates N; failed N; unsupported N;
semantic pending N." Every count is a real, queryable aggregate over rows that reference
`source_import_batch_id` (§B.4, and equivalent FKs on future source tables) — not a
self-reported summary a job writes once and could drift from reality.

## F. Parser / format routing

Verified against the actual code this session (`app/rag/zip_import.py`'s
`ALLOWED_EXTENSIONS = {.pdf, .docx, .txt, .md, .markdown, .json, .html, .htm}`,
`app/rag/extract.py`'s PDF/DOCX/HTML branches):

| Format | Status today | Bootstrap action |
|---|---|---|
| ZIP (incl. nested, encrypted-detected) | IMPLEMENTED | Reuse as-is |
| PDF, DOCX, TXT, MD, JSON, HTML | IMPLEMENTED | Reuse as-is |
| CSV, XLSX | **Not supported today** — not in `ALLOWED_EXTENSIONS`, no extractor branch | Add: deterministic (no AI) — CSV/XLSX are structured, parseable without a model; smallest addition in this whole proposal |
| Images / binary attachments | Not supported as a first-class source | Add: store as an immutable binary original (Source Vault, no different from any other blob) with `PARSER_MISSING`/`UNSUPPORTED` marked for semantic understanding — never dropped |
| Repo/code trees | Not supported as import input today (MainAI reads GitHub directly via `app/integrations/github_client.py`, a different, already-real path) | Deliberately out of scope for this bootstrap — code already has its own real ingestion path; do not build a second one |
| Anything else (unknown extension) | N/A | `status=UNSUPPORTED`/`PARSER_MISSING`, source still stored and batch-counted (§8's exact requirement: "ingen source får försvinna bara för att capability saknas") |

## G. ChatGPT structural ingestion

**Explicit acknowledgment before design:** this session has not seen a real ChatGPT export
file — `~/Documents/mainai_intake/chatgpt_export/` is on the founder's Mac, not in this repo or
session. The design below is grounded in the *publicly documented* ChatGPT export shape
(`conversations.json` — an array of conversation objects, each with a `mapping` of node-id →
`{message, parent, children}` forming a tree, not a flat list — plus a top-level
`conversation_id`/`title`/`create_time`), which is why S1C/S2's design already anticipates
parent-linkage rather than pure sequential order. **Before implementation, not before this
proposal:** the founder should supply one real (even small/redacted) export file so the parser
is validated against actual structure, not documentation — flagged here explicitly per the
founder's own §7 instruction, not skipped.

Mapping (design, pending that validation):

```
chatgpt_export_imports
  id, owner_id, source_import_batch_id
  source_document_id           -- FK to documents -- the export ZIP itself, Source Vault original
  export_conversation_id         -- ChatGPT's own conversation id, as-exported
  conversation_id                 -- FK to our own conversations row created for it
  message_count, imported_message_count
  status                            -- pending | importing | completed | partial | failed
```

Per conversation: one `conversations` row (owner = the founder, `source='chatgpt_export'` — an
addition to `DocumentSource`-shaped provenance, not a new concept). Per message: one `messages`
row with `sequence_number` assigned by the existing S1B trigger (tree order flattened to a
deterministic sequence — the mapping's parent-pointers are preserved separately, see below, so
flattening for `sequence_number` does not lose the tree). Per message, a `message_source_units`
row (S1C) carrying: `export_conversation_id`+ChatGPT's own message id (the
`source_identity_key`, exactly analogous to `document_chunk:<chunk_id>`), parent-message-id
(preserving the tree ChatGPT actually exported, not just a flat sequence), role
(user/assistant/tool), timestamp, and a pointer back to `chatgpt_export_imports`/the original
export archive blob. Attachments referenced by a message become their own Source Vault originals
(§D), linked via a `document_source_units`-style subtype, not embedded as message text.

**No semantic analysis in this bootstrap** (§12's explicit constraint) — the importer's job ends
at "every message is a correctly-linked, correctly-ordered `message_source_units` row"; claim
extraction, thread creation, and cross-conversation linking are Life Memory work, deliberately
deferred (§O).

## H. Background job model

New `mainai_jobs` `job_kind`s, dispatched by the existing `app/worker.py` pattern (no runtime
change, per §A):

- `corpus_batch_discover` — walk an uploaded root (could itself be a large ZIP or a folder of
  files), populate `source_import_batches`' `discovered_*` counts, without processing content
  yet — matches the founder's UPLOAD→validate→hash step ordering (§9 of the Canonical
  Architecture pass: discovery is cheap and should happen before commitment to full processing).
- `corpus_source_ingest` — one job per discovered file/archive-entry: hash, store original,
  route to a parser (§F), or mark unsupported.
- `chatgpt_export_ingest` — one job per discovered ChatGPT export archive (§G).
- `message_sequence_backfill` (S1B) and a new `message_source_unit_backfill` (S1C) — reuse the
  exact `app/rag/backfill/` pattern already proven for `memory_source_units`/`message_sequence`.

## I. Retry / checkpoint / resume

No new mechanism — this is exactly what `mainai_jobs`' lease+heartbeat+retry already guarantees,
proven across V0.1-V0.3's 7 required crash-recovery demos (dead-before-work, dead-after-edit,
dead-after-commit, dead-after-push, dead-after-PR, stale-worker-returns, ambiguous-state). A
`corpus_source_ingest` job that dies mid-file is picked up by the same dead-job detection V0.2
already built, with no bootstrap-specific recovery logic required.

## J. Error / unsupported handling

Per §5/§8's explicit requirement — **UNKNOWN is a valid, final result, never invented
semantics.** Concretely: a file with no matching parser gets `status=UNSUPPORTED` (stored,
batch-counted, never dropped); a file that fails mid-parse gets `status=PARSE_FAILED` with the
real exception recorded (never silently swallowed — same discipline `IndexStatus`'s six-state
split already established for provider-vs-content failures, `MAINAI_PROJECT_UNDERSTANDING_PLAN.md`
§4.7); a ChatGPT message with a malformed/missing parent pointer gets `occurred_at_basis=unknown`
and `parent_message_id=NULL` rather than a guessed reconstruction — silence about uncertainty is
exactly the failure mode §12 forbids.

## K. Security boundaries

- Founder-only system — no cross-owner concern for corpus import itself (single owner), but
  the exact same RLS/ownership discipline applies to every new table (§L) because that
  discipline is proven, tested, and cheap to keep consistent, not because multi-tenancy is
  imminent.
- Archive safety (zip-slip, decompression bombs, nested archives, path collisions) — fully
  reused from `app/rag/zip_import.py`, no new attack surface introduced by CSV/XLSX parsing
  (both are non-executable, deterministically parseable formats with mature, safe libraries).
- Immutability (§D) is the security-relevant novel work in this proposal.

## L. RLS / privileges

- `source_import_batches`, `source_import_batch_failures`, `chatgpt_export_imports`: owner-
  scoped RLS, `FORCE ROW LEVEL SECURITY`, following the exact pattern every table since
  migration 0006 has used.
- `message_source_units` (S1C): same RLS/privilege model as `document_source_units` (S1A) —
  already fully designed in `MAINAI_PROJECT_UNDERSTANDING_PLAN.md` §4.8, not redesigned here.
- `documents.storage_key`/`file_path` privilege narrowing (§D.3): `apply_runtime_privileges`
  (the existing boot-time privilege-verification step) gets two more columns' worth of
  `REVOKE`, verified via the same `has_table_privilege` boot check already proven for S1A.

## M. Tests

Following the project's own established discipline (mutation-tested regression tests for every
guard, not just happy-path coverage):

- Corpus manifest counts genuinely match reality (insert N sources across 2+ batches, assert
  `source_import_batches` aggregates match actual row counts, not a self-reported number).
- Crash-mid-ingest resumability (kill a `corpus_source_ingest` job mid-file, assert the next
  worker tick picks it up, no duplicate `memory_source_units` row created — reuses S1A's
  existing find-or-create idempotency proof).
- Immutability: `mainai_app` cannot `UPDATE`/`DELETE` `documents.storage_key`/`file_path`
  directly (mutation-tested the same way S1A's own privilege tests are).
- CSV/XLSX parser: malformed/huge/formula-injection-shaped input handled safely (formula
  injection — a leading `=`/`+`/`-`/`@` in a CSV/XLSX cell — is a real, known risk class for
  spreadsheet parsers and must be in the test list, not assumed away).
- ChatGPT importer: parent-pointer preservation, `sequence_number` assignment correctness
  against S1B's existing collision-freedom proof, attachment linkage, and the explicit
  "malformed message still stored, marked unknown, never dropped" case (§J).
- End-to-end: a synthetic multi-format batch (ZIP containing PDF+DOCX+CSV+a nested ZIP+an
  unsupported file type) processed through the real pipeline, `source_import_batches`'
  completeness counts verified against ground truth afterward (§P).

## N. Migration order

1. `source_import_batches` + `source_import_batch_failures` (independent, no dependencies).
2. S1C (`message_source_units`) — already fully designed, this is "build what was already
   approved in principle," not new design work; independent of the batch manifest.
3. `chatgpt_export_imports` + `documents.source_import_batch_id` (depends on #1).
4. `documents.storage_key`/`file_path` privilege narrowing (§D.3) — independent, can land
   anytime, should land early since it's the P0 invariant.
5. New `mainai_jobs` `job_kind`s (§H) — code-only, no migration, depends on #1-3 existing.

## O. What is deliberately deferred to semantic Life Memory

Per §12's explicit constraint, this bootstrap does **not** build: claim extraction from
imported material, Memory Thread creation/membership, conversation segmentation (S2),
project-entity/decision/idea classification (P4), founder-memory-note creation (P6), or any
cross-source semantic linking. All of that reads the deterministic layer this bootstrap
produces — none of it is a prerequisite for the bootstrap itself.

## P. How we verify "all sources imported"

`source_import_batches`' `discovered_*` counts (populated by the discover job, §H, before any
processing) versus its `stored_originals_done`/`parsed_done`/`duplicate_count`/`failed_count`/
`unsupported_count` counts (populated as processing completes) must reconcile:
`discovered = stored_originals_done + failed_count` (every discovered source ends up either
stored or explicitly recorded as failed, nothing silently vanishes), and
`parsed_done + unsupported_count + semantic_pending_count = stored_originals_done` (every
stored original is accounted for in exactly one of: parsed, unsupported, or awaiting semantic
processing). A batch is only `completed` when both equations hold — this is the concrete,
testable definition of "we can prove the whole corpus was processed," not a self-reported status
string.

---

## Design note requested by the founder: Memory Thread member polymorphism

Already resolved in `docs/LIFE_SOURCE_VAULT_AND_MEMORY_ARCHITECTURE.md` §3 (updated in this same
pass) — `memory_thread_members` now uses `member_kind` + `member_ref_id` (with a dedicated
`external_reference`+`external_ref` pair for things like GitHub PRs that have no row in this
database at all), instead of forcing every thread member to be a `memory_source_units` row.
Raw evidence (documents, chunks, and — once this bootstrap ships — messages) join a thread as
`member_kind='memory_source_unit'`; derived/system knowledge (claims, project entities,
engineering lessons, MainAI tasks, founder memory notes) reference their own real tables
directly. See that document for the full corrected design and rationale.
