# Project knowledge ingestion design (Pass 3) — the next practical step after Pass 1/Pass 2

Founder directive, 2026-11-15 ("CONTINUE ACTIVE ENGINEERING"), Priority 3: *"Preserve the
results of #430/#432. Prepare the next practical knowledge-ingestion design using existing
canonical source, claim, entity and work-candidate infrastructure... Do not automatically
create executable tasks from extracted ideas."*

This is a design document, not an ingestion script. §4 explains precisely why: reading the
real `app.rag.memory_source`/`app.project_entities.service` code this design must build on
surfaced one genuine, unresolved architectural fork that has to be decided before any row can
be written into `memory_source_units`/`knowledge_claims` for this corpus — writing code that
silently picked one side of that fork would be exactly the kind of unauthorized architectural
decision this whole session has been careful never to make unilaterally.

## 1. What Pass 1/Pass 2 already give this design (preserved, not re-derived)

- `docs/LIFE_PROJECT_KNOWLEDGE_INVENTORY_PASS2.json` — 158 records, each with: `source_file`,
  `line_no`, `text`, `proposed_entity_type`, `classifier_strategy`, `inferred_status`,
  `record_id` (a stable SHA-256 of `source_file::proposed_entity_type::text`, **not**
  line-number-based — survives independent edits that shift line numbers), `confidence`
  (`high`/`medium`/`low`, from `_CONFIDENCE_BY_STRATEGY`), `source_last_commit` (the file's own
  `git log -1 --format=%H`), `references` (extracted `#PR`/date mentions), `candidate_module`
  (for `requirement` records only), `promoted: false` (hardcoded, every record).
- The measured precision data in `docs/LIFE_PROJECT_KNOWLEDGE_INVENTORY_PASS2.md` — critically,
  that `superseded_marker` (91 of 158 records, the largest category) is only 30-60% precise on
  manual review, and that a stricter auto-filter was tried and rejected because it would have
  dropped a real decision (`docs/AUTH_THREAT_MODEL.md:1`) along with the noise.

This design's own job is everything AFTER that JSON exists: how it becomes real, reviewable
signal inside `app.project_entities`/`app.knowledge_claim`, never how it was produced.

## 2. Incremental re-scan — reuse `source_last_commit`, never a full rescan

A later Pass-3 run should not reprocess every file every time. Concretely:

1. Load the previous run's output JSON (keyed by `source_file`).
2. For each file in the current corpus scan (`corpus_files()`, unchanged from Pass 1/2):
   compute its current `git log -1 --format=%H -- <file>` (already how Pass 2 computes
   `source_last_commit` — reused, not reinvented) and compare against the previous run's
   stored value for that file.
3. **Unchanged files are skipped entirely** — their previous records are carried forward
   byte-for-byte (same `record_id`s, same everything), not re-extracted.
4. **Changed files are re-scanned in full** (line numbers are not stable across an edit
   anyway, so partial re-scan within one file is not attempted — the existing `record_id`
   content-hash already means an unchanged LINE within a changed FILE produces the identical
   `record_id` it always did, so it is recognized as "the same record" even after a full
   re-scan of that one file).
5. A file that disappears from the corpus (deleted, renamed, moved out of `docs/`) has its
   prior records flagged `source_file_removed: true` in the next run's output — never silently
   dropped, since a human reviewer may still want to know a requirement/decision that used to
   live there existed.

This is the entire incremental-ingestion mechanism the founder asked for: it needs no new
infrastructure, only a diff against the previous run's own JSON — the same "stable identity
survives independent edits" property `record_id` was already built for in Pass 2.

## 3. Stable requirement identity, traceability, dedup, uncertainty — already solved, just wired through

- **Stable identity:** `record_id` IS the natural `idempotency_key` for
  `app.project_entities.service.record_interpretation_proposal(..., idempotency_key=...)`.
  That function is already idempotent by construction (`_same()` re-checks that a reused key's
  fields haven't silently changed meaning, raising `ProjectEntityError` if they have, rather
  than quietly overwriting) — re-running ingestion against an unchanged record is a no-op by
  the existing primitive's own design, not something this design has to re-implement.
- **Traceability:** `source_file` + `line_no` + `source_last_commit` map directly into the
  proposal's `provenance` JSONB field — a reviewer (or MainAI itself, later) can always answer
  "which exact file, line, and commit did this claim come from" without re-deriving it.
- **Dedup within one file:** already solved — `record_id`'s content hash means the identical
  text extracted twice (e.g. by two classifiers matching the same line) is the same record,
  not a duplicate. **Dedup ACROSS files** (the same requirement restated in two different
  docs) is explicitly a different, harder problem — not solved here. The closest existing
  primitive is `app.concept_reconciliation.service.find_same_concept()`, already used by
  `promote_interpretation_proposal()`'s own "Stage B: SAME collapse" — but that runs at
  PROMOTION time, on a human-supplied `title`, not at ingestion time on raw extracted text.
  Named here as a real open question for a future pass, not quietly assumed solved.
- **Contradiction/supersession:** `superseded_marker`/`conflict_marker` records map their
  `classifier_strategy` into the proposal's `classifier_reasoning` field verbatim (e.g.
  `"superseded_marker: text contains 'supersedes'/'ersätter' -- see
  docs/LIFE_PROJECT_KNOWLEDGE_INVENTORY_PASS2.md for this strategy's measured 30-60%
  precision"`). The proposal's `status` stays `"unreviewed"` like every other record — a
  contradiction flag is a prioritization hint for a human reviewer, never something this
  design resolves automatically. Only a human-invoked `promote_interpretation_proposal(...,
  supersedes_entity_id=...)` call can actually record a real supersession relationship.
- **Explicit uncertainty:** Pass 2's `confidence` tier maps directly onto
  `classifier_confidence` — already the exact field `InterpretationProposal` has for this.

None of the above required inventing a new mechanism; every one is Pass 2's own output field
mapped onto a parameter an existing, already-reviewed function already accepts.

> **Update (`claude/repository-source-identity-foundation`, founder directive "CONTINUE
> MAINAI INTEGRATION", Priority 2): the fork below is now resolved, not merely named.**
> Option A vs. B is decided in favor of **Option B** — a new, narrowly-scoped `SourceKind.
> repository_file` plus its own `repository_file_source_units` subtype table (migration
> 0069), identified by `(repo_identity, file_path, commit_sha)`. The deciding evidence:
> `SourceKind` is NOT a native Postgres enum (migration 0019 used `varchar(32)` + a CHECK
> constraint), and this exact "third-arm" extension was already proven safe once before —
> `MessageSourceUnit` (migration 0037) added a SECOND arm the same way. Option A would have
> meant actually running the Library upload pipeline for ~114 files and reinterpreting
> `KnowledgeVersion` (upload-time versioning) to mean "git commit," a concept it was never
> built to hold — not smaller, just differently-shaped work. See the migration's own docstring
> for the full evidence trail and the now-sixth entry in the already-tracked 0069 revision
> collision (restated, not resolved, there). This update does NOT retract §4 below, which
> remains the accurate record of the fork as it stood before this decision.

## 4. The real blocker: `MemorySourceUnit` has no slot for a git-tracked repo file

This is the finding that keeps this design a design, not a script. Every path from raw text to
a real `InterpretationProposal` must first exist as a `KnowledgeClaim`, and every
`KnowledgeClaim` must first be bound to a `MemorySourceUnit` (`memory_source_id`, `RESTRICT`
on delete — a claim can never outlive its source silently). The ONLY existing, reviewed way to
create one for document-derived content is
`app.rag.memory_source.get_or_create_memory_source_unit()`, which takes a
`DocumentSourceLocator` requiring a real `document_id` (optionally `chunk_id`/`version_id`) —
i.e. it is built for content that has ALREADY been uploaded through the existing Library/RAG
pipeline and exists as a real `Document`/`DocumentChunk`/`KnowledgeVersion` row.

**The repo's own `docs/*.md`/`CLAUDE.md`/`README.md` files Pass 1/2 scan are not uploaded
Library documents — they are read directly off the git working tree by a standalone script.**
There is no `document_id` to anchor a `MemorySourceUnit` to for any of these 158 records
today. This is not a gap this design can route around with a clever default; it is a real
fork between two different, both-legitimate answers:

**Option A — ingest the repo docs corpus as real Library Documents first, then re-run
classification on real `DocumentChunk` rows.** Maximal reuse: once each `docs/*.md` file is a
real uploaded `Document`, the EXISTING chunking/embedding/claim-extraction pipeline
(`app.rag.claims.extract_claims_for_document()`, the same live hot path mentioned in
`record_interpretation_proposal()`'s own docstring) could run on it exactly like any other
founder-uploaded document, and Pass 1/2's own regex classifiers would become a NEW
`classifier_strategy` registered against real `KnowledgeClaim` rows rather than a side
pipeline. This is the architecturally cleanest answer and the one this design recommends, but
it is a bigger lift than it sounds: it means re-running (or triggering) the Library upload
flow for ~111 files, and re-expressing Pass 1/2's 8 regex classifiers as claim-level
classifiers operating on `DocumentChunk.content_text` rather than raw file lines — a real
follow-on implementation task, not a one-line change.

**Option B — a new, narrowly-scoped `SourceKind`** (e.g. `repository_file_line_range`) for
git-tracked source-controlled text, identified by `(repo_path, file_path, line_no,
git_commit_sha)` instead of a `document_id` — exactly matching the pattern
`SourceKind`'s own docstring already names as the intended extension point ("Future source
kinds ... follow the identical pattern: a new enum value here plus a new subtype table, never
a nullable column bolted onto an existing subtype"). Smaller lift than Option A, but it is a
real schema change (new enum value, new subtype table, a new migration) to a trust-model-
relevant table whose lifecycle is enforced by database triggers and `SECURITY DEFINER`
functions — exactly the kind of change this session's own standing instructions say requires
independent review before it exists, not just before it's used.

**This design does not choose between A and B.** Recommending Option A as the better
long-term fit, while naming Option B as the smaller immediate lift, is as far as this document
goes — the actual choice is an architecture decision for the founder/an independent reviewer,
not something a knowledge-extraction design doc should quietly decide by writing the migration
first and asking forgiveness after.

## 5. What this design guarantees structurally, not just by promise

- **No `InterpretationProposal` is ever created by this design** (because doing so first
  requires a `KnowledgeClaim`, which first requires resolving §4 — there is nothing to call
  `record_interpretation_proposal()` with yet). The "review before promotion" requirement is
  therefore not yet exercised at all for this corpus, which is the honest state of things, not
  a gap this document is hiding.
- **Once §4 is resolved and real `InterpretationProposal` rows exist**, promotion to a real
  `ProjectEntity` still only ever happens through
  `app.project_entities.service.promote_interpretation_proposal()`, which this design never
  calls and which structurally requires a human-supplied `authority`/`basis` — never inferred
  from `classifier_confidence`. This is not a convention this design is asking future code to
  respect; it is already how the only real write path is built.
- **No `WorkCandidate` is ever created by this design**, for the same reason: `WorkCandidate`
  rows are only ever written by `_record_work_candidate_if_actionable()`, which is itself only
  ever called FROM `promote_interpretation_proposal()`. Since this design never promotes
  anything, "do not create executable tasks from extracted ideas" is true by construction, not
  by instruction.

## 6. What this document explicitly does not do (as originally written — see the update note above §4 for what changed since)

- As originally written, did not add a migration, a new `SourceKind` value, or any new table,
  and did not pick between Option A and Option B in §4. **The update note above §4 records
  that `claude/repository-source-identity-foundation` (migration 0069) has since done exactly
  that** — a new `SourceKind.repository_file` value, a new `repository_file_source_units`
  table, and the code implementing it. That branch is itself still an isolated, unmerged,
  independently-reviewable draft — nothing here claims it is authoritative until reviewed.
- Still does not run the Library upload pipeline against the docs corpus (the migration
  implements Option B, which specifically does not need this).
- Still does not write a single row to `interpretation_proposals`, `project_entities`, or
  `work_candidates` — migration 0069 only reaches as far as `memory_source_units`/
  `repository_file_source_units` (the source layer), never past it.
- Does not treat Pass 1/2's 158 records as anything more authoritative than they already were
  (`promoted: false`, reconnaissance only) — this document changes nothing about their status.
