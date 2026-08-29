# ACTIVE WORK — Cursor: CORRECTION PASS — do not start the long autonomous experiment yet

**Owner:** Cursor
**Handed off by:** Claude (independent red-team review), founder-approved
**Integration tip at handoff:** `claude/det-kommer-mer-879lcm` (post-#195, this doc's own commit)
**Supersedes the "only after 1-4" ordering language in `docs/ACTIVE_WORK_CURSOR_NIGHT_RUN_FOLLOWUPS.md`** — that doc's sections 1-2 (the #182/#183 instructions themselves) are still fully in force; this doc adds sharper requirements found in the #192-195 review plus two new phases.

## Why this doc exists

Independent review found three proof gaps and confirmed one process failure. None of this
reopens what's already merged as wrong — #192-195's core mechanisms are real. But the proof
level is lower than their own descriptions claimed, and **the ordered queue was not followed**:
the handoff explicitly required `#182 → #183 → cancel boundaries → restart soak`. Cursor
completed the cancel boundaries and the soak (items 3-5) and never landed #182 or #183 (items
1-2). **Do not silently reorder security work again** — if a priority item is judged already
closed, document the exact evidence and say so; otherwise land it before advancing to later,
more interesting work.

**Do not start the long 8-12 task self-directed autonomous experiment until Phases 1-5 below
are merged green.**

---

## PHASE 1 — Close #182 Window B

Trace the exact provider call lifecycle: reserve → `adapter.propose()` begins → transport/
request may cross the process boundary → response/exception → settle OR release.

**Current concern:** a reservation may be released on a generic exception even when the
provider request may already have left the process.

**Invariant: UNKNOWN EXTERNAL EFFECT != NO EXTERNAL EFFECT.**

Classify provider failures into:

- **A. Provably pre-invoke** — the request cannot have left the process. Release is allowed.
- **B. Definitely invoked / response observed** — settle truthfully.
- **C. Ambiguous invocation** — the request may have crossed the external boundary, but local
  code received an exception or lost the response. For C:
  - Do **not** release as unused.
  - Do **not** automatically re-invoke the same semantic call.
  - Keep conservative, durable accounting.
  - Recovery must produce a truthful unresolved/ambiguous disposition, not a guess.

Use the existing reservation state model if possible. Do not invent a broad new state machine
unless genuinely necessary.

**Required negative control:**
- Pre-fix: an ambiguous exception fired *after* the invocation boundary → reservation released
  → retry can invoke the provider again.
- Post-fix: same scenario → reservation remains conservatively consumed/held → retry produces
  **zero** second adapter invocation.

Use a fake adapter that can deterministically signal "request boundary crossed, then exception"
— **do not** model this as an exception raised before `adapter.propose()` is even entered; that
would only prove case A, not case C, which is the actual gap.

Also add/verify a true concurrent first-reserve/retry test if the #182 review identified that
gap (it did — see that PR's own comment thread).

---

## PHASE 2 — Close #183 heal identity gap

Current crash heal (`disk content == requested after_hash`) must **not** be sufficient by
itself to conclude "this exact consequential operation already happened." Bind the heal to the
SAME semantic operation.

Audit available durable identity: `idempotency_key`, `job_id`, `task_id`, `path`, action/
capability, expected `after_hash`, worktree, lease generation/worker identity where relevant.

**Scenario:** Operation X (`idempotency_key=X`) writes H to P and crashes before audit.
Operation Y — a genuinely different semantic operation, `idempotency_key=Y` — also happens to
request writing H to P. Y must NOT steal/heal X's missing audit record as though Y had already
executed.

**Required tests:**
1. Same operation identity → disk already H → heal succeeds without rewrite (existing behavior,
   keep it working).
2. Different idempotency key → same path + same H → must NOT heal as the same operation.
3. Different task/job, if structurally meaningful → same content coincidence → no cross-
   operation heal.
4. #191's second-worker/current-authority tests remain green (do not regress that already-
   proven property).

**Negative control:** remove the new identity binding → confirm a different operation is
falsely healed by the old code. Keep the fix minimal — prefer tightening the existing condition
over a new schema addition.

---

## PHASE 3 — Harden the true effect-time refresh (`_require_context()`)

Independent review found: `_require_context()`, the authoritative last fence before every
Operator effect, uses ordinary non-refreshing `select()` calls for job/task. Current callers
happen to refresh earlier in the call chain (verified by tracing every real call site), so
**no demonstrated exploit exists today** — but the last fence should not rely on every caller
remembering to refresh first.

Audit carefully. If safe, make `_require_context()` itself fetch CURRENT durable job/task state
using `populate_existing=True` (or an equivalent explicit refresh/locking mechanism appropriate
to the path).

**Goal: THE LAST FENCE BEFORE EFFECT MUST OWN ITS OWN FRESHNESS.** Do not trust SQLAlchemy
identity-map cached state to already be correct just because it happens to be today.

**Required regression, two real sessions:**
- Session A loads/holds a stale cached job/task (do not manually refresh it).
- Session B performs a real authority change (founder cancel / lease takeover / whatever's
  relevant) and commits.
- Session A calls an Operator effect **without** manually refreshing first.
- Expected: `_require_context()` observes the current DB authority and rejects.
- Pre-fix negative control should fail (i.e., without the `populate_existing=True` fix, this
  exact test should demonstrate the stale-read problem).

Do not duplicate the higher-level #192/#185 checks — this specifically proves the final
Operator fence is independently correct, not reliant on caller discipline.

---

## PHASE 4 — Make the cancel/finalize race genuinely concurrent

#194 proves ordering semantics using two sessions, but its `threading.Barrier` calls
intentionally serialize the decisive ordering (confirmed in review) — it doesn't exercise true
simultaneity the way #178-180 did.

Add a stress/concurrency test comparable to #178-180: run real concurrent cancel-vs-finalize
attempts repeatedly (not just once per ordering). Assertions must permit only valid outcomes:

- cancel commits first and is visible under finalize's lock → `cancelled`.
- finalize commits first → `completed` remains historical; a later cancel does not rewrite past
  completion.

**Forbidden:** durable cancel-first followed by a `completed` overwrite; inconsistent task/job
state; deadlock or a leaked lock. Confirm both legitimate orderings actually occur across
repeated trials (or otherwise demonstrate the database synchronization mechanism directly —
e.g. by showing the lock genuinely serializes the two paths).

Do not replace #194's deterministic tests — add genuine-race coverage beside them.

---

## PHASE 5 — Upgrade #195's restart proof

Current #195: `del worker_a` → `worker_b = Worker()` → **same DB session**. This proves the
Worker Python object's own memory isn't required, but does NOT fully prove process/session
reconstruction.

Create a restart soak v3:
- Worker A uses DB Session A, runs a partial goal.
- Close Session A completely. Discard Worker A.
- Optionally dispose/recreate the session factory or engine connection where practical.
- Worker B starts with a genuinely NEW Session B.
- Reload all goal/task/job/envelope/lease/spend state from durable DB — do not pass any Python
  runtime object from A representing authority/planning/work state into B.

Prefer reconstructing Worker, Supervisor inputs, current envelopes, leases, spend authority, and
task state from canonical durable sources only. The source/worktree filesystem may persist —
that is durable effect state, not process memory, and is fine to keep.

**Required invariant: A NEW WORKER + NEW DB SESSION CAN CONTINUE SAFELY FROM DURABLE STATE
ONLY.**

---

## Process rule

Do not skip ordered security blockers because later work is more interesting. If a priority
item is judged already closed, document the exact evidence and then move on — don't silently
reorder. Otherwise land it before advancing.

**Only after Phases 1-5 merge green: proceed to the longer 8-12 task autonomous experiment.**

## Current status assessment (for reference, not to be silently trusted as still-accurate later)

```
Authority chain:            strong
Cancel semantics:           strong, but race test can be strengthened (Phase 4)
Recovery authority:         strong
Fresh-process recovery:     partially proven, not fully (Phase 5)
Provider ambiguity:         still open (Phase 1)
Crash-heal identity:        still open (Phase 2)
Long autonomy run:          wait until these are closed
```
