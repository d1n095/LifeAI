# Life Founder Alpha — Single Operational Priority & Capability Audit (2026-11-09)

**Status: documentation only. No code, migration, RLS policy, `render.yaml`, deploy, merge or
activation is changed by this document. `first_cycle_go=false`.**

Base of record: default branch `claude/det-kommer-mer-879lcm` @ `64c07d5b`. Every module path
and migration number below was verified against that tree; every PR status was verified against
the GitHub API on 2026-11-09 (see `LIFE_FIRST_START_READINESS_MATRIX_2026-11-09.md` for the
run-level evidence). This document **audits existing modules first** and **proposes reuse, not
new subsystems** — per `CLAUDE.md` ("En funktion = ett tydligt syfte"; "föreslå återanvändning
innan ny kod skrivs") and the Founder directive ("Reuse existing memory, executive planning,
agent coordination, GitHub integration and Blueprint primitives. Do not build duplicate
subsystems.").

---

## 0. The single operational priority (the answer, first)

> **Life Founder Alpha's single operational priority is to become *operational as a
> coordinator* by activating the foundations that already exist — not by building new ones.**

The audit below shows that **all ten** required first-capabilities already have a real
data+service foundation committed to the codebase. What is missing is almost never a missing
subsystem; it is that these foundations are, by their own design, **"data + service layer only"**
(no autonomous wiring, provider activation deliberately disabled), and the PRs that *wire and
activate* them are **unmerged drafts stacked on the default branch** (nothing has merged —
default is still `64c07d5b`).

Therefore the operational priority decomposes into exactly three ordered, Founder-gated steps,
none of which invents product architecture:

1. **Merge the already-built, already-reviewed foundations** in a safe order (security P1s first:
   PR #371, #372; then the capability/coordination/Blueprint wiring PRs), behind independent
   review. This is the long pole — see the readiness matrix.
2. **Wire the foundations into the one composed loop that already exists for this purpose**
   (`app.mainai_executive`, whose own `__init__` says: *"This package does NOT replace existing
   subsystems. It wires them."*) and flip it from "provider activation disabled" to a
   Founder-authorized, egress-gated, spend-bounded first cycle.
3. **Prove the operational chain end-to-end once** (Founder instruction → … → Blueprint
   proposal; Mission 4's chain) with real execution evidence and independent review, then record
   the result in durable memory.

The Founder remains the final authority for every sensitive/consequential action at every step
(this is already structural: see §2 capabilities 8 and 10, and the egress/spend/approval gates).

---

## 1. Method & honest-status vocabulary

Each capability is classified with the project's own capability-reality vocabulary
(`docs/LIFE_CAPABILITY_REALITY.md`, migration 0048) rather than a vague "done/not done":

| Class | Meaning here |
|---|---|
| **IMPLEMENTED (data+service)** | Durable schema + service functions exist and are committed to default; typically "no UI, not autonomously wired" by the module's own design. |
| **TESTED** | Has committed backend tests exercising the service path (unit/integration), but not necessarily a real end-to-end production run. |
| **OPERATIONAL** | Reachable and exercised through a real running path on behalf of the Founder. **Today: nothing is OPERATIONAL in production — default is unactivated, `first_cycle_go=false`.** |
| **BLOCKED** | Built but gated behind an unmet dependency (a merge, an authorization, a provider activation). |
| **FUTURE** | Deliberately deferred; a named limitation, not a prohibition (per each module's "Explicitly deferred layers"). |

"IMPLEMENTED" never implies "OPERATIONAL" — that distinction is the whole point of capability #4,
and the system has a module (`app.capability_reality`) whose only job is to keep callers honest
about it.

---

## 2. Capability audit — the ten required first-capabilities → existing modules

### Capability 1 — Read & correctly interpret authorized files and documents
- **Reuse:** `app.rag` (ingestion/extraction/claims pipeline), DOCX completeness work
  (PR #327, `claude/docx-extraction-completeness`), shared-original retrieval (PR #329/#330),
  `app.storage`. Egress-safe claim extraction now gated (PR #372, this session).
- **Status:** IMPLEMENTED (data+service) + TESTED. Interpretation beyond extraction (claims,
  concept reconciliation `app.concept_reconciliation`) exists as service layers.
- **Gap:** the ChatGPT-export importer is specced but unbuilt — PR #369 /
  `docs/CHATGPT_EXPORT_IMPORTER_SPEC_2026-11-08.md` (named top ingestion gap in PR #368's
  `FOUNDER_KNOWLEDGE_INGESTION_READINESS_2026-11-08.md`).

### Capability 2 — Build persistent, source-grounded project understanding
- **Reuse:** `app.inspectable_memory` (*"projection + truth-claim receipts … does NOT create a
  second canonical memory store"*), `app.founder_memory` (migration 0049), `app.memory_threads`
  (links facts without collapsing them), `app.capability_reality` (migration 0048),
  `app.project_entities`, `app.active_context`.
- **Status:** IMPLEMENTED (data+service) + TESTED. These are the P4/P6 memory layers from
  `docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md`.
- **Gap:** no founder-facing UI (deferred by design); "understanding" is queryable but not yet
  assembled into a single session-start context bundle that is enforced (the Context Bundle
  contract in `docs/FOUNDER_KNOWLEDGE_BOOTSTRAP.md` §3 remains a design, `scripts/
  build_context_bundle.py` unwritten).

### Capability 3 — Know what the whole LifeAI project is intended to become
- **Reuse:** `docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md` (1783 lines; architecture, data model,
  §8 build order P1–P7), `docs/MAINAI_ARCHITECTURE.md`, `app.project_entities`, `app.life_intents`
  (goals/dreams with blockers+dependencies, migration 0041).
- **Status:** IMPLEMENTED as durable docs + entity/intent service layers; TESTED at the service
  level.
- **Gap:** the vision is authoritative in prose; a machine-readable "intended end-state" that
  Life can reason over is partial (entities + intents exist; a single linked project-vision graph
  does not). The UserAI/MainAI hierarchy correction (`FOUNDER_KNOWLEDGE_BOOTSTRAP.md` §0/§7)
  remains an open architecture item.

### Capability 4 — Distinguish implemented / tested / operational / blocked / future
- **Reuse:** `app.capability_reality` (migration 0048) — **this capability is the module's exact
  purpose.** `capability_records` (live status) + `capability_observation_events` (append-only,
  DB-trigger-enforced history), closed status vocabulary, `authority` reuse from migration 0042,
  `record_capability_gap()` can only ever produce `status="planned"`.
- **Status:** IMPLEMENTED (data+service) + TESTED. Strongest-grounded capability in the set.
- **Gap:** automatic capability discovery/scoring is deferred by design; today every record is an
  explicit caller assertion. To make capability #4 *operational for the whole project*, the
  readiness classifications in this package should be recorded as capability records once the
  Founder authorizes a first run.

### Capability 5 — Understand dependencies & decide which steps are feasible
- **Reuse:** `app.life_intents` (dependencies/blockers), `app.safe_planner`, `app.work_candidates`,
  `app.autonomous_gap`, and `docs/BRANCH_REGISTRY.md` as the manual dependency map today.
- **Status:** IMPLEMENTED (data+service) + TESTED for the planning/candidate layers.
- **Gap:** cross-branch/PR dependency reasoning is still **manual** (`CLAUDE.md` says so
  explicitly: MainAI doing this continuously is the goal, "**Detta är INTE något som byggs nu**").
  `app.mainai_executive.priority` / `missing_piece` exist as the wiring seam.

### Capability 6 — Track current GitHub branches, PRs, reviews, CI, agent activity
- **Reuse:** `app.integrations.github_client` (real client: `get_ref`, `get_pull_request`,
  `list_pull_requests_for_head`, `list_check_runs`, branch/blob/tree/commit/PR create+update,
  `commit_multiple_files`), the GitHub-fed operating cycle (PR #338,
  `claude/coordination-operating-cycle-github-fed`), `app.agent_coordination.runtime_view`.
- **Status:** IMPLEMENTED (data+service) + TESTED (client + coordination). **BLOCKED** at the
  operational layer: the read-only-GitHub-intake operating cycle is PR #338 (open, stacked on
  #314) and the authorized-dispatch step is PR #339 — neither merged.
- **Gap:** activation only; the client and cycle exist. (`github_client.is_configured()` means a
  token is a deploy-time config step, not a code gap.)

### Capability 7 — Communicate with authorized dev agents via verified APIs/CLI
- **Reuse:** `app.agent_coordination` (adapters for Claude Code / Cursor Agent / Codex;
  `adapter_availability()` real reachability; `dispatch.py`, `execution_control.py`,
  `routing.py`), `app.workforce` (Stage-T contracts over coordination + spend + egress).
- **Status:** IMPLEMENTED (data+service) + TESTED (adapter availability, routing, dispatch
  bookkeeping). **BLOCKED** at operational dispatch: real dispatch is PR #339 (durable
  `WAITING_FOR_PROVIDER` fallback) and workforce activation gates (`app.workforce.activation_gates`)
  remain closed.
- **Gap:** activation + a configured provider; the integration surface exists and never mints a
  second queue (`agent_coordination.service` docstring).

### Capability 8 — Assign work, receive results, coordinate independent verification
- **Reuse:** `app.mainai_execution` (GOAL→PLAN→DURABLE TASKS→EXECUTOR→VERIFY→**APPROVAL GATE**→
  FINAL REPORT; `approval.py`, `verify.py`, `recovery_*`), `app.workforce.verification` +
  `audit_receipt`, `app.mainai_executive.safe_composed_run`, the executive business workflow
  (PR #335) and model-backed workers (PR #336).
- **Status:** IMPLEMENTED (data+service) + TESTED. Independent verification and a Founder-gated
  approval step are **already first-class** in the schema, not an afterthought.
- **Gap:** real model-backed execution (PR #336, depends on #335) is unmerged; provider activation
  disabled. Independent *review* of results is today performed by separate agents/humans (Copilot/
  Codex) — the directive's "independent review" requirement is met process-wise, not yet
  automated.

### Capability 9 — Maintain continuity across sessions without Founder micromanagement
- **Reuse:** `app.mainai_executive.continuity` + `multi_session`, `app.founder_memory`,
  `app.memory_threads`, the handover pattern (`docs/FOUNDER_KNOWLEDGE_BOOTSTRAP.md` §4.3;
  `docs/HANDOFF_*`), `app.mainai_startup_readiness` (machine-checkable readiness *levels*, not one
  boolean).
- **Status:** IMPLEMENTED (data+service) + TESTED. The handover/continuity substrate exists.
- **Gap:** continuity is **document-driven and manual today** (this very package is an instance of
  it). The enforced session-start Context Bundle (capability #2's gap) is the missing automation.

### Capability 10 — Create future source-grounded Blueprints for Founder approval
- **Reuse:** the Blueprint draft machinery in **PR #340** (`claude/founder-alpha-blueprint-draft`,
  item 8/9: versioned, source-grounded Blueprint + migration 0070, RLS, draft-pending-approval),
  its egress hardening (PR #360/#361), and `app.provider_planning` (bounded provider assistance
  *outside* execution authority) for synthesis.
- **Status:** **BLOCKED / IN-FLIGHT.** Verified: there is **no `blueprint` module or model on the
  default branch `64c07d5b`** — the primitives live entirely in the unmerged draft PR #340 (and
  migration 0070 ships there). So Blueprint capability is real but not yet on default.
- **Gap:** merge PR #340 (and its egress remediations #360/#361) behind review; the Founder
  approval gate is already part of the design ("draft-pending-approval").

---

## 3. Reuse map (one line each) — what NOT to rebuild

| Capability | Canonical existing owner (reuse, do not duplicate) |
|---|---|
| 1 Read/interpret files | `app.rag`, `app.storage`, `app.concept_reconciliation` (+ PR #327/#329/#369) |
| 2 Project understanding | `app.inspectable_memory`, `app.founder_memory` (0049), `app.memory_threads`, `app.project_entities` |
| 3 Whole-project intent | `docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md`, `app.life_intents` (0041), `app.project_entities` |
| 4 Status distinctions | `app.capability_reality` (0048) |
| 5 Dependencies/feasibility | `app.life_intents`, `app.safe_planner`, `app.work_candidates`, `docs/BRANCH_REGISTRY.md` |
| 6 GitHub tracking | `app.integrations.github_client`, PR #338 cycle, `app.agent_coordination.runtime_view` |
| 7 Agent communication | `app.agent_coordination` (adapters/dispatch/routing), `app.workforce` |
| 8 Assign/verify/approve | `app.mainai_execution` (approval+verify), `app.workforce.verification`, PR #335/#336 |
| 9 Cross-session continuity | `app.mainai_executive.continuity`/`multi_session`, `app.mainai_startup_readiness` |
| 10 Blueprint proposals | PR #340 Blueprint draft (0070) + `app.provider_planning` (+ #360/#361) |

**The composed loop that already exists to wire these:** `app.mainai_executive`
(`attention`, `priority`, `missing_piece`, `continuity`, `safe_composed_run`, `loop`) — its own
docstring states it wires, never replaces, and keeps provider activation disabled and
Claude-owned gates never self-verified.

---

## 4. What is genuinely missing (integration, not architecture)

Consistent with Mission 4 ("Identify missing integration without inventing new product
architecture"), the real gaps are:

1. **Nothing is merged.** Default `64c07d5b` carries the foundations but none of the activation/
   wiring PRs. This is the dominant blocker.
2. **Enforced session-start Context Bundle** (capabilities #2/#9): the design exists
   (`FOUNDER_KNOWLEDGE_BOOTSTRAP.md` §3) but `scripts/build_context_bundle.py` is unwritten. This
   is the one small *new* artifact worth building — and it is glue, not a subsystem.
3. **Provider activation + spend grant** remain deliberately off (`first_cycle_go=false`); every
   execution/dispatch capability is BLOCKED on a Founder-authorized, egress-gated, spend-bounded
   activation, which is a decision, not code.
4. **Machine-readable project-intent graph** (capability #3) is partial — entities/intents exist,
   a single linked vision graph does not. Deferrable; not a first-start blocker.

None of these requires new product architecture. Items 1 and 3 are Founder/sequencing decisions;
item 2 is a small, source-grounded script; item 4 is future.

---

## 5. Relationship to existing status docs (no duplication)

This audit intentionally does not restate, and does not supersede:
- `docs/LIFE_CAPABILITY_REALITY.md` (the capability-reality *mechanism*);
- `docs/FOUNDER_KNOWLEDGE_INGESTION_READINESS_2026-11-08.md` (PR #368, ingestion matrix — 78
  passing ingestion tests; ChatGPT importer top gap);
- `docs/FOUNDER_REQUIREMENTS_DELTA_2026-11-08.md` (PR #368, requirements A–F);
- `LIFEAI_STATUS.md` (PR #331, living status);
- `docs/LIFE_FIRST_START_READINESS_MATRIX_2026-11-09.md` (this package, Mission 4) — the
  per-capability readiness matrix and first-start blockers.

It adds exactly one new thing: the explicit **single operational priority** (§0) and the
capability→module reuse map (§3) framed for *activation sequencing*.

---

*Companion documents in this package:*
*`FOUNDER_NOTES_STATUS_OVERLAY_2026-11-09.md` (Mission 1),*
*`LIFE_FIRST_START_READINESS_MATRIX_2026-11-09.md` (Mission 4),*
*`FUTURE_BLUEPRINT_INPUT_GEO_RETAILER_INTELLIGENCE.md` (Mission 3).*
