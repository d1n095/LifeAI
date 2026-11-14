# Canonical task lifecycle & execution authority — decision record (2026-11-14)

Founder directive ("Resolve execution authority and security gaps") asked for one canonical
decision, not another survey: "Identify one canonical task lifecycle and execution authority.
Prefer reusing existing infrastructure; provide migration/adaptation boundaries for the
others." This is that decision, grounded in a code-level trace (not names), cross-checked
against Copilot's own independent proposal on PR #406 (see that PR's comment thread —
Copilot proposed `MainAITask`/`MainAIJob` as canonical before this document was written; this
confirms, not originates, that convergence).

## The decision

**`MainAITask`/`mainai_jobs` (via `app.mainai_execution`) is the canonical task lifecycle and
execution authority.** Not a preference — the only one of the three that is live,
approval-gated, and RLS-protected today:

- `executor.dispatch_ready_task()` calls `require_task_approval()` unconditionally before
  `create_job()` (`executor.py:197`, confirmed on the call path, not just defined).
- `execution_job.py`'s `run_task_execution_job()` does real work today — real local git
  commits in an isolated worktree, real `create_pull_request()` calls — for `repo_edit`/
  `open_pr`/`run_tests` task types.
- `cancel_task()` (`executor.py:285`) is the one idempotent, audited cancel entry point:
  immediate for pending/ready/blocked/retryable_failed/waiting_ci; cooperative (via
  `app.jobs.service.request_cancel()`) for `running`.
- `mainai_tasks`/`mainai_jobs`/`mainai_goals` are all in `app/rls.py`'s owner-scoped table
  list, enforced through the restricted `mainai_app` role.

## The other two, with migration/adaptation boundaries

**`app.agent_coordination`** — NOT decommissioned, NOT merged into MainAI's own dispatch.
Its adapter abstraction (`AgentAdapter`/`LocalCLIAdapter`/`get_real_adapter()`) is the correct
long-term target for dispatching a task to a REAL external AI provider, which
`mainai_execution` cannot do today (it only ever executes in-process). But
`dispatch_assignment()` — the only function that would invoke a real adapter — has zero
callers anywhere in `app/routers/` or `app/worker.py`: built, tested, inert.

**Migration boundary, not built in this PR** (a real architecture change needing its own
reviewable increment, per the directive's own "isolated, reviewable increments" instruction):
1. Add a nullable FK from `AgentWorkAssignment` to the `mainai_jobs`/`MainAITask` row it
   executes on behalf of. Does not exist today — confirmed, no cross-reference anywhere.
2. Add a new task_type (or a `mainai_jobs.job_type` variant) in `execution_job.py` that
   routes through `agent_coordination.dispatch.dispatch_assignment()` instead of the
   in-process git-commit path, for task types that genuinely need a real external provider.
3. Until (1) exists, `AgentWorkAssignment` does not "attribute an existing MainAI task" (the
   framing Copilot's own PR #406 comment proposed) — there is no task to attribute yet. Any
   UI surfacing agent assignment should stay read-only over already-authorized MainAI
   evidence until this FK exists, exactly as Copilot's own comment on #406 already concluded
   independently.
4. `first_cycle_go` (PR #408) is wired at `get_real_adapter()` specifically because this is
   the path that would need it once (1)-(2) land — it changes nothing today since nothing
   calls it yet.

**`app.workforce`** — structurally blocked from real provider invoke by a literal
unconditional `raise ProviderActivationBlocked(...)` in `provider_worker.py` (not a config
default). What runs today instead (`_in_process_worker()`) is a keyword-matching stub that
classifies text as "personal"/"work" and returns no real effects — it does no real
engineering work and was never a competitor to the other two for that reason. No migration
boundary is proposed for it here: whether to decommission, keep as a non-engineering
personal-task lane, or revisit later is a founder decision this document does not make.

## Security Kernel — corrected finding

`docs/LIFE_SAFETY_ARCHITECTURE_VERIFICATION.md` (PR #407) reported "Security Kernel" as a
naming gap with RLS as "the closest functional equivalent." A deeper trace for this directive
found stronger evidence than that softer framing suggested: `app/db.py`'s `SessionLocal`
(used for every normal request) is bound to a connection credentialed ONLY as the restricted
`mainai_app` role — there is no code path in this application that ever holds a connection
privileged enough to bypass RLS for an ordinary request. A separate `migration_engine` exists
only for migrations/boot-time privilege verification, never for serving requests. This makes
RLS enforcement independently authoritative in the literal sense the directive asks about:
not just "correctly used application code that a bug could skip," but a security boundary a
code bug in unrelated application logic structurally cannot cross, because the role itself
holds no grant to abuse. `app.mainai_execution.approval`/`app.execution_envelopes` remain
real, correctly-enforced gates, but are same-process application code by contrast.

No module in this repository is named "Security Kernel." If the founder wants that literal
name to exist, the natural candidate to rename/document as such is `app/rls.py` plus its
boot-time privilege-verification step in `apply_mainai_execution_privileges()` — not a new
component.

## Minimal integration contract for `AgentWorkAssignment` ↔ canonical execution (Priority 4)

A second Founder security review asked for the minimal contract design, not an implementation
(kept out of this PR on purpose — a real architecture change belongs in its own reviewable
increment, same discipline as everything else recorded here). Concretely:

1. **One new column**: `AgentWorkAssignment.mainai_job_id` — nullable `UUID REFERENCES
   mainai_jobs(id) ON DELETE SET NULL`. Nullable because every assignment today (100%) has no
   such backing job; only assignments created via the future migration path below would ever
   set it. This is the join point Copilot's own question on #406 asked for, and the answer
   today is "it doesn't exist yet" — this column is what would make it exist.
2. **Single creator, same transaction**: the NEW task_type's dispatch handler in
   `execution_job.py` (not any other caller) is the only place that creates an
   `AgentWorkAssignment` row, and does so in the SAME transaction as the `mainai_jobs` row it
   backs — never created ahead of time or speculatively. This is what prevents split-brain
   state: an `AgentWorkAssignment` with `mainai_job_id` set is never orphaned from the job
   that owns it, because the two rows are born together or not at all.
3. **No parallel approval**: `task_id` becomes REQUIRED (not optional) for any assignment
   created through this path — `evaluate_dispatch_readiness()` already delegates to
   `require_task_approval()` exactly when `task_id` is set (`dispatch.py`'s own documented
   behavior, item 5). Nothing new is built; the existing gate is simply never bypassable by
   leaving `task_id` unset for this specific creation path.
4. **Ownership stays with MainAITask for cancellation, checkpoints, auditability**:
   `MainAITask.cancel_task()` (`executor.py:285`) remains the ONE entry point; for a task
   backed by an `AgentWorkAssignment`, its cooperative-cancel branch would additionally call
   `agent_coordination.service.transition_status(assignment, "cancelled")` as one more step of
   the SAME cancellation, never a second, independently-triggerable cancel path.
   `MainAICheckpoint` remains canonical; `AgentWorkAssignment.status` becomes a read-only
   reflection of where the external agent is, never a second source of truth about whether the
   work itself is done. Both `MainAITaskEvent` and `AgentWorkAssignmentEvent` continue to
   exist afterward — the job owns "what happened to the work," the assignment owns "which
   external agent handled it" — but only the new dispatch handler writes both, in that order,
   inside one transaction.

This keeps `MainAITask`/`mainai_jobs` as the sole execution authority exactly as decided above;
`agent_coordination` becomes a backing mechanism it can delegate to, never a second one.

## Code touched under this directive so far (exact SHAs)

- PR #408 (`claude/first-cycle-go-activation-gate`, now `304a03b`): `first_cycle_go` fail-closed
  gate -- extended past its first pass (`b05114f`, env-var-only) after a security review found
  that insufficient. Now: the enforcement point lives in `LocalCLIAdapter.start_assignment()`
  itself (closing the `dispatch_assignment()` bypass a call-graph trace found -- that function
  takes an adapter as a caller-supplied parameter and never calls `get_real_adapter()`), and a
  new `system_activation_authorizations` table (migration 0069, `mainai_app` REVOKEd to
  SELECT-only, no `authorize_*` function anywhere in application code) supplies the durable
  evidence an env var alone can no longer substitute for. Both conditions required, re-checked
  fresh on every real invocation attempt.
- PR #409 (`claude/compromised-worker-authority-tests`, now `105013e`): RLS isolation test for
  `agent_work_assignments` (gap found: in RLS's owner-scoped list since migration 0046, no
  dedicated test until now); regression tests proving `apply_dispatch_result()` cannot grant
  MainAITask approval or escalate past the state machine. Original push (`ccbe63e`) had two
  real CI failures -- both investigated via the actual job logs and fixed (`105013e`):
  `apply_dispatch_result()` structurally requires a real `task_id`, and a dependency-free
  assignment starts at `ready`, not `planned`.
- This document (Priority 4's integration contract added in this revision), plus a reply on
  PR #406's comment thread converging with Copilot's own independent proposal.

No merge, deploy, or release-authority change made. Founder Alpha remains NOT READY until
independently verified, per the directive's own instruction. `FIRST_CYCLE_GO` is not set and
no `system_activation_authorizations` row exists anywhere as a result of this work.
