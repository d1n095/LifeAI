# Unresolved security consideration: authorization is not atomic with dispatch

Founder directive, 2026-11-15 ("Complete Current Verification Before New Work"), Priority 2:
"The existing sequential authorization re-check is valuable, but it does not establish
atomic authorization under concurrent revocation and dispatch... Do not claim the issue is
resolved based on the existing sequential TOCTOU regression."

**This is correct and this document does not claim otherwise.** The TOCTOU test in
`backend/tests/backend/mainai/test_workforce_task_approval_enforcement.py`
(`test_gate_always_rechecks_fresh_state_not_an_earlier_decision`) proves the gate re-queries
fresh state on each SEPARATE call — it proves sequential correctness, not atomicity across a
single dispatch attempt. This document records the gap precisely, as its own named,
unresolved item, shared with the already-existing `app.agent_coordination` path — not
something this PR introduces, and not something this PR fixes.

> **Update (`claude/workforce-authorization-dispatch-atomicity`, same founder-directive
> cycle, Priority 1 of "STOP IDLING, CONTINUE ENGINEERING"):** the authorization gate now
> takes a real `SELECT ... FOR UPDATE` lock on the task row (see §1's first bullet, updated
> below, and the genuine two-connection proof in
> `test_workforce_task_authorization_concurrency.py`). **This closes the primitive-level
> gap, not the full end-to-end one**: the lock correctly serializes against a concurrent
> `cancel_task()`-style writer and against a second concurrent authorization attempt, for as
> long as the caller's transaction stays open. It does NOT yet wire any real dispatch effect
> to stay inside that same transaction, because no real dispatch effect exists yet
> (`ProviderActivationBlocked` is still unconditional and untouched) — there is nothing
> downstream to hold the lock across. When a future, separately-reviewed activation commit
> adds the real invoke step, that step MUST occur before this function's caller commits or
> releases the lock (e.g., by passing the locked `db` session through, never opening a new
> one) for the full guarantee to hold end-to-end. This document's §4/§5 are updated to
> reflect what is now implemented vs. what remains a future integration responsibility.

> **Second update (`claude/agent-coordination-dispatch-authorization-boundary`, same
> founder-directive cycle, Priority 1 of "CONTINUE ACTIVE ENGINEERING"): the end-to-end gap
> §1 named for `app.agent_coordination.dispatch_assignment()` is now closed** — this is the
> one path, of the two named in §1, where a real external call (`await
> adapter.start_assignment()`) already exists today, so "closed" here means something
> stronger than workforce's still-primitive-only fix above: the atomic re-check genuinely
> protects a real invoke, proven by a genuine two-connection test that observes the lock held
> DURING that call
> (`test_lock_is_held_through_the_real_adapter_invoke_not_only_the_earlier_check`,
> `backend/tests/backend/mainai/test_agent_dispatch_authorization_boundary.py`). This is
> deliberately NOT a lease/fencing redesign — the lock is held across the adapter call on
> purpose, justified by reading the one real adapter implementation
> (`LocalCLIAdapter.start_assignment()`): it only calls
> `asyncio.create_subprocess_exec` and returns, never awaiting the agent's own work, so the
> hold is bounded by "time to start a process." §1/§4/§5 below are updated; the "what this
> document does not do" section is corrected to drop the now-false claim that this path is
> unfixed.

## 1. The precise race window

**As originally written, neither** `app.workforce.provider_worker.
require_live_task_authorization_for_workforce_dispatch()` **nor**
`app.agent_coordination.dispatch.dispatch_assignment()` (pre-existing) held any row-level
lock (`SELECT ... FOR UPDATE`, an advisory lock, or an optimistic version check) across the
gap between "read the authorization state" and "act on it". **Workforce's side is now
fixed at the primitive level** (see the update note above this section) — this section is
kept as the original finding, with the fix noted inline, so the before/after is legible:

- **Workforce (now fixed at the primitive level):** the gate's task lookup now takes a real
  `SELECT ... FOR UPDATE` lock, held for the remainder of the caller's transaction — the
  identical lock `app.mainai_execution.executor._lock_task()` already takes on the same row
  for `cancel_task()`/`dispatch_ready_task()`/`retry_task()`. Proven against a genuinely
  separate connection in `test_workforce_task_authorization_concurrency.py`. Still true today,
  unchanged: nothing meaningful happens between that locked read and the function's own
  return/raise, because there is still no real invoke step to protect — see §2.
- **Agent coordination (now fixed end-to-end for the authorization dimension):**
  `dispatch_assignment()` (`dispatch.py`) still calls `evaluate_dispatch_readiness()` first
  (a plain, unlocked read — kept, since it is also called standalone for pure readiness
  display/testing, where taking a lock would be wrong), but now ALSO calls
  `app.mainai_execution.approval.require_live_task_approval_for_dispatch()` immediately
  before `transition_status(..., "waiting_agent", ...)` and `await
  adapter.start_assignment()` — taking a real `SELECT ... FOR UPDATE` lock on the linked
  `MainAITask`, held through both of those calls (not committed in between). A concurrent
  `cancel_task()`-style write attempting to lock the same row blocks until this function's
  caller commits or rolls back; proven in
  `test_for_update_lock_blocks_a_concurrent_cancel_task_style_write` and, for the real invoke
  window specifically, in
  `test_lock_is_held_through_the_real_adapter_invoke_not_only_the_earlier_check`
  (`test_agent_dispatch_authorization_boundary.py`). `assignment.approval_required` with no
  `task_id` is unaffected (unchanged, unconditional fail-closed — there is no task row to
  lock).

**This is not a new gap introduced by this PR.** It already existed in the only currently-live
dispatch path's design; this PR's own gate shares the same unlocked-read pattern because it
reuses the same `require_task_approval()` call style `evaluate_dispatch_readiness()` already
established, deliberately not inventing a stricter pattern unilaterally (see §4).

## 2. Does the existing unconditional `ProviderActivationBlocked` gate currently prevent exploitation?

**Yes, for workforce, today — but only incidentally, not by design against this specific
race.** `execute_workforce_assignment()`'s `activate_provider` branch calls the new gate,
then (after an `allow_execution_effects` check and a `provider_spend` import check — neither
of which does meaningful I/O) hits the pre-existing, untouched unconditional
`raise ProviderActivationBlocked(...)` with NO real invocation ever reached. There is
currently no code path in this function where meaningful wall-clock time passes between the
gate's read and the function returning/raising — so the race window is real in principle but
has no real consequence to race against today, because nothing real is ever dispatched
regardless of the gate's answer.

**`app.agent_coordination` was different, and is why it was fixed first (not just at the
primitive level): it is live, and the window was real there already**, independent of
workforce's own status. `dispatch_assignment()` genuinely awaits a real adapter call
(`start_assignment()`) after its own `transition_status()` write — the race this §2 describes
was not purely theoretical in this codebase overall, it already existed on the one path that
is actually reachable in production today. That is now closed: see the second update note at
the top of this document and §1's updated bullet.

## 3. When does this become relevant for workforce specifically?

If and when `provider_worker.py`'s staged raise is intentionally lifted (its own comment:
"enable only in the post-verification activation commit") AND the real invocation step that
replaces it takes meaningful wall-clock time (any real provider call will), the race becomes
live: a task approved at the moment of the gate check, then cancelled (or, if a true
approval-revocation primitive is ever added — none exists today, see
`docs/LIFE_EXECUTION_ARCHITECTURE_CONVERGENCE_RECOMMENDATION.md` §2 — revoked) by a concurrent
transaction that commits after the gate's read but before the real call completes, would have
its real invocation proceed on stale authorization. **This document takes no position on
when or whether that activation should happen** — only on what must be true before it does.

## 4. Candidate approaches for atomic authorization + dispatch — **approach 1 selected for the primitive, not yet wired end-to-end**

Originally named for the founder/future reviewer to choose among. **Approach 1 is now
implemented at the primitive level** (workforce's gate), chosen as the smallest correct
option because it reuses `_lock_task()`'s own established idiom exactly rather than adding a
new mechanism:

1. **Row-level lock held across the whole attempt** — `SELECT ... FOR UPDATE` on the
   `MainAITask` row, at the start of authorization, held until the caller's transaction ends.
   **Implemented** in `require_live_task_authorization_for_workforce_dispatch()`. The
   caveat below is still real and still open: holding this lock for the duration of a slow
   external call is a real behavioral change from `_lock_task()`'s own precedent (used only
   for short, synchronous DB work today) — **wiring a future real invoke step to stay inside
   this same transaction, for however long that external call takes, is a separate decision
   for whoever implements that activation commit**, not resolved by this document or by
   adding the lock itself.
2. **Conditional atomic transition** — a single `UPDATE ... WHERE status = 'ready' AND
   <approval still holds>`, checking the affected-row count rather than trusting a prior
   read. Not implemented; remains a candidate for the real-invoke step specifically if
   holding a row lock across slow I/O (approach 1's caveat above) proves unacceptable.
3. **Optimistic concurrency via a version/sequence column** — not implemented; would need
   every state-changing operation on the task (including `cancel_task()`) to bump a version,
   a `WorkCandidate`-style schema change out of scope for a locking-only fix.
4. **A short-lived advisory lock scoped to the task id**, released before any slow I/O — not
   implemented; a partial mitigation, not selected because approach 1 already provides the
   full guarantee for whatever the lock is actually held across.

**For `dispatch_assignment()`, approach 1 is now wired end-to-end, not just at the
primitive level** — see the second update note above §1. The open caveat above (slow
external call inside the lock) was resolved by evidence, not by assumption: the one real
`AgentAdapter` implementation's `start_assignment()` only spawns a subprocess and returns, so
"held across the real invoke" means "held for a process-spawn syscall," not "held for however
long the agent's actual work takes." If a future adapter implementation makes
`start_assignment()` itself slow or network-bound, this approach should be revisited in favor
of approach 2 or 4 for that specific call — flagged here as a condition to watch, not a
present defect.

## 5. Required adversarial PostgreSQL concurrency tests

**Written and passing** (genuine two-connection concurrency, not sequential simulation) in
`backend/tests/backend/mainai/test_workforce_task_authorization_concurrency.py`, mirroring
PR #379's own established pattern (`test_blueprint_approval_concurrency.py`) of a lock-holder
connection, a `SET LOCAL lock_timeout` on the contending connection, and explicit commit/
rollback to show the lock's exact lifetime:

1. **Revocation vs. dispatch** — `test_for_update_lock_blocks_a_concurrent_cancel_task_style_write`.
2. **Competing workers** — `test_two_concurrent_authorization_attempts_on_the_same_task_serialize_not_interleave`.
3. **Transaction rollback / retry** — `test_rollback_releases_the_lock_and_an_immediate_retry_sees_unchanged_state`.
4. **The real production function, not just the mirrored primitive** —
   `test_the_real_gate_function_holds_the_lock_until_its_callers_transaction_ends`, calling
   `require_live_task_authorization_for_workforce_dispatch()` itself and proving via a
   genuinely separate connection that its lock is still held after the call returns.

**For `app.agent_coordination.dispatch_assignment()`, written and passing**
(`backend/tests/backend/mainai/test_agent_dispatch_authorization_boundary.py`), closing every
item the previous revision of this section listed as not yet written for that path:

5. **Real-invoke timing, through the actual call this time** —
   `test_lock_is_held_through_the_real_adapter_invoke_not_only_the_earlier_check`: a
   deliberately slow fake adapter makes the lock-hold window during `await
   adapter.start_assignment()` observable from a genuinely separate connection, which cannot
   acquire the same row's lock until `dispatch_assignment()`'s caller commits.
6. **Liveness** — `test_locks_on_different_tasks_never_contend_unrelated_dispatch_is_never_blocked`:
   two different tasks' locks, taken from two separate connections, never contend — proving
   row-level, not table-level, exclusion.
7. **Revocation vs. dispatch, through the real production path** —
   `test_dispatch_assignment_rejects_a_task_cancelled_after_an_earlier_readiness_check_passed`:
   a real, earlier `evaluate_dispatch_readiness()` call passes, the task is then genuinely
   cancelled via `cancel_task()`, and the real `dispatch_assignment()` call still rejects —
   the adapter is never invoked.
8. **Competing workers / rollback+retry** —
   `test_for_update_lock_blocks_a_concurrent_cancel_task_style_write`,
   `test_two_concurrent_authorization_attempts_on_the_same_task_serialize_not_interleave`,
   `test_rollback_releases_the_lock_and_an_immediate_retry_sees_unchanged_state` — the same
   three proofs §5 items 1-3 give for workforce, given again for the `mainai_tasks` lock as
   `dispatch_assignment()` actually uses it.
9. **The real production function** —
   `test_the_real_gate_function_for_dispatch_holds_the_lock_until_callers_transaction_ends`.

**Still not written, and out of scope:**
- A duplicate-dispatch race on the SAME `AgentWorkAssignment` row itself (two concurrent
  `dispatch_assignment()` calls for the identical assignment, neither linked lock protects
  this — only the linked MainAITask's row is locked). This is a different class of gap
  (dispatch idempotency, not authorization bypass) that predates this fix and is not what the
  founder's directive scoped as "the authorization race" — named here so it is not mistaken
  for closed.
- A lease/fencing-token redesign for `start_assignment()` — not needed today per §4's
  evidence-based finding, only flagged as a future condition to watch.

## What this document (and the fixes it now describes) explicitly does not do

- **Does not claim workforce's race is fully resolved end-to-end.** Its primitive-level lock
  is implemented and proven; nothing yet exists downstream for it to protect, because
  `ProviderActivationBlocked` still blocks every real invocation unconditionally there.
  "Atomic authorization and dispatch" as a complete guarantee for workforce is only true once
  a future real-invoke step is wired to stay inside the same transaction — that wiring is
  explicitly NOT done here for workforce.
- **Does claim `app.agent_coordination.dispatch_assignment()`'s race is now closed
  end-to-end** for the authorization dimension specifically (a revoked/cancelled task cannot
  have its dispatch proceed once this check has run) — backed by the real-path tests in §5
  items 5-9, not merely the primitive. It does NOT claim the separate duplicate-dispatch
  race on the assignment row itself is closed (§5's "still not written" note).
- Does not add a version column or any new schema (approach 1 needed neither, for either
  path).
- Does not activate `app.workforce`'s dormant real-provider path, and does not touch the
  `ProviderActivationBlocked` staged-raise.
- Does not introduce a second approval engine or dispatcher — both fixes reuse
  `require_task_approval()` and `_lock_task()`'s own locking idiom exactly as they already
  exist; `app.mainai_execution.approval.require_live_task_approval_for_dispatch()` is new code
  but a thin, direct application of that same existing idiom, not a new mechanism.
