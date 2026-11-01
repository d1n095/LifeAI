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
**This Blueprint is a traceable synthesis of reachable evidence. It does not invent missing history,
overstate coverage, rewrite originals, or claim authority. `first_cycle_go=false`.**
