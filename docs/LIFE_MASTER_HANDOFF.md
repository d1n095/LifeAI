# Life (MainAI) — MASTER HANDOFF

**Read this first in any new conversation or agent session.** It is a *pointer index*, not a second
knowledge store. When this disagrees with an authoritative doc, the authoritative doc wins — fix this file.
Keep it short. `first_cycle_go=false` everywhere.

_Rev. 2 — 2026-11-01 — Claude Opus 4.8 (independent). Base: `claude/det-kommer-mer-879lcm` @ `64c07d5b0`._

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
historical corpus is not yet reachable). Then:
Vision/memory: `docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md` · Readiness: `docs/MAINAI_STARTUP_READINESS.md`
· Autonomy: `docs/MAINAI_V1_GOAL_TO_AUTONOMY.md` + `docs/FIRST_AUTONOMOUS_TASK_BLOCKER_MAP.md` · Local-first:
`docs/MAINAI_LOCAL_INTELLIGENCE_SCHOOL.md` · Vault/egress: `docs/LIFE_VAULT_EGRESS_CONTROL.md` · Branch map:
`docs/BRANCH_REGISTRY.md` · **Full recon (A–I):** `docs/LIFE_TAKEOVER_READINESS_RECON.md` · Local-first
ingestion demo + first blocker: `docs/LIFE_LOCAL_FIRST_INGESTION_DEMO.md` · Governance: repo `CLAUDE.md`.

## 3. Verified facts (this session; corrected Rev. 2)
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
- **Provider-key pre-verification missing** (invalid-but-set key → silent `failed`).
- **Background non-blocking:** two durable lease job systems (`knowledge_import_jobs`/worker.py;
  `mainai_jobs`/jobs service) + `InteractiveIsolation` = built.
- Autonomy proven on a disposable mirror (#205), NOT the real checkout.

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
  `LIFE_MASTER_BLUEPRINT.md`, draft, docs-only), **#323** (npm-audit + CI Playwright pin, green),
  #316/#317/#318 (passkeys/PWA). Deep cursor/* stack #285–#315 unmerged.

## 5. Genuinely open Founder decisions (block M2; recon §H)
1. **Designate ONE authorized source** for the first ingestion proof (existing Library/Vault upload, or a
   new export e.g. ChatGPT data export) — I will not assume access.
2. Confirm **local-first/offline embedding (no egress)** for the first proof.
3. Confirm the **ingestion/comprehension proof (M2)** as the first supervised demonstration.

## 6. Ownership
Claude (this lane): exams #319–#322 (done, PASS); recon + handoff (#325). Codex: #319/#321/#322 builder +
**#324 active**. Cursor: autonomy/first-cycle/large-file/messaging lanes (#285–#315). Don't overlap.

## 7. Next safe action
**Priority (per the Founder's refinement): verified sources + Blueprint first, then build.** The Blueprint
(`LIFE_MASTER_BLUEPRINT.md`) and the local-first ingestion demo (#326) are delivered. The gating item is
now **§5 Q1 — designate ONE authorized historical source** so the ⟨access⟩-marked corpus can actually be
ingested and the vision verified (today it is not reachable from Git). Extraction-fidelity build units,
each its own reviewed PR: **✅ DOCX tables/headers/footers done (#327)**; remaining, in order —
**(1)** scanned-PDF/image OCR *(needs a local OCR-engine decision — local-first)*, **(2)** standalone image
ingestion, **(3)** full extraction of detected-but-unsupported DOCX constructs (text boxes/footnotes),
**(4)** configurable `embedding_dim` (needs Founder model decision), **(5)** S4 message→claim + export
importer. No implementation without established scope + authorization.
M1 (reconcile `BRANCH_REGISTRY` ↔ real PR graph) remains a zero-risk docs step that can proceed now.
