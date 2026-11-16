# Post-dispatch authorization lifecycle — what #433 protects, and what it does not

Founder directive, 2026-11-15 ("CONTINUE MAINAI INTEGRATION"), Priority 1, items 4-5: *"Check
whether the subprocess launcher or any downstream worker can continue after authorization
changes. Identify any remaining need for execution leases, cancellation or fencing after
dispatch... Do not claim that protecting the initial invoke automatically protects the entire
lifetime of an agent task."*

**Direct answer: yes, a dispatched agent process can continue running after the MainAITask it
is working on is cancelled or its approval is revoked, and nothing in this codebase today
re-checks or re-propagates that.** #433 closes the race at the ONE moment authorization is
decided (the atomic check immediately before `adapter.start_assignment()`); it was never
claimed to protect anything after that moment, and this document verifies, by reading every
call site, that nothing else does either — while also establishing the one fact that bounds
today's actual risk from this: the entire subsystem this applies to is currently unreachable
from any router or background worker in production.

## 1. What actually happens after a successful dispatch

Once `dispatch_assignment()` returns `OUTCOME_ASSIGNABLE`, the assignment is `running` and a
real external process may exist. Everything that happens to it afterward lives in
`app.agent_coordination.execution_control`:

- `record_execution_event()` / heartbeats / `reconcile_execution_state()` — observe and
  classify the process's state (alive / exited / lost / disconnected). **None of these call
  `require_task_approval()` or `require_live_task_approval_for_dispatch()`.** Verified by
  reading every function in `execution_control.py`: none imports from
  `app.mainai_execution.approval` at all.
- `collect_and_ingest_execution_result()` — wraps `dispatch.collect_dispatch_result()` to
  apply a finished run's result. Also never re-checks approval.
- `cancel_execution()` — the ONE function that actually stops a running process
  (`adapter.cancel()` + a `cancelled` transition). It exists, is correctly implemented, and
  checks the adapter's own `supports_cancel` capability — but it is only ever invoked by an
  explicit caller choosing to cancel THIS SPECIFIC assignment. Nothing calls it automatically.

**Grep-verified: `app.mainai_execution.executor.cancel_task()` — the function a founder
cancelling a `MainAITask` actually goes through — contains zero references to
`AgentWorkAssignment`, `agent_coordination`, or anything in this module.** Cancelling the
formal task a dispatched assignment is linked to (`assignment.task_id`) does not look up,
does not cancel, and does not even notice that linked assignment. The same is true in the
other direction: nothing in `agent_coordination` watches `MainAITask.status` or re-derives
whether approval still holds for an assignment already past its dispatch check.

**Also grep-verified: nothing calls `reconcile_execution_state()`, `collect_and_ingest_
execution_result()`, or `cancel_execution()` anywhere except their own module's `__init__.py`
re-export and their own tests.** There is no router endpoint and no background worker that
drives this subsystem today — `app.agent_coordination.dispatch`/`execution_control` is a
reviewed, tested foundation, not yet wired to anything that would make it run on its own or
be reachable from an API call, exactly the same "dormant until a future activation commit"
posture `app.workforce`'s `ProviderActivationBlocked` already has, just without a single
named gate to point at (the gate here is simply the absence of a caller).

## 2. What this means today versus if this subsystem is ever activated

**Today: no live exploitable consequence.** A race or a missing cancellation-propagation path
in code nothing calls is not a live vulnerability — there is no way, in production right now,
for an `AgentWorkAssignment` to actually be dispatched through this path at all (confirmed:
zero router exposure), so there is no running process whose authorization could go stale in
practice.

**The moment this changes:** whenever a future commit adds a router endpoint or a background
worker that calls `dispatch_assignment()` for real, or adds any periodic caller of
`reconcile_execution_state()`, the gap above becomes live immediately, with no additional code
change needed to trigger it — it is already fully present in the code as written. That
future commit's own review must not treat #433's atomic dispatch-time check as having already
solved this; it has not, and was never intended to (see #433's own PR description and the
updated `docs/LIFE_WORKFORCE_AUTHORIZATION_RACE_WINDOW.md`, both of which describe only the
dispatch-moment guarantee).

## 3. What would actually be needed — named, not implemented

These are open design questions for whoever builds that future activation commit, not
resolved or chosen here:

1. **Cancellation propagation.** `cancel_task()` (or a new explicit call it makes) would need
   to look up any `AgentWorkAssignment` with `task_id == task.id` that is currently
   `waiting_agent`/`running`, and call `cancel_execution()` on it — the function already
   exists and is already correct; it is simply never reached from the task-cancellation path.
   This is additive (a new lookup + call), not a new mechanism.
2. **Periodic re-authorization.** Whatever future loop ends up calling
   `reconcile_execution_state()` would need to also re-run `require_live_task_approval_for_
   dispatch()` on each tick for assignments with a `task_id`, and call `cancel_execution()`
   when it now fails. `reconcile_execution_state()`'s own docstring already establishes the
   right doctrine for this ("observe and classify only, never itself decide the assignment's
   fate") — a re-authorization check would need to live in whatever CALLS it, matching that
   same separation, not inside the classification function itself.
3. **No lease/fencing-token field exists yet.** `AgentDispatchExecution` (migration 0047) has
   `last_heartbeat_at` (usable for a future staleness check — "no heartbeat in N minutes,
   assume lost") but no explicit lease-expiry or fencing-token column. Whether one is needed
   depends on what the real adapter/worker's own liveness guarantees turn out to be once
   built — named here as an open question, not a decision this document makes.

## 4. What this document explicitly does not do

- Does not implement cancellation propagation, periodic re-authorization, or a lease/fencing
  field — all three are named as open work for a future, separately-reviewed activation
  commit, matching exactly how `docs/LIFE_WORKFORCE_AUTHORIZATION_RACE_WINDOW.md` already
  treats `app.workforce`'s own dormant path.
- Does not claim #433's fix was supposed to cover this — it explicitly only ever claimed the
  dispatch-moment atomic check, and this document exists specifically so that claim is never
  silently stretched further than it was made.
- Does not touch `cancel_task()`, `execution_control.py`, or any production code.
