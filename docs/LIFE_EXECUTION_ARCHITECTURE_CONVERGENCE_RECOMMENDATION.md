# Execution-architecture convergence recommendation

Founder directive, 2026-11-15 ("PR #418 Verified Progress — Continue Architectural
Convergence"), Priority 1: using `docs/LIFE_THREE_EXECUTION_SYSTEMS_BOUNDARY_MAP.md`'s
findings, produce an evidence-based convergence recommendation across
`app.mainai_execution`, `app.agent_coordination`, and `app.workforce` — reusable components,
duplicate authority, missing integration points, a safe incremental migration path. No
dormant dispatcher is activated here, no fourth system is created, and
`ProviderActivationBlocked`'s unconditional staged-raise is left exactly as it is.
`first_cycle_go` remains `false`.

All claims below are either read directly from the cited `file:line` on the current dev tip,
or proven by the new executable regression test in
`backend/tests/backend/mainai/test_execution_authority_convergence_gap.py` (added by this
same PR, real Postgres, no mocks) — not inferred or asserted without evidence.

## 1. The canonical authority already exists and is already shared correctly in one place

`app.mainai_execution.approval.require_task_approval()` (`backend/app/mainai_execution/
approval.py:93`) is the one real enforcement point: the ONLY function in the codebase that
queries for a genuine `approval_granted` `MainAITaskEvent` and raises `ApprovalRequiredError`
if none exists. It is called from two places today:

- `app/mainai_execution/executor.py`'s `dispatch_ready_task()` — the live worker-loop path
  (`app/worker.py`'s auto-advance loop), unconditionally, before any `mainai_jobs` row is
  created.
- `app/agent_coordination/dispatch.py:160-178` (`evaluate_dispatch_readiness()`) — when an
  `AgentWorkAssignment` has a linked `task_id`, it loads the real `MainAITask`/`MainAIGoal`
  and calls `require_task_approval(db, task=task, goal_approval_policy=goal.approval_policy)`
  verbatim, converting `ApprovalRequiredError` into `OUTCOME_APPROVAL_REQUIRED`. When
  `task_id` is `None` but `assignment.approval_required` is set, it fails closed
  (`OUTCOME_APPROVAL_REQUIRED`, "has no linked task_id to check the real approval gate
  against") rather than treating "nothing to check" as "approved."

**This is correct, already-converged design.** `app.agent_coordination` does not
re-implement approval; it delegates to the one real authority. The earlier boundary map's
"zero live callers" finding (nothing in `app/routers/` or `app/worker.py` calls
`dispatch_assignment()`) is about wiring a trigger, not about authority — the authority
question is already answered correctly for this system.

`test_agent_coordination_dispatch_outcome_tracks_the_same_tasks_approval_state` (new test,
this PR) proves it executably: the same `MainAITask`, same `AgentWorkAssignment`, same
`evaluate_dispatch_readiness()` call — `OUTCOME_APPROVAL_REQUIRED` before
`grant_task_approval()`, `OUTCOME_ASSIGNABLE` after.

## 2. The actual gap: `app.workforce` holds a task link it never checks

`WorkforceDelegationRequest` (`backend/app/models/workforce.py:88`) has its own
`task_id` column, nullable, no FK declared at the model level but matching the same
`mainai_tasks` identity `AgentWorkAssignment.task_id` points to. `submit_delegation_request()`
(`backend/app/workforce/broker.py:32-39`) accepts it as a real parameter.

But `WorkforceAssignment` itself (`backend/app/models/workforce.py:125-172`) has **no
`task_id` column at all** — only `delegation_request_id`. `resolve_delegation()`
(`backend/app/workforce/broker.py:66-161`) never reads `request.task_id` and never surfaces
it anywhere onto the `WorkforceAssignment` it constructs (not as a column — there is none —
and not into the one JSON field that could hold it, `provenance`, either). The link exists
on the request; it is silently dropped the moment a request becomes an assignment.

`execute_workforce_assignment()` (`backend/app/workforce/provider_worker.py:154-260`), the
function that actually runs a workforce assignment, never queries `MainAITask` and never
calls `require_task_approval()` anywhere on its `activate_provider=True` path
(`provider_worker.py:236-260`). It calls exactly one gate — `require_activation_allowed()`
(`backend/app/workforce/activation_gates.py:179-182`), a **global**, not per-task, safety-gate
set — then, regardless of that outcome, an unconditional
`raise ProviderActivationBlocked("Gates satisfied but first real provider invoke remains
staged — enable only in the post-verification activation commit")` (`provider_worker.py:258-
260`) that blocks every real invocation today no matter what.

**The new test, `test_workforce_dispatch_outcome_is_blind_to_the_same_tasks_approval_state`,
proves this executably**: the identical `MainAITask` used in Section 1's test — approval-
required, then genuinely approved via the same real `grant_task_approval()` — produces the
exact same exception, with the exact same message, from `execute_workforce_assignment()`
both before and after approval. Approving the task this assignment is nominally for changes
nothing observable on workforce's own dispatch path, because nothing on that path ever looks
at it.

This is not exploitable today — the unconditional staged-raise blocks real invocation
regardless. The risk is latent: if that staged-raise is ever intentionally lifted by a
dedicated, security-reviewed activation-commit PR (which is exactly how
`provider_worker.py`'s own docstring says it is meant to happen — "enable only in the
post-verification activation commit"), nothing in the current code would then stop a
workforce assignment from invoking a real provider for an unapproved `MainAITask`, because
the approval-delegation step this convergence recommends simply does not exist yet to be
exercised.

## 3. Reusable components vs. duplicate authority

**Reusable, not duplicate:** `require_task_approval()` itself. It is already the single
source of truth; nothing about converging workforce onto it requires inventing new authority
logic — only calling the function that already exists, the same way
`app.agent_coordination.dispatch` already does.

**Not duplicate authority, despite looking similar — a genuinely different invariant:**
`app.workforce.activation_gates.ActivationGateSet` (`REQUIRED_ACTIVATION_GATES`,
`GateStatus`, `require_activation_allowed()`) checks **process/infrastructure readiness**
("has a human verified these N safety preconditions with real evidence refs") — a
founder-alpha-wide precondition, not a per-task decision. `app.agent_coordination` has no
equivalent of this at all; its closest analog, `adapter_config.adapter_availability()`
(gated via `evaluate_dispatch_readiness()`'s `require_adapter_enabled` flag), only asks "is
this one adapter's executable configured and enabled," a narrower, per-adapter question.

**Recommendation: do not discard or replace workforce's `ActivationGateSet` when
converging.** It protects something `require_task_approval()` does not (infra-wide
readiness vs. per-task founder sign-off) and something `adapter_config` does not either
(cross-cutting safety evidence vs. one adapter's executable config). The convergence here is
additive — workforce needs to gain the per-task check it is missing, in addition to the
infra-wide check it already has — not a replacement of either side.

## 4. Other duplicate/parallel structures (already catalogued by #419, not re-litigated here)

- Two independently-named "activation gate" concepts that never reference each other
  (`agent_coordination`'s reliance on `adapter_config.adapter_availability()` vs.
  `workforce.activation_gates.ActivationGateSet`) — see Section 3's resolution above; this is
  now characterized precisely rather than left as an open question.
- `WorkforceAssignment.coordination_assignment_id` (migration 0067) — a bare, non-FK'd,
  never-read-or-written UUID column, confirmed still true on the current dev tip. This
  convergence work does not resolve its fate (wire it as the real cross-system link, or drop
  it) — that remains the founder decision #419 already flagged it as.
- `app.mainai_executive` (`loop.py`/`dashboard.py`/`staff_manager.py`) has zero live callers
  into either `agent_coordination` or `workforce` — unaffected by this recommendation,
  unchanged from #419's finding.

## 5. Safe incremental migration path

Each step below is independently safe, independently testable, and does not require the
step after it to also happen. No step activates a dormant dispatcher, lifts the
`ProviderActivationBlocked` staged-raise, or changes `first_cycle_go`.

1. **(This PR.)** Record the gap with a real, executable regression test rather than a design
   claim. Done — see Section 2.
2. **(Future, separate PR, mechanical.)** Fix `resolve_delegation()` to surface
   `request.task_id` onto the `WorkforceAssignment` it creates — either as a new nullable
   column (a real migration, additive, backward compatible: existing rows get `NULL`) or,
   lower-risk and requiring no migration at all, into the assignment's existing
   `provenance` JSON field under a stable, documented key. This PR intentionally does not
   decide between the two or implement either — that choice, and its own isolated tests,
   belongs to its own PR, not bundled into a recommendation document.
3. **(Future, separate PR, security-reviewed — this is the real authority change and
   deserves its own independent review, not a drive-by addition here.)** Inside
   `execute_workforce_assignment()`'s `activate_provider` branch, before the existing
   unconditional raise, resolve the assignment's task link (however step 2 chose to store it)
   and call `require_task_approval()` exactly the way `dispatch.py:160-178` already does. The
   staged-raise stays in place below it — this step only makes the check exist for whenever
   that raise is eventually, separately, lifted.
4. **(Founder decision, not an engineering task.)** Decide `coordination_assignment_id`'s
   fate, and whether `app.workforce`'s real-provider path is ever meant to go live or is
   permanently staged by design. Out of scope for any single PR per the founder's own
   standing instruction not to activate dormant dispatchers or create a fourth system.

## What this recommendation deliberately does not do

- Does not add the `require_task_approval()` call to `provider_worker.py` itself (step 3 is
  named, not implemented, here — it is a real authority change and belongs in its own
  reviewed PR).
- Does not touch `coordination_assignment_id`.
- Does not lift or weaken `ProviderActivationBlocked`'s unconditional raise.
- Does not change `first_cycle_go`, which remains `false`.
- Does not create a new, fourth execution/authority system — every recommendation above
  reuses `require_task_approval()` as the one canonical authority, already proven live and
  already correctly delegated to by `app.agent_coordination`.
