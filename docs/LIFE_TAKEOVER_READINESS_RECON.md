# Life (MainAI) — Whole-Project Reconnaissance & Verified State-of-the-Project Report

**Status:** Read-only reconnaissance. **Proposal for Founder–Life discussion — NOT authorization to build
the roadmap.** No code written, no merge, no deploy, no spend, no paid-provider egress, no Recall, no
autonomous enablement. `first_cycle_go=false`.

**Author:** Claude Opus 4.8 (independent). **Base:** default branch `claude/det-kommer-mer-879lcm`
@ `64c07d5b0` (= origin default tip). **Method:** direct reading of the repository at that SHA — 113
`docs/`, `backend/app/` (44 packages, 55 models, 68 migrations, 198 backend test files, 62 RLS policies),
and the open-PR graph via the GitHub API. Every major claim is traceable; plans vs verified behaviour are
labelled. **Rev. 2 (2026-11-01)** folds in Founder scope corrections and a deep ingestion→memory inventory.

> **Scope corrections the Founder issued for this revision (authoritative):**
> 1. Life's full vision is **substantially broader** than the technical categories documented in the repo.
>    **Business, finance, communication, multiple companies and other Founder-defined domains are already
>    part of the intended scope** — they are *not* open authorization questions.
> 2. **Absence from Git ≠ nonexistence.** Historical material (conversations, documents, images, project
>    files) may already live in Life's uploads, Library, Vault, database, or external Founder-controlled
>    files, and must eventually be read from all *authorized* sources, originals preserved.
> 3. The Founder **prioritizes whole-project comprehension before extensive new development.**
> 4. **Local-first processing and reducing paid-AI dependence are strategic objectives.**
> 5. Independent examinations: **#319/#320/#321/#322 all PASS**; **#324** is Codex's CC3/CC4/AR6 work —
>    inspect, do not duplicate.
> This report does **not** impose a fixed product taxonomy and does **not** treat the current architecture
> docs as the complete vision; the full architecture must be *discovered and reconciled from the historical
> record*, not invented from a summary.

---

## A. Verified existing inventory — what is present and where (in Git @ `64c07d5b0`)

**Backend (44 packages).** Capability clusters:
- **Memory / knowledge:** `rag/` (`extract.py`, `chunking.py`, `claims.py`+objective `grounding_score`,
  `trust.py`, `retrieve.py`, `vector_store.py`, `ingest.py`, `library_import.py`, `media_import.py`,
  `memory_source.py`, `message_source.py`, `corpus_batch.py`, `zip_import.py`, `backfill/`),
  `inspectable_memory/`, `founder_memory/`, `memory_threads/`, `memory_work_linkage/`, `project_entities/`,
  `project_memory/`, `concept_reconciliation/`, `work_candidates/`, `active_context/`, `context/resolver.py`.
- **Autonomy / execution:** `mainai_execution/`, `mainai_executive/` (`why_graph.py`, `missing_piece.py`,
  `continuity.py`), `development_supervisor/`/`development_driver/`/`development_operator/`, `safe_planner/`,
  `execution_envelopes/` (mig 0057), `autonomous_gap/`, `jobs/` (durable `mainai_jobs`), `workforce/`,
  `agent_coordination/` (durable ledger).
- **Local-first learning (strategic objective):** `mainai_school/` (#236 tip), `capability_reality/`,
  `strategy_synthesis/`, `strategy_evaluation/`, `work_intelligence/`, `problem_learning/`, `corpus_trial/`.
- **Security kernel:** `rls.py` (FORCE RLS, 62 policies), **`egress_policy/`** (default-deny gate),
  `provider_spend/` (mig 0060), `workforce/kill_switch.py`, `workforce/authority.py`, `account/erasure.py`.
- **Readiness/diagnostics:** `mainai_startup_readiness/`, `diagnosis/`, `intelligence_governance/`.

**Memory/knowledge schema (models):** `document`, `document_chunk`, `knowledge_claim`, `knowledge_version`,
`claim_relationship`, `source_relationship`, **`memory_source_unit`** (unified provenance: subtypes
document_chunk/version/record **and `message`**), `memory_thread`, `memory_truth_claim`, `project_entities`,
`project_memory`, `founder_memory`, `conversation`(+`message`), `import_job`, `source_import_batch`,
`corpus_trial_run`, `mainai_job`, `memory_source_backfill_run`.

**Life Vault** (design + partial build): `docs/LIFE_VAULT_EGRESS_CONTROL.md` + `egress_policy/`. Invariant
`REPO-WRITE ≠ PROVIDER-SPEND ≠ EGRESS AUTHORITY`; two of three built (envelope 0057, spend 0060); egress
gate now exists but is **wired only into the Safe Planner path** (see D/F).

**Not on the default branch (in-flight on PRs):** `large_file_lane/` (resumable large-file intake) lives on
cursor PRs #311–#315 / codex #320, **absent at `64c07d5b0`** — large-file intake is *not* yet a default-
branch capability. `docs/LIFE_SOURCE_FOUNDATION_BOOTSTRAP.md` (the S1 message-source design) is likewise
absent on default although its *code* (`rag/message_source.py`, migration 0037) is present.

---

## B. Historical-source coverage matrix — honest accounting (**absence from Git ≠ nonexistence**)

This repository is **not** the authoritative store of Life's history. The following four-way matrix states
what was actually examined vs what remains.

| Class | What it covers | This session's status |
|---|---|---|
| **EXAMINED** (read directly) | ~20 strategic docs (listed in Sources); `backend/app/` module tree; key pipeline modules (`claims.py`, `message_source.py`, `egress_policy`, `jobs/service.py`, `retrieve.py`, `media_import.py`, `storage/local_fs.py`); the #319/#320/#321/#322 candidates; open-PR graph. | **High-confidence** for the *technical platform*. |
| **DISCOVERABLE-BUT-UNEXAMINED** (present, not yet read in depth) | ~90 of 113 `docs/` (remaining `LIFE_*`/`MAINAI_*`/`ACTIVE_WORK_*`/`HANDOFF_*`); full source of most packages; all 68 migrations individually. | Named/located; **not** line-read. A future pass can read these without new authorization. |
| **INACCESSIBLE FROM GIT — requires live system or Founder-authorized access** | The **running system's data**: actual Library/Vault contents, uploaded **originals** (`lifeai_uploads` volume), memory DB rows, `conversation`/`message` rows, corpus-trial runs. **Historical ChatGPT conversations and other external Founder files.** | **Not inspected** (no running deployment reachable here; no export in repo). May already contain large parts of the vision. **Not claimed read.** |
| **MISSING / NOT YET BUILT** | A ChatGPT/conversation **export importer**; autonomous **source discovery**; message→claim extraction (S4); memory layers 4–8 completion; automatic relationship discovery; egress gating on chat/RAG paths; provider-key pre-verification. | Evidence-backed gaps (see D/F). |

**Completeness statement (not overstated):** the *technical platform* picture is high-confidence; the
*historical-content* picture is **known-incomplete** because the authoritative sources are the live
system + Founder-controlled files, which this read-only Git pass cannot reach. The broader vision very
likely exists — in those sources — and must be ingested and reconciled, not inferred.

---

## C. Reconstructed vision map (from available evidence; discovered, not imposed)

Recurring, cross-referenced intentions (same idea often under different names):
1. **One unified, RLS-scoped memory/knowledge base** with an unbroken provenance chain
   original→source→claim→project/idea/decision/founder/constitution (`MAINAI_PROJECT_UNDERSTANDING_PLAN.md`);
   the `memory_source_unit` abstraction already generalises "source" across documents **and messages**.
2. **Life as the Founder's persistent partner & overarching intelligence** — coordinating specialist agents
   and future companies, acting within delegated authority, with APIs as *teachers/critics/examiners*
   (`MAINAI_LOCAL_INTELLIGENCE_SCHOOL.md`) and progressive local-first independence.
3. **Authority separation as a first principle** — `REPO-WRITE ≠ PROVIDER-SPEND ≠ EGRESS`; approval
   double-gates; `UNKNOWN != VERIFIED`.
4. **Supervised autonomy before unattended** — bounded cycles, STOP/epoch fencing, provider completion
   cannot bypass independent verification (#319/#321 verified this session).
5. **Whole-project comprehension before new development** (Founder priority, this revision).
The business/finance/communication/multi-company domains are **in scope by Founder definition**; they are
not yet represented in Git content and are expected to arrive via authorized historical ingestion (B).
*No source is re-dated or altered; conflicting/superseded status must be read from each doc's own header.*

---

## D. Implementation reality + ingestion→memory capability map

### D.1 Stage-by-stage pipeline (the Founder's named chain), evidenced at `64c07d5b0`
| Stage | State | Evidence / note |
|---|---|---|
| 1. Authorized source **discovery** | **Absent / manual** | Intake is Founder-initiated upload via `/api/library/import`; no autonomous external-source scan/watch. |
| 2. Large-file **intake** | **Not on default** (built on PRs #311–#315/#320) | Default has standard upload + `zip_import.py` (MAX_FILES=500, 200 MB/pkg; no nested-zip/password handling). |
| 3. Integrity + **immutable originals** | **Built & verified** | `storage/local_fs.py` content-addressed `{sha256}`, atomic write→fsync→rename, dedup, path-traversal/symlink-safe; durable worker stores+verifies original *before* any AI step. |
| 4. Secure **extraction** (text/image/structured) | **Built (text/md/html/docx-paras/PDF-text-layer/A+V); two measured gaps** | `extract.py` + `media_import.py` STEG 12. **Fidelity probed this session** (real `extract_text`): text/md/html/docx-paragraphs/PDF-text-layer ✅; **GAP — DOCX tables/headers/footers dropped** (`doc.paragraphs` only; reproduced), **GAP — scanned/image-only PDF → empty text (no OCR)** + standalone images unhandled. See `LIFE_MASTER_BLUEPRINT.md` §1.2. |
| 5. **Chunking** + source refs | **Built** | `chunking.py`; `document_chunk` carries source refs; media chunks carry `[start,end)` time ranges for exact-moment citation. |
| 6. **Classification** + semantic reconciliation | **Built (document path)** | `claims.py` (claim_type + objective word-overlap `grounding_score`, bounded one-call/chunk capped/doc); `concept_reconciliation/` deterministic, provider-free, one canonical `ProjectEntity` per idea (mig 0064). |
| 7. Durable **memory integration** | **Built layers 1–3; `memory_source_unit` incl. message subtype (S1C)**; **layers 4–8 partial/missing**; **message→claim (S4) not wired** | `claims.py` extracts from `DocumentChunk` only; `MessageSourceUnit` rows exist (provenance) but do not yet flow into claims. |
| 8. Cross-document **relationships** | **Partial** | `source_relationship`/`claim_relationship` models exist but are created **manually**; automatic discovery absent; `ClaimRelationship` is read by `trust.py`, written by no code path yet. |
| 9. **Retrieval with provenance** | **Built** | `retrieve.py`→`search()` returns chunk hits with document/chunk identity + score; media opens at timestamp. |
| 10. **Whole-project understanding** | **Partial** | `project_entities/` + `concept_reconciliation/` + `mainai_executive/why_graph.py`/`missing_piece.py`; the unified queryable project-map over all layers is the design target, partially realised. |

### D.2 Responsiveness, durability, egress, safety
- **Background without blocking the interface:** **Built.** Two durable, lease-fenced job systems —
  `knowledge_import_jobs` (`worker.py`, `FOR UPDATE SKIP LOCKED`, restart-safe) and `mainai_jobs`
  (`jobs/service.py`, append-only `MainAIJobEvent` + `AuditLog`, RLS-scoped) — plus `InteractiveIsolation`
  (admits interactive Founder work alongside background). Large imports run in the worker, not the request.
- **Untrusted source content / egress:** **Built & wired (maturing).** _Correction to Rev. 2: a code
  re-read this session shows the gate is **no longer** Safe-Planner-only._ `egress_policy/` default-deny
  gate (`enforce_egress_policy`, `MODEL_REQUESTED_CONTEXT != AUTHORIZED_EGRESS_CONTEXT`) is wired into the
  Safe Planner **and** the RAG embedding path (`embed_with_policy`, used by `rag/ingest.py`,
  `rag/retrieve.py`, `rag/media_import.py`, `routers/library.py`) **and** chat
  (`chat_with_fallback`) — each turned on via `owner_id` (verified in `routers/chat.py:132,221`,
  `rag/ingest.py:98`, `rag/retrieve.py:27`). The real remaining gap was that the gate **could not
  distinguish a local on-host provider from an external one**, so an on-host Ollama embed was ledgered as
  an external-AI disclosure — no checkable no-egress invariant. **Fixed in PR #326** (truthful
  `decision='local'`; migration 0069; never relaxes a deny). Chat-path NEVER_EGRESS behaviour on degraded
  replies remains a smaller follow-up.
- **Provider-key pre-verification:** **Missing.** Status checks `is_configured()` only; an invalid-but-set
  key yields a silent `failed` import (`MAINAI_PROJECT_UNDERSTANDING_PLAN.md` §0).
- **Independently-verified autonomy:** partial — #205 self-improve ran on a disposable mirror, not the real
  checkout.

---

## E. Current agent / PR landscape

~40 open PRs, **none merged to default yet**. Independent-exam verdicts this session (posted on each PR):
**#319 `397672d84` PASS · #320 `6c1240d8` PASS · #321 `e1f649a9` PASS · #322 `a7c1ffa3` PASS**
(single migration heads 0100/0101 verified; full suites reproduced 3545/3530/3555/3561, 0 genuine failures).
**#324 `306b980f4`** (`codex/coordination-ledger-dedup-takeover`, parent #322, mig 0102) = **active Codex
CC3/CC4/AR6 work — inspected scope/ancestry only; not duplicated/modified.** Claude lanes: **#326**
(this session — egress local/external distinction + local-first ingestion demo, **draft, review required**),
**#323** (npm-audit + CI Playwright `ubuntu-24.04` pin, CI green), #316/#317/#318 (passkeys/PWA), **#325**
(this reconnaissance + `LIFE_MASTER_BLUEPRINT.md`). Integration risks: stacked bases (merge order matters; no rebase-ahead-of-dependency per
`CLAUDE.md`); per-doc status must be read, not inferred; `BRANCH_REGISTRY` needs reconciliation vs the real
PR graph; #324 sequences after #322.

---

## F. Critical architecture gaps (esp. turning historical sources into trustworthy durable understanding)

1. **No path from historical conversations → durable claim memory.** Message *source units* exist (S1C) but
   message→claim extraction (S4) is not wired; and there is **no importer** for a ChatGPT/export archive.
   This is the biggest blocker to "stop developing from fragmented conversation context."
2. **Egress local/external truthfulness** — _corrected:_ chat + RAG-embedding paths **are** gated
   (`owner_id`); the gap was that on-host local calls were ledgered as external disclosures. **Addressed
   in PR #326** (`decision='local'`, checkable no-egress invariant). Remaining: chat-path NEVER_EGRESS on
   degraded replies (smaller).
3. **Provider-key pre-verification missing** — real ingestion fails silently on an invalid-but-set key.
4. **No autonomous source discovery** — intake is manual upload; large-file lane not on default.
5. **Memory layers 4–8 + automatic relationship discovery** partial/manual.
6. **Fragmentation** — 40 unmerged PRs + 113 docs; this report + MASTER HANDOFF begin a single reconciled
   pointer-of-record (not a competing store).

---

## G. Smallest viable next milestones (ordered by dependency / safety / cost / Founder value)

Guiding principle: **plan the country's infrastructure, build only today's house.** Reuse existing modules
(`rag/`, `memory_source_unit`, `jobs/`, `egress_policy`, `concept_reconciliation`) — introduce **no**
competing store/queue/memory system.
1. **M1 — Reconcile the map (docs-only):** this report + MASTER HANDOFF + `BRANCH_REGISTRY` ↔ real-PR-graph
   + per-doc active/proposed/superseded pass. Zero risk, highest leverage.
2. **M2 — Local-first historical-ingestion proof** (see I) on ONE authorized existing file/export, reusing
   the built intake→original→extract→chunk→claim→retrieve chain, with **no paid-provider egress**
   (local/offline embedding or deferred) and provider-key pre-verification. *This is the Founder's priority.*
3. **M3 — Close the ingestion safety gaps M2 surfaces** (only the smallest necessary): provider-key
   pre-verification + a distinct resumable `blocked_provider` state; extend the default-deny egress gate to
   the ingestion/embedding path actually used by M2.
4. **M4 — Message→claim (S4) minimal slice:** let ONE authorized conversation become provenance-linked
   claims through the existing `MessageSourceUnit`→claims path (reusing `concept_reconciliation`).
5. **M5 — Readiness truth board** + the supervised real-checkout cycle (closes the #205 mirror caveat), only
   after comprehension milestones land.
Deferred (design now, build later): memory layers 5–8, autonomous discovery, local weight-training,
multi-company/business expansion surfaces, constitution activation (P7B).

---

## H. Genuinely unresolved Founder decisions (only what evidence cannot settle)

*(Business/finance/communication/multi-company scope is settled = in scope, not asked here.)*
1. **Which authorized source first?** Point to ONE existing file/export for M2 — a current Library/Vault
   upload already in the system, or a new Founder-provided export (e.g. a ChatGPT data export). *I will not
   assume access; I need you to designate it.*
2. **Local-first embedding for ingestion:** confirm M2 must use a **local/offline** embedding path (no paid
   provider, no egress) for the first proof — or whether a classified subset may use a paid embedder once
   the egress gate covers that path.
3. **First-proof emphasis:** the ingestion/comprehension proof (M2, recommended) as the first supervised
   demonstration — confirm, or redirect.

---

## I. Recommended first measurable proof — supervised, local-first historical ingestion

**One Founder-authorized existing file/export → trustworthy, retrievable understanding — no paid egress.**
Reuse the built chain; add only the smallest missing piece M3 names. **Acceptance criteria (all required):**
1. **Original preserved byte-for-byte** (content-addressed store; re-hash matches on read-back).
2. **Source identity + hash verified** before any AI step (`original_stored` reached first).
3. **Extraction accuracy checked** against the original (spot-verifiable text/segment offsets).
4. **Historical claims linked to exact source locations** (claim→chunk→version→source→original, with
   objective grounding score).
5. **Related concepts reconciled** via `concept_reconciliation` **without silently inventing decisions**
   (new interpretations are *proposals*, not auto-approved).
6. **Memory + retrieval persist across sessions** (durable rows; a later session retrieves the same claims).
7. **Life answers a question with traceable evidence** (every answer cites its exact sources).
8. **Failures visible + recoverable** (invalid key → distinct resumable `blocked_provider`, never silent
   `failed`; worker restart resumes).
9. **Background processing does not monopolize the interactive interface** (import runs in the durable
   worker; `InteractiveIsolation` keeps Founder chat admitted).
10. **No unauthorized external data transfer** (egress default-deny on the path M2 uses; local-first
    embedding or deferred; independently reviewed).

If any prerequisite is missing, implement **only** the smallest piece required (M3), not a broader build.

---

## Sources
Repository @ `64c07d5b0`: `docs/` — `MAINAI_PROJECT_UNDERSTANDING_PLAN.md`, `MAINAI_STARTUP_READINESS.md`,
`MAINAI_V1_GOAL_TO_AUTONOMY.md`, `MAINAI_V1_READINESS.md`, `FIRST_AUTONOMOUS_TASK_BLOCKER_MAP.md`,
`MAINAI_LOCAL_INTELLIGENCE_SCHOOL.md`, `MAINAI_CONCEPT_RECONCILIATION.md`, `LIFE_VAULT_EGRESS_CONTROL.md`,
`LIFE_SOURCE_VAULT_FUTURE_COMPATIBILITY.md`, `SECURITY_BLOCKERS.md`, `ROADMAP.md`; code —
`backend/app/` tree, `rag/{claims,message_source,memory_source,retrieve,media_import,chunking,extract}.py`,
`egress_policy/service.py`, `jobs/service.py`, `worker.py`, `storage/local_fs.py`, `models/*`, `rls.py`;
open-PR graph (#285–#325); exam verdicts #319/#320/#321/#322 (this session). Content searches over all 113
docs are reproducible. Items in class INACCESSIBLE were **not** read and are **not** claimed as read.

**This is a proposal for Founder–Life discussion. No implementation authority is claimed or requested.
`first_cycle_go=false`.**
