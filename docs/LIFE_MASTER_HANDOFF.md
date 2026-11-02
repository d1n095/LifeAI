# Life (MainAI) — MASTER HANDOFF

**Read this first in any new conversation or agent session.** It is a *pointer index*, not a second
knowledge store. When this disagrees with an authoritative doc, the authoritative doc wins — fix this file.
Keep it short. `first_cycle_go=false` everywhere.

_Rev. 4 — 2026-11-02 — Claude Opus 4.8 (independent). Base: `claude/det-kommer-mer-879lcm` @ `64c07d5b0`._

## 0. Standing invariants (never violate without explicit Founder say-so)
No merge · no deploy · no DNS/STRATO · no paid/live provider calls · **no paid-provider data egress** · no
Recall · no autonomous GO · don't modify frozen candidates · don't overwrite another agent's branch ·
independent reviewer required (no self-cert) · `UNKNOWN != VERIFIED` · `REPO-WRITE ≠ PROVIDER-SPEND ≠ EGRESS`.

## 1. Founder scope (authoritative — not open questions)
Life = the Founder's **persistent digital partner & overarching intelligence**; coordinates specialist
agents and **future companies**, acts within delegated authority, communicates naturally, gains progressive
**local-first** coding/operational independence. **Business, finance, communication, multiple companies and
other Founder-defined domains are already in scope.** The full vision is **broader than the repo docs** and
must be **discovered + reconciled from authorized historical sources**, not invented from a summary.
**Absence from Git ≠ nonexistence** — history may live in uploads/Library/Vault/DB/external Founder files.
**Priority: whole-project comprehension before extensive new development.**

## 2. Authoritative documents (read these; don't re-summarize here)
**START HERE → `docs/LIFE_MASTER_BLUEPRINT.md`** — the shared, source-linked entry point to the whole
intended system (vision, maturity, dependency graph, roadmap, open decisions; marked INCOMPLETE where the
historical corpus is not yet reachable). **For getting Life running: `docs/LIFE_FIRST_STARTUP_READINESS.md`**
— the operational runbook (readiness verdict, offline-vs-model split, branch integration order, first
blocker, Founder-supervised startup checklist). Then:
Vision/memory: `docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md` · Readiness: `docs/MAINAI_STARTUP_READINESS.md`
· Autonomy: `docs/MAINAI_V1_GOAL_TO_AUTONOMY.md` + `docs/FIRST_AUTONOMOUS_TASK_BLOCKER_MAP.md` · Local-first:
`docs/MAINAI_LOCAL_INTELLIGENCE_SCHOOL.md` · Vault/egress: `docs/LIFE_VAULT_EGRESS_CONTROL.md` · Branch map:
`docs/BRANCH_REGISTRY.md` · **Full recon (A–I):** `docs/LIFE_TAKEOVER_READINESS_RECON.md` · Local-first
ingestion demo + first blocker: `docs/LIFE_LOCAL_FIRST_INGESTION_DEMO.md` · Governance: repo `CLAUDE.md`.

## 3. Verified facts (this session; corrected Rev. 2)
- **Runtime status (Founder fact):** last Founder-used version = **v0.1** (messaging worked, **upload did
  not**, **no historical corpus ever provided**). Current ~40-PR system **never started end-to-end with the
  Founder** → not a Founder-tested runtime. First supervised startup + verified small-file upload must
  precede real historical comprehension. Missing corpus is **expected**, not lost. See startup runbook.
- **Startup audit (default `64c07d5b0`, Docker unavailable in review env):** minimal run (login, navigate,
  message, upload one small file, durable store + sha256, extract) = **READY-pending-boot**; offline
  **retrieval is BLOCKED** (`index_document` needs embedding before any chunk; only offline embedder = Ollama,
  whose dim ≠ hardwired `embedding_dim=1536`); large-file/resumable UI not on default (#311–#315). Messaging
  send/receive stores offline; assistant replies need a model.
- Platform mature: 44 backend pkgs, 55 models, 68 migrations, 198 test files, 62 RLS policies.
- **Ingestion→memory (default branch @ 64c07d5b0):** immutable content-addressed originals + durable
  restart-safe worker **built & verified**; extraction text+A/V built (image-OCR likely gap); chunking +
  provenance built; claim extraction + objective grounding + concept-reconciliation built **for documents**;
  retrieval-with-provenance built. **`memory_source_unit` unifies provenance across documents AND messages
  (S1C built)** — BUT **message→claim extraction (S4) is NOT wired** and there is **no ChatGPT/export
  importer**. **Large-file lane is NOT on default** (cursor PRs #311–#315/#320). Memory layers 4–8 +
  automatic relationship discovery partial/manual.
- **Egress (corrected this session):** default-deny gate **is** wired into Safe Planner **+ RAG embed**
  (`embed_with_policy`: ingest/retrieve/media/library) **+ chat** (`chat_with_fallback`), all via `owner_id`.
  The real gap — on-host local calls ledgered as external disclosures — is **fixed in PR #326**
  (`decision='local'`, migration 0069, checkable no-egress invariant).
- **Extraction fidelity:** text/md/html/PDF-text-layer/A+V ✅; **DOCX tables/nested/headers/footers now
  extracted (PR #327, draft)**; **remaining GAPS: scanned/image PDF → empty text (no OCR); standalone
  images; DOCX text boxes/footnotes (detected+reported, not extracted).**
- **`embedding_dim=1536` hardwired** → a real local embedder needs a schema migration + Founder decision (blocker).
  **Why it is coupled (verified Rev. 3):** an HNSW index (`document_chunks_embedding_hnsw_idx`,
  `vector_cosine_ops`, migration 0004) sits on the `vector(1536)` column; pgvector ANN indexes require a fixed
  dimension, so one column can't serve two model sizes with ANN. All good local embedders are 768/1024-dim.
  *Model identity+dim* can be made configurable **non-destructively** (additive columns + fail-closed guard —
  Blueprint §14.3); the *width change* (→1024) is a one-time Founder-gated re-embed migration (vectors must be
  recomputed). Recommended local models (validate on Founder data via ScandEval/MTEB-sv): embedding **bge-m3**
  or **multilingual-e5-large** (both 1024) / KBLab Swedish (768); chat **Qwen2.5-7B** / Gemma-2-9B / Llama-3.1-8B
  / GPT-SW3 (Swedish-native). See Blueprint §14.
- **CI reality (verified Rev. 3, 2026-11-01):** **#323 (`04b27e9`) is fully green and ready (not draft),
  MERGEABLE/CLEAN.** #326/#327 fail **only** the 3 checks #323 fixes (E2E Playwright, E2E same-origin proxy,
  npm audit); their backend/RLS/alembic checks pass → their red is base-inherited, not a regression. Fresh
  local regression (Postgres 5433 + pgvector 0.8.6): #326 egress+locality+restart-demo = **26 passed**; #327
  `tests/backend/rag/` = **407 passed / 1 skip / 2 env-only fails** (both reproduce on untouched base — a
  host-UTC+1 timezone artifact + a Docker-only test; zero #327 regressions).
- **Provider-key pre-verification missing** (invalid-but-set key → silent `failed`).
- **Background non-blocking:** two durable lease job systems (`knowledge_import_jobs`/worker.py;
  `mainai_jobs`/jobs service) + `InteractiveIsolation` = built.
- Autonomy proven on a disposable mirror (#205), NOT the real checkout.
- **Source-to-memory architecture (Rev. 4, verified read-only @ `0a265ba` — Blueprint §16):** the storage +
  provenance substrate is genuinely strong and tested (content-addressed write-once originals, hash-verified
  dedup/self-repair, cross-owner-safe purge via SECURITY DEFINER, immutable MemorySourceUnit lifecycle,
  decision-vs-proposal gate with human-review-only promotion). Lifecycle test run: **278 passed, 1 fail**
  (the one fail is a Docker-entrypoint infra test, not a lifecycle regression). The verified gaps are all
  *missing connections, not broken foundations*: (a) **no original-download route for non-media sources and no
  Life-facing original-retrieval tool** (Founder+Life cannot fetch a whole text/PDF/DOCX original); (b)
  message→claim (S4) unwired + `ClaimRelationship` never populated (trust.py reads edges nothing creates); (c)
  the multi-agent workforce verifier (`BUILDER_CANNOT_SELF_VERIFY`) is a built framework driven by the gated
  executive loop, not wired into document analysis. Smallest next milestone + acceptance criteria: Blueprint §16.6.

## 4. Exact PR / commit state (2026-11-01)
- Default `claude/det-kommer-mer-879lcm` @ `64c07d5b0`; no open PR merged into it yet.
- **Exam verdicts (posted on each PR): #319 `397672d84` PASS · #320 `6c1240d8` PASS · #321 `e1f649a9` PASS
  · #322 `a7c1ffa3` PASS** (single mig heads 0100/0101; full suites 3545/3530/3555/3561, 0 genuine fails).
- **#324 `306b980f4`** `codex/coordination-ledger-dedup-takeover` (parent #322; mig 0102) = **active Codex
  CC3/CC4/AR6**. Inspect only; do not duplicate/modify; sequence after #322.
- Claude: **#327** (DOCX extraction completeness — tables/nested/headers/footers, **draft @ `69cdaf342`**,
  review required; rag regression 409 pass/1 skip), **#326** (egress local/external distinction + local-first
  ingestion demo, **draft @ `3a07f58b0`**, review required; migration 0069 single head; 1193+1-skip regression
  green), **#325** (recon + handoff +
  `LIFE_MASTER_BLUEPRINT.md`, draft, docs-only, @ `0aaa419`), **#323** (npm-audit baseline + CI Playwright
  pin, @ `04b27e9`, **now fully green & ready/not-draft**), **#328** (`claude/basic-startup-no-model-proof`,
  **draft @ `fa840e4`**, test-only off base, review required — executable proof that upload/extract/persist +
  restart survival work with NO embedding/chat model; 2 passed locally), #316/#317/#318 (passkeys/PWA). Deep
  cursor/* stack #285–#315 unmerged. (BRANCH_REGISTRY predates #323–#328 — reconciling it is the tracked M1
  zero-risk docs task, §7.)

## 5. Genuinely open Founder decisions (block M2; recon §H)
1. **Designate ONE authorized source** for the first ingestion proof (existing Library/Vault upload, or a
   new export e.g. ChatGPT data export) — I will not assume access.
2. Confirm **local-first/offline embedding (no egress)** for the first proof.
3. Confirm the **ingestion/comprehension proof (M2)** as the first supervised demonstration.

## 6. Ownership
Claude (this lane): exams #319–#322 (done, PASS); recon + handoff (#325). Codex: #319/#321/#322 builder +
**#324 active**. Cursor: autonomy/first-cycle/large-file/messaging lanes (#285–#315). Don't overlap.

## 7. Next safe action
**Rev. 3 lead:** the integration bottleneck is **review, not more code**. (1) Review+merge **#323 first**
(green/ready today) → turns #326/#327's three red checks green. (2) Independent review of **#327 then #326**
(package + fresh evidence in `LIFE_FIRST_STARTUP_READINESS.md` §7). (3) Founder picks the **local embedding +
chat models** (Blueprint §14) → unblocks the configurable-embedding PR (§14.3, additive/non-destructive,
sequenced *after* #326's `0069`) and then the one-time re-embed migration. (4) Supervised `docker compose up`
dry-run of the startup checklist (runbook §5) in an authorized env. No 4th PR was stacked this round, by
design (don't dilute review; don't collide with `0069`; the offline blocker is a model decision, not a bug).

**Priority (per the Founder's refinement): verified sources + Blueprint first, then build.** Blueprint
(`LIFE_MASTER_BLUEPRINT.md`, incl. §11 source audit, §12 comprehension gap analysis, §13 next-blocker +
acceptance criteria) and the local-first ingestion demo (#326) are delivered. **Source-access audit (§11):
no authorized historical source is reachable in the review environment** — only disposable `examiner_*` DBs
and `.env.example` templates. **Immediate path = first supervised startup** (runbook `LIFE_FIRST_STARTUP_READINESS.md`): assign
independent review in order **#323 → #327 → #326**, then run a supervised `docker compose up` dry-run of the
startup checklist in an authorized env (Docker unavailable in the review clone). Decisions that gate further
comprehension work: **(i)** that independent review; **(ii)** choose a **local embedding model** → turns the
`embedding_dim` blocker into a bounded migration (unblocks offline retrieval); **(iii)** designate ONE
authorized source (Blueprint §11 A or B); **(iv)** choose a local-first *chat* model (claim extraction needs
one) or authorize a classified egress. The historical-ingestion gate remains
**Blueprint §10 Q1 — designate ONE authorized historical source** so the ⟨access⟩-marked corpus can actually be
ingested and the vision verified (today it is not reachable from Git). Extraction-fidelity build units,
each its own reviewed PR: **✅ DOCX tables/headers/footers done (#327)**; remaining, in order —
**(1)** scanned-PDF/image OCR *(needs a local OCR-engine decision — local-first)*, **(2)** standalone image
ingestion, **(3)** full extraction of detected-but-unsupported DOCX constructs (text boxes/footnotes),
**(4)** configurable `embedding_dim` (needs Founder model decision), **(5)** S4 message→claim + export
importer. No implementation without established scope + authorization.
M1 (reconcile `BRANCH_REGISTRY` ↔ real PR graph) remains a zero-risk docs step that can proceed now.
