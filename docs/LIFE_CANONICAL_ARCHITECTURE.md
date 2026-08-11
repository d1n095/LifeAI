# Life — Canonical Architecture

**Status:** Discovery/architecture pass, no code written. Produced per the founder's explicit
"Canonical Architecture Recovery & AI-Independent Core Audit" mandate (2026-08-11), run
immediately after MainAI V0.3 merged (mainline `27f0d1e550aa158cf11e44b1c69e8d074959df73`).
Feature work is frozen until this is reviewed and approved.

**What this document is:** the single map of what Life is, what already exists, what's
designed-but-not-built, and what's genuinely missing — assembled from (1) the actual repo/DB
schema as it exists today, (2) every architecture/plan document already in the repo, (3) the
project's own recorded decisions (`docs/BRANCH_REGISTRY.md`, `CLAUDE.md`), and (4) the
founder's explicit instructions in this mandate. It does not replace the existing deep-dive
documents it builds on — it is the index and the reconciliation layer over them, following the
founder's own instruction to extend better existing documents rather than duplicate them.

**Key finding before anything else:** this repo already contains an unusually mature target
architecture. `docs/MAINAI_ARCHITECTURE.md` §1 already states, nearly word-for-word, the exact
principle the founder's mandate opens with — "MainAI är systemets intelligens, inte en extern
tjänst" / System Core vs. MainAI vs. External models. `docs/MEMORY_ARCHITECTURE.md` already has
a mature two-axis (class × scope) memory model designed for millions of users. Neither of these
documents has been implemented much beyond its foundations, and — this is the important part —
**neither document has been updated since the MainAI V0.1–V0.3 execution engine (Goals, Plans,
Tasks, durable Jobs, waits, retries, auto-recovery, replanning, engineering lessons) was built.**
`MAINAI_ARCHITECTURE.md`'s own domain model (§4) does not mention `mainai_goals`/`mainai_plans`/
`mainai_tasks`/`mainai_jobs` at all. This is the single clearest piece of evidence for why the
founder called this pass: real capability was added faster than the canonical map was updated.
This document reconciles that.

---

## A. Executive summary

Life is a local-first, single-founder-owned platform ("LifeOS", repo name `LifeAI` for
historical reasons) consisting of a deterministic core (auth, storage, documents, RAG,
projects, jobs, memory substrate) and an intelligence layer (MainAI) that plans, orchestrates,
and executes work on top of that core, using external LLM providers as replaceable
tools/experts rather than as the system itself. Today's actual repository is furthest along in
three areas: (1) a hardened founder-only auth/RLS/audit foundation, (2) a Founder Knowledge
Studio / Life Library ingestion-and-retrieval pipeline with a real durable worker, and (3) a
three-version-deep MainAI execution engine (V0.1–V0.3, just merged) that gives MainAI durable,
resumable, cooperatively-cancellable, auto-recovering background work with a real GitHub
integration. It is weakest exactly where the founder's mandate points: there is no formal Life
Core / LifeAI boundary enforced anywhere (it exists only as prose in `MAINAI_ARCHITECTURE.md`
§1), no Source Vault invariant enforced at the DB-privilege level, no Memory Thread concept, no
Goal/Dream/Dependency graph, and no Founder HQ as a single control surface — Founder-facing UI
today is a handful of independent admin pages (`/admin/jobs`, `/admin/agents`,
`/admin/mainai-execution`, `/admin/memory`, `/library`, `/knowledge`), not one command center.

---

## B. Canonical Life system map

```
Layer 6 — Ecosystem / extensions           (not started: third-party capabilities, marketplace)
Layer 5 — Client / experience layer        Next.js web app today; Life App (mobile/desktop/voice) not started
Layer 4 — Domain modules / workspaces       Library/Knowledge (built), Projects (partial), Business/Commerce (not started)
Layer 3 — Intelligence / LifeAI control     MainAI execution engine (V0.1-V0.3, built), providers, Trust Engine
Layer 2 — Shared deterministic platform     RAG/retrieval, storage, jobs/worker, RLS, audit, memory substrate
Layer 1 — Life Kernel / Core                auth/identity, users, permissions, DB schema, event log
Layer 0 — Infrastructure / runtime          Postgres+pgvector, Redis, FastAPI, Next.js, Docker, Strato VPS / Render
```

This is a **refinement**, not a replacement, of the founder's own proposed Layer 0-6 sketch in
the mandate — verified against the code and kept because nothing in the actual repo contradicts
it. One correction from what's actually built: there is no separate "Layer 2 shared platform
services" vs. "Layer 1 kernel" split enforced anywhere today — auth, RLS, storage, and RAG all
live in the same `backend/app/` process and the same `mainai_app` database role. The layer
boundary is currently a **code-organization** boundary (separate modules, separate migrations,
separate test suites — see `docs/MAINAI_ARCHITECTURE.md` §2/§3), not a **deployment** or
**process** boundary. That's an intentional, already-documented choice ("modulär monolit
medvetet, inte mikrotjänster") and should stay that way until a real scaling driver forces the
split — the seams are already drawn (§2 below) for when that day comes.

### Product model — verified against the files

The founder's product model (Life / Life Core / LifeAI-MainAI / Life Orb / Founder HQ / Life
App / Business-Commerce Office / Domain Modules) does **not** appear anywhere in the existing
docs under these exact names. What the docs and code do establish, mapped onto the founder's
naming:

| Founder's term | What exists today under a different name | Status |
|---|---|---|
| **Life** | The whole repo/product, called "LifeOS"/"Life OS" in `docs/ARCHITECTURE.md`, `MAINAI_CONTEXT_BUNDLE.md` | Name exists, scope matches |
| **Life Core** | No single named component. Closest: `MAINAI_ARCHITECTURE.md` §1's "System Core" (deterministic, no-AI-required layer) | Concept exists in prose only, not a code/deployment boundary |
| **LifeAI / MainAI** | `backend/app/mainai_execution/`, `app/providers/`, `app/rag/trust.py` — MainAI is the actual, single, already-used product name throughout the codebase and commit history | Real, extensively built |
| **Life Orb** | Referenced once, historically, in `docs/MAINAI_0.1_PLAN.md` ("animerad AI-boll") as a **frontend chat UI element** — not a cross-surface presence concept | Name exists, scope is much narrower than the founder's framing |
| **Founder Headquarters** | Does not exist as a named surface. Closest: the four independent `/admin/*` pages plus `/library`, `/knowledge` | Concept does not exist; pages that would feed it do |
| **Life App / Life Clients** | Only a Next.js web app exists. No mobile/desktop/voice client | Not started |
| **Business / Commerce Office** | Does not exist anywhere in code or docs. `MAINAI_CONTEXT_BUNDLE.md` mentions "My Money Master" and "4thepeople AB" as separate products, not as a Life module | Not started; explicitly named in founder material as a future product, not yet related to Life Core |
| **Domain Modules** | `Project`/`Task` (finance/work/health/home/games not started); Library/Knowledge is itself the most-built "module" | Partially real (Projects), mostly not started |

**Recommendation:** adopt the founder's naming going forward (it's clearer and more complete
than what's in the docs today), but do so by **renaming/reframing existing real components**,
not by starting five new parallel systems. Concretely: "Life Core" = today's System Core +
Layer 1/2 above; "Founder HQ" = a new UI shell that surfaces the four existing `/admin/*` pages
plus Library/Knowledge/Projects under one roof, not four new dashboards; "Life Orb" = the
existing (unbuilt) animated chat presence, deliberately generalized to appear wherever MainAI is
present (chat, HQ, future clients) instead of being chat-page-only.

---

## C. Current repository reality matrix

Verified directly against `backend/app/models/*.py` (40 tables), `backend/alembic/versions/`
(36 migrations, head `0036`), `backend/app/routers/*.py` (15 routers), `backend/app/rag/*.py`,
`backend/app/mainai_execution/*.py` (18 modules), `frontend/app/(shell)/*` (10 route groups) —
not estimated, read directly this session.

| Domain | Tables / modules | Status |
|---|---|---|
| **Infrastructure** | Docker Compose, `Dockerfile.combined`, Strato VPS compose, Render blueprint | REUSE AS-IS — dual-deploy-target (Render legacy/disabled, Strato VPS active) already works, documented in `docs/VPS_ARCHITECTURE.md`/`docs/RENDER_DEPLOY.md` |
| **Auth/identity** | `users`, `refresh_tokens`, `revoked_access_tokens`, `email_verification_tokens`, `password_reset_tokens` | REUSE AS-IS — founder-only, cookie+CSRF, Argon2id, rotation+reuse-detection; hardened over many passes, see `docs/AUTH_THREAT_MODEL.md` |
| **Permissions/RLS** | `app/rls.py`, `mainai_app` non-superuser DB role, per-table `FORCE ROW LEVEL SECURITY` | REUSE AS-IS as a *mechanism*; EXTEND coverage — binary `admin`/`member` role only, no resource-level permissions yet (`MAINAI_ARCHITECTURE.md` §9 target) |
| **Audit** | `audit_log` (append-only) | REUSE AS-IS; not yet unified with MainAI's own `mainai_task_events`/`mainai_job_events` append-only logs (three parallel audit-shaped logs today, not one) |
| **Documents/storage** | `documents`, `document_chunks`, `LocalFilesystemStorage` (`app/storage/local_fs.py`), content-addressed, atomic write, referenced-based purge | REUSE AS-IS — this is the closest thing that exists today to the founder's "Source Vault" (§9 below), but it is **not yet a formally named, DB-privilege-enforced invariant** — see Constitution doc |
| **Knowledge/RAG** | `knowledge_versions`, `knowledge_claims`, `claim_relationships`, `source_relationships`, `document_chunks`+pgvector HNSW | REUSE AS-IS — Trust Engine (`app/rag/trust.py`), claim-level trust (STEG 10), hybrid search with local fallback |
| **Conversations/messages** | `conversations`, `messages` (+`sequence_number`, own RLS since migration 0031) | REUSE AS-IS; MISSING: not yet a first-class memory source (S1C `message_source_units` not built — see Requirement Traceability) |
| **Memory provenance** | `memory_source_units`, `document_source_units`, `memory_source_lifecycle_events`, `memory_source_backfill_runs/failures` | REUSE AS-IS — S1A is fully built (universal, owner-scoped, `SECURITY DEFINER`-gated provenance layer); this is real infrastructure the founder's Source Vault/Memory design should build on, not replace |
| **Jobs/queues/worker/scheduler** | `mainai_jobs`, `mainai_job_events`, `mainai_job_proposals`, `storage_deletion_tasks`, `app/worker.py`, `app/jobs/` (lease, heartbeat, retry) | REUSE AS-IS — real lease-fenced, heartbeated, retryable, crash-recoverable durable job runtime (`docs/MAINAI_JOB_RUNTIME.md`). This is the concrete substrate for the founder's "background ingestion must be resumable" requirement — it already exists |
| **Recovery** | `mainai_task_worktrees`, `mainai_recovery_records`, `mainai_recovery_events` | REUSE AS-IS — V0.2's dead-agent recovery classifier/salvage/takeover, now polled automatically by V0.3 |
| **Goals/plans/tasks/approvals/checkpoints/events** | `mainai_goals`, `mainai_plans`, `mainai_tasks`, `mainai_task_dependencies`, `mainai_task_events`, `mainai_checkpoints`, `mainai_task_waits` | REUSE AS-IS — this is MainAI's entire durable execution loop (V0.1-V0.3): planning, dependency-ordered dispatch, checkpoint/resume, cooperative cancellation, CI-wait, retry-with-backoff, replanning. **Not yet reflected in `MAINAI_ARCHITECTURE.md`'s domain model** — see Executive Summary |
| **Engineering lessons** | `engineering_lessons` | REUSE AS-IS — durable safety-memory with conflict detection (V0.3), the closest existing thing to the founder's "problem→solution→lesson" learning memory (§17 below) |
| **AI providers** | `app/providers/` — OpenAI, Anthropic, Gemini, DeepSeek, OpenRouter, Ollama, all behind `LLMProvider` | REUSE AS-IS for chat/embedding; EXTEND — `docs/AI_PROVIDER_ARCHITECTURE.md`'s capability-based-interface/manifest/conformance-suite target design is NOT built (today's interface is one `Protocol` with `chat`/`embed`, not per-capability) |
| **AI orchestration** | `chat_with_fallback()` (`app/providers/registry.py`) | EXTEND — single-provider-with-fallback only; `docs/AI_ORCHESTRATION_ENGINE.md`'s routing/consensus/multi-model design is architecture-only, not built |
| **APIs** | 15 routers, thin HTTP layer over `app/rag`/`app/mainai_execution`/`app/providers` | REUSE AS-IS pattern |
| **Frontend** | Next.js App Router, `(shell)` route group: chat, library, knowledge(legacy), documents(dead), projects, workbench, account, admin/{jobs,agents,mainai-execution,memory} | REUSE AS-IS individual pages; MISSING — no unified Founder HQ shell (§20 below); `documents` route confirmed dead (redirects to `/library`, PR C finding) |
| **Modules/capability registry** | Does not exist | MISSING — see Capability Architecture (§8) |

---

## D. Requirement traceability

Full record-by-record traceability lives in `docs/LIFE_REQUIREMENT_TRACEABILITY.md`. This
section gives the top-level status only.

---

## E. Source/authority model

See `docs/LIFE_REQUIREMENT_TRACEABILITY.md` §"Source authority" for the classification scheme
applied throughout. Short version: `MAINAI_CONTEXT_BUNDLE.md` and the founder's direct mandate
messages are **FOUNDER REQUIREMENT/DECISION** (highest authority); `MAINAI_ARCHITECTURE.md`,
`MEMORY_ARCHITECTURE.md`, `AI_ORCHESTRATION_ENGINE.md`, `AI_PROVIDER_ARCHITECTURE.md`,
`LIFE_LIBRARY_PLAN.md`, `MAINAI_PROJECT_UNDERSTANDING_PLAN.md` are **AI-authored architecture
proposals**, written by a prior Claude Code session, not founder-authored — they carry real
design weight (they're detailed, internally consistent, and already partially built) but are
not founder decisions unless the founder explicitly approved them (no evidence of a formal
approval step was found for most of them — they read as accepted-by-continuation, i.e. later
work built on them without the founder rejecting them, which is meaningfully weaker than an
explicit sign-off). Implemented code is **IMPLEMENTED REALITY** and wins over any document when
the two disagree — this document defers to code every time a conflict was found.

---

## F. Life Core vs LifeAI boundary

**This is the single most load-bearing section of this document**, because it's the literal
subject of the founder's §0 "grundlag."

**What already exists (prose only):** `MAINAI_ARCHITECTURE.md` §1's three-layer table:

| Layer | Responsibility (verbatim from the existing doc) |
|---|---|
| System Core (deterministic, never requires an AI model) | Files, database, projects, chat storage, users, settings, memory, rules, checkpoints, queues |
| MainAI (the system's own intelligence) | Plans, remembers, reasons, proposes, learns, orchestrates — uses local tools/knowledge FIRST |
| External models (replaceable, optional) | Used when local capacity isn't enough; verifies/supplements local reasoning; can be swapped or entirely absent without System Core or MainAI's core functions stopping |

That same document is explicit that **this is only partially true today**: `chat_with_fallback`
switches between external providers on failure, but there is no local reasoning/model fallback
if *all* external providers are unavailable. Retrieval (RAG search) is already fully local
(pgvector — no external call needed to search). Hybrid search already has a tested local
fallback path when the embedding provider is down (PR #18).

**What this means concretely, verified against the code, for the founder's "Windows can run
without Copilot" analogy:**

- **Already true today, verified:** if every external AI provider is unreachable, a founder can
  still log in, browse `/library`, browse `/projects`, read past conversations, search already-
  indexed documents (pgvector search does not call an LLM), and see MainAI's execution-engine
  state (`/admin/mainai-execution`, `/admin/jobs`). None of that requires AI.
  Storage/auth/RLS/audit/jobs/worker/scheduler/recovery genuinely do not call an LLM anywhere in
  their own code paths (verified by module inventory — `app/storage/`, `app/jobs/`,
  `app/mainai_execution/executor.py`'s dispatch/lease/checkpoint logic, `app/worker.py`'s tick
  functions contain no provider calls).
- **Not true today:** new chat messages, new document ingestion past raw storage (chunking
  works locally, but embedding requires a provider), MainAI's planner (`propose_plan_via_ai`),
  replanning, and lesson-conflict detection **do** require a reachable AI provider — there is no
  local model and no "queue it for later" behavior for these; they fail/block instead of
  degrading gracefully. This is the honest gap between the founder's principle and the current
  implementation.

**Formal Life Core / LifeAI boundary — recommended, not yet built:** today the boundary is
enforced nowhere except code organization (which functions happen not to call a provider). The
founder's mandate requires it be **structural**: a capability should be able to declare
`AI_REQUIRED_TODAY` vs `NO_AI_REQUIRED` (§G below) and Life Core's own health/status should
never depend on LifeAI being reachable. Concretely this means: the FastAPI app itself, the
database connection, the worker's poll loop, and the RLS/auth layer must never import or call
`app/providers/` at all (grep-verifiable invariant, could become a CI check the same way the
existing `AST allowlist` tests already check write-path discipline, see
`test_storage_local_fs.py`'s established pattern) — this is a **cheap, concrete, testable next
step**, not a redesign.

---

## G. AI-dependency matrix

Classified per the founder's five-way scheme, verified against actual module dependencies
(what calls `app/providers/*` directly or transitively):

| Subsystem | Classification | Evidence |
|---|---|---|
| Storage (`app/storage/`) | **NO_AI_REQUIRED** | Zero provider imports; content-addressed, hash-verified, no LLM anywhere in the write/read/purge path |
| Auth/permissions/audit (`app/deps.py`, `app/rls.py`, `app/security.py`) | **NO_AI_REQUIRED** | Zero provider imports |
| Jobs/queue/worker/scheduler/retry (`app/jobs/`, `app/worker.py`) | **NO_AI_REQUIRED** for the runtime itself | The job *runtime* (lease, heartbeat, retry, dispatch) has zero provider imports; individual job *handlers* vary — `message_sequence_backfill` is a good example of a genuinely AI-free job type (per its own doc: "the first with no AI at all") |
| Import/archive extraction/checksums/dedup (`app/rag/zip_import.py`, `app/storage/local_fs.py`) | **NO_AI_REQUIRED** | Verified — magic-byte checks, zip-bomb budget, dedup are all deterministic |
| File-type detection/parser dispatch (`app/rag/extract.py`) | **NO_AI_REQUIRED** for text extraction | PDF/DOCX/HTML/text extraction is deterministic library code, not an LLM call |
| Chunking (`app/rag/chunking.py`) | **NO_AI_REQUIRED** | Word-based sliding window, deterministic |
| Embedding (`app/rag/ingest.py`) | **AI_REQUIRED_TODAY** | Calls the configured embedding provider directly; `awaiting_provider`/`blocked_provider` states exist in design (`MAINAI_PROJECT_UNDERSTANDING_PLAN.md` §4.7, P1) precisely so a missing/invalid key degrades to "paused, resumable" rather than "failed" — **P1 IS built** (provider verification), the graceful *awaiting_provider* pause state for a plain missing-key case should be re-verified against current `IndexStatus` enum, flagged UNCLEAR |
| Search/retrieval (`app/rag/vector_store.py`) | **AI_OPTIONAL** | pgvector similarity search needs an embedding for the *query*, but has a tested local fallback (text search) when the embedding provider is down — this is the best existing example of the founder's target pattern |
| Chat (`app/routers/chat.py`) | **AI_REQUIRED_TODAY**, **SHOULD_BECOME_AI_OPTIONAL** | Core value proposition requires an LLM; no local-model fallback exists yet (`MAINAI_ARCHITECTURE.md` §1's own stated gap) |
| Claim extraction (`app/rag/claims.py`) | **AI_REQUIRED_TODAY** | Explicitly an LLM call per chunk |
| MainAI planner (`app/mainai_execution/planner.py`) | **AI_REQUIRED_TODAY** | `propose_plan_via_ai()` calls a provider directly; no deterministic fallback |
| MainAI executor/dispatch/checkpoint/recovery (`executor.py`, `checkpoint.py`, `recovery_*.py`) | **NO_AI_REQUIRED** | The orchestration *mechanics* (locking, leasing, checkpointing, dead-worker detection) are pure Postgres/Python — only the *content* of what a task does may itself call a provider |
| MainAI replanning/lesson-conflict detection | **AI_REQUIRED_TODAY** | Both explicitly call a provider (`replan.py`, `lesson_conflicts.py`'s "a real AI judgment for borderline cases") |
| Capability registry / module registry | **SHOULD_NEVER_DEPEND_ON_AI** | Does not exist yet — see §H, must be built AI-free from the start per this classification |
| Event system (`mainai_task_events`, `mainai_job_events`, `audit_log`) | **NO_AI_REQUIRED** | Pure DB writes, verified append-only via DB triggers |
| Deterministic verification (`app/mainai_execution/verify.py`) | **NO_AI_REQUIRED** for structural checks (tests/migrations/lint pass/fail) | The *decision to trust* a verification result is deterministic; an AI-assisted verification signal, if ever added, would need to be a separate, clearly-labeled input, not blended in |

**Bottom line:** the deterministic core the founder wants ("Life Core") is **already
substantially AI-free in practice** — it just isn't *declared* or *enforced* as such anywhere.
The actual gap is narrower than it might appear: it's chat, embedding, claim extraction,
planning, and replanning that need AI today, and of those, embedding already has a graceful-
degradation precedent (hybrid search fallback) worth generalizing to the others.

---

## H. Capability architecture

**Status: MISSING entirely.** No `capabilities` table, no capability registry, no "can Life do
X" query surface exists anywhere in the schema or code today. This is a real, clean gap — not a
renaming of something that already exists.

**Recommended minimal shape** (design only, not built), directly satisfying the founder's
required field list and grounded in patterns already proven elsewhere in this codebase (the
`ProviderConfig`/`ProviderVerificationCheck` pattern for "is this actually usable, not just
configured" is the closest existing precedent and should be reused, not reinvented):

```
capabilities
  id, name, version
  installed (bool), enabled (bool)
  provider              -- 'local' | provider_name | null
  permissions            -- what this capability may read/write, in RLS/role terms
  inputs, outputs         -- JSON schema references
  dependencies            -- other capability ids
  health_status            -- mirrors provider_verification_checks' ok/invalid/unreachable pattern
  local_available (bool)    -- can run with zero external calls (feeds the AI-dependency matrix, §G)
  security_classification
  installation_source
  update_policy, uninstall_behavior
```

`LifeAI ska kunna fråga "Kan Life göra X?"` maps directly onto a deterministic
`SELECT ... FROM capabilities WHERE name = ? AND enabled` — genuinely no AI required to answer
that question, which is itself an instance of the AI-independence principle applied to MainAI's
own self-knowledge. This is new work, not an extension of an existing table — flagged MISSING,
not UNCLEAR.

---

## Known gaps in this document (honest, not hidden)

- This pass read every top-level architecture/plan document in `docs/` and `backend/docs/` and
  did a real, code-grounded inventory of every DB table, backend module, and frontend route —
  but it did **not** do a line-by-line formal `requirement_id` extraction of every sentence in
  every source document (that would be several hundred atomic records across ~10,000 lines of
  existing docs). `docs/LIFE_REQUIREMENT_TRACEABILITY.md` covers every *major* requirement
  theme with real status, sourced and dated, but is a first canonical pass, not an exhaustive
  mechanical extraction — expanding it further is cheap, additive follow-up work, not a
  correction of anything wrong here.
- Founder-authored *external* material referenced in `MAINAI_CONTEXT_BUNDLE.md`
  (`~/Documents/mainai_intake/` — FKP_curated, Life_OS_Claude_Handoff, chatgpt_export,
  savings-story-scanner-main) is **not accessible from this repo/session** — it lives on the
  founder's Mac, outside this environment's reach. Everything in this document is grounded in
  what's actually in the `d1n095/LifeAI` repository and its GitHub history. This is flagged
  explicitly rather than silently assumed absent of content — see §25's stop-condition list.
