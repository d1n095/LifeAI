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
document ingestion (Path A) all the way through AI-driven task decomposition, execution-
envelope authorization, task approval, provider-spend authorization, and autonomous Worker
pickup — with zero test-only construction in the *core* chain. **Update, found on a dedicated
adversarial re-verification pass, then closed the same session**: this full chain was proven
complete for Path A goals from the start; Path B (direct founder-typed
`POST /api/mainai/execution/goals`) had one real gap — no route existed to create an
execution-scope proposal for a directly-created goal — **now closed** via a new founder-invoked
bridge route, see gap #0 below for the full fix and its production-shaped E2E proof. Also
resolved this session: a real concurrent-decomposition bug (unhandled `IntegrityError` on a
genuine race, now fixed with a regression test) and full confirmation that decomposition
crash-recovery is completely safe (single-transaction atomicity — see Deliverable 2). **Both
founder goal-entry paths now converge on the same canonical FOUNDER INTENT → AUTHORITY
PROPOSAL → FOUNDER APPROVAL → DURABLE ENVELOPE → WORKER → SAFE AUTONOMOUS EXECUTION chain.**

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
| Execution-authorization-envelope | **1. Production-derived (both paths)** | Real founder-facing route, `POST /api/execution-envelopes/proposals/{id}/authorize`. `propose_execution_scope()` now has TWO production callers: `work_candidates/service.py:95` (Path A, automatic) and the new `POST /api/execution-envelopes/goals/{goal_id}/propose` (Path B, founder-invoked, added this session). Both converge on the same `authorize_execution_scope()` — proven via a real production-shaped E2E test, `backend/tests/backend/test_path_b_execution_scope_bridge.py`. |
| Task approval records | **1. Production-derived** | Real founder-facing route, `POST /api/mainai/tasks/{id}/approve`. |
| Provider planning request | **1. Production-derived** | Real chain from `Worker` → `Supervisor` → `_invoke_live_gap()` → `plan_with_provider()`, confirmed reachable via `Worker.run_once()`, not only via direct test calls (this session's #181/#196 reviews independently confirmed this exact call chain). |
| Provider-spend authorization | **1. Production-derived (both paths)** | Real founder-facing route, `POST /api/provider-spend/authorize`, chains from the envelope-authorization response's `id` — reachable for both Path A and Path B goals now that the envelope gap above is closed. |
| Worktree binding | **1. Production-derived** | `goal_worktree_path()`/`ensure_goal_worktree_sync()` (`development_supervisor/production_worktree.py:65,76`) are real production functions. |
| Task readiness | **1. Production-derived** | `recompute_task_readiness()` has 6 real production callers across `execution_job.py`, `worker.py`, `plan_insertion.py`, `executor.py`, `planner.py`, `graph.py`, `replan.py`. |
| Final report linkage | **1. Production-derived** | `_finalize_task_outcome()` → `record_final_report()`, confirmed via this session's #168/#193 review — the canonical, not-bypassable completion gate. |

**No item in this list is classified "3. still manually translated" or "4. unsafe to
auto-derive because it is authority-bearing."** Every authority-bearing step (envelope,
approval, spend) already has a real founder-facing HTTP route requiring an explicit founder
action — none of them are currently AI-derivable, and none should become so; this is the
correct shape, not a gap.

### The actual remaining gaps (narrower than the original framing assumed)

0. **CLOSED this session.** Path B goals (direct `POST /api/mainai/execution/goals` — the
   correct real prefix, `/api/mainai/goals` in earlier drafts of this document was imprecise)
   previously had no route to ever get an execution-scope proposal created, so they could
   plan/decompose but never reach execution authority through any real API interaction.
   **Fixed via a new founder-invoked route**, `POST /api/execution-envelopes/goals/{goal_id}/
   propose` (`app/routers/execution_envelopes.py`) — Option (a) from the original framing
   below, kept for the historical record. Calls the same `propose_execution_scope()` Path A's
   automatic trigger uses; the proposal carries zero authority regardless of content
   (`authorize_execution_scope()` never copies from it — proven structurally, not just
   assumed). Idempotency key is a content-hash of `(goal_id, proposed_paths, proposed_
   capabilities, proposed_risk, repository_identity)` — an accidental retry with identical
   content is a true no-op; a deliberate new proposal with different content (e.g. after a
   rejection) is never blocked by history. A second, related bug was found and fixed in the
   same underlying function: `propose_execution_scope()`'s check-then-insert had no protection
   against two genuinely concurrent callers sharing an idempotency_key (Path A's automatic
   trigger racing a Path B founder call, or a client retry) — the loser used to raise an
   unhandled `IntegrityError` instead of gracefully converging on the winner's row. Fixed with
   `INSERT ... ON CONFLICT DO NOTHING` (not a `SAVEPOINT`+catch, which was tried first and
   reverted after it broke RLS test isolation when run alongside other execution-envelope
   test modules — a real regression, reproduced and root-caused before switching approach).
   Full production-shaped E2E proof (goal → decomposition → proposal → founder authorization →
   task approval → `eligible_authorized_goals()` genuinely includes the goal) plus 4 negative
   tests (no-approval, broader-proposal-never-widens, cancel-before-approval,
   retry-never-duplicates) in `backend/tests/backend/test_path_b_execution_scope_bridge.py`;
   the concurrent-proposal race has its own dedicated two-thread regression in
   `backend/tests/backend/mainai/test_execution_envelopes.py`. No test-side manual
   `SupervisorScope`/`WorkBinding`/envelope/task-status construction anywhere in the new
   suite — every transition is a real HTTP call or the same service function its own router
   calls.

   Original framing, kept for context: two options were considered — (a) add a
   `propose_execution_scope()`-creating route reachable for a directly-created goal (the one
   implemented), or (b) explicitly document Path B as execution-incomplete for V1 and require
   founders to use Path A instead. (a) was chosen as the smaller, more consistent fix.
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

**Cancellation behavior — traced and resolved.** No cancellation check exists inside
`create_plan()`/`propose_and_create_plan()` — `MainAIGoal` has no `cancel_requested` field at
all (that field lives only on `MainAIJob`, a deliberately separate task/job-level concept per
`app/models/mainai_execution.py:116-118`'s own comment). A founder cancel landing mid-request
would not be observed by decomposition. **This is a real, minor gap, not a blocker**: the
entire decomposition (provider call + all DB writes) is a single synchronous HTTP request —
typically seconds — a categorically narrower exposure window than the Driver's multi-tick
per-step loop #185/#192/#193 already cover. Recommend as a IMPORTANT-POST-V1 follow-up, not a
V1 blocker.

**Provenance**: `MainAITask.plan_id` links every task back to the `MainAIPlan` that created it;
`MainAIPlan` itself is linked to the goal and (implicitly, via the provider-planning
checkpoint chain) to whatever provider call produced it, if any. Sufficient for V1.

**Idempotency / duplication risk — traced and resolved; one real bug found and fixed this
session.** No idempotency-key mechanism exists for decomposition — but `MainAIPlan`'s own
`uq_mainai_plans_goal_version` unique constraint (migration 0032) provides real structural
protection: a sequential re-call correctly supersedes the previous plan (proven by the
existing `test_create_plan_called_again_supersedes_previous_plan_and_cancels_its_unstarted_
tasks` test), and no scenario produces duplicate/coexisting task graphs. **The real bug**: two
genuinely CONCURRENT decomposition calls for the same goal had no lock, so both could compute
the same `next_version` and race to insert conflicting `MainAIPlan` rows — the unique
constraint correctly prevented the duplicate row, but the loser's `db.flush()` raised an
**unhandled `IntegrityError`** (confirmed empirically via a real two-thread/two-connection
test) instead of gracefully superseding the winner's plan. **Fixed this session**:
`create_plan()` now locks the goal row (`with_for_update()`, `populate_existing=True` —
required because this project's `expire_on_commit=False` session config means a plain locked
SELECT does not by itself refresh an already-identity-mapped `goal` object's attributes)
before reading `previous_active`/`next_version`, serializing concurrent attempts into the
same well-defined sequential-supersede behavior. Regression test:
`test_two_genuinely_concurrent_create_plan_calls_for_the_same_goal_never_raise_unhandled_
integrity_error` (`tests/backend/test_mainai_execution_planner.py`, section J).

**Recovery after crash midway through decomposition — traced and resolved, fully safe.**
`create_plan()` uses only `db.flush()` internally, never `db.commit()` — the single commit
point is the router (`propose_and_create_plan()`, `mainai_execution.py:138`), *after* both the
provider call and all of `create_plan()`'s DB writes (plan row, task rows, dependency edges,
events, goal-status update) complete. **The entire decomposition is one atomic transaction.**
A crash anywhere before the commit leaves zero durable rows (Postgres rolls back the
abandoned, uncommitted transaction); a crash after the commit means decomposition fully
succeeded. There is no reachable intermediate/partial state — confirmed by direct trace, not
assumed. This also resolves the dependency-edge-consistency concern: since task rows and
their dependency edges are both created pre-commit in the same transaction (`planner.py:263-
297`), a task can never durably exist with a missing/inconsistent dependency edge — and
`recompute_task_readiness()`'s "no dependencies → immediately ready"
rule (`graph.py:52-56`) is therefore safe, since the only way a task can have zero recorded
dependencies is if it genuinely has none by design, never as a crash artifact. A crash
*before* `create_plan()` even starts (i.e. after the provider call returns but before
`create_plan()` is invoked) wastes the provider call on retry (no `request_hash`/checkpoint-
replay mechanism exists here, unlike `plan_with_provider()`'s own) but creates zero risk of
duplicate/inconsistent tasks, since nothing was ever inserted from the lost attempt.

**Scope discipline**: per the founder's own instruction, this is NOT a generic agent framework
— it documents and lightly tightens the bounds of the mechanism MainAI V1 already has, not a
new design.
