# LIFE MASTER BLUEPRINT

**A shared, evidence-backed entry point to the whole intended system** — for the Founder,
ChatGPT, Claude, Codex and eventually Life herself. It is a **traceable synthesis**, not a
replacement for the originals: every claim links to its source and carries a verification status.

> **INCOMPLETE BY CONSTRUCTION.** This Blueprint is built from what is **verifiably reachable
> today** (the Git repository, its 112 docs, the code, the open-PR graph). The Founder's full
> historical corpus — ChatGPT conversations, uploaded files/images, archives, and the live
> deployment's Library/Vault/DB — is **NOT reachable from here** and has **NOT** been read (see
> §1). Sections that depend on it are marked **⟨needs Founder-provided access⟩**. Nothing here
> invents history or claims coverage it does not have.

_Rev. 1 — 2026-11-01 — Claude Opus 4.8 (independent). Base: `claude/det-kommer-mer-879lcm` @
`64c07d5b0`. Companion evidence: [`LIFE_TAKEOVER_READINESS_RECON.md`](LIFE_TAKEOVER_READINESS_RECON.md).
`first_cycle_go=false`. A proposal for Founder–Life discussion; no implementation authority claimed._

> **RUNTIME STATUS (Founder fact, 2026-11-01).** The last version the Founder actually used was Life
> **v0.1**: **basic messaging worked, file upload did not, and no historical corpus was ever successfully
> provided.** The current ~40-PR system has **never been started/tested end-to-end with the Founder** —
> these candidates are **not** a Founder-tested runtime. **A first supervised startup and a verified
> small-file upload must precede any real historical comprehension.** Operational runbook + readiness
> verdict + startup checklist: [`LIFE_FIRST_STARTUP_READINESS.md`](LIFE_FIRST_STARTUP_READINESS.md).
> (This also means §1's missing historical corpus is *expected*, not a loss.)

## Legend
**Verification:** ✅ verified-in-code · 🟡 partial/one-sided · 🔴 absent · ⟨access⟩ needs Founder source.
**Status of interpretation:** *fact* (read from source) · *proposal* (my best reconciliation,
not an approved requirement — never treat as settled).

---

## 1. Verified source inventory + reading accuracy (P0)

### 1.1 What each source class is, and whether it was examined
| Source class | Reachable here? | Examined? | Notes / evidence |
|---|---|---|---|
| Git repository code (`backend/app`, 313 modules) | Yes | **Examined** | Primary evidence for all ✅ rows below. |
| Repository docs (`docs/*.md`, 112 files) | Yes | **Partially** — key docs read, all content-searchable | Authoritative set in §6. |
| Open PR / branch graph (GitHub) | Yes | **Examined** | #285–#326; see §7. |
| Committed historical corpora (archives/exports/dumps/images) | Yes | **Examined = none exist** | `git ls-files` finds **zero** archives/exports/pdf/docx/db in Git. History is deliberately **not** in Git. |
| Live deployment Library/Vault/DB (content-addressed store, `memory_source_unit`, claims) | **No** (not on this host) | **Not read** | The store lives outside Git (`storage/local_fs.py`, STORAGE_ROOT). ⟨access⟩ |
| Founder's ChatGPT conversations / exports | **No** | **Not read** | No export is present; must be Founder-provided. ⟨access⟩ |
| Uploaded files, images, scanned archives | **No** | **Not read** | ⟨access⟩ |
| External Founder files (business/finance/comms, other companies) | **No** | **Not read** | In scope (§2), but evidence is ⟨access⟩. |

**Honest conclusion:** the *technical* system is well-evidenced; the *historical corpus that
defines the fuller vision* is **not yet reachable** and must be designated by the Founder. Do not
treat the vision as fully captured until those sources are ingested and verified.

### 1.2 File-reading / extraction correctness (measured, not assumed)
Probed the real extractor (`backend/app/rag/extract.py::extract_text`) across formats:

| Format | Fidelity | Evidence |
|---|---|---|
| text / markdown / code | ✅ exact | byte→text round-trip, encoding preserved |
| HTML | ✅ (minor noise) | tags stripped; **note**: `BeautifulSoup.get_text` keeps `<script>/<style>` text |
| DOCX paragraphs | ✅ | all paragraphs, in order |
| **DOCX tables / nested tables / headers / footers** | ✅ **FIXED (PR #327, draft)** | `app/rag/docx_extract.py` walks body children in reading order; tables + nested tables + headers/footers extracted, merged cells de-duplicated; 12 unit tests + e2e. Previously `doc.paragraphs`-only (dropped tables). |
| DOCX text boxes / footnotes / comments / SmartArt | 🟡 **detected, not extracted** | #327 **reports** these as an explicit limitation note instead of dropping them silently |
| PDF (digital text layer) | ✅ | `pypdf` extracts the embedded text layer |
| **PDF (scanned / image-only)** | 🔴 **GAP** | no OCR → image-only pages yield **empty text** (silent content loss) |
| Images (standalone) | 🔴 **GAP** | no image-text/OCR path confirmed |
| Audio / video | ✅ | `rag/media_import.py` STEG 12 (timed transcript segments) |

The DOCX-completeness gap is **fixed in draft PR #327** (review pending). The remaining concrete
extraction blockers to *comprehensive* historical processing are **scanned-PDF/image OCR** and
**standalone images** — see §9 deliverable #4. (Fidelity probe reproducible: `backend` + the real
`extract_text`.)

---

## 2. Founder's intended vision & long-term direction (*fact* where cited; scope is settled)

Life (MainAI) is the Founder's **persistent digital partner and overarching intelligence** — not a
chatbot and not only a code generator. Intended direction, reconciled from the authoritative docs:

- **One shared knowledge/memory base** that Life, a future user-AI and future expert agents all
  read/write under different RLS filters — *not* a separate memory per feature
  ([PLAN §3](MAINAI_PROJECT_UNDERSTANDING_PLAN.md)). *fact.*
- **Whole-project technical memory & coordinator**: track structure, detect overlap, propose reuse,
  analyse branch/PR dependencies, protect prior decisions — today done manually via
  [`CLAUDE.md`](../CLAUDE.md) + [`BRANCH_REGISTRY.md`](BRANCH_REGISTRY.md), *intended* to become Life
  herself once the memory base exists. *fact (as a stated target).*
- **Progressive, local-first operational independence** — reduce paid-AI dependence; run on-host
  models where possible ([`MAINAI_LOCAL_INTELLIGENCE_SCHOOL.md`](MAINAI_LOCAL_INTELLIGENCE_SCHOOL.md)).
  *fact.*
- **Delegated authority, natural communication, agent coordination; future companies and
  business/finance/communication domains are already in scope** (settled by the Founder, not an open
  question). The *detail* of those domains is ⟨access⟩-gated on the historical corpus. *fact (scope)
  + proposal (detail pending sources).*

---

## 3. Existing systems & actual maturity (✅/🟡/🔴 verified in code @ `64c07d5b0`)

The Founder's **eight memory layers** ([PLAN §2–§3](MAINAI_PROJECT_UNDERSTANDING_PLAN.md)):

| Layer | Maturity | Evidence |
|---|---|---|
| 1. Original memory (immutable) | ✅ | `storage/local_fs.py` content-addressed `{sha256}`, atomic write→fsync→rename, path/symlink-safe; verified before any AI step |
| 2. Source memory | ✅ | `KnowledgeVersion`, `DocumentChunk`; `rag/{extract,chunking}.py` |
| 3. Claim/fact memory (documents) | ✅ | `rag/claims.py` (`claim_type` + objective `grounding_score`, bounded one-call/chunk) |
| 4. Project memory | 🟡 | `Project`/`Task` exist but not `derived_from`-linked to the sources that produced them |
| 5. Idea memory | 🔴 | only a Workbench "idea" save; no alternatives/why/links structure |
| 6. Decision memory | 🟡 | `active`+`supersedes` base exists; who/when/why only as free-text notes |
| 7. Founder memory | 🔴/🟡 | `context/resolver.py` classifies turns but persists nothing; `MessageSourceUnit` provenance exists (S1C) |
| 8. Constitution | 🔴 | split P7A (ingestion, frozen) / P7B (activation, last); chat system prompt is a hardcoded constant |
| Universal provenance | ✅ | `memory_source_unit` unifies document **and** message provenance (S1A/S1C; migrations 0037-era) |
| Retrieval w/ provenance | ✅ | `rag/retrieve.py`→`vector_store.search()` returns chunk+document identity+score; media opens at timestamp |
| Egress control | ✅ wired, 🟡 maturing | `egress_policy/` default-deny gate wired into Safe Planner **and** RAG embed (`embed_with_policy`, used by ingest/retrieve/media/library) **and** chat (`chat_with_fallback`). Local-vs-external distinction added in **PR #326**. |
| Durable background work | ✅ | two lease-fenced job systems (`worker.py` `knowledge_import_jobs`; `jobs/service.py` `mainai_jobs`) + `InteractiveIsolation` |
| Provider pre-verification | ✅ (P1) | `providers/verification.py` real probe; ingestion pauses `awaiting/blocked_provider` |

**Correction vs earlier recon:** the egress gate is **not** "unwired from chat/RAG" — both are gated
via `owner_id` today (verified in `routers/chat.py`, `rag/ingest.py`, `rag/retrieve.py`). The genuine
gap was that it could not tell a **local on-host provider** from an external one; **PR #326** fixes
that (truthful `decision='local'`, a checkable no-egress invariant).

---

## 4. Components, relationships & dependency graph

```
            AUTHORIZED SOURCE (upload / export / archive)   ⟨access⟩ for real history
                         │
        ┌────────────────▼────────────────┐   egress gate (default-deny, local vs external) ── PR #326
        │ 1 ORIGINAL  storage/local_fs     │   provider pre-verify ── providers/verification.py
        └────────────────┬────────────────┘   durable worker ── worker.py / jobs/service.py
         extract.py ─────▼  (DOCX-tables & scanned-PDF/OCR = GAP, §1.2)
        ┌────────────────▼────────────────┐
        │ 2 SOURCE  KnowledgeVersion/Chunk │─── embed (embed_with_policy, gated) ─► pgvector
        └────────────────┬────────────────┘
         claims.py ──────▼  (documents ✅ ; messages→claims S4 = NOT WIRED)
        ┌────────────────▼────────────────┐
        │ 3 CLAIMS  KnowledgeClaim + type  │── grounding_score ── concept_reconciliation/ (dedup)
        └────────────────┬────────────────┘
      interpretation queue (P4, "proposal not commit")
        ┌──────┬─────────┼─────────┬──────┐
        ▼      ▼         ▼         ▼      ▼
      4 PROJ  5 IDEA  6 DECISION 7 FOUNDER 8 CONSTITUTION (P7A import / P7B activate)
        └──────┴────┬────┴─────────┘
                    ▼
            PROJECT MAP — one queryable graph over all layers (shared by every consumer)
                    ▼
            retrieve.py → Life answers WITH provenance (claim→chunk→version→source→original)
```
**Invariant (*fact*):** every higher layer inherits an unbroken provenance chain to the original
file; AI-derived conclusions are never conflated with original sources
([PLAN §3, §6.10](MAINAI_PROJECT_UNDERSTANDING_PLAN.md)).

---

## 5. Shared foundations & domain-specific extensions
- **Shared foundations (built, reused by every domain):** RLS `FORCE` owner isolation on every
  table; immutable content-addressed originals; universal `memory_source_unit` provenance; durable
  lease-fenced workers + `InteractiveIsolation`; default-deny egress with local/external
  distinction; "proposal ≠ approved" and `PROPOSED_SCOPE ≠ AUTHORIZED_SCOPE` authority invariants.
- **Domain-specific extensions (*proposals*, scope settled but detail ⟨access⟩):** business/finance,
  communication, multiple companies, agent coordination, local-model operations — each is expected
  to ride the *same* tables/queues/governance, **not** a competing store (per
  [`CLAUDE.md`](../CLAUDE.md) and PLAN §3). Concrete requirements await the historical corpus.

---

## 6. Authoritative sources (read these; this Blueprint only points)
Vision/architecture/build-order: [`MAINAI_PROJECT_UNDERSTANDING_PLAN.md`](MAINAI_PROJECT_UNDERSTANDING_PLAN.md)
(§2 gaps, §3 target, §4 data model, §7 risks, §8 build order) · Readiness:
[`MAINAI_STARTUP_READINESS.md`](MAINAI_STARTUP_READINESS.md), [`MAINAI_V1_READINESS.md`](MAINAI_V1_READINESS.md) ·
Autonomy: [`MAINAI_V1_GOAL_TO_AUTONOMY.md`](MAINAI_V1_GOAL_TO_AUTONOMY.md),
[`FIRST_AUTONOMOUS_TASK_BLOCKER_MAP.md`](FIRST_AUTONOMOUS_TASK_BLOCKER_MAP.md) · Local-first:
[`MAINAI_LOCAL_INTELLIGENCE_SCHOOL.md`](MAINAI_LOCAL_INTELLIGENCE_SCHOOL.md) · Egress/Vault:
[`LIFE_VAULT_EGRESS_CONTROL.md`](LIFE_VAULT_EGRESS_CONTROL.md),
[`LIFE_SOURCE_VAULT_FUTURE_COMPATIBILITY.md`](LIFE_SOURCE_VAULT_FUTURE_COMPATIBILITY.md) ·
Reconciliation: [`MAINAI_CONCEPT_RECONCILIATION.md`](MAINAI_CONCEPT_RECONCILIATION.md) · Security:
[`SECURITY_BLOCKERS.md`](SECURITY_BLOCKERS.md) · Process/map: [`CLAUDE.md`](../CLAUDE.md),
[`BRANCH_REGISTRY.md`](BRANCH_REGISTRY.md) · This session's evidence:
[`LIFE_TAKEOVER_READINESS_RECON.md`](LIFE_TAKEOVER_READINESS_RECON.md),
[`LIFE_LOCAL_FIRST_INGESTION_DEMO.md`](LIFE_LOCAL_FIRST_INGESTION_DEMO.md).

---

## 7. Current PR / branch status & implementation gaps (verified vs GitHub, 2026-11-01)
- **Default** `claude/det-kommer-mer-879lcm` @ `64c07d5b0`; **no open PR merged into it yet** (~40 open).
- **Codex** (coordination lane): #319 `397672d84`, #320 `6c1240d8`, #321 `e1f649a9`, #322 `a7c1ffa3`
  — independently **PASS** this session; **#324** `306b980f4` active (CC3/CC4/AR6; inspect-only, sequences after #322).
- **Claude**: **#327** (DOCX extraction completeness — tables/nested/headers/footers, **draft, review required**),
  **#326** (egress local/external distinction, **draft, review required**), #325 (recon + handoff + this
  Blueprint, docs), #323 (npm-audit + CI pin, green), #316/#317/#318 (passkeys/PWA).
- **Cursor**: #285–#315 (first-cycle, founder-workflow, coordination, large-file, messaging) — unmerged.
- **Gaps (ranked):** (1) no historical-conversation→claim path (S4 not wired; no ChatGPT/export importer);
  (2) extraction fidelity: DOCX tables + scanned-PDF/image OCR (§1.2); (3) `embedding_dim=1536` hardwired
  blocks a real local embedder without a schema migration + Founder decision; (4) memory layers 4–8 +
  automatic relationship discovery partial/manual; (5) `BRANCH_REGISTRY` ↔ real-PR-graph reconciliation;
  (6) fragmentation (40 PRs / 112 docs) — this Blueprint is the single reconciled entry point.

---

## 8. Security, privacy, authority & cost principles (*fact*, from PLAN §7 + code)
- **Default-deny egress; retrieval authority ≠ egress authority; model request ≠ authorization;
  proposed scope ≠ authorized scope.** Retrieved/external content is **data, never instructions**.
- **Owner isolation by the database** (`FORCE ROW LEVEL SECURITY`), including derived-owner tables
  (`messages`, migration 0031) — never relying on app call-order.
- **Provider keys never surfaced in errors**; local/offline processing preferred; cost controlled by
  piggy-backing classification on existing calls and gating relation-compare behind a cheap embedding pre-filter.
- **Constitution dual-lock**: `approved` (P7A import) ≠ `active` (P7B); only explicit two-step Founder
  confirmation activates governance, regardless of what a document claims (prompt-injection defence).
- **Standing operational guards:** no merge/deploy/DNS/paid egress/Recall/autonomous GO; frozen
  candidates untouched; independent reviewer required (no self-cert); `first_cycle_go=false`.

---

## 9. Phased roadmap (dependency-ordered; *fact* from PLAN §8, annotated with today's state)
Built & in code: **P1** provider pre-verify · **P2** zip hardening · **P3** document claim-typing ·
**S1A** `memory_source_units` · **S1B/S1B-RLS** message sequence + RLS · **S1C** `message_source_units`
+ `knowledge_claim_evidence`. Remaining, in order:
1. **S3** `memory_processing_jobs` (generic memory work-queue) — infra, no behaviour change.
2. **S4** document-claim backfill + **S5** conversation-segment claim extraction (needs S2 segments) —
   *this is what turns historical conversations into provenance-linked claims* (gap #1).
3. **P4** interpretation queue + relationship discovery + project map — the big one; everything is a
   **proposal**, never auto-commit.
4. **P5** first understanding report (grouped bulk review) → **P6** founder memory + correction loop.
5. **P7A** governance ingestion (frozen; powerless until P7B) → **P7B** activation (**last**, needs P4 proven).
- **Cross-cutting, Founder-prioritized now:** verified **local-first ingestion** (demo in
  [`LIFE_LOCAL_FIRST_INGESTION_DEMO.md`](LIFE_LOCAL_FIRST_INGESTION_DEMO.md); no-egress invariant via #326),
  then the extraction-fidelity fixes (§1.2) and the `embedding_dim` decision.

### Smallest missing capabilities for comprehensive historical processing (deliverable #4)
Ordered by value/safety, each its own bounded, independently-reviewed PR:
- ✅ **DOCX table/header/footer extraction** — **done in draft PR #327** (review pending).
1. **Image/scanned-PDF OCR** path (larger; needs a local OCR engine choice — local-first). *← next smallest verified blocker.*
2. **Standalone image ingestion** (depends on the OCR/vision choice above).
3. **Full extraction of the detected-but-unsupported DOCX constructs** (text boxes/footnotes/comments) — currently reported, not extracted.
4. **Configurable `embedding_dim`** / per-model vector columns (schema migration + **Founder decision** on the local model).
5. **S4 message→claim wiring** + a **ChatGPT/export importer** (the biggest comprehension unlock; reuses `memory_source_unit`).

---

## 10. Outstanding Founder decisions (only what evidence cannot settle)
*(Business/finance/communication/multi-company scope is settled = in scope.)*
1. **Designate ONE authorized historical source** to ingest first (an existing Library/Vault upload,
   or a Founder-provided export). Until then §1's corpus stays ⟨access⟩ and the vision stays partial.
2. **Confirm local-first/offline embedding (no egress)** for the first ingestion, and whether to take
   the `embedding_dim` migration now or defer.
3. **Prioritize** the extraction-fidelity fixes (§9 #1–#2) vs the message→claim unlock (§9 #4) as the
   next build after evidence is established.

---

## 11. Historical-source access audit (read-only, verified 2026-11-01)
Probed the actual environment — **not** inferred from Git. Result: **no authorized historical source is
reachable here.**

| Candidate source | Status | Evidence |
|---|---|---|
| Life Library / uploaded-document storage | **inaccessible** | no `lifeai_uploads`/data volume on disk; the `backend/app/storage` paths are module *source code*, not data; `STORAGE_ROOT` is a disposable per-test temp dir |
| Vault / database records | **present-but-not-authorized** | the DB server holds only disposable `examiner_*` exam/probe databases (synthetic test data from prior reviews) — **no** production Life DB, and none is a designated authorized source |
| Conversation archives / previously imported files | **missing here** | none committed to Git; none on disk in this clone |
| Founder-approved local file collections | **needs Founder action** | only `.env.example`/`.env.vps.example` templates exist — no real config pointing at a populated store |
| Export/import interfaces | **built, idle** | `/api/library/import`, `zip_import.py`, `library_import.py` exist and work, but have no authorized source to run against here |

**Minimum access procedure the Founder must complete (pick one):**
- **A — point at existing deployed data:** give this (or an authorized) environment read-only access to the
  live deployment's `DATABASE_URL` **and** its `STORAGE_ROOT` volume (or a copy), so the existing Library
  can be enumerated and ONE source chosen. Do **not** copy private originals into GitHub.
- **B — provide one file/export:** place ONE authorized source (e.g. a ChatGPT data export `.zip`/`.json`,
  or a specific document) at a designated local path this session may read, and confirm it is authorized for
  ingestion.
- **Either way:** confirm local-first/offline embedding **and** a local-first *chat* model (claim extraction
  needs a chat-capable model — see §12), or explicitly authorize a classified egress for the first proof.

## 12. Comprehension readiness — what retrieval proves, what it does not, and the 8-layer gaps
**Proven today** (local-first ingestion demo, PR #326 + DOCX completeness #327): exact original preservation,
correct extraction (incl. DOCX tables/headers/footers), **source-linked retrieval**, durability across a
worker/session restart, owner isolation (RLS), and zero external egress. **Not proven: semantic
comprehension.** Vector + keyword retrieval with provenance shows Life can *find and cite* the right source;
it does **not** establish that she *understands* relationships, reconciles contradictions, or separates a
decision from an idea. Those need the claim/interpretation layers below — and **claim extraction requires a
chat-capable model** (`rag/claims.py` → `chat_with_fallback`, one call/chunk), so comprehension is gated on a
local-first chat model **or** authorized egress, plus a designated source.

**Gap analysis for "Life reconstructs her own Blueprint" (eight-layer architecture):**

| Capability needed | State | Precise gap |
|---|---|---|
| Historical **conversation ingestion** | 🟡 | `MessageSourceUnit` provenance built (S1C); no importer for a ChatGPT/export archive |
| **Message→claim** extraction (S4) | 🔴 | `claims.py` reads `DocumentChunk` only; no path from `MessageSourceUnit`→`KnowledgeClaim` |
| **Cross-source semantic reconciliation** | 🟡 | `concept_reconciliation/` is deterministic entity **dedup** wired into `project_entities`, **not** contradiction/relationship resolution across sources; automatic relationship discovery absent |
| **Decision provenance + Founder approval** | 🟡 | `claim_type` distinguishes decision/idea (needs a model); the interpretation/approval queue that turns claims into *proposed* project/idea/decision memory (plan P4) is **not built** — inferred ideas must never auto-promote to approved decisions |
| **Blueprint generation + versioning** (by Life) | 🔴 | net-new; today the Blueprint is agent-authored. Needs a queryable project-map (plan P4) + a generation/versioning surface |
| **Continuous updates without losing history** | 🟡 | immutable originals + append-only provenance exist; no living-Blueprint update loop |
| **Background processing without blocking the Founder** | ✅ | two durable lease-fenced job systems + `InteractiveIsolation` |

**Additional components actually required for cross-document understanding** (beyond retrieval): (1) the
message→claim path (S4) + an export importer; (2) a chat-capable **local** model for claim/`claim_type`
extraction (to stay no-egress); (3) relationship/contradiction discovery over claims (plan P4, beyond
today's dedup); (4) the interpretation/approval queue so conclusions are **proposals**, not auto-approved.

## 13. Next bounded blocker — report + acceptance criteria (P5)
No new code is landed this round: every high-impact next unit needs a Founder decision, and #326/#327 are
still awaiting independent review (stacking more unreviewed code would dilute review and risk rework). The
smallest verified blocker that *directly* advances comprehension is **the message→claim (S4) slice with a
local-first chat model**, which is **blocked on**: (a) §11 source designation, and (b) a local chat-model
choice (or authorized egress). Acceptance criteria for that unit, when unblocked:
1. ONE authorized conversation/export ingested through the existing `MessageSourceUnit` path (no new store).
2. Claims extracted from messages via a **local** chat model — zero external egress (ledger shows `local`).
3. Every claim carries provenance back to the exact message/source/original (unbroken chain).
4. `claim_type` separates decision vs idea vs task; **inferred items are proposals, never auto-approved**.
5. Durable + restart-safe (runs as a `memory_processing_jobs`/worker job, not in the request).
6. Owner isolation (RLS) verified on every new row; untrusted content never gains authority.
7. Explicit reporting of what was NOT understood/extracted (no silent gaps).
8. Adversarial + regression tests; independent review before merge.
Until (a)+(b) are resolved, this stays a blocker, not an implementation.

## 14. Local AI model compatibility & configurable-embedding architecture (P2)
Goal = **long-term provider independence**, not swapping one paid API for another. Two separate questions:
the *model choice* (Founder decision, below) and the *schema coupling* that must stop forcing that choice.

### 14.1 The coupling to break (verified in code @ `64c07d5b0`)
`document_chunks.embedding` is `vector(1536)` — the dimension is **fixed at migration time**
(`0004_pgvector_document_chunks.py`) and mirrored by `config.embedding_dim = 1536`
(`app/models/document_chunk.py:43`). An **HNSW index** (`document_chunks_embedding_hnsw_idx`,
`vector_cosine_ops`) sits on that column; pgvector's ANN indexes **require a declared dimension**, so a single
physical column cannot serve two models of different size while keeping ANN search. Consequences:
- A real local embedder (all good options below are 768/1024-dim, **not** 1536) cannot write a chunk today —
  it hits the column type, and `index_document` embeds *before* it writes any chunk, so retrieval is fully
  blocked offline (Blueprint §13, runbook §4). This is the first genuine offline blocker.
- **What can be made configurable without a destructive migration:** the *model identity and dimension as a
  recorded, validated pair* — additive columns + a fail-closed guard (§14.3). **What cannot:** the vector
  width itself. Changing 1536→1024 is an `ALTER COLUMN ... TYPE vector(N)` + HNSW rebuild + a **full
  re-embed** (the stored numbers must be recomputed in the new model's space); that is inherently a one-way
  data operation, so it is done *once*, Founder-gated, into a new/parallel column with verify-then-swap — not
  avoided, but made safe and non-silent. Mixing dimensions in one column is unsafe: a query vector of the
  wrong width raises a raw pgvector error mid-request.

### 14.2 Model comparison (grounded; validate on Founder data before committing — see sources)
**Embedding (retrieval) — Swedish-capable, offline, CPU-tolerant:**
| Model | Dim | Swedish | Footprint | Notes |
|---|---|---|---|---|
| **bge-m3** (BAAI) | **1024** | strong (100+ langs) | ~1.2 GB, CPU-OK | dense+sparse+ColBERT; 8k ctx; in Ollama lib (default pull = dense only). Best general multilingual pick. |
| **multilingual-e5-large** (intfloat) | **1024** | strong; appears on Swedish ScandEval excerpts | ~1 GB | mature, well-benchmarked for Scandinavian retrieval; needs `query:`/`passage:` prefixes. |
| multilingual-e5-base | 768 | good | ~0.5 GB | lighter fallback for constrained hardware. |
| **KBLab sentence-bert-swedish-cased** | 768 | Swedish-native (Kungliga biblioteket) | small | best *Swedish-specific* semantics; runs via sentence-transformers, not Ollama — ops cost. |
| nomic-embed-text | 768 | weak (English-centric) | ~0.3 GB | **not** recommended for Swedish despite being the easy Ollama default. |

**Chat (claim extraction / decision-vs-idea classification) — Swedish, local:**
| Model | Swedish | Hardware | Notes |
|---|---|---|---|
| **Qwen2.5-7B-Instruct** | good multilingual | ~6–8 GB RAM (quantized), ~18 tok/s on Apple-silicon CPU | strong instruction-following; 3B variant for very constrained hosts. |
| Llama-3.1-8B-Instruct | good | similar | solid general multilingual. |
| Gemma-2-9B | often cited best for European langs | ~8–10 GB | community-preferred for EU languages. |
| GPT-SW3-6.7B-instruct (AI Sweden) | Swedish-native | ~7 GB | Swedish-first; verify a current GGUF/Ollama build + instruct quality before relying on it. |
| Phi-4-mini / Qwen2.5-3B / Llama-3.2-3B | fair | ~2.5 GB, CPU-friendly | low-hardware fallback; expect weaker Swedish nuance. |

**Benchmarks to settle the choice on real data, not vibes:** ScandEval (Scandinavian NLU/generation) and
MTEB filtered to Swedish/Scandinavian retrieval. Pick embedding + chat **together**, record exact model
version+revision for reproducibility, and re-validate after any hardware migration.

### 14.3 Recommended smallest safe change (ready to build; not landed this round)
A **separate, isolated draft PR, sequenced *after* #326 merges** (to avoid colliding with #326's migration
`0069` and to respect the repo's "don't stack ahead of a dependency" rule — repo `CLAUDE.md`). Scope =
"record embedding provenance + fail closed on mismatch", **without choosing a model**:
1. Config: treat `embedding_model` + `embedding_dim` as one validated pair (a known-models table maps
   model→dim; startup refuses an inconsistent pair instead of trusting a bare `1536`).
2. Additive migration (**non-destructive**, nullable): `document_chunks.embedding_model text`,
   `embedding_dim int`. Back-fill existing rows to the current model/1536. No data rewrite, reversible.
3. `vector_store.upsert_chunks` records the active model/dim per chunk; `search`/`retrieve` **fail closed**
   (explicit error, never a raw pgvector throw and never a cross-space comparison) when the query model/dim
   ≠ the stored rows' — making a model switch *detectable*, not silently wrong.
4. The actual width change (→1024) stays a **later, Founder-gated** re-embed migration (§14.1), unblocked
   only once the Founder picks the model. This PR is the safe prerequisite that makes that switch auditable.
Acceptance: additive migration applies+reverses on a from-scratch DB; mixed-model corpus is rejected with a
clear error (adversarial test); existing 1536/OpenAI path unchanged (regression); owner isolation intact;
independent review before merge.

## 15. First historical-comprehension test — design (P4; runs only after startup+upload are proven)
Milestone after a verified supervised startup (runbook §5) and the local-model path (§14). Pipeline reuses
existing infrastructure end to end — **no new store, no autonomy**:
Founder-authorized historical file → §1.2/#327 extraction (fidelity-checked) → `memory_source_unit`
provenance → **local** semantic analysis (claims via a local chat model, ledger `local`) → concept
reconciliation (deterministic, provider-free) → **provisional** Blueprint proposal → Founder review.
Life must distinguish, and label, five epistemic classes — **historical fact · Founder-approved decision ·
proposal/inference · superseded decision · unresolved contradiction** — and must **never silently promote an
inference into an authoritative requirement** (proposals stay proposals until the Founder approves). Where a
decision is strategic, Life **gathers questions and respectfully proposes a meeting**; it does not
independently schedule one without an explicit mandate. Success = every surfaced claim is source-linked, the
five classes are separated, contradictions are shown rather than resolved away, and what was *not* understood
is reported explicitly. This is a design, not an implementation — it is gated on §13(a)+(b) and §14.

---
**This Blueprint is a traceable synthesis of reachable evidence. It does not invent missing history,
overstate coverage, rewrite originals, or claim authority. `first_cycle_go=false`.**
