# Three parallel agent-execution systems — boundary map (2026-11-15)

Founder directive: "Investigate the three parallel agent execution systems and document their
actual authority boundaries, shared capabilities and integration gaps. Do not create another
dispatcher or unilaterally change security authority." This document is mapping only — no code
changed, no new dispatcher, no authority changed. Every claim below is cited to an exact
file:line and was verified by reading the actual code on the branches named, not inferred from
docstrings or prior reports alone. It builds on, and in two places corrects/extends,
`docs/LIFE_EXECUTION_AUTHORITY_CANONICAL_MAPPING.md` (branch `claude/execution-authority-canonical-mapping`,
PR #410) and PR #406's own research note in `docs/BRANCH_REGISTRY.md`.

Branches read: `claude/det-kommer-mer-879lcm` (dev tip), `claude/first-cycle-go-activation-gate`
(PR #408), `claude/execution-authority-canonical-mapping` (PR #410),
`claude/mainai-integration-context-lessons` (PR #414, which itself carries PR #377/#380/#381's
code). All file:line citations below are against `backend/app/...` on these branches as of
2026-11-15; a path with no branch noted is present on dev tip.

---

## 1. `app.agent_coordination`

**What creates a unit of work.** `create_work_assignment()` (`app/agent_coordination/service.py:328`)
creates an `AgentWorkAssignment` row. No authorization check inside the function itself beyond
duplicate-work detection (`is_duplicate_canonical_work`, same file) — any caller with a DB
session can call it. In practice: **nothing in the live application ever calls it.** Confirmed
by `grep -rn "create_work_assignment(" app/ --include="*.py"` returning only its own definition,
docstring mentions, and test files — zero real callers anywhere in `app/routers/`, `app/worker.py`,
or any other module's production code path.

**What executes/dispatches.** `dispatch_assignment()` (`app/agent_coordination/dispatch.py:187`)
is the only function that invokes a real external adapter
(`adapter.start_assignment(assignment.id)`, same file, inside the function body). It is a pure
library function — no HTTP route, no worker-loop caller. `grep -rn "dispatch_assignment("`
across `app/` finds it mentioned only in docstrings/comments of sibling files
(`execution_control.py:192`, `adapters.py:97,149`) — never an actual call.

**Authority check between creation and execution.** `evaluate_dispatch_readiness()`
(`dispatch.py`, called at the top of `dispatch_assignment()`) checks capability match, lease
requirement, and — only when `assignment.task_id is not None` — delegates to
`app.mainai_execution.approval.require_task_approval()`. Since nothing ever sets `task_id` on a
real assignment (see §4 below), this delegation path has never fired outside tests. Below that,
`app.agent_coordination.activation_gate.require_system_activation_authority()`
(PR #408, `activation_gate.py`) is the actual-effect gate: enforced inside
`LocalCLIAdapter.start_assignment()` itself (PR #408, `adapters.py`), requiring BOTH
`FIRST_CYCLE_GO=true` and a current row in `system_activation_authorizations` (a table
`mainai_app` cannot write to — migration 0069). Neither is set anywhere in this repository as of
this writing.

**Failure/recovery.** `dispatch_assignment()` distinguishes `ProviderNotConfiguredError`/
`AdapterProcessLostError`/`AdapterTimeoutError`/any-other-exception, transitioning the
assignment to `blocked` with a structured reason before propagating (`dispatch.py:260-280`, cited
in this session's own earlier security-review work). `AgentScopeLease` provides lease-based
concurrency fencing (`service.py`). None of this has ever run against a real external process in
production — there is no live caller to trigger it.

**HTTP/worker reachability.** Zero. `grep -rl "agent_coordination" app/routers/*.py` returns
nothing. The only non-test, non-docstring reference anywhere outside the package itself is
`app/worker.py:37`, which imports `expire_stale_active_leases` — a cleanup/lease-expiry
function, not dispatch. `bootstrap_known_agents()` (`app/agent_coordination/bootstrap.py:75`,
exported from the package `__init__.py`) is **also never called** — not from `app/main.py`'s
startup sequence, not from anywhere. The entire module is reachable only from its own test
suite.

---

## 2. `app.workforce`

**What creates a unit of work.** A `WorkforceAssignment` row (`app/models/workforce.py:134`) is
created via the delegation/broker machinery (`broker.py`, `selector.py`) reached from
`staff_manager.decide_staffing()` (imported by `app/mainai_executive/loop.py:40`). No HTTP route
or worker-loop entry point calls `mainai_executive.loop` either (see below) — so in practice
nothing creates a real `WorkforceAssignment` outside tests today.

**What executes/dispatches.** `provider_worker.py`'s real-provider path is **structurally
unreachable by construction**, not by configuration default: `_in_process_worker()`
(`app/workforce/provider_worker.py:268`) is preceded by an unconditional
`raise ProviderActivationBlocked(...)` (lines 241, 257, 261) — there is no code path, env var,
or flag that currently lets real provider execution proceed. What DOES run when a
`WorkforceAssignment` is processed today is a keyword-matching classification stub
(confirmed by PR #410's own trace, re-verified here) that does no real engineering work.

**Authority check.** `app.workforce.authority.TaskScopedAuthority` /
`require_live_assignment_authority()` (`app/workforce/authority.py:16,58`) is workforce's OWN,
independent scope/authority primitive — explicit allowed-paths/tool-classes/spend-ceiling/
expiry, checked against a live, non-revoked, non-expired `WorkforceAssignment`. It shares **no
code, no shared base class, no cross-reference** with either
`app.mainai_execution.approval.require_task_approval()` or
`app.agent_coordination.activation_gate`. Separately, `app.workforce.activation_gates`
(`app/workforce/activation_gates.py`) is **yet another, independently-named "activation gate"**
— `ActivationGateSet`/`GateStatus`/`record_gate_verification()`, keyed by PR-number strings
(`"pr_218_consequential_confirmation"`, etc., lines 26-33) and raising the SAME
`ProviderActivationBlocked` exception `provider_worker.py` raises unconditionally. This is a
**third, structurally unrelated activation-gate implementation**, confusingly similar in name
and purpose to PR #408's `agent_coordination.activation_gate`, sharing no code with it.

**Failure/recovery.** `app/workforce/failure.py`, `lifecycle.py`, `kill_switch.py` provide their
own event/lifecycle/kill-switch primitives, independent of `app.mainai_execution`'s
checkpoint/recovery machinery (§3) and `app.agent_coordination`'s lease-fencing (§1).

**HTTP/worker reachability.** Zero direct router references (`grep -rl "workforce" app/routers/*.py`
returns nothing beyond a stale `.pyc`). The only real, source-level callers from outside the
package are four files in `app/mainai_executive/` (`loop.py`, `dashboard.py`, `observability.py`,
`safe_composed_run.py`), reaching `workforce.kill_switch`/`org_view`/`staff_manager`/
`vertical_slice`. **`app.mainai_executive` itself has zero router, worker, or `main.py` startup
callers for these specific submodules** — confirmed by `grep -rln "mainai_executive" app/ --include="*.py"`
outside its own package, which finds only `app/session_context/assembler.py` (importing
`mainai_executive.continuity`, a disjoint, unrelated checkpoint-read submodule — not `loop.py`
or any workforce-touching file) and `app/mainai_school/offline.py` (a dynamic
`importlib.import_module("app.mainai_executive")` import-health check, not a functional call
into `loop.py`). **The entire `mainai_executive.loop → workforce.staff_manager/kill_switch`
chain is unreachable from any live HTTP request, scheduled worker tick, or app-startup path
today**, exactly like `app.agent_coordination`.

---

## 3. MainAI's own self-execution (`app.mainai_execution`)

**What creates a unit of work.** `planner.create_goal()`/`create_plan()`
(`app/mainai_execution/planner.py`), reached live via `POST /api/mainai/goals` and
`POST /api/mainai/goals/{id}/plan` (`app/routers/mainai_execution.py:91,129`, both gated by
`Depends(require_founder)`).

**What executes/dispatches.** `executor.dispatch_ready_task()` (`app/mainai_execution/executor.py:143`)
— confirmed still at this exact function, now line 143 (PR #410 cited line 197 for the
approval call specifically, which still holds, see below). This is genuinely live: besides any
future router caller, **`app/worker.py:447` calls it directly** —
`dispatch_ready_task(db, task=task, goal=goal, dispatched_by="mainai_worker_auto_advance")` —
inside the background worker's auto-advance loop. This is the one of the three systems with a
real, scheduled, non-HTTP execution trigger in addition to its API surface.

**Authority check.** `require_task_approval(db, task=task, goal_approval_policy=goal.approval_policy)`
— confirmed at `executor.py:197`, called first, before `create_job()` (line 208) — matching PR
#410's own citation exactly. `app.mainai_execution.approval.require_task_approval()`
(`approval.py:94`, its own docstring: "THE enforcement point") is the sole approval primitive
for this system — no parallel approval path exists inside `app.mainai_execution` itself.

**Provider/adapter abstraction: confirmed NONE.** PR #406's claim holds on direct inspection:
`execution_job.py`'s `run_task_execution_job()` performs real work in-process (local git
commits in an isolated worktree, real `create_pull_request()` calls for `repo_edit`/`open_pr`/
`run_tests` task types) — there is no `AgentAdapter`-style indirection anywhere in this module.
It cannot dispatch to an external AI provider process the way `agent_coordination.adapters`
could; it only ever executes its own in-process Python code.

**Failure/recovery.** The most built-out of the three: `recovery_classifier.py`,
`recovery_salvage.py`, `recovery_takeover.py`, `recovery_approval.py`,
`mainai_job_lease.py`-based dead-worker fencing, and `cancel_task()`
(`executor.py:285`, confirmed) as the single idempotent, audited cancel entry point — immediate
for pending/ready/blocked/retryable_failed/waiting_ci, cooperative (via
`app.jobs.service.request_cancel()`) for `running`. `recovery_takeover.py:133` itself calls
`executor.dispatch_ready_task()` — recovery reuses the SAME dispatch function, not a parallel
one.

**HTTP/worker reachability.** Both: `app/routers/mainai_execution.py`,
`execution_envelopes.py`, `project_entities.py` all route into this system under
`require_founder`, AND `app/worker.py` drives `dispatch_ready_task()` on a schedule. **This is
the only one of the three systems genuinely live in production today.**

---

## 4. Convergence analysis

### Already shared

- **RLS/owner_id isolation.** All three systems' core tables are present in `app/rls.py`'s
  owner-scoped verification lists (`agent_work_assignments`/`coordination_agents`:
  3 references; `workforce_assignments`/`workforce_teams`: 2; `mainai_tasks`/`mainai_jobs`/
  `mainai_goals`: 13), enforced through the same single restricted `mainai_app` Postgres role
  (`app/db.py`'s `SessionLocal`, per PR #410's own correct finding — re-verified unchanged
  here). This is genuine, structural convergence: a cross-tenant bug in any one system would be
  caught by the exact same database-level mechanism as the other two.
- **Postgres as the single store.** No system maintains a separate queue/cache/external store —
  all three persist exclusively to the same Postgres database via SQLAlchemy.

### Real, previously undocumented convergence point (dormant)

- **`WorkforceAssignment.coordination_assignment_id`** (`app/models/workforce.py:140`, added in
  migration `0067_workforce_foundation.py:157`) is a nullable `UUID` column whose name strongly
  suggests it was meant to reference an `app.agent_coordination.AgentWorkAssignment` row. It
  carries **no FK constraint** (confirmed: the migration declares it as a bare `uuid` column,
  no `REFERENCES`), **no index**, and — confirmed by `grep -rn "coordination_assignment_id" app/`
  returning only its own single declaration — **is never read or written by any code anywhere**.
  This is a structural placeholder for exactly the kind of workforce↔agent_coordination link the
  founder's directive is asking about, already present in the schema, entirely unused. Neither
  PR #406's research nor PR #410's canonical-mapping document mentioned this column; it was
  found during this investigation.

### Real duplication (not yet reconciled)

- **Three independent authority/approval primitives**, none sharing code:
  1. `app.mainai_execution.approval.require_task_approval()` — the one actually enforced in
     production (§3).
  2. `app.agent_coordination.activation_gate` (env var + DB row) — built, tested, never
     exercised outside tests (§1).
  3. `app.workforce.authority.TaskScopedAuthority`/`require_live_assignment_authority()` — a
     third, independent scope-envelope model (§2).
- **Two independently-named "activation gate" concepts** that are not the same mechanism:
  `app.agent_coordination.activation_gate.require_system_activation_authority()` (PR #408: env
  var + durable DB row, two conditions) vs. `app.workforce.activation_gates.ActivationGateSet`
  (PR-number-keyed verification flags, `record_gate_verification()`). Both exist to answer "is
  real external execution authorized," both raise/guard against unauthorized activation, neither
  references the other.
- **No shared execution-record schema.** `AgentWorkAssignmentEvent`, and MainAI's own
  `MainAITaskEvent`/`MainAICheckpoint`, and workforce's own lifecycle-event tables
  (`WorkforceLifecycleEvent` et al.) are three independent audit-trail shapes for conceptually
  the same kind of fact ("what happened to this unit of work").

### Real integration gaps (confirmed absent, not assumed)

- `AgentWorkAssignment.task_id` (`app/models/agent_coordination.py:174`) exists and is nullable,
  but is never actually set by any live code path to a real `MainAITask.id` — re-confirming PR
  #410's finding.
- `WorkforceAssignment.coordination_assignment_id` exists and is never set (new finding, above).
- No table or code path anywhere links a `WorkforceAssignment` to a `MainAITask`/`MainAIJob`
  row, in either direction.
- PR #414's own new `app.workforce.task_dispatch_preparation.prepare_agent_task_context()`
  (built this session, integrating PR #377/#380/#381) is itself, as of this writing, **also
  uncalled by anything** — confirmed via `grep -rn "prepare_agent_task_context(" app/` finding
  only its own definition. It composes session-context + integration-lessons + task-context
  packet-building, but nothing yet calls it to actually prepare a dispatch. This is the same
  "built and tested, inert" pattern as `dispatch_assignment()` and
  `dispatch_agent_task_context()` themselves (the latter: confirmed zero callers anywhere,
  including in docstrings) — not a criticism of PR #414, which was explicit that it stops short
  of live dispatch, but worth naming plainly in this map rather than leaving implicit.

---

## 5. Open questions for founder decision

This document does not answer these — they are architecture/authority decisions, not mapping
conclusions, and the directive explicitly asks that they not be resolved unilaterally here.

1. **Should `MainAITask`/`mainai_jobs` become the single execution authority, with
   `agent_coordination` and `workforce` reduced to backing mechanisms it delegates to** (the
   direction PR #410 already sketched a migration boundary for, Priority 4's integration
   contract) — **or should `workforce` remain permanently separate** for a different use case
   (e.g. the "non-engineering personal-task lane" PR #410 raised as an open possibility, never
   decided)?
2. **What should happen to the two independent activation-gate implementations**
   (`agent_coordination.activation_gate` vs. `workforce.activation_gates`)? Converge them into
   one, keep both for genuinely different purposes, or deprecate one? Converging them is itself
   a security-authority change this document is not authorized to make.
3. **Is `WorkforceAssignment.coordination_assignment_id` a dead column to drop, or the intended
   join point once real wiring is built?** If the latter, it would need an actual `ForeignKey`
   constraint added — a schema change, not something this document proposes.
4. **Does `app.workforce`'s real-provider path (`ProviderActivationBlocked`) ever get
   unblocked**, or is it intentionally permanent (i.e., `app.workforce` exists only for the
   non-real, keyword-classification stub use case, by design)? PR #410 explicitly left this
   undecided; it remains undecided here too.
5. **Is `app.mainai_executive` (the `loop.py`/`dashboard.py`/`staff_manager` layer sitting
   between `app.workforce` and nothing else today) meant to become a live, scheduled entry point
   eventually, or is it itself dormant scaffolding that should be explicitly marked as such?**
   This investigation found it has zero live callers for its workforce-facing submodules, a
   fact not previously documented anywhere found in this repository's own docs.

No merge, deploy, or authority change made by this document. `first_cycle_go=false`. Founder
Alpha remains NOT READY.
