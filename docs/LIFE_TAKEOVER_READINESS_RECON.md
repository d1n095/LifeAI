# Life (MainAI) — Whole-Project Reconnaissance & Verified State-of-the-Project Report

**Status:** Read-only reconnaissance. **Proposal for Founder–Life discussion — NOT authorization to build
the roadmap.** No code written, no merge, no deploy, no spend, no Recall, no autonomous enablement.
`first_cycle_go=false`.

**Author:** Claude Opus 4.8 (independent examiner/reconnaissance). **Base:** default branch
`claude/det-kommer-mer-879lcm` @ `64c07d5b0` (verified = origin default tip, 0 ahead / 0 behind).
**Method:** direct reading of the repository at that SHA — 113 `docs/`, `backend/app/` (44 packages,
55 models, 68 migrations, 198 backend test files, 62 registered RLS policies), and the open-PR graph via
the GitHub API. Every major claim is traceable to a source. Where a claim is a *plan/proposal* rather than
*verified behaviour*, it is labelled. **Honesty rule observed:** this report distinguishes what was
actually examined from what is present-but-unread and from what is inaccessible; it does not claim to have
read material it could not reach, and it does not turn an interpretation into a Founder-approved requirement.

---

## A. Verified existing inventory — what is present and where

**Backend (`backend/app/`, 44 packages).** Capability clusters (each a real package at `64c07d5b0`):
- **Memory / knowledge:** `rag/` (content-addressed storage, chunks, KnowledgeClaim + objective
  `grounding_score`, `trust.py`), `inspectable_memory/`, `founder_memory/`, `founder_memory_signals/`,
  `memory_threads/`, `memory_work_linkage/`, `project_entities/`, `concept_reconciliation/`,
  `work_candidates/`, `active_context/`, `context/` (turn `resolver.py`).
- **Autonomy / execution:** `mainai_execution/`, `mainai_executive/`, `development_supervisor/`,
  `development_driver/`, `development_operator/`, `safe_planner/`, `execution_envelopes/`,
  `autonomous_gap/`, `jobs/`, `workforce/`, `agent_coordination/` (durable ledger `durable.py`).
- **Local-first learning:** `mainai_school/` (Local Intelligence School, #236 = current tip),
  `capability_reality/`, `strategy_synthesis/`, `strategy_evaluation/`, `work_intelligence/`,
  `problem_learning/`, `corpus_trial/` (corpus intake trial harness, migration 0052).
- **Security kernel:** `rls.py` (FORCE ROW LEVEL SECURITY, **62 policies**), `egress_policy/`,
  `provider_spend/` (reserve→settle, SPEND-STOP, mig 0060), `workforce/kill_switch.py`,
  `workforce/authority.py`, `execution_envelopes/` (authority envelope, mig 0057), `account/erasure.py`.
- **Readiness / diagnostics:** `mainai_startup_readiness/`, `diagnosis/`, `intelligence_governance/`.
- **Providers / integration:** `providers/`, `provider_planning/`, `integrations/`,
  `providers/transcription.py`.

**Frontend:** Next.js 16 / React 19 portal (Dashboard, Chat, Knowledge, Projects, Documents, Admin).

**Life Vault:** a real design lane (`docs/LIFE_VAULT_EGRESS_CONTROL.md`,
`docs/LIFE_SOURCE_VAULT_FUTURE_COMPATIBILITY.md`, `docs/LIFE_VAULT_V4_V5_V7_V8_DESIGN_MEMOS.md`,
`docs/CLAUDE_LIFE_VAULT_EGRESS_LANE.md`). Core invariant: **REPO-WRITE ≠ PROVIDER-SPEND ≠ EGRESS AUTHORITY.**
Target long-term shape: original→hashing→dedup→chunking→compression→encryption→durable/cold storage, Life
working from derived knowledge and decrypting original chunks on demand (future-compat review concludes **no
code change needed yet** — the abstraction boundary is already compatible).

**Stored source material in git:** **none** — no committed originals/archives/ZIP/PDF. Actual uploaded
content lives in the `lifeai_uploads` volume and DB, not the repository (see B).

---

## B. Historical coverage — what was examined, what is missing, how complete this picture is

**Actually examined (read this session):** `MAINAI_PROJECT_UNDERSTANDING_PLAN.md` (8-layer memory, §0/§2),
`MAINAI_STARTUP_READINESS.md`, `MAINAI_V1_GOAL_TO_AUTONOMY.md`, `MAINAI_V1_READINESS.md`,
`FIRST_AUTONOMOUS_TASK_BLOCKER_MAP.md`, `MAINAI_LOCAL_INTELLIGENCE_SCHOOL.md`,
`MAINAI_CONCEPT_RECONCILIATION.md`, `SECURITY_BLOCKERS.md`, `ROADMAP.md`, `LIFE_VAULT_EGRESS_CONTROL.md`,
`LIFE_SOURCE_VAULT_FUTURE_COMPATIBILITY.md`; the full `backend/app/` module tree; `rls.py`; the open-PR
graph; and (full source read) the #319/#320/#321/#322 candidates.

**Present but not yet fully read (113 docs total; ~90 unread in depth):** the remaining `LIFE_*`, `MAINAI_*`,
`ACTIVE_WORK_*`, `HANDOFF_*` design docs. Named in the index; not line-read here.

**Inaccessible / requires Founder-authorized access (NOT examined, do not assume content):**
- **Historical ChatGPT conversations** — the Founder's primary concern. There is **no importer** and **no
  stored export** in the repo. `account/export.py` exports the user's *own account data*; it does not import
  ChatGPT. A conversation data model exists (`models/conversation.py`, `memory_threads/`), and
  `context/resolver.py` classifies turns, but **conversation turns are not yet a first-class memory source**
  feeding the claim pipeline (`MAINAI_PROJECT_UNDERSTANDING_PLAN.md` §2 layer 7).
- **The broader business / financial / communication / multi-project vision.** A content search of all 113
  docs (Swedish + English terms) returned **zero** hits for business/finance/revenue/communication/
  multi-project narrative. **Evidence-backed conclusion:** the Founder's belief that the full vision is
  broader than the recent technical categories is consistent with the evidence — **that broader vision is
  not captured in this repository.** It most likely lives in the inaccessible ChatGPT conversations and
  other external sources, and will require authorized export to reconstruct.
- **The running system's data** — the actual Library/Vault contents, memory DB rows, uploaded originals —
  exist only in a live deployment/volume, not in git; not inspected here.

**Completeness estimate:** the *technical platform* picture is **high-confidence** (code+migrations+tests
read directly). The *historical-vision* picture is **low-confidence and known-incomplete** — the material
that would complete it is inaccessible from the repo alone.

---

## C. Reconstructed vision map (from repo evidence only; NOT a Founder-approved spec)

Recurring, cross-referenced intentions evidenced in the docs/code:
1. **One unified, RLS-scoped memory/knowledge base** (not per-feature silos) with an unbroken provenance
   chain original→source→claim→project/idea/decision/founder/constitution
   (`MAINAI_PROJECT_UNDERSTANDING_PLAN.md`).
2. **Life as persistent operator/partner**, external APIs as *teachers/critics/examiners* not permanent
   brain (`MAINAI_LOCAL_INTELLIGENCE_SCHOOL.md`), progressing toward local-first.
3. **Authority separation as a first principle** — REPO-WRITE ≠ PROVIDER-SPEND ≠ EGRESS; approval
   double-gates; `UNKNOWN != VERIFIED` (`LIFE_VAULT_EGRESS_CONTROL.md`, `MAINAI_STARTUP_READINESS.md`).
4. **Supervised autonomy before unattended autonomy** — bounded cycles, STOP/epoch fencing, independent
   verification that provider completion cannot bypass (#319/#321 verified).
5. **The same concept under different names** — "MainAI" / "Life"; "whole-project understanding" /
   "project map" / `project_entities` + concept-reconciliation; "constitution" = layer 8 governing docs.

Concepts appearing under different names, shared foundations, and dependencies are mapped in §A clusters.
**Conflict/supersession:** doc status must be read per-doc (many carry explicit `Status:` headers); the
deep PR stack (§E) contains successors that re-scope earlier candidates. This report does **not** re-date or
alter any source.

---

## D. Implementation reality (built / partial / proposed / independently verified / blocked)

| Capability | State | Evidence |
|---|---|---|
| Immutable content-addressed originals, atomic store, dedup, path-traversal/symlink-safe | **Built & verified** | `storage/local_fs.py`, durable-worker (`MAINAI_PROJECT_UNDERSTANDING_PLAN.md` §1.1) |
| Durable restart-safe worker (`FOR UPDATE SKIP LOCKED`), per-file state machine received→…→indexed | **Built & verified** | `worker.py` |
| Extraction → chunk → claim + objective grounding | **Built** (layers 1–3) | `rag/`, KnowledgeClaim, `trust.py` |
| Project/idea/decision/founder/constitution memory (layers 4–8) | **Partial / missing** | `MAINAI_PROJECT_UNDERSTANDING_PLAN.md` §2 |
| Provider-key **pre-verification** (key *works*, not just set) | **Missing (blocker)** | §0 of the plan |
| Cross-source reconciliation (one canonical entity per idea) | **Built, provider-free** | `concept_reconciliation/`, mig 0064 |
| Founder→worker autonomy chain (both entry paths) | **Substantially built** | `MAINAI_V1_GOAL_TO_AUTONOMY.md` (file:line-cited) |
| Responsiveness while background workers run (interactive isolation) | **Built** | `InteractiveIsolation`, `operational_handover/absence` |
| Authorized external comms (messaging/delivery gateway, GitHub intake) | **Partial, on unmerged PRs** | #300–#304 |
| Supervised agent coordination + review + follow-up | **Built + hardening under exam** | `agent_coordination/`, #319/#321/#322 |
| Failure detection / safe resume after interruption | **Built** | durable ledger, lease/epoch fencing (#321 verified) |
| Evidence-based prioritization / readiness levels | **Built** | `mainai_startup_readiness/`, `FIRST_AUTONOMOUS_TASK_BLOCKER_MAP.md` |
| Local code dev/test without paid APIs | **Framework only, NOT production-ready** | `mainai_school/`; `TRAINING DATA ≠ MODEL TRAINED ≠ IMPROVED ≠ SAFE` |
| **External-AI egress classification/filtering** | **Missing** — chat sends raw content to providers unfiltered | `LIFE_VAULT_EGRESS_CONTROL.md` |
| Independently-verified autonomy behaviour | **Partial** | #205 self-improve ran on disposable mirror, not real checkout (`MAINAI_V1_READINESS.md`) |

---

## E. Current agent / PR landscape (coordination truth)

~40 open PRs, **none merged into default yet**, on long stacked chains:
- **codex/\*** frozen review successors: **#319→#321→#322→#324** (plus independent **#320**). #319/#320/#321
  independently examined & PASS this session; #322 examination completing. **#324
  `codex/coordination-ledger-dedup-takeover`** (parent `a7c1ffa3a` = #322 head; adds mig
  `0102_coordination_ledger_authority`) is **active Codex work** closing **CC3/CC4/AR6** — the exact gaps
  #322 scoped out. *Inspected scope/ancestry only; not duplicated or modified.*
- **cursor/\*** chains: coordination, provider integration, large-file lanes, founder-workflow, first-cycle
  execution, activation-readiness (#285–#315), messaging-gateway/GitHub-intake (#300–#304).
- **claude/\***: #316/#317/#318 (passkeys/PWA), **#323** (npm-audit baseline + CI Playwright `ubuntu-24.04`
  pin; CI green).

**Integration risks:** (a) stacked bases → merge **order** matters and rebase-ahead-of-dependency is
forbidden (`CLAUDE.md`); (b) active vs proposed vs superseded must be read per-doc, not inferred;
(c) `docs/BRANCH_REGISTRY.md` must be reconciled against the real PR graph before any sequencing is proposed;
(d) #324 overlaps the coordination layer #319/#321/#322 harden — sequence after #322.

---

## F. Critical architecture gaps (esp. turning historical sources into durable understanding)

1. **No path from historical conversations → durable memory.** No ChatGPT importer; conversations not a
   first-class claim source. This is the single biggest blocker to "stop developing from fragmented
   conversation context." Requires authorized export + an additive conversation-provenance model (layer 7).
2. **No external-AI egress classification** — sensitive source content can leave to providers unfiltered
   (`LIFE_VAULT_EGRESS_CONTROL.md`). Trust in durable memory is undercut if ingestion/egress is unclassified.
3. **Provider-key pre-verification missing** — real ingestion silently fails on an invalid-but-set key (§0).
4. **Memory layers 4–8 partial/missing** — provenance links (Task→claim→source), idea/decision/founder/
   constitution structure.
5. **Autonomy proven on a mirror, not the real checkout** (#205 caveat) — the supervised real-checkout cycle
   is unproven.
6. **Fragmentation itself** — 40 unmerged PRs + 113 docs; no single reconciled source of truth (this report
   + the MASTER HANDOFF begin to address it, pointing to authoritative docs rather than replacing them).

---

## G. Smallest viable next milestones (ordered by dependency / safety / cost / Founder value)

Target rung: **`READY_FOR_SAFE_INTERNAL_RUN`** (bounded, supervised, local, no paid provider). Each milestone
is independently verifiable and gated on the readiness evaluator + independent review:
1. **M1 — Reconcile the map (docs-only).** This report + MASTER HANDOFF + `BRANCH_REGISTRY` ↔ real-PR-graph
   reconciliation + per-doc active/proposed/superseded status pass. *Verify:* a reviewer derives truth &
   merge order with no prior context. (Zero code; highest leverage.)
2. **M2 — Historical-source intake path (the Founder's priority).** Design + minimal build of an **authorized
   conversation/export importer** into the existing immutable-original → claim pipeline (reusing `rag/` +
   `memory_threads/`, NOT a new memory store). *Verify:* one authorized export → immutable original →
   extracted → claims with provenance, retrievable. *Depends on:* Founder-authorized export + egress stance.
3. **M3 — Provider-key pre-verification + distinct resumable `blocked_provider` state** (§0 blocker).
4. **M4 — Readiness truth board.** Wire `evaluate_startup_readiness(...)` into one live read-only
   "what blocks the first safe internal run now" view (machine-checked gates, not hardcoded).
5. **M5 — Supervised internal run on the real checkout, bounded** (closes the #205 mirror caveat): one small,
   reversible, local task, full STOP/epoch fencing, focused verification, `first_cycle_go` stays false.
Deferred (design now, build later): memory layers 5–8, egress-classification build, automatic relationship
discovery, local weight-training, multi-project/business expansion, constitution activation (P7B).

---

## H. Questions for the Founder (only genuine, evidence-unresolvable ambiguities)

1. **ChatGPT history:** can you provide an authorized export (e.g., the OpenAI data-export archive)? Without
   it, the historical vision cannot be reconstructed from the repo. *(I will not assume access.)*
2. **Egress stance for ingestion:** when historical/sensitive sources are imported, what may leave to
   external providers for embedding/classification — nothing (local-only), or a defined classified subset?
   (Drives whether M2 can use paid embeddings.)
3. **Scope of "whole project":** is the business/financial/communication/multi-project layer meant to live in
   Life's memory now, or is the immediate scope the technical platform + knowledge base? (The repo contains
   only the latter.)
4. **First-takeover target:** do you want M5 (a bounded code task on the real checkout) as the first
   demonstration, or an ingestion/comprehension demonstration (M2)?

---

## I. Recommended first supervised takeover demonstration (measurable, with acceptance criteria)

**Option A (comprehension-first, matches the Founder's stated priority):** *Authorized-export → durable
understanding.* Founder provides one authorized conversation/document export; Life ingests it through the
immutable-original → extraction → claim pipeline, reconciles concepts against existing `project_entities`,
and answers a Founder question with **full provenance** (claim→chunk→source→original).
*Acceptance:* (1) original stored immutably + verified before any AI step; (2) ≥N claims created with
grounding scores; (3) a retrieval answer cites its exact sources; (4) an invalid provider key yields a
distinct resumable state, never silent `failed`; (5) no unclassified egress of flagged content; (6)
independently reviewed; `first_cycle_go=false`.

**Option B (code-first):** the bounded supervised real-checkout cycle (M5) — one small reversible change,
STOP/epoch-fenced, focused verification, independent review.

*Recommendation:* **Option A** — it directly attacks the fragmentation problem the Founder raised and
exercises the intake→memory→comprehension chain end-to-end, with lower blast radius than a code-writing run.

---

## Sources
Repository @ `64c07d5b0`: the `docs/` named in §B; `backend/app/` module tree (44 pkgs, 55 models, 68
migrations, 198 test files, 62 RLS policies); `backend/app/rls.py`; `storage/local_fs.py`; `worker.py`;
`models/conversation.py`; `context/resolver.py`; `account/export.py`; the open-PR graph (#285–#324) via the
GitHub API; independent-examination verdicts #319/#320/#321/#322 (this session). Content searches over all
113 docs are reproducible with the queries in §B. Items labelled "to be re-verified" (e.g. hardware) are
prior-session notes requiring confirmation.

**This is a proposal for Founder–Life discussion. No implementation authority is claimed or requested.
`first_cycle_go=false`.**
