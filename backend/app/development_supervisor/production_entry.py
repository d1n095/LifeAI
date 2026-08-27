"""The production Supervisor entry point -- closes the founder-decided execution authority
chain (see docs/LIFE_EXECUTION_AUTHORIZATION_ENVELOPE.md and this module's own tests):

    active ExecutionAuthorizationEnvelope
    -> eligible MainAIGoal
    -> durable worker trigger (app/worker.py's `_advance_authorized_supervisor_goals`)
    -> reconstructed SupervisorScope   (copied ONLY from the envelope, never invented)
    -> per-task WorkBindings           (bounded by the envelope's own ceiling -- see note below)
    -> run_supervisor()
    -> Safe Planner / bounded local execution

PRECISION NOTE (found by adversarial review after this module first merged): every
`WorkBinding.allowed_paths` / `OperatorContext.allowed_paths` built here is set to the FULL
`scope.allowed_paths` -- i.e. the entire goal-level ceiling -- for every task, not something
individually narrower per task. This is NEVER wider than the founder-authorized envelope (the
actual authority invariant this whole foundation exists to enforce), but it is not yet true
task-level delegation either, and earlier wording in this docstring and in
docs/LIFE_SUPERVISOR_PRODUCTION_ENTRY.md overstated it as "narrower." The honest reason:
`MainAITask` (app/models/mainai_execution.py) has no `allowed_paths`/`allowed_capabilities`
field at all today -- there is no real per-task signal to narrow FROM. Inventing one by
parsing `task.description`/`task_type` would violate the founder decision's own "task_type may
suggest, never authorize" principle and this module's own "never derive authority from goal
prose" rule -- so until a real, explicit, founder-traceable per-task scope signal exists
(likely a future Safe-Planner-derived or explicitly-authorized field), giving every task the
full goal ceiling is the correct, honest, fail-closed choice, not a shortcut being hidden.

AUTHORITY RECONSTRUCTION: every `SupervisorScope` field below comes directly from the
CURRENTLY ACTIVE `ExecutionAuthorizationEnvelope` row for this goal -- never from goal prose,
planner output, task_type, a `WorkCandidate` proposal's own content, or a provider response. A
goal with no current active envelope is not eligible, full stop (`eligible_authorized_goals()`
below is the one place that decides eligibility, and it is a plain read of `status = 'active'`
-- nothing here ever synthesizes a missing authorization).

STILL NOT AUTONOMOUS REMOTE WRITE, on purpose:
every `OperatorContext` this module builds leaves `remote_write_authorized` at its default
`False` (real GitHub pushes remain a separate, NOT-YET-authorized capability -- see
`app/development_supervisor/production_worktree.py`'s own docstring).

PROVIDER SPEND is no longer a hardcoded False: `scope.provider_spend_authorized` is derived
ONLY from a matching live founder-granted provider-spend authorization for this owner + goal
+ current envelope (`provider_spend_is_live`). No grant / revoked / expired / exhausted /
wrong-envelope → False (fail closed). A bare authorized envelope never implies spend.

Without a live spend grant (and without a hand-built `PlanCandidate` or gap-derived
deterministic repair), provider-assisted planning still parks as
`PROVIDER_SPEND_NOT_AUTHORIZED` — honest and wakeable after founder authorize.

CRASH/RETRY/CONCURRENCY: `app/development_supervisor/lease.py`'s `supervisor_goal_leases`
(migration 0059) is the sole mutual-exclusion primitive -- at most one worker may run
`run_supervisor()` for a given goal at a time, a crashed worker's lease is only ever reclaimed
after it genuinely expires (never blindly), and `AUTHORITY NEVER INCREASES ON RETRY`: every
reconstruction of `SupervisorScope` re-reads the goal's CURRENT active envelope fresh -- if the
founder narrows, supersedes, or has not (yet) re-authorized after a prior envelope was
superseded, the very next tick sees that immediately, never a cached or assumed-still-valid
scope."""

import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.development_operator.service import COMMAND_PROFILES, OperatorContext
from app.development_supervisor.lease import (
    claim_supervisor_goal_lease,
    release_supervisor_goal_lease,
    renew_supervisor_goal_lease,
)
from app.development_supervisor.production_worktree import (
    ensure_goal_worktree_sync,
    reset_goal_worktree_to_clean_head,
    worker_source_repo_root,
)
from app.development_supervisor.service import (
    SupervisorBounds,
    SupervisorError,
    SupervisorResult,
    SupervisorScope,
    WorkBinding,
    instruction_sha256,
    run_supervisor,
)
from app.execution_envelopes import get_current_execution_envelope
from app.intelligence_governance import record_execution
from app.jobs.mainai_job_lease import claim_specific_mainai_job
from app.models.execution_envelope import ExecutionAuthorizationEnvelope
from app.models.mainai_execution import MainAIGoal, MainAIGoalStatus, MainAITask, MainAITaskStatus
from app.models.mainai_job import MainAIJobStatus
from app.provider_spend import provider_spend_is_live
from app.work_intelligence import bind_strategy_execution, create_strategy

logger = logging.getLogger("mainai.development_supervisor.production_entry")

# The only two statuses `run_supervisor()`'s own `discover_candidates()`/`select_candidate()`
# could ever actually dispatch -- see that function's own status handling. Binding every other
# status would be pure dead weight: `discover_candidates()` already reports an unbound task as
# non-actionable ("execution binding is unavailable") without needing one.
_BINDABLE_TASK_STATUSES = (MainAITaskStatus.ready, MainAITaskStatus.running)

# SupervisorBounds.max_elapsed_seconds (default 900s) only bounds the OUTER while loop between
# task attempts -- it is not a watchdog that can interrupt a SINGLE already-running operator
# action. The real ceiling on how long the goal lease must survive is the longest single
# action any capability could actually take: app.development_operator.service.COMMAND_PROFILES'
# own timeout_seconds (full_backend_pytest is currently 1800s -- previously EQUAL to this
# constant, a real bug found by adversarial review: a worst-case single action could run right
# up to, or past, the lease's own expiry, letting a second worker reclaim the goal while the
# first was still legitimately executing). Derived from COMMAND_PROFILES itself (never a
# hand-typed guess) so a future longer profile automatically widens this margin too, and
# doubled for real headroom beyond the theoretical worst case.
_MAX_SINGLE_OPERATOR_ACTION_SECONDS = max(profile.timeout_seconds for profile in COMMAND_PROFILES.values())
DEFAULT_SUPERVISOR_LEASE_SECONDS = _MAX_SINGLE_OPERATOR_ACTION_SECONDS * 2


def eligible_authorized_goals(db: Session, *, limit: int = 20) -> list[tuple[MainAIGoal, ExecutionAuthorizationEnvelope]]:
    """The one place that decides "eligible for autonomous Supervisor execution": a goal must
    be `running` (has actionable work per `record_final_report()`'s own rollup -- a `waiting`/
    `blocked`/terminal goal has nothing a Supervisor tick could usefully do right now) AND have
    a CURRENTLY ACTIVE authorization envelope for the SAME owner. A goal that has never been
    authorized, or whose envelope was superseded/rejected and never re-authorized, simply never
    appears here -- fail closed by construction, not by an extra check someone could forget."""
    rows = db.execute(
        select(MainAIGoal, ExecutionAuthorizationEnvelope)
        .join(
            ExecutionAuthorizationEnvelope,
            (ExecutionAuthorizationEnvelope.goal_id == MainAIGoal.id)
            & (ExecutionAuthorizationEnvelope.owner_id == MainAIGoal.owner_id)
            & (ExecutionAuthorizationEnvelope.status == "active"),
        )
        .where(MainAIGoal.status == MainAIGoalStatus.running)
        .order_by(MainAIGoal.created_at.asc())
        .limit(limit)
    ).all()
    return [(goal, envelope) for goal, envelope in rows]


async def run_authorized_goal_supervisor_tick(
    db: Session,
    *,
    goal: MainAIGoal,
    envelope: ExecutionAuthorizationEnvelope,
    worker_id: str,
    lease_seconds: int = DEFAULT_SUPERVISOR_LEASE_SECONDS,
    bounds: SupervisorBounds | None = None,
) -> SupervisorResult | None:
    """Runs (at most) one bounded `run_supervisor()` call for `goal`, under a fenced
    `supervisor_goal_leases` claim. Returns `None` (nothing done this tick) when: another
    worker already holds this goal's lease, there is currently no `ready`/`running` task to
    bind, or the SupervisorScope itself is rejected by `validate_scope()` before any task is
    even attempted -- these are not errors, they are legitimate "nothing to do right now"
    outcomes a caller must not retry-with-force.

    `envelope` is the caller's own recently-read envelope from `eligible_authorized_goals()`
    -- but this function does NOT simply trust it: a real gap exists between that read and the
    goal lease actually being claimed (a founder could narrow/revoke/supersede the envelope in
    that window), so it is RE-VERIFIED against the current DB state immediately after the goal
    lease is claimed, and again at every task-dispatch boundary inside `prepare_context` (a
    revocation mid-run, across several dispatched tasks, is caught just as fast as one caught
    before the run even starts). Only the re-verified envelope is ever used to build
    `SupervisorScope` -- never the caller-supplied object directly."""
    claim = claim_supervisor_goal_lease(
        db, owner_id=goal.owner_id, goal_id=goal.id, envelope_id=envelope.id, worker_id=worker_id, lease_seconds=lease_seconds
    )
    if claim is None:
        return None
    lease_id, lease_generation = claim

    def _reverify_authority() -> ExecutionAuthorizationEnvelope:
        """The TOCTOU close: re-reads the goal's CURRENT active envelope fresh and requires it
        to be the EXACT SAME row `eligible_authorized_goals()` handed the caller. Raises
        (never silently substitutes a different envelope, and never continues under the
        stale one) if the founder narrowed, revoked, or superseded authorization since --
        whether that happened before this tick even started or between two of its own
        dispatched tasks makes no difference, both are the same failure."""
        current = get_current_execution_envelope(db, owner_id=goal.owner_id, goal_id=goal.id)
        if current is None or current.id != envelope.id:
            raise SupervisorError(
                f"execution authorization for goal {goal.id} changed since eligibility was read "
                f"(expected active envelope {envelope.id}); refusing to execute under stale authority"
            )
        return current

    try:
        current_envelope = _reverify_authority()

        tasks = (
            db.execute(
                select(MainAITask)
                .where(
                    MainAITask.owner_id == goal.owner_id,
                    MainAITask.goal_id == goal.id,
                    MainAITask.status.in_(_BINDABLE_TASK_STATUSES),
                )
                .order_by(MainAITask.priority.desc(), MainAITask.created_at.asc(), MainAITask.id.asc())
            )
            .scalars()
            .all()
        )
        if not tasks:
            return None

        repo_root, base_sha, branch = ensure_goal_worktree_sync(
            goal_id=goal.id, source_repo_root=worker_source_repo_root()
        )

        scope = SupervisorScope(
            owner_id=goal.owner_id,
            goal_id=goal.id,
            authority_kind="authorized_goal",
            authority_ref=str(current_envelope.id),
            authorized_instruction_sha256=instruction_sha256(goal.original_instruction),
            repository_identity=str(repo_root.resolve()),
            allowed_paths=tuple(current_envelope.authorized_paths or ()),
            allowed_capabilities=tuple(current_envelope.authorized_capabilities or ()),
            maximum_risk=current_envelope.authorized_risk,
            provider_spend_authorized=provider_spend_is_live(
                db,
                owner_id=goal.owner_id,
                goal_id=goal.id,
                execution_envelope_id=current_envelope.id,
            ),
        )

        strategy = create_strategy(
            db,
            owner_id=goal.owner_id,
            strategy_key=f"authorized-supervisor:{goal.id}",
            version=1,
            work_category="repository_work",
            idempotency_key=f"authorized-supervisor-strategy:{goal.id}",
        )

        def prepare_context(task: MainAITask, job) -> OperatorContext:
            """The real, non-test `WorkBinding.prepare_context`.

            Two things happen here BEFORE anything else, on every single task attempt within
            this bounded run_supervisor() call, not just once at entry:

            1. Re-verify authority (`_reverify_authority()`) -- a founder narrowing/revoking
               the envelope midway through a multi-task run must stop the NEXT dispatch just as
               fast as it would stop a run that had not started yet. `scope` itself was already
               built from whatever the FIRST call returned; a real (not yet built) narrower
               scope would require aborting and letting the next tick pick it up fresh anyway,
               so this only needs to detect "authority changed", not reconstruct a new scope
               mid-run.
            2. Renew the goal lease (`renew_supervisor_goal_lease`) -- SupervisorBounds.
               max_elapsed_seconds only bounds the OUTER while loop BETWEEN task attempts, it
               cannot interrupt a single already-running operator action (some of which, e.g.
               `full_backend_pytest`, can legitimately take up to
               COMMAND_PROFILES['full_backend_pytest'].timeout_seconds). Renewing right before
               each task's own action begins resets the lease's clock to a full fresh window
               for THAT action specifically, so DEFAULT_SUPERVISOR_LEASE_SECONDS's own margin
               (see that constant's docstring) only ever has to cover one action's worst case,
               never the accumulated time of a whole multi-task run.

            THEN claims the job's own `mainai_jobs` lease, fencing it against the unrelated
            V0.1 `_advance_mainai_execution_tasks`/`claim_next_mainai_job` poll picking up the
            very same freshly `queued` job this Supervisor call just created via
            `dispatch_ready_task()` and double-executing it through `run_task_execution_job()`.

            A job already `running` (never `queued`) is a RESUME candidate, not necessarily a
            fresh dispatch -- `run_supervisor()`'s own two-call resume design (see
            test_two_job_chain_and_interruption_resume_are_canonical) calls `prepare_context`
            again for a task it selected but did not finish in an earlier call. `
            supervisor_goal_leases` guarantees at most one worker runs `run_supervisor()` for
            this goal AT ANY GIVEN MOMENT, but NOT across the gap BETWEEN ticks -- the goal
            lease is released in this function's own `finally` block at the end of EVERY tick,
            so a task deferred (not completed) in tick N leaves its `mainai_jobs` row `running`
            under tick N's worker for up to the FULL `mainai_jobs` lease TTL, independent of the
            goal lease. A later tick (this same worker resuming its own recent claim, OR a
            genuinely different worker, e.g. after a process restart) can therefore legitimately
            observe a `running` job it did not itself just claim.

            The ONLY safe resume is the SAME worker_id, within its OWN still-valid lease window
            -- verified here explicitly (locked_by AND lease_expires_at), never assumed from
            goal-lease possession or job.status alone. A different worker_id, or an expired
            lease, is treated as a hard failure: the established `task_execution`
            stale-lease/takeover pipeline (app/mainai_execution/recovery_takeover.py) is the
            ONLY path allowed to transfer ownership of an expired task_execution job -- this
            function must never invent a second, silent reclaim path by copying whatever
            lease_generation happens to be on the row.

            A FRESH claim (not a resume) additionally resets the shared goal worktree to its
            last clean commit (`reset_goal_worktree_to_clean_head`) before this brand-new task
            touches anything -- an EARLIER, DIFFERENT task under this same goal may have left
            uncommitted changes behind (a `patch_file`/`create_file` step that succeeded but
            whose plan never reached its own `commit_scoped_changes` step, e.g. because
            verification failed or the tick itself raised). Every real write already fails
            closed on an unexpected `before_sha256` (app.development_operator.service.
            write_file()), so that leftover mess could never silently corrupt a later task's
            own write -- but without this reset it WOULD incorrectly block/fail every later
            task under the goal indefinitely, since nothing else ever cleans this shared
            directory between distinct attempts. Deliberately NOT done on resume: a resume's
            entire point is continuing exactly where THIS SAME task's own prior attempt left
            off, uncommitted changes included."""
            _reverify_authority()
            renew_supervisor_goal_lease(db, lease_id=lease_id, worker_id=worker_id, lease_generation=lease_generation, lease_seconds=lease_seconds)
            claimed_generation = claim_specific_mainai_job(db, job_id=job.id, worker_id=worker_id, lease_seconds=lease_seconds)
            if claimed_generation is not None:
                reset_goal_worktree_to_clean_head(repo_root)
            if claimed_generation is None:
                db.refresh(job)
                still_valid = (
                    job.status == MainAIJobStatus.running
                    and job.locked_by == worker_id
                    and job.lease_expires_at is not None
                    and job.lease_expires_at > datetime.utcnow()
                )
                if not still_valid:
                    raise SupervisorError(
                        f"could not claim mainai_jobs lease for job {job.id}: not queued, and not a "
                        "still-valid resume of this same worker's own earlier claim"
                    )
                claimed_generation = job.lease_generation
            execution = record_execution(
                db,
                owner_id=goal.owner_id,
                task_id=task.id,
                job_id=job.id,
                idempotency_key=f"authorized-supervisor-execution:{task.id}:{job.id}",
            )
            binding_row = bind_strategy_execution(
                db,
                owner_id=goal.owner_id,
                strategy_id=strategy.id,
                execution_id=execution.id,
                idempotency_key=f"authorized-supervisor-binding:{task.id}:{job.id}",
            )
            return OperatorContext(
                owner_id=goal.owner_id,
                task_id=task.id,
                job_id=job.id,
                worker_id=worker_id,
                lease_generation=claimed_generation,
                repository_root=repo_root,
                expected_base_sha=base_sha,
                expected_branch=branch,
                strategy_execution_id=binding_row.id,
                worktree_id=None,
                allowed_paths=scope.allowed_paths,
                allowed_capabilities=scope.allowed_capabilities,
            )

        bindings = tuple(
            WorkBinding(
                task_id=task.id,
                prepare_context=prepare_context,
                repository_identity=scope.repository_identity,
                allowed_paths=scope.allowed_paths,
            )
            for task in tasks
        )

        return await run_supervisor(db, scope=scope, bindings=bindings, worker_id=worker_id, bounds=bounds or SupervisorBounds())
    finally:
        # A False return here means this worker's OWN lease was no longer the active one by
        # the time this tick tried to release it -- e.g. an operator action that legitimately
        # ran long enough to outlive the TTL despite the per-task renewal in prepare_context(),
        # or a genuine bug. Never a correctness problem (the fenced UPDATE already guarantees
        # this call can never release a DIFFERENT worker's now-current claim) -- but silently
        # swallowing it would erase the one signal available to notice a worker's lease was
        # lost mid-run, which is exactly the "detect lost ownership at a consequential
        # boundary" this codebase already expects at every other lease-fenced write.
        if not release_supervisor_goal_lease(db, lease_id=lease_id, worker_id=worker_id, lease_generation=lease_generation):
            logger.warning(
                "Worker %s: supervisor_goal_lease %s (goal %s) was no longer this worker's own active "
                "lease by the time this tick tried to release it -- likely reclaimed after an unexpectedly "
                "long-running operator action outlived the TTL.",
                worker_id, lease_id, goal.id,
            )
