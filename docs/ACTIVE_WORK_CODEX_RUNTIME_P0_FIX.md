# Codex Runtime P0 Fix — ACTIVE, owned by Codex

Independent examination of the frozen Codex "autonomous execution runtime" candidate has
**FAILED**. This document is the handoff contract for the fix. Owner: Codex. Do not start
this work from any other lane — see `docs/BRANCH_REGISTRY.md`'s own "no duplicate work"
discipline.

## Verdict

- **Examiner:** Claude (independent — did not build this candidate; built via four parallel
  adversarial attack forks plus the lead examiner's own direct, hand-written reproduction of
  both P0 findings).
- **Reviewed SHA:** `c1883262902622cc72871ab473808ca4969e1a87`
- **STATUS:** `INDEPENDENT_ATTACK_FAIL`
- **Full formal report:** delivered directly to the founder in-session (not yet posted to a
  PR — no open PR exists for this candidate's branches,
  `codex/mainai-continuous-supervision` / `codex/universal-personal-recall`). The directive
  below is the actionable summary; ask the founder for the full report transcript if the
  underlying root-cause detail here is insufficient to start.

## Do NOT

- Modify the frozen SHA itself (`c1883262902622cc72871ab473808ca4969e1a87`) — create a new
  branch/worktree from it instead.
- Touch: the continuous-supervision branch's own unrelated work, Claude's frozen
  `dev_director` candidate (`ab1c0ce03a7a0f7b11f2f716304f9e1235a54d3e`), the Founder
  Reasoning/Judgment candidate (merged into `claude/mainai-v2-sovereign` at `4814d78`), `#245`
  (`818dfb732da47901eb5ae06ffdd9c829fe00c4c5`), Personal Recall
  (`024835547850035667c3d77383fd75699ceab178`), `main`/`master`, or any production wiring.
- Merge, deploy, enable real providers, or self-certify the fix.

---

## P0-1 — Certification bypass

**File/function:** `backend/app/mainai_execution/substrate.py::ExecutionSubstrate.eligible_jobs()`

**Root cause:** `eligible_jobs()`'s own docstring claims it returns jobs "whose exact
dependency SHAs are completed and **certified**." The implementation only checks
`state == COMPLETED` and SHA match — it never checks certification at all.
`backend/app/mainai_execution/production_adapter.py::DependencyEngine.satisfied()` is the one
function in the codebase that actually requires `certified=True` — it has **zero callers**
anywhere in `app/` outside the pre-existing test file.

**Reproduced directly** (both by the examiner and independently by two attack forks): submit
a parent job, claim it, complete it with genuine evidence, **never call `examine()`/
`freeze()`** — `eligible_jobs()` still reports a dependent child as eligible. A second proof:
an already-certified job's artifact can move to a new, never-reviewed SHA, and
`eligible_jobs()` unblocks downstream work on it exactly as readily as the reviewed one —
nothing forces re-certification.

**Violated invariants:** BUILDER != EXAMINER, TEST PASS != CERTIFICATION, OLD REVIEW != NEW
SHA AUTHORITY, DEPENDENCY READY != CERTIFICATION AUTHORITY.

**Do not patch only the reproduced test.** Audit the entire certification path: job
readiness, dependency readiness, review state, review identity, artifact SHA, attempt, lease,
authority epoch, completion evidence, certification transition. Fix the bug class, not the
symptom — certification truth currently lives ONLY in `RuntimeOrchestrator`'s in-memory
Python dicts (`self.frozen`/`self.reviews`/`self.certified`), never persisted, structurally
disconnected from anything `eligible_jobs()` can see. Add adversarial regression tests
covering: no examiner; builder pretending to be examiner; stale examiner result; examiner
result for SHA_A reused for SHA_B; dependency marked ready but review absent; test evidence
present but certification absent; wrong job review; wrong attempt review; cancelled job;
superseded job; stale authority epoch; duplicate review events; replayed review result.
Certification must fail closed.

## P0-2 — Protected-ref bypass

**File/function:** `backend/app/mainai_execution/production_adapter.py::freeze_artifact()`
and `RuntimeOrchestrator.freeze()`

**Root cause:** `freeze_artifact()` always calls
`inspect_worktree(worktree, expected_sha=None, protected_refs=...)`. Because `expected_sha`
is hardcoded `None`, the real observed SHA is never checked against `protected_refs`'s
SHA-shaped entries — only the branch *name* is checked (`inspect_worktree`'s
`protected = branch in set(protected_refs)`). Compounding this: `RuntimeOrchestrator.freeze()`
— the method actually reachable from the runtime's own claim→freeze→examine flow — has **no
`protected_refs` parameter at all**, so even branch-name protection never applies through the
real orchestration path.

**Reproduced directly:** a real git worktree whose actual current HEAD SHA is a protected
value froze cleanly through `freeze_artifact()` when its branch name didn't separately match.
A real worktree on a branch literally named `"main"` (one of `PROTECTED_REFS`'s own entries)
froze cleanly through `RuntimeOrchestrator.freeze()` with zero rejection.

**Violated invariant:** protected-ref validation must apply at effect time, not only earlier
planning/validation time.

Audit all Git/ref mutation paths. Protect at minimum: `main`, `master`, `#245`'s protected
state, frozen-candidate refs/SHAs, configured protected branches, protected aliases, branch
names resolving to protected targets, and ref changes between validation and mutation
(TOCTOU: validate safe ref → ref moves → mutation attempt). Attack: branch alias, symbolic
ref, detached HEAD, renamed branch, same SHA reachable through an alternate ref, protected SHA
with unprotected branch name, unprotected SHA becoming protected before effect. Do not rely
only on string comparison — the effect broker must revalidate the actual target immediately
before any consequential Git mutation. Minimal fix class: thread the real observed SHA
through a protected-value check in `inspect_worktree()` independent of `expected_sha`, and add
`protected_refs` to `RuntimeOrchestrator.freeze()`'s signature, defaulting to the module's
real `PROTECTED_REFS` constant.

---

## 3. `ProductionExecutionAdapter` — build direct adversarial tests

`backend/app/mainai_execution/production_adapter.py::ProductionExecutionAdapter` (~305 lines,
the actual production seam) had **effectively zero direct test coverage before this
examination**. That is unacceptable for this layer. Testing lower-level components is not the
same as testing the effect seam. Cover directly, against this class itself, not just the
primitives it wraps: owner scope, authority revalidation, attempt fencing, lease fencing,
provider authorization, capability enforcement, completion evidence, protected refs,
Git/worktree state, cancellation, restart, duplicate execution, outbox/event effects, error
handling, wrong SHA, stale review, stale authority, cross-owner input.

One concrete gap already found: `ProductionExecutionAdapter.heartbeat()` calls
`renew_mainai_job_lease()` directly, bypassing `_job()`'s owner check — unlike `complete()`/
`fail()`, which correctly reject a forged `owner_id`. Low practical impact today (exploiting
it requires already knowing values sufficient to legitimately renew anyway), but a real
inconsistency worth closing. Also note: this class has **no `cancel()` method at all** —
cancellation currently only reaches `app.jobs.service` primitives directly, bypassing this
adapter's own surface — decide whether that's the intended shape and, if so, document it;
if not, add one with the same fencing discipline as `complete()`/`fail()`.

## 4. The claimed 500-job/1500-event soak was too shallow — do not reuse that characterization

Read directly (`tests/backend/test_execution_substrate.py::
test_postgres_three_owner_five_hundred_job_event_soak`): this test performs one single-threaded,
single-transaction bulk insert of 500 `MainAIJob` rows and 1500 hardcoded
`MainAIExecutionEvent` rows (the `"JOB_CLAIMED"` event's `lease_generation: 1` is a literal,
not derived from any real claim), then asserts only `COUNT(*) == 1500`. It never calls
`claim_next_mainai_job()`, uses zero threads, and runs entirely on the **superuser connection**
— which bypasses RLS policy checks, so it doesn't exercise RLS despite the name. It proves
Postgres can durably store the rows; it proves nothing about claim/lease concurrency,
scheduling, cancellation, provider failover, or RLS enforcement.

Build a real runtime concurrency soak exercising: submit, dependency readiness, claim, lease,
heartbeat, lease expiry, late result, cancellation, reclaim, provider failover, owner
isolation, completion, review, certification gate — with multiple real workers/schedulers
(real threads/connections, not sequential simulation). Prove: zero duplicate active claims,
zero stale-lease completion, zero cross-owner effects, zero wrong-SHA certification, zero
protected-ref mutation, zero stale-cancellation resurrection. (The examiner's own replacement
soak — 5 real threads, 3 owners, 120 jobs, every fault injected — is the shape to match or
exceed; ask the founder for it as a reference if useful, it is not part of the candidate's own
code and should not be copied verbatim, only used as a design reference.)

## 5. Zombie cancellation bug

`ExecutionSubstrate.request_cancel()` sets `cancel_requested=1`; nothing in the module ever
clears it. `_fence()` treats it as a permanent, row-level (not attempt-scoped) kill switch. If
the cancelled attempt crashes before observing cancellation, `abandon_stale()` makes the job
reclaimable again — but every future reclaim immediately and permanently fences on its first
write. Net effect: the job can never reach `CANCELLED`, `COMPLETED`, or any clean terminal
state. Reproduce the crash-during-cancellation scenario; determine the exact state transition
that causes this; fix the root cause so that after crash/restart, cancelled work remains
cancelled — no impossible intermediate state, no resurrection, no permanent unrecoverable
zombie, no stale completion accepted. Add crash-boundary regression tests.

## 6. Owner-scoping gaps

Two found: (a) `ProductionExecutionAdapter.heartbeat()`'s missing owner check (see §3); (b)
`DirectorContract.recover_incomplete_jobs()` (SQLite substrate) takes no `owner_id` and
processes every owner's jobs in one unscoped pass. Reproduce both. Audit all runtime paths for
explicit owner binding. Test three distinct real owners with colliding job IDs, attempt IDs,
artifact identifiers, event IDs, review IDs, provider state. Any cross-owner consequential
effect must fail closed.

## 7. Bug-class audit — do not patch only the reproduced repros

Search specifically for the same underlying mistake classes elsewhere in this candidate:
validation performed before an effect but not re-validated at the effect itself; dependency
state mistaken for authority; review *presence* mistaken for review *validity*; branch name
mistaken for protected-target identity; cached/stale state; owner inferred rather than
checked; event existence mistaken for authority; ORM session staleness; SHA currentness not
revalidated at the point of use.

## 8. Regression + adversarial tests, after fixes

Run: runtime tests, `ProductionExecutionAdapter` direct tests, scheduler, dependency gate,
claim/lease, completion, examiner/certification, protected refs, Git/worktree,
cancellation/recovery, owner isolation, outbox/events, provider capability, authority,
RLS/PostgreSQL — then the real concurrency soak — then `ruff`, `compileall`,
`git diff --check`. Run race-sensitive tests more than once.

## 9. Execution contract

One fix program. Do not return after fixing only P0-1. Do not return after fixing only P0-2.
Do not return after one green suite. Do not return partial while implementation-side work
remains. Keep an internal remaining-work queue. Continue until: all reproduced P0s are fixed,
all related bug classes audited, all direct effect-seam tests exist, a real claim/lease
concurrency proof exists, the cancellation zombie is fixed, owner gaps are fixed, full
regression is green, and the working tree is clean.

## 10. When complete

Commit, clean tree, record the exact new SHA. Report back in this shape:

```
STATUS: CODEX_RUNTIME_P0_FIX_CANDIDATE_READY_FOR_INDEPENDENT_REATTACK
BASE SHA: c1883262902622cc72871ab473808ca4969e1a87
NEW SHA: <fill in>

CERTIFICATION BYPASS: root cause / fix / tests
PROTECTED-REF BYPASS: root cause / fix / tests
PRODUCTION EXECUTION ADAPTER: direct tests / attack coverage
ZOMBIE CANCELLATION: root cause / fix / crash proof
OWNER SCOPING: findings / fixes / RLS proof
REAL CLAIM/LEASE CONCURRENCY: jobs / workers / schedulers / events / faults / result
BUG CLASSES AUDITED: <list>
TEST RESULTS: <pass/fail counts>

P0 REMAINING: independent reattack only
P1 REMAINING: <list>

Confirm untouched:
  Claude dev_director: ab1c0ce03a7a0f7b11f2f716304f9e1235a54d3e
  #245: 818dfb732da47901eb5ae06ffdd9c829fe00c4c5
  Personal Recall: 024835547850035667c3d77383fd75699ceab178
```

Do not merge. Do not deploy. Do not enable real providers. Do not modify the frozen
`c1883262902622cc72871ab473808ca4969e1a87` candidate. Do not self-certify — the fix candidate
goes back to an independent examiner (Claude or otherwise) before it is trusted, and the old
review result does not apply to the new SHA.
