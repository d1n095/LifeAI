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

## 1. The precise race window

Neither `app.workforce.provider_worker.require_live_task_authorization_for_workforce_dispatch()`
(this PR) nor `app.agent_coordination.dispatch.dispatch_assignment()` (pre-existing) holds any
row-level lock (`SELECT ... FOR UPDATE`, an advisory lock, or an optimistic version check)
across the gap between "read the authorization state" and "act on it":

- **Workforce (this PR):** the gate does a plain `SELECT` (via SQLAlchemy `.query().filter()`)
  of the `MainAITask` row, with no locking clause. Today, nothing meaningful happens between
  that read and the function's own return/raise — see §2.
- **Agent coordination (pre-existing, same class of gap):** `dispatch_assignment()`
  (`dispatch.py:187-256`) calls `evaluate_dispatch_readiness()` (a plain, unlocked read),
  then `transition_status(db, assignment=assignment, new_status="waiting_agent", ...)`
  (`service.py:465-488` — compares against the in-memory Python object's current `.status`
  attribute, not a fresh locked re-read, then writes), then `await adapter.start_assignment()`
  — a REAL external call (subprocess/provider) that can take meaningful wall-clock time. If a
  concurrent transaction changes the task's approval-relevant state (the closest existing
  equivalent being `cancel_task()`, which DOES lock the task row via `_lock_task()`) and
  commits strictly between `evaluate_dispatch_readiness()`'s read and `start_assignment()`'s
  actual invocation, that change is not seen — the already-computed `OUTCOME_ASSIGNABLE`
  decision is used as-is.

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

**`app.agent_coordination` is different: it is live, and the window is real there already,**
independent of this PR. `dispatch_assignment()` genuinely awaits a real adapter call after its
own `transition_status()` write. This is pre-existing and out of scope for this PR to fix, but
it means the race this document describes is not purely theoretical in this codebase overall
— it already exists on the one path that is actually reachable in production today.

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

## 4. Candidate approaches for atomic authorization + dispatch (none committed to)

Named for the founder/future reviewer to choose among — not a recommendation, since choosing
one is itself a security-relevant design decision this PR does not make unilaterally:

1. **Row-level lock held across the whole attempt** — `SELECT ... FOR UPDATE` on the
   `MainAITask` row (and/or the assignment row) at the start of dispatch, held until the real
   invocation either completes or is given up on. Simplest to reason about; risks holding a
   lock for the duration of a slow external call, which this codebase's existing patterns
   (e.g. `cancel_task()`'s `_lock_task()`) use only for short, synchronous DB work, never
   across an `await` to an external system — a real behavioral change from precedent.
2. **Conditional atomic transition** — a single `UPDATE ... WHERE status = 'ready' AND
   <approval still holds>` (or an equivalent check constrained into the same statement/
   transaction as the state transition), checking the affected-row count rather than trusting
   a prior read. Closer to `transition_status()`'s existing allowed-transition check, but
   would need the approval condition expressed as a WHERE clause the database itself
   evaluates atomically with the write, not as a separate prior Python-level check.
3. **Optimistic concurrency via a version/sequence column** — re-check a version number at
   the moment just before the real invocation; abort if it changed since the authorization
   read. Avoids holding a lock across slow I/O, but requires every state-changing operation
   on the task (including `cancel_task()`) to bump the version, which is itself a `WorkCandidate`-
   style schema change, not something to add silently as a side effect of this PR.
4. **A short-lived advisory lock scoped to the task id**, released immediately after the
   authorization decision is used (not held across the external call) — narrows the window to
   "between the lock release and the real invocation" rather than eliminating it; a partial
   mitigation, not a complete one.

## 5. Required adversarial PostgreSQL concurrency tests (not written here)

These cannot be written as currently-passing tests, because no atomic mechanism exists yet to
prove — writing them now would either be tautological (testing that the known gap exists,
which the TOCTOU test already does sequentially) or would require implementing one of §4's
approaches first, which this document explicitly does not commit to. Once an approach is
chosen, the required tests are:

1. **Genuine two-connection concurrency** (real separate `psycopg2`/SQLAlchemy connections in
   separate OS threads or processes — not sequential calls in one test function, which is
   what today's TOCTOU test already does and explicitly does not claim to be this): connection
   A begins an authorization-to-dispatch attempt and pauses at the exact point after its
   authorization read; connection B concurrently cancels (or revokes, once that primitive
   exists) the task and commits; connection A resumes and its attempt must fail.
2. The same test shape repeated for a "real" provider invocation actually taking non-trivial
   time (simulated with a deliberate delay in a test double), since the practical race window
   is proportional to how long the real external call takes.
3. A test proving the chosen mechanism does NOT deadlock or needlessly serialize unrelated,
   non-conflicting dispatch attempts (a correctness fix that introduces a liveness regression
   is not actually an improvement).
4. The equivalent set of tests for `app.agent_coordination.dispatch_assignment()`, since §1
   established this is not a workforce-specific gap.

## What this document explicitly does not do

- Does not claim the race is resolved, mitigated, or acceptable long-term.
- Does not pick one of §4's candidate approaches.
- Does not add any locking, version column, or new schema.
- Does not activate `app.workforce`'s dormant real-provider path, and does not touch the
  `ProviderActivationBlocked` staged-raise.
- Does not claim this is a workforce-specific defect — §1 traces the identical pattern in the
  one path that is actually live today.
