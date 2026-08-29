# MainAI V1 — Goal-to-Autonomy Completion Design

Traces the actual, current, production code path from founder intent to autonomous Worker
execution, and specifies the minimum safe contract for autonomous task decomposition. Grounded
in a direct read of the codebase (via a dedicated read-only investigation pass — every claim
below has a file:line citation), not aspiration. Companion to `docs/MAINAI_V1_READINESS.md`
(the full blocker matrix) and `docs/MAINAI_SELF_IMPROVEMENT_ACCEPTANCE.md` (the first real
self-improvement run's safety contract).

**Headline finding, corrects the initial framing this document was commissioned under**: the
founder-to-Worker chain is substantially more production-complete than "some of this is real,
much is still manual" suggested. A full, end-to-end founder-facing HTTP API already connects
document ingestion (or direct goal creation) all the way through AI-driven task decomposition,
execution-envelope authorization, task approval, provider-spend authorization, and autonomous
Worker pickup — with zero test-only construction in the *core* chain. The real gaps are
narrower and more specific than "goal intake is unwired" — see the classification below.

## Deliverable 1 — Goal intake gap analysis

### The real, current production chain

**Path A — system-derived origination** (a document/claim autonomously suggests work):
```
extract_claims_for_document()          app/rag/claims.py (automatic, on document ingest)
  → record_interpretation_proposal()   app/project_entities/service.py (staging, no authority yet)
  → promote_interpretation_proposal()  creates real ProjectEntity; requires explicit
                                        authority/basis -- never auto-inferred from AI confidence
  → _record_work_candidate_if_actionable()   project_entities/service.py:35-77, automatic
                                              side effect for entity_type in
                                              {"idea","decision","task_reference"}
  → record_work_candidate()            app/work_candidates/service.py:116-156
                                        creates WorkCandidate, status="unreviewed"

FOUNDER: POST /api/project-entities/work-candidates/{id}/authorize
         app/routers/project_entities.py:136
  → authorize_work_candidate()         work_candidates/service.py:181-222
                                        delegates to create_goal() (never duplicates goal-
                                        creation logic) AND calls
                                        _propose_execution_scope_if_actionable() as a side
                                        effect when the source entity supports it
```

**Path B — direct founder-typed goal:**
```
FOUNDER: POST /api/mainai/goals       app/routers/mainai_execution.py:89-107
  → planner.create_goal()             directly; original_instruction is founder-typed text
```

**Both paths converge on real, production AI-driven decomposition:**
```
FOUNDER: POST /api/mainai/goals/{goal_id}/plan    mainai_execution.py:127-140
  → propose_plan_via_ai()             app/mainai_execution/planner.py:332, a REAL provider call
  → create_plan()                     planner.py:169
                                       -- one goal genuinely becomes multiple MainAITask rows
                                       via a real HTTP route, not test-only construction.
```

**Then the founder-governed authority chain:**
```
FOUNDER: POST /api/execution-envelopes/proposals/{id}/authorize
         app/routers/execution_envelopes.py:62
  → authorize_execution_scope()
    (this router's own module docstring: "Everything upstream of this edge is already
    live/automatic ... This router closes the remaining founder-governed step.")

FOUNDER: POST /api/mainai/tasks/{task_id}/approve     mainai_execution.py:187-189
  → grant_task_approval()

FOUNDER: POST /api/provider-spend/authorize            app/routers/provider_spend.py:79
  → authorize_provider_spend()        (only needed if the task actually requires provider
                                       assistance -- a deterministic plan needs no spend grant)
```

**Autonomous pickup, no further human action required:**
```
Worker.run()'s real `while True` poll loop     app/worker.py:107
  → run_once()                        automatically, once a task is ready + approved +
                                       envelope-authorized -- no manual/HTTP trigger needed.
```

### Per-object classification

| Object | Status | Evidence |
|---|---|---|
| `MainAIGoal` | **1. Production-derived** | Two real routes (Path A via `WorkCandidate` authorization, Path B direct `POST /api/mainai/goals`), both converging on `create_goal()`. |
| `MainAITask` | **1. Production-derived** | `propose_plan_via_ai()` → `create_plan()`, a real HTTP-triggered AI decomposition path, not test-only. |
| Task dependencies | **1. Production-derived** | `_detect_cycle()` (`planner.py:134`) is real production cycle-detection wired directly into `create_plan()` — dependency graphs come from the AI's own proposed output, not hand-set only in tests. |
| `WorkBinding` | **1. Production-derived** | Constructed at `development_supervisor/production_entry.py:371`, `development_supervisor/service.py:545,1468`, **and** `app/autonomous_gap/service.py:863` (the gap/repair path also builds real bindings). |
| `SupervisorScope` | **1. Production-derived** | Single real construction site, `production_entry.py:212`. |
| Execution-authorization-envelope | **1. Production-derived** | Real founder-facing route, `POST /api/execution-envelopes/proposals/{id}/authorize`. |
| Task approval records | **1. Production-derived** | Real founder-facing route, `POST /api/mainai/tasks/{id}/approve`. |
| Provider planning request | **1. Production-derived** | Real chain from `Worker` → `Supervisor` → `_invoke_live_gap()` → `plan_with_provider()`, confirmed reachable via `Worker.run_once()`, not only via direct test calls (this session's #181/#196 reviews independently confirmed this exact call chain). |
| Provider-spend authorization | **1. Production-derived** | Real founder-facing route, `POST /api/provider-spend/authorize`. |
| Worktree binding | **1. Production-derived** | `goal_worktree_path()`/`ensure_goal_worktree_sync()` (`development_supervisor/production_worktree.py:65,76`) are real production functions. |
| Task readiness | **1. Production-derived** | `recompute_task_readiness()` has 6 real production callers across `execution_job.py`, `worker.py`, `plan_insertion.py`, `executor.py`, `planner.py`, `graph.py`, `replan.py`. |
| Final report linkage | **1. Production-derived** | `_finalize_task_outcome()` → `record_final_report()`, confirmed via this session's #168/#193 review — the canonical, not-bypassable completion gate. |

**No item in this list is classified "3. still manually translated" or "4. unsafe to
auto-derive because it is authority-bearing."** Every authority-bearing step (envelope,
approval, spend) already has a real founder-facing HTTP route requiring an explicit founder
action — none of them are currently AI-derivable, and none should become so; this is the
correct shape, not a gap.

### The actual remaining gaps (narrower than the original framing assumed)

1. **No founder-facing route to reject or narrow an AI-proposed plan's task list before
   creation.** `propose_and_create_plan()` commits the AI's full proposed task set
   immediately; the founder's only granular control point is per-task approve/reject
   afterward, not plan-level editing before task rows exist. Worth an explicit founder
   decision: is per-task approval-after-creation sufficient (current behavior), or does V1
   need a plan-level "review before commit" step? Recommend treating current behavior as
   sufficient for V1 — task-level approval is a real, working gate — and deferring
   plan-level review to a later milestone unless the founder specifically wants it sooner.
2. **No founder-facing route to create a `WorkCandidate` directly** — correct by design
   (`WorkCandidate` is meant to only ever originate from automatic entity-promotion, "never a
   claim of authorization" per its own architecture) — not a gap, noted here only so it isn't
   mistaken for one later.
3. **No founder-facing route to manually trigger a Supervisor tick** — correct by design
   (automatic-only via `Worker.run()`'s poll loop) — not a gap, same reasoning as above.

## Critical rule, restated precisely against real code

**LLM MAY DERIVE WORK. LLM MAY NOT DERIVE AUTHORITY.**

The exact boundary, grounded in what's actually enforced today:
- `propose_plan_via_ai()` may derive the *shape* of work (which tasks, in what order, with
  what dependencies) — real, already happens.
- It may NOT derive `authorized_paths`/`authorized_capabilities` (those come from
  `authorize_execution_scope()`, a founder action) — already correctly enforced; `#166`'s
  plan-derived-scope-narrowing can only ever *intersect* with the founder's envelope, never
  expand it (verified this session via `narrow_task_scope_from_accepted_development_plan`).
- It may NOT derive whether provider spend is authorized (`authorize_provider_spend()`, founder
  action) — already correctly enforced.
- It may NOT derive task approval (`grant_task_approval()`, founder action) — already correctly
  enforced.
- Free-text fields (interpretation, rationale, purpose) it generates carry zero authority
  regardless of content — empirically proven this session (#190's prompt-injection regression
  test).

**Conclusion: the boundary is already correctly drawn in production code.** V1's goal-intake
gap is not "AI derives too much" — it's the two narrow, deliberate design gaps named above,
neither of which is a security concern.

## Deliverable 2 — Autonomous task decomposition contract

Since `propose_plan_via_ai()` → `create_plan()` is already a real, production-wired mechanism
(not something V1 needs to newly build), this section specifies the contract that ALREADY
governs it today, made explicit, plus the specific bounds worth confirming/tightening for V1.

**What MainAI may propose** (already true, confirmed via `create_plan()`/`_detect_cycle()`):
task descriptions, task types, verification plans, step ordering, and dependency edges between
tasks within the same plan.

**What comes from founder authority ceiling** (already true, confirmed):
`authorized_paths`/`authorized_capabilities` (from the execution envelope, intersected not
replaced by any AI-proposed narrower scope), whether provider assistance is permitted at all,
risk-tier ceiling.

**Path/capability intersection semantics**: MAX-restrictive-wins — an AI-proposed plan can only
narrow the founder's envelope, never widen it. Already enforced (`narrow_task_scope_from_
accepted_development_plan`, verified this session).

**Maximum task count / dependency depth / recursion bounds**: `FounderPlanningRequest` already
has `max_plan_steps: int = 20`, `max_alternatives: int = 3`, `max_replans: int = 3` (per this
session's earlier reading of `app/safe_planner/service.py`). **Recommend for V1**: confirm
these same bounds apply to `propose_plan_via_ai()`'s own task-count output (not just Safe
Planner's step count within a single task) — if `create_plan()` has no independent cap on the
NUMBER of tasks a single AI decomposition can create, add one (a bounded default, e.g. 12,
matching the long-autonomy-experiment's own stated 8-12 task scale) before V1 ships, to
prevent a single decomposition call from creating an unbounded task graph.

**Provider involvement**: already gated correctly — decomposition itself may use a real
provider call (`propose_plan_via_ai`), but each individual task's OWN later execution still
goes through its own independent `plan_with_provider()`/spend-reservation fence; decomposition
authority does not pre-authorize execution spend.

**Deterministic fallback**: already exists — `plan_founder_request`'s
`DETERMINISTIC_PLAN_AVAILABLE` branch, and the gap/repair loop's `recipe_candidate` mechanism
(see `docs/MAINAI_V1_READINESS.md`'s gap/repair audit) both provide deterministic paths that
skip provider assistance when a known-safe recipe applies.

**Rejection behavior**: `assess_authority()`/`validate_candidate()` fail closed with
`CandidateValidationError`/`NEEDS_AUTHORIZATION`/`NEEDS_CLARIFICATION` — already real, already
tested extensively this session (#186, #190).

**Cancellation behavior**: mid-decomposition cancellation is not something this session traced
specifically — **flagged as an open question for `docs/MAINAI_V1_READINESS.md`'s blocker
matrix**: does a founder cancel landing WHILE `propose_plan_via_ai()`/`create_plan()` is
in-flight get honored correctly (no tasks created from a cancelled decomposition), or is there
a window here analogous to the Operator-effect-time races already found and partially closed
this session (#184/#185/#192/#193)? Recommend Cursor's correction-pass discipline
(classify the exact window, don't assume zero) be applied to this specific boundary too, once
Phases 1-5 are closed.

**Provenance**: `MainAITask.plan_id` links every task back to the `MainAIPlan` that created it;
`MainAIPlan` itself is linked to the goal and (implicitly, via the provider-planning
checkpoint chain) to whatever provider call produced it, if any. Sufficient for V1.

**Idempotency**: `create_plan()`'s real production behavior on this axis was not independently
re-verified in this pass — **flagged as a specific follow-up question**: does calling
`POST /api/mainai/goals/{id}/plan` twice for the same goal create duplicate task graphs, or is
there an idempotency guard? Worth a dedicated empirical check before V1, given how central
idempotency-key discipline has been to every other part of this system (spend reservation,
Operator writes).

**Recovery after crash midway through decomposition**: not independently verified this pass —
same recommendation as cancellation above, flagged for `docs/MAINAI_V1_READINESS.md`.

**Scope discipline**: per the founder's own instruction, this is NOT a generic agent framework
— it documents and lightly tightens the bounds of the mechanism MainAI V1 already has, not a
new design.
