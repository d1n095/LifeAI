# Dependency Staircase: Founder-Login → Knowledge Import → Project Memory
**Purpose:** Per the founder's explicit 2026-07-19 instruction ("Bygg en prioriterad beroendetrappa från fungerande founder-login vidare mot MainAI:s kunskapsimport och projektminne"), this is a prioritized, dependency-ordered sequence of concrete next steps, starting from the now-working founder-only launch and running through knowledge import and project memory.
**Relation to the source phase-gate structure:** The original phase gates (Phase 0–7, from `09_OPEN_QUESTIONS_RISKS_AND_PHASES.md`, carried forward verbatim in `SOURCE_PHASE_GATES.md` in this folder) are broad and generic. This document re-sequences and makes concrete the founder's specifically requested near-term priority — knowledge import and project memory — pulling relevant pieces of the source Phase 2 and Phase 5 gates earlier and more specifically, without contradicting the source structure's dependency logic.
**Status:** Planning document. Nothing below is implemented except Step 0, which already is.

---

## The staircase

Each step lists: what it is, why it's next (dependency reasoning), what blocks it, and what evidence would prove it's done. Steps must be read as inherently sequential — later steps assume earlier ones are real, not just planned.

### Step 0 — Founder-only launch: BUILT, CI-GREEN, awaiting merge/deploy approval ✅ (done, not yet merged)

- **What:** Single founder identity, public registration blocked, MainAI-surface routes gated. `claude/founder-only-launch@bf31fad`, reviewed and fixed at `e9b4b76`.
- **Why first:** Everything else assumes there is exactly one authenticated, gated founder session to build on top of.
- **Blocked by:** Nothing technical. Blocked only on the founder's explicit merge and deploy approval — a decision this session will not make unprompted.
- **Evidence it's done:** `backend/tests/account/test_founder_only.py`, `frontend/e2e/auth.spec.ts`, CI run `29689918729` (success). See `06_PROJECT_STATUS/IMPLEMENTED_AND_VERIFIED.md`.

### Step 1 — Merge and deploy (founder decision, not autonomous)

- **What:** Merge `claude/founder-only-launch` into the deploy branch; separately, decide when/whether to trigger an actual Render deploy.
- **Why next:** Nothing after this step is real in production until it happens — but it's also the one step in this staircase this session must not perform without explicit approval, per standing instructions.
- **Blocked by:** Founder approval only. No technical blocker.
- **Evidence it's done:** Merge commit on the deploy branch; (separately) a successful Render deploy with `/api/health` green.

### Step 2 — Founder account hardening: TOTP (OD-02)

- **What:** Add a second authentication factor to the one account that now matters more, once it's live in production.
- **Why here, not earlier:** Before this account holds anything of higher value than "can log in" (i.e., before Step 4's knowledge import), the cost of a password-only compromise is bounded to "someone can use the chat with the founder's provider keys." After Step 4, it also means "someone can read the founder's imported knowledge." Sequence hardening before the thing worth protecting exists.
- **Blocked by:** Step 1 (no reason to build auth hardening against a branch that isn't even live yet, though it can be developed in parallel on its own branch).
- **Recommendation basis:** `02_DECISIONS/OD_02_03_04_17_ANALYSIS.md` §OD-02 — TOTP recommended over WebAuthn/passkey for v1 (lower cost, solved recovery pattern via backup codes) and over email-OTP (not an independent factor given the existing email-based reset flow).
- **Evidence it's done:** New encrypted TOTP-secret column + enrollment/verification endpoints + backup codes + tests exercising both success and lockout-recovery paths.

### Step 3 — Ownership/scope foundation (OD-04)

- **What:** Add an explicit `owner_id` column (defaulting to the founder's fixed UUID) to `documents` and `projects`.
- **Why here:** Cheapest the moment before these tables start receiving real, meaningful data from knowledge import (Step 4) — retrofitting ownership onto already-populated tables later is a harder, riskier migration. Doing it now, while data volume is still small, is the "cheap insurance" case made in the OD-04 analysis.
- **Blocked by:** Nothing beyond Step 1 being real (no reason to migrate a schema that isn't deployed yet, though again this can be developed in parallel).
- **Recommendation basis:** `02_DECISIONS/OD_02_03_04_17_ANALYSIS.md` §OD-04 — explicitly recommends option B (add the column now) over doing nothing (A, defers the cost) or building the full data-zone taxonomy now (C, premature).
- **Evidence it's done:** Migration adding `owner_id`, default backfilled to the founder UUID, no behavior change yet (single owner still).

### Step 4 — Knowledge import v1 ("MainAI:s kunskapsimport")

- **What:** Extend LifeAI's existing, working RAG ingest pipeline (`backend/app/rag/ingest.py`, `chunking.py`, `extract.py`) into a founder-knowledge intake path: import documents (starting with this very FKP v1.1 package as the first real test case — it is, after all, exactly the kind of founder knowledge this pipeline should be able to ingest), track provenance (source file, checksum, import date), and distinguish "raw imported material" from "reviewed/trusted" the way `02_DECISIONS/DECISION_REGISTER.md` D-19's "raw_material is untrusted-by-design" principle intends — without necessarily building the full UPGRADE_26 v2 ten-table model in one step; start from what already exists (`document_chunk.py`) and extend it, per `03_ARCHITECTURE/TARGET_ARCHITECTURE.md`'s note that any adaptation should build on the existing model, not overwrite it.
- **Why here:** This is the founder's explicitly named next priority after founder-login. It also naturally exercises Step 3's ownership column and makes Step 2's MFA recommendation concretely worth doing (there's now something behind the login worth protecting).
- **Blocked by:** Step 1 (needs a live, gated founder session to import *into*), benefits from Step 3 (ownership column) being in place first so imported documents aren't later retrofitted.
- **Where OD-03 (Founder Vault) becomes relevant:** any *sensitive* originals imported here (not just this FKP package, which is not secret) raise the storage-location question analyzed in OD-03. Knowledge import v1 should start with non-sensitive material (this FKP package, public/architecture docs) and treat Founder Vault-grade encryption as a prerequisite before importing anything genuinely private.
- **Evidence it's done:** A founder-triggered import of at least one real document set (recommended: this FKP package itself) into `document_chunk`/equivalent, searchable via the existing `/api/knowledge/search` endpoint, with provenance visible.

### Step 5 — Project memory v1 ("projektminne")

- **What:** Implement the core of the Conversation Context Resolver (`08_HANDOVER/AGENT_CONTEXT_RULES.md`) on top of LifeAI's existing `conversations`/messages model: current-message priority, last 3–5 messages verbatim, last 15 as a structured timeline, and a minimal decision ledger (even a simple append-only table tagging messages as `decision`/`correction`/`open-question` would satisfy the D-18 "decisions as typed knowledge objects, not a separate ad-hoc table" principle without building the full ten-table model).
- **Why here, not earlier:** Project memory needs *something* to remember — it's more valuable once knowledge import (Step 4) has given the system real founder material to reason about and connect conversations to, though the two could be developed in parallel once Step 1 is live.
- **Blocked by:** Step 1 (needs live conversations to build memory over). Benefits from, but is not strictly blocked by, Step 4.
- **Evidence it's done:** A conversation that references an earlier decision or correction and the system's context assembly demonstrably applies the newer belief, with the older one marked superseded rather than silently dropped — matching `08_HANDOVER/AGENT_CONTEXT_RULES.md`'s belief-lifecycle rule.

### Step 6 — Founder Control Room / agent orchestration foundation

- **What:** The first real implementation slice of `03_ARCHITECTURE/AI_RESOURCE_ORCHESTRATION.md`/`ADAPTIVE_WORK_ORCHESTRATION.md` — starting with the simplest useful piece (a visible task/decision/handover view over what Steps 4–5 already produce), not the full capability-registry/task-graph engine at once.
- **Why here:** Orchestrating multiple agents over knowledge and memory only makes sense once there's real knowledge (Step 4) and real memory (Step 5) to orchestrate over.
- **Blocked by:** Steps 4 and 5.

### Steps 7+ — Deferred, unchanged from source phase gates

Founder UserAI as a separate identity (source Phase 4), Source Hub/Life Library expansion beyond Step 4's v1 (source Phase 5), LifeWeb/voice/multimodal (source Phase 6), future users and Life City (source Phase 7). See `SOURCE_PHASE_GATES.md` for their original exit criteria, which still apply unchanged — this staircase does not alter or accelerate them, it only makes the immediate next few steps concrete per the founder's specific request.

## Summary sequence

```
Step 0 (done, unmerged) → Step 1 (founder approval required)
                              │
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
          Step 2 (MFA)   Step 3 (owner_id)   [parallel-safe]
                              │
                              ▼
                     Step 4 (knowledge import)
                              │
                              ▼
                     Step 5 (project memory)
                              │
                              ▼
                Step 6 (orchestration foundation)
                              │
                              ▼
              Steps 7+ (UserAI, Life Library, LifeWeb,
                         future users / Life City — deferred)
```
