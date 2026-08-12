# Life — Source Vault and Memory Architecture

**Status:** Architecture-only, no code written. This document is deliberately **not** a
rewrite of `docs/MEMORY_ARCHITECTURE.md` or `docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md` §4 — both
already contain a mature, internally-consistent, partially-implemented design that directly
satisfies most of the founder's §9-15 mandate. This document's job is to (1) name the founder's
specific new requirements (Source Vault as an absolute invariant, dual deterministic/Life
Memory, Memory Threads, ChatGPT-export ingestion) against that existing design, (2) show exactly
where each one already has real infrastructure vs. genuinely missing, and (3) resolve naming so
future work builds one system, not three overlapping ones.

---

## 1. Source Vault

**The founder's requirement:** every uploaded file goes UPLOAD → validate → hash/checksum →
immutable original storage → source record → provenance → access policy → deterministic
ingestion. The original must be preserved byte-for-byte. MainAI must never alter/replace/
rewrite/delete it. This must be technically enforced by storage/DB permissions, not just prompt
policy.

**What already exists, verified against the code, mapped step by step:**

| Founder's step | Existing implementation | Status |
|---|---|---|
| UPLOAD | `app/routers/library.py` streamed upload, `MAX_UPLOAD_BYTES` | IMPLEMENTED |
| validate | `app/rag/zip_import.py` (magic bytes, extension allowlist, executable block) | IMPLEMENTED |
| hash/checksum | sha256 streamed during upload, before any full read (`app/storage/local_fs.py`) | IMPLEMENTED |
| immutable original storage | Content-addressed path (`{sha256[:2]}/{sha256}`), atomic write (temp→fsync→rename), never the user's filename | IMPLEMENTED |
| source record | `documents` row, `original_stored` pipeline status reached before any AI processing starts | IMPLEMENTED |
| provenance | `memory_source_units`/`document_source_units` (S1A) | IMPLEMENTED |
| access policy | RLS on `document_chunks`/S1A tables, `documents` intentionally shared (see Canonical Architecture §C note on the `Document`/`DocumentChunk` asymmetry) | IMPLEMENTED, with one open, already-flagged product tension (not a bug) |
| deterministic ingestion | received→original_storing→original_stored→extracting→extracted→embedding→indexed pipeline | IMPLEMENTED |

**What's genuinely missing:** none of the above is missing as a *mechanism*. What's missing is
the founder's **name and absolute-invariant framing** — nothing in the existing code or docs
calls this "the Source Vault," and there is no single test suite that proves the *complete*
chain end-to-end under the name "Source Vault invariant." Concretely recommended (not built):

1. Adopt "Source Vault" as the canonical name for `documents`+blob storage+`memory_source_units`
   together, in docs, so future work has one name instead of three ad-hoc descriptions.
2. Extend the DB-privilege pattern S1A already proved for `memory_source_units`
   (`REVOKE UPDATE, DELETE FROM mainai_app`, `SECURITY DEFINER`-gated exceptions, boot-time
   verification via `has_table_privilege`) to the blob storage layer itself — today
   `LocalFilesystemStorage`'s immutability is a *code convention* (atomic write, content-
   addressed naming makes in-place edits meaningless) rather than a *privilege-enforced*
   guarantee the way `memory_source_units` now is. This is the one real gap between "MainAI
   runtime should lack normal UPDATE/DELETE on canonical source originals" (founder's exact
   words) and what's proven today.

## 2. Dual memory model — deterministic vs. Life Memory

**The founder's requirement:** two complementary layers on top of the Source Vault — (A)
deterministic memory built without AI where possible (filenames, archive structure, MIME,
timestamps, checksums, duplicates, conversation/message IDs, sequence, speaker/role,
attachments, URLs, deterministic tags), and (B) "Life Memory" — AI-derived understanding
(concepts, topics, ideas, goals, decisions, corrections, lessons, preferences, relationships,
projects, timelines, contradictions, summaries, plans).

**This maps directly onto existing, already-designed layers — it is not a new axis:**

- **(A) Deterministic memory** is, almost field-for-field, what `memory_source_units` (S1A,
  built) plus `messages.sequence_number` (S1B, built) already store: `source_identity_key`,
  `content_hash`, `observed_at`/`occurred_at`, chunk/version/document linkage, and (for
  messages) `conversation_id`/parent linkage/sequence/role/timestamps. This layer is real and
  requires no new design — only S1C (extending the same pattern to `Message` as its own source
  type) is missing, and that's a scoped, already-designed extension (§4 below), not new
  architecture.
- **(B) Life Memory** is what `MAINAI_PROJECT_UNDERSTANDING_PLAN.md` §2/§6 already calls
  "lager 3-8" (fact/claim memory, project memory, idea memory, decision memory, founder memory,
  MainAI's constitution) plus `docs/MEMORY_ARCHITECTURE.md`'s `SEMANTIC`/`STRUCTURED` memory
  classes. `KnowledgeClaim` (fact/claim memory) is real and built; `project_entities` (idea/
  decision/project memory, P4) is designed, not built; `founder_memory_notes` (P6) is designed,
  not built.

**Recommendation:** keep the founder's "deterministic vs. Life Memory" framing as the
**top-level name** going forward (it's clearer to a human reader than "layers 1-8" or "class ×
scope"), but implement it as a **relabeling and completion of the existing S1-S5/P1-P7 plan**,
not a parallel new schema. Concretely: deterministic memory = `memory_source_units` +
`document_source_units`/`message_source_units` (S1A/S1B/S1C); Life Memory = `KnowledgeClaim` +
`project_entities` + `founder_memory_notes` + `governance_documents` (P3/P4/P6/P7), all of which
already carry `derived_from`/`memory_source_id` links back into deterministic memory — the
provenance chain the founder's model requires already exists as a design, and partially as
code.

**Provisional note (2026-08-11):** this document, like `docs/LIFE_CANONICAL_ARCHITECTURE.md`, is
part of the Bootstrap Map, not the final architecture — see that document's status note. The
Memory Threads design in §3 below has already been through one founder-review correction round
(the member-kind polymorphism fix); treat it as more settled than the rest of this pass's
inventory work, but still design-only.

## 3. Memory Threads

**The founder's requirement:** a Memory Thread is a first-class object, not the same thing as a
chat — it can link N conversations, messages, documents, decisions, PRs, engineering lessons,
and future goals; the same source can belong to multiple threads; threads can merge, split,
relate, supersede, branch, version, carry confidence and provenance.

**Status: genuinely missing.** This is the one major piece of the founder's mandate with **no**
existing precedent of this shape anywhere in the docs or code. The closest existing things, and
why none of them is actually a Memory Thread:

- `SourceRelationship`/`ClaimRelationship` — real, built, directed edges between exactly two
  documents or two claims (`derived_from`/`supersedes`/`contradicts`/etc.). A graph of pairwise
  edges is not the same structure as a named, many-member, mergeable/splittable thread object —
  you can *simulate* a thread by graph traversal, but there's no first-class object to merge,
  split, or attach a name/confidence to as a unit.
- `conversation_segments` (S2, designed not built) — groups messages *within one conversation*
  into an analysis window; explicitly scoped smaller than the founder's cross-conversation
  thread concept.
- `mainai_goals`/`mainai_plans`/`mainai_tasks` — MainAI's own engineering work graph, unrelated
  in purpose (see Requirement Traceability §6's note on keeping this distinct from the founder's
  personal Goal/Dream graph).

**Design correction (2026-08-11, founder review):** an earlier version of this section proposed
that every thread member be a `memory_source_units.id`, on the theory that PRs/engineering-
lessons/goals would "get into a thread for free" once each became its own `memory_source_units`
subtype. The founder correctly rejected this: `memory_source_units` (S1A) is specifically the
**raw evidence** layer — content-hashed, immutable, provenance-anchored *original* material. A
decision, a goal, an engineering lesson, a `mainai_task`, or a PR is **derived/system knowledge**,
not raw evidence, and forcing it into the raw-evidence table just to be thread-addressable would
be exactly the kind of category error §2 above warns against for Life Memory generally (a
generated conclusion must never be indistinguishable from the source it was drawn from). The
corrected design below fixes this before any schema is locked.

**Recommended shape** (design only, corrected):

```
memory_threads
  id, owner_id, title, summary
  status              -- active | merged | superseded | archived
  confidence
  created_at, updated_at

memory_thread_members
  thread_id
  member_kind          -- 'memory_source_unit' | 'knowledge_claim' | 'project_entity' |
                        -- 'engineering_lesson' | 'mainai_task' | 'founder_memory_note' |
                        -- 'external_reference'   -- for things with no row in this DB at all,
                        --                           e.g. a GitHub PR — see below
  member_ref_id         -- UUID, meaning depends on member_kind; NULL when member_kind=
                        -- 'external_reference'
  external_ref           -- text (e.g. a GitHub PR URL/number), set ONLY when member_kind=
                        -- 'external_reference', NULL otherwise
  added_at, added_by
  UNIQUE (thread_id, member_kind, member_ref_id)      -- member_ref_id nullable-safe per Postgres
                                                        -- NULL-distinct semantics; external_ref
                                                        -- gets its own uniqueness rule instead
                                                        -- when member_kind='external_reference'

memory_thread_relationships
  from_thread_id, to_thread_id, relationship_type   -- merges_into | splits_from | relates_to |
                                                      -- supersedes
```

**The critical design decision, made here explicitly so it isn't re-litigated later:** a thread
member is identified by **kind + id**, following the exact discriminator pattern S1A already
proved for `memory_source_units.source_kind`/`document_source_units` — not a single foreign key
into one table, and not a fabricated `memory_source_units` row for something that isn't raw
evidence. `member_kind='memory_source_unit'` is how raw sources (documents, chunks, and later
messages via S1C) join a thread; every other kind references its own real table
(`knowledge_claims`, `project_entities`, `engineering_lessons`, `mainai_tasks`,
`founder_memory_notes`) directly, and `external_reference` covers things that are real but have
no row anywhere in this database at all (a GitHub PR is GitHub's data, not ours). A future
implementation should verify per-`member_kind` ownership with a trigger, the same pattern S1A's
`transition_own_memory_source()` already uses to check `memory_source_units.owner_id` before
allowing a transition — extended here to check whichever table `member_kind` points at, not a
new mechanism. This is more implementation work than the single-FK version would have been, but
it is the version that doesn't lie about what a claim, a goal, or a PR *is*.

## 4. ChatGPT / large chat export ingestion model

**The founder's requirement (mandate §13-14, and the closing "why now" paragraph):** a ~2GB
ChatGPT export must be uploadable and processed as a real background job; each conversation/
message/attachment isolated as an individual source object while preserving conversation_id,
message_id, parent linkage, sequence, role, timestamps, attachments, archive path/checksum;
individual isolation must not mean context-free analysis — Life must be able to reconstruct
message → parent/context → conversation → related threads → related other conversations →
existing memory; ingestion must be streaming/bounded/resumable/checkpointed/idempotent/
restart-safe as a background job; if external AI is down, raw ingestion continues and the
semantic queue waits.

**This is, almost exactly, `MAINAI_PROJECT_UNDERSTANDING_PLAN.md`'s S1C + S2 + S3, already
designed and sequenced, not a new problem:**

| Founder's requirement | Existing design | Status |
|---|---|---|
| Streaming/bounded/resumable/checkpointed/restart-safe background job | `mainai_jobs` durable job runtime (V0.1-V0.3, proven via 7 crash-recovery demos) + the durable-worker pattern from PR #6 | IMPLEMENTED (the runtime); the specific ChatGPT-import job handler does not exist |
| Nested/large archive safety | `app/rag/zip_import.py` (P2) | IMPLEMENTED |
| Individual message as isolated source with conversation_id/message_id/parent/sequence/role/timestamps preserved | `message_source_units` (S1C) + `messages.sequence_number` (S1B) | S1B IMPLEMENTED; S1C MISSING (designed) |
| Reconstructable context (message → parent → conversation → related threads → related conversations) | `conversation_segments` (S2) + Memory Threads (§3 above, new) | S2 MISSING (designed); Memory Threads MISSING (new in this pass) |
| If AI is down, raw ingestion continues, semantic processing queues | `awaiting_provider`/`blocked_provider` status split (§4.7 of the memory plan, P1 built) | PARTIAL — mechanism exists for document ingestion, not yet verified/extended to a conversation-import job type |

**The direct consequence for build sequencing:** the founder's own instruction that "the first
code version after the architecture pass should be Life Core / Source Vault / capability-
boundary, not more standalone MainAI functionality" lines up exactly with what the dependency
chain above already requires — **S1C and S2 are prerequisites for the ChatGPT-import feature
the founder actually wants next**, and both were already fully designed (not invented in this
pass) before this mandate was written. This is the clearest evidence in the whole discovery pass
that the founder's instinct to stop and map the architecture was correct: the next feature the
founder wants was already blocked on infrastructure that had a finished design sitting unbuilt.

## 5. What this document adds that didn't exist before

Only one genuinely new design: **Memory Threads (§3)**. Everything else in this document is a
reconciliation — naming the founder's Source Vault/dual-memory/ChatGPT-ingestion requirements
against `docs/MEMORY_ARCHITECTURE.md` and `docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md`'s existing,
mostly-still-valid design, and being explicit about the one place (blob-storage-layer privilege
enforcement, §1) where the founder's exact wording ("technically enforced, not just prompt
policy") is not yet fully met even though the *behavior* mostly already holds.
