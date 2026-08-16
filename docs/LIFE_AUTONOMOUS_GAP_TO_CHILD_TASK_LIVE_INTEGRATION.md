# Autonomous Gap → Child-Task Generation: Live Integration

Original integration: PR #79. Current hardening work is on
`cursor/pr79-live-loop-hardening`.

Wiring: `app/development_supervisor/service.py`'s `run_supervisor()` driver/planner-result handling
calls `app.autonomous_gap.service.handle_live_gap_signal()`. The PR #79 hardening also changes
attempt-event persistence, lease fencing, fail-closed lineage, live bounds, auto-binding, and
repair/resume behavior described below.
Tests: `backend/tests/backend/mainai/test_autonomous_gap_live_integration.py`.

## PR #79 live-loop hardening

The hardened live path keeps semantic gap identity separate from execution attempts:
`requested_by` is no longer stored in `LifeProblem.provenance`. Each attempt instead appends a
`LifeProblemEvent(event_type="outcome_recorded", detail={"gap_attempt": true, "requested_by": ...})`
carrying the worker identity (reusing an existing allowed event_type from migration 0042 — no new
migration). A takeover by a differently named worker therefore converges on the same `LifeProblem`
and child rather than raising a provenance conflict.

Before either durable gap recording or child insertion, `require_live_gap_lease()` revalidates the
operator context against the owner-scoped job, lock owner, running state, and lease generation.
`GapLeaseLostError` is isolated by the Supervisor, so stale work inserts nothing while unrelated
work remains eligible to continue.

Lineage depth is owner-scoped and fail-closed. `_live_generation_depth(db, owner_id=..., task=...)`
must resolve every `autonomous_gap_child:` event to exactly one parent `LifeProblem`; malformed,
missing, or ambiguous lineage becomes `DEPTH_BOUND_REACHED`, never an implicit depth of zero.
Live breadth counters also enforce `max_gaps_per_run`, `max_children_per_run`, and
`max_unresolved_gaps` across Supervisor checkpoints.

The Supervisor now derives a child `WorkBinding` from the gap's structured execution envelope.
The derived binding deliberately has `candidate=None`: Safe Planner first tries its deterministic
recipe registry with the live operator context, and only then reaches the separately authorized
provider path. No caller or test has to hand-author a child `PlanCandidate` or `WorkBinding`, and
this does not auto-approve the child.

The deterministic calculator recipe recognizes the foundation instruction ("calculator",
"verified test(s)", and "multiply"/"multiplication"), repairs or adds `multiply`, writes the
focused test, verifies, stages, and commits within the path envelope. A completed repair child
resumes its parked source task with a reverify-only candidate, so the original defective plan is
not replayed.

Structured signals now cover:

- Driver `VERIFICATION_REQUIRED` and repairable `FAILED_NONRETRYABLE` failures.
- Driver and Safe Planner pre-driver `CAPABILITY_MISSING`.
- Concrete missing capability names only; absent/empty names fail closed through
  `GapCapabilityError` and are never rewritten to `"unknown"`.
- Structured deferred reason codes (`DEFERRED_VERIFICATION_REQUIRED`,
  `DEFERRED_CAPABILITY_MISSING`, and related codes), not English substring classification.
- The narrow path intersection of `WorkBinding.allowed_paths` and
  `SupervisorScope.allowed_paths`.

The approval boundary is unchanged: insertion is not execution approval. A generated child still
returns `WAITING_APPROVAL` until `grant_task_approval()` records founder approval.

### Merge readiness

Hardening is implemented on `cursor/pr79-live-loop-hardening` stacked on PR #79 head
`bed835a`. **No new Alembic migration** — attempt audit reuses `outcome_recorded` (head `0045`).

Verified locally against Postgres: live hardening suite, PR #78 gap primitives, PR #77 partial
plan insertion, Scoped Supervisor, Safe Planner, Provider-Assisted Planning, Development Driver,
and Development Operator suites. Founder approval remains required for `repo_edit` under
`autonomous_development_work`.

## Why this exists

`docs/LIFE_AUTONOMOUS_GAP_TO_CHILD_TASK.md` (PR #78) built and proved the gap → child-task
primitive in isolation: given a `DiscoveredGap`, it records evidence, assesses authority, and —
if authorized — inserts a bounded child task via `insert_plan_tasks()`. It was never called from
anywhere in the live execution path; every test drove it directly.

This pass wires that primitive into the ONE place MainAI actually discovers a gap while doing
real work: `app.development_supervisor.service.run_supervisor()`'s own driver-result handling,
immediately after `run_driver()` returns a classification. No second supervisor, no second
execution loop, no new entry point a founder or caller has to know about — the live loop is:

```
AUTHORIZED GOAL
  -> run_supervisor() selects a candidate task (existing discover_candidates()/select_candidate())
  -> dispatches it, runs the Development Driver/Operator (existing run_driver())
  -> driver returns COMPLETE, or VERIFICATION_REQUIRED / CAPABILITY_MISSING, or another classification
       VERIFICATION_REQUIRED / CAPABILITY_MISSING
         -> handle_live_gap_signal() (NEW call, purely additive at this one call site)
              -> gap_from_verification_required() / gap_from_capability_missing() (NEW)
                   builds a DiscoveredGap that is a PURE function of (task.id, task.description[, capability])
              -> generate_child_task_for_gap() (UNCHANGED from PR #78)
                   record_gap() -> depth bound -> assess_gap_authority() -> insert_plan_tasks()
       every other classification (WAITING_PROVIDER, WAITING_APPROVAL, NEEDS_*, OUT_OF_SCOPE, ...)
         -> handle_live_gap_signal() returns None immediately, existing behavior unchanged
  -> _checkpoint() folds the gap outcome into the Supervisor's OWN existing checkpoint (no second write)
  -> if binding.independent, the task is deferred and the SAME run tries other candidates
  -> the newly inserted child is an ordinary MainAITask row -- the NEXT run_supervisor()
     invocation's discover_candidates() finds it exactly like any other task (no special
     "discovery" code needed: it is discovered because it is a real row for the same goal_id)
  -> the Supervisor derives a WorkBinding from structured gap provenance (`candidate=None`) and
     the run continues: Safe Planner / Provider-Assisted Planning -> Development Driver -> Operator
     -> verification -> reassess -> continue, within the SAME existing run bounds
```

## The smallest correct integration point (what was inspected first)

Before writing anything, the exact live handoff points were traced:

- `run_supervisor()`'s driver-result branch (`app/development_supervisor/service.py`, the
  `if driver.classification != "COMPLETE":` block right after `driver = run_driver(...)`) is the
  ONE place a driver classification is available together with the `scope`/`goal`/`plan`/`task`
  the gap-generation primitive needs. Nothing upstream (candidate discovery, planning) or
  downstream (checkpointing, `_result()`) has all four.
- `CAPABILITY_MISSING` already had a defer-and-continue branch (`deferred[task.id] = ...; continue`
  when `binding.independent`); `VERIFICATION_REQUIRED` did NOT — a genuine pre-existing
  inconsistency, not something this pass introduced. Extending the SAME pattern to
  `VERIFICATION_REQUIRED` (rather than inventing a different mechanism) is both the smallest
  change and the one that makes "goal continues" actually true for a repair gap, not just a
  capability gap.
- `select_candidate()`'s no-actionable-candidate fallback classifier matches substrings in
  deferred assessment reasons (`"capability"` → `CAPABILITY_MISSING`, `"not authorized"` →
  `PROVIDER_SPEND_NOT_AUTHORIZED`, `"provider"` → `WAITING_PROVIDER`, else `BLOCKED`). Extending
  defer-and-continue to `VERIFICATION_REQUIRED` without adding a matching `"verification"` branch
  here would have silently degraded every deferred verification failure to the generic `BLOCKED`
  classification on the next loop iteration — fixed by adding that branch, mirroring the existing
  `"capability"` one exactly.
- `MainAICheckpoint` (migration 0032, step `"development_supervisor"`) already fires once per
  driver-result branch. The gap outcome is folded into that SAME checkpoint's `state` dict under
  a new `"gap_generation"` key rather than a second checkpoint write — one checkpoint per
  Supervisor decision stays true.
- `Safe Planner`/`Provider-Assisted Planning`'s structured `CAPABILITY_MISSING`
  (`planning.classification != "ACCEPTED"`) is also wired through the same bounded gap handler.

## Circular-import avoidance

`app.autonomous_gap.service` originally imported `SupervisorScope`/`RISK_ORDER` FROM
`app.development_supervisor.service` (PR #78). Wiring `development_supervisor.service` to call
INTO `autonomous_gap.service` would make that a genuine cycle. Fixed by:

- `from __future__ import annotations` at the top of `autonomous_gap/service.py`, so every type
  hint referencing `SupervisorScope` is a lazy string, never evaluated at import time.
- `if TYPE_CHECKING: from app.development_supervisor.service import SupervisorScope` — the class
  is duck-typed (accessed by attribute only: `.repository_identity`, `.allowed_paths`, `.self_work`,
  ...), never imported at runtime.
- `RISK_ORDER = {"low": 0, "medium": 1, "high": 2}` duplicated locally (a plain dict, which — unlike
  a type hint — cannot be deferred the same way).

Both modules import cleanly in either order; verified with `python -c "import app.autonomous_gap.service"`
and `python -c "import app.development_supervisor.service"`.

## Which signals are wired, and why only these two

`app.autonomous_gap.service.LIVE_GAP_SIGNAL_CLASSIFICATIONS = frozenset({"VERIFICATION_REQUIRED", "CAPABILITY_MISSING"})`

These are the only two `run_driver()` result classifications that are structured, durable,
deterministic evidence a real gap exists — not a transient wait a retry resolves on its own, and
not a terminal state that already carries its own explanation:

| `run_driver()` classification | Wired? | Why |
|---|---|---|
| `VERIFICATION_REQUIRED` | **Yes** | A real verification gate failed against real recorded evidence — `gap_from_verification_required()` builds the gap from `task.id`/`task.description` only. |
| `CAPABILITY_MISSING` | **Yes** | The Development Operator's own `capability_missing()` reported a real, named missing capability (`driver.detail["requested_capability"]`) — `gap_from_capability_missing()` builds the gap from `task.id`/`task.description`/`capability` only. |
| `WAITING_PROVIDER` | No | Transient — a provider becoming available resolves it without any new task. |
| `WAITING_APPROVAL` | No | A founder decision is already pending; generating a competing gap-child would be noise, not new information. |
| `EXTERNAL_REVIEW_REQUIRED` | No | Already routes to a human; a gap-generated child cannot substitute for that review. |
| `BLOCKED` / `CANCELLED` / `ACTION_BOUND_REACHED` / `NEEDS_SELECTION` | No | Bound/administrative states, not evidence of a missing piece of work. |
| Safe Planner's own `CAPABILITY_MISSING` (before a driver plan exists) | **Yes** | The structured planning result carries a concrete `requested_capability`; absent names fail closed. |

`handle_live_gap_signal(classification=...)` returns `None` immediately for anything outside
`LIVE_GAP_SIGNAL_CLASSIFICATIONS` — verified directly (`test_non_gap_classifications_can_never_become_a_live_gap`).

## Idempotency: excluding per-attempt values

`gap_from_verification_required()`/`gap_from_capability_missing()` build `reason`/`required_outcome`/
`source_ref` as **pure functions of `(task.id, task.description[, capability])`** — nothing that
varies between retries of the identical underlying failure (in particular, `run_driver()`'s own
per-attempt `driver.checkpoint_id` is deliberately never included). This matters for two reasons:

1. `_gap_identity_key()`'s content hash (feeding `record_gap()`'s idempotency key) must be stable
   across retries, or a stale worker retrying the same task after a lease loss, or the Supervisor
   being re-invoked after a crash, would mint a brand-new `LifeProblem` every time instead of
   converging on the same one.
2. `create_problem()`'s own idempotency check compares the WHOLE `provenance` dict for exact
   equality on replay (`_same()`) — any field that varies between calls under the SAME
   idempotency key raises `ProblemLearningError` as a semantic conflict, not a silent duplicate.

## Lineage depth for chained live gaps (the one real gap this pass had to close)

`GapGenerationBounds.max_generation_depth` (`DEFAULT_MAX_GENERATION_DEPTH = 3`) only means
anything if `DiscoveredGap.generation_depth` reflects a real lineage. `gap_from_verification_required()`/
`gap_from_capability_missing()` build a fresh `DiscoveredGap` from `task` alone with no memory of
prior generations — without an explicit lookup, EVERY live-generated gap would default to
`generation_depth=0` even when `task` is itself a previously gap-generated repair/capability
child, letting a repeated live chain (a repair child that later fails verification again,
generating its own repair, ...) run forever.

`_live_generation_depth(db, task=task)` closes this without a new column or migration, by reusing
data this module already writes durably:

1. A gap-generated task's own `created` `MainAITaskEvent` carries an
   `insertion_idempotency_key` of the shape `autonomous_gap_child:<problem idempotency_key>`
   (`insert_plan_tasks()`'s existing idempotency mechanism, `docs/LIFE_PARTIAL_PLAN_INSERTION.md`).
2. Stripping the `autonomous_gap_child:` prefix recovers the PARENT `LifeProblem`'s own
   `idempotency_key`.
3. That `LifeProblem.provenance["generation_depth"]` (set by `record_gap()`) + 1 is this task's
   own depth.
4. An ordinary, non-gap-generated task (no such event, or no such prefix) is depth 0.

`gap_from_verification_required()`/`gap_from_capability_missing()` now take `db` and call this
helper to populate `DiscoveredGap.generation_depth`; `handle_live_gap_signal()` passes `db=db`
through. Proved end to end by `test_repeated_gap_chain_stops_at_depth_bound_and_checkpoints_resumably`
— a REAL chain of 4 live `run_supervisor()` invocations (depths 0→1→2→3), the 4th stopping at
`DEPTH_BOUND_REACHED` with no 5th child inserted, evidence still durably preserved.

## Checkpoint shape

The gap outcome is folded into the Supervisor's own existing checkpoint (no second write):

```python
MainAICheckpoint.executor_state = {
    "job_id": ..., "step": "development_supervisor", "phase": "VERIFICATION_REQUIRED",
    "supervisor_state": {
        "completed_task_ids": [...], "selected_task_id": ..., "driver_checkpoint_id": ...,
        "followup": "verification_repair",  # only for VERIFICATION_REQUIRED
        "gap_generation": {
            "classification": "ACCEPTED",   # or NEEDS_AUTHORIZATION / NEEDS_CLARIFICATION /
                                             # NEEDS_SELECTION / OUT_OF_SCOPE /
                                             # EXTERNAL_REVIEW_REQUIRED / CAPABILITY_MISSING /
                                             # DEPTH_BOUND_REACHED, or null if not a gap signal
            "problem_id": "<uuid>",
            "inserted_task_ids": ["<uuid>", ...],
        },
    },
}
```

## No duplicate generation / recovery

Both properties are inherited unchanged from the underlying primitives, composed the same way
PR #78 already documents — this pass adds no new state of its own:

- **Interruption after gap record, before insertion**: `record_gap()`'s idempotency key is a
  deterministic hash of the gap's semantic identity, so re-recording the SAME gap (whether from a
  genuine retry or a resumed/re-invoked live path) returns the SAME `LifeProblem` row, then
  proceeds to insert exactly once. Proved by `test_interruption_after_gap_record_resumes_without_duplicate`.
- **Interruption after insertion, before the caller observes it**: `insert_plan_tasks()`'s own
  idempotency key (derived from the `LifeProblem`'s own key) makes a second `generate_child_task_for_gap()`
  call for the identical gap return the SAME inserted task, not a second one. Proved by
  `test_interruption_after_insert_discovers_not_recreates`, which also runs the resumed child to
  completion to prove it is real, executable work.
- **Stale worker**: in addition to the Operator's normal checks, `require_live_gap_lease()` fences
  the job immediately before durable gap work. A lease lost after the driver returns but before
  insertion raises `GapLeaseLostError`; the Supervisor isolates it and inserts no problem or child.
- **Founder correction racing gap handling**: `assess_gap_authority()`'s existing staleness check
  (`instruction_sha256(goal.original_instruction) != scope.authorized_instruction_sha256`) rejects
  generation with `NEEDS_AUTHORIZATION` the moment a correction lands, even if it lands AFTER the
  task was dispatched under the old authority but BEFORE this task's own gap handling runs — proved
  by `test_founder_correction_between_dispatch_and_gap_handling_rejects_stale_generation` via a
  `prepare_context` wrapper that applies the correction mid-flight, the realistic race shape.
- **Takeover**: resumes from the same canonical `MainAICheckpoint`/`LifeProblem`/`MainAITask`
  state any other Supervisor resume already does — no gap-generation-specific takeover logic was
  needed or added.

## Approval/authority — no bypass

- `autonomous_development_work` approval policy remains the sole authority gate
  (`app.mainai_execution.approval`, unchanged, still enforced inside `insert_plan_tasks()` and
  `run_driver()`'s own `require_task_approval()`).
- Gap DETECTION (a real `VERIFICATION_REQUIRED`/`CAPABILITY_MISSING` classification) is never
  itself authorization — `assess_gap_authority()` runs unconditionally and independently for every
  gap, authorized parent goal or not.
- Child INSERTION is never itself approval — the inserted task still carries `approval_required`
  per `propose_child_task_spec()` (currently `False`, same default `insert_plan_tasks()` itself
  documents; the task still goes through the SAME `require_task_approval()` gate as any task when
  it is later dispatched).
- Provider output is never authority — `gap_from_capability_missing()`'s `capability` string comes
  from `driver.detail["requested_capability"]`, itself sourced from
  `app.development_operator.service.capability_missing()`'s own structured result, never from raw
  provider prose. `handle_live_gap_signal()` has no code path that reads provider output directly.

## Self-work — no special bypass

An authorized self-improvement goal (`SupervisorScope.self_work=True`) uses the EXACT SAME
`handle_live_gap_signal()` call site, the exact same `generate_child_task_for_gap()` chain, and
the exact same `assess_gap_authority()` self-work gate (`gap.gap_type in {"capability_missing",
"self_improvement"}` requires `scope.self_work=True`) that already existed in PR #78. Proved by
`test_self_work_verification_gap_runs_through_the_exact_same_chain` (a `VERIFICATION_REQUIRED`
gap under a real self-improvement-themed goal, completed through the same repair-and-continue
loop as an ordinary goal) and `test_capability_missing_live_loop_under_self_work_authority`.

## Bounds

Unchanged from PR #78, now genuinely enforced across a chained live sequence (see "Lineage depth"
above): `GapGenerationBounds.max_generation_depth` (default 3) via `_live_generation_depth()`; the
existing Supervisor-level `SupervisorBounds` (`max_jobs`, `max_elapsed_seconds`) bound each
individual `run_supervisor()` call as before, unaffected by this wiring.

## Required live end-to-end scenarios (12/12)

All in `backend/tests/backend/mainai/test_autonomous_gap_live_integration.py`, every one driving
the REAL `run_supervisor()` loop through a real local git repository and real Postgres — never a
mock of the live call site:

1. **Verification Failure Live Loop** — a task fails verification → repair gap recorded → child
   inserted → a second `run_supervisor()` invocation discovers and completes it.
2. **CAPABILITY_MISSING Live Loop** — under an authorized self-work scope, a real capability gap
   becomes an inserted, `ready` capability-development child.
3. **Capability Gap Not Authorized** — the identical signal outside self-work authority is
   `NEEDS_AUTHORIZATION`, no task inserted, evidence still recorded, unrelated independent work
   still completes in the same run.
4. **Interruption After Gap Record** — pre-recorded evidence (simulating a crash right after
   `record_gap()`) never becomes a duplicate `LifeProblem` when the live path is invoked again.
5. **Interruption After Insert** — a pre-inserted child (simulating a crash right after insertion)
   is discovered as the SAME row, not recreated, and completes normally.
6. **Stale Worker** — a lease lost between context capture and driver execution fails closed
   (`OperatorAuthorizationError`) before gap generation is ever reached; no gap, no child.
7. **Provider Wait + Independent Gap** — a provider-dependent task stays waiting while an
   unrelated task's real live gap is recorded, its child inserted, and later completed
   independently of the provider wait.
8. **Founder Correction** — a correction landing between dispatch and gap handling both rejects
   the gap generation itself (`NEEDS_AUTHORIZATION`) AND surfaces as the run's own overall
   `AUTHORITY_CHANGED` result once the deferred binding re-enters the Supervisor's loop and its
   own top-of-loop `validate_scope()` re-checks the same staleness — a stronger proof than the
   gap layer alone.
9. **Depth/Count Bound** — a real chain of 4 live invocations stops generation at
   `DEPTH_BOUND_REACHED`, checkpointed resumably, evidence preserved.
10. **Self-Work** — the same verification-failure chain, run under an authorized self-improvement
    goal, no bypass.
11. **No Human Job Translation** — the repair child's own `created` event proves it was generated
    from the canonical gap. Worker identity is recorded separately on `outcome_recorded`
    `LifeProblemEvent` rows with `detail.gap_attempt=true`, not semantic `LifeProblem.provenance`.
12. **No Top-Level Goal Creation** — `MainAIGoal` row count is unchanged before/after a full live
    gap-generation-and-repair cycle; the child is always subordinate to the SAME goal.

## Security

- No unrestricted shell/merge/deploy/production-mutation/force-push path — the generated child's
  `InsertedTaskSpec` (`propose_child_task_spec()`) never carries a capability field at all
  (`test_generated_child_spec_never_carries_a_capability_field`); execution still goes through
  `app.development_driver.service.FORBIDDEN_CAPABILITIES`, unchanged.
- No merge/deploy anywhere in this module or its call site — confirmed by inspection (no `git
  push`/`git merge`/deploy-shaped calls exist in `autonomous_gap/service.py` or the new
  `development_supervisor/service.py` call site).
- Provider cannot grant authority — `LIVE_GAP_SIGNAL_CLASSIFICATIONS` only recognizes driver
  results already structurally validated by `run_driver()`/the Development Operator;
  `handle_live_gap_signal()` returns `None` for every other classification, proved directly.
- Owner isolation, repo/path scope, approval enforcement, self-work no bypass, stale lease
  fencing, idempotency/replay — all inherited unchanged from `assess_gap_authority()`/
  `insert_plan_tasks()`/`run_supervisor()`'s own existing coverage (`test_autonomous_gap_child_task.py`,
  `test_partial_plan_insertion.py`, `test_scoped_development_supervisor.py`) plus the live-specific
  proofs above (scenarios 3, 6, 8, 11).

## Migration

**No new migration.** Alembic head remains `0045`. Attempt audit reuses the existing
`outcome_recorded` event_type. `_live_generation_depth()` derives lineage entirely from existing
`MainAITaskEvent.detail` JSON and `LifeProblem.provenance` JSON — no new column.

## Explicit non-goals

- **"Missing prerequisite" / "unresolved dependency" as automatic LIVE triggers** — PR #78's
  primitive already supports these `evidence_kind`s when a caller constructs the `DiscoveredGap`
  directly (see `test_autonomous_gap_child_task.py`'s "Missing Prerequisite" scenario), but no
  code path in the current live execution loop (`run_supervisor()`/`run_driver()`) surfaces either
  as a structured classification the way it does for `VERIFICATION_REQUIRED`/`CAPABILITY_MISSING`.
  Wiring these live would require inventing a NEW detection mechanism (e.g. a deterministic
  repository-inspection pass) that does not exist anywhere in this codebase today — out of scope
  for a wiring pass, which only connects signals that already exist.
- Provider-suggested gap candidates remain "PROVIDER IDEA != AUTHORITY" — unchanged from PR #78;
  no new provider-consuming code path was added.
