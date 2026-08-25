"""The production Supervisor entry point -- closes the founder-decided execution authority
chain (see docs/LIFE_EXECUTION_AUTHORIZATION_ENVELOPE.md and this module's own tests):

    active ExecutionAuthorizationEnvelope
    -> eligible MainAIGoal
    -> durable worker trigger (app/worker.py's `_advance_authorized_supervisor_goals`)
    -> reconstructed SupervisorScope   (copied ONLY from the envelope, never invented)
    -> narrower per-task WorkBindings  (never wider than the envelope's own ceiling)
    -> run_supervisor()
    -> Safe Planner / bounded local execution

AUTHORITY RECONSTRUCTION: every `SupervisorScope` field below comes directly from the
CURRENTLY ACTIVE `ExecutionAuthorizationEnvelope` row for this goal -- never from goal prose,
planner output, task_type, a `WorkCandidate` proposal's own content, or a provider response. A
goal with no current active envelope is not eligible, full stop (`eligible_authorized_goals()`
below is the one place that decides eligibility, and it is a plain read of `status = 'active'`
-- nothing here ever synthesizes a missing authorization).

STILL NOT AUTONOMOUS REPO-WRITING, on purpose, in this "smallest coherent implementation":
`scope.provider_spend_authorized` is hardcoded `False` (a bare authorized envelope never
implies provider spend -- see `SupervisorScope`'s own docstring, a founder P1 review finding
from before this module existed) and every `OperatorContext` this module builds leaves
`remote_write_authorized` at its default `False` (real GitHub pushes remain a separate,
NOT-YET-authorized capability -- see `app/development_supervisor/production_worktree.py`'s own
docstring). Without a hand-built `PlanCandidate` or a gap-derived deterministic repair recipe,
every real task this wiring reaches will legitimately defer as `PROVIDER_SPEND_NOT_AUTHORIZED`
-- an honest, safe, fully testable RUNTIME REACHABLE outcome, not a bug. Expanding either gate
is a separate, later founder act, exactly like the founder decision's own staging describes.

CRASH/RETRY/CONCURRENCY: `app/development_supervisor/lease.py`'s `supervisor_goal_leases`
(migration 0059) is the sole mutual-exclusion primitive -- at most one worker may run
`run_supervisor()` for a given goal at a time, a crashed worker's lease is only ever reclaimed
after it genuinely expires (never blindly), and `AUTHORITY NEVER INCREASES ON RETRY`: every
reconstruction of `SupervisorScope` re-reads the goal's CURRENT active envelope fresh -- if the
founder narrows, supersedes, or has not (yet) re-authorized after a prior envelope was
superseded, the very next tick sees that immediately, never a cached or assumed-still-valid
scope."""

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.development_operator.service import OperatorContext
from app.development_supervisor.lease import claim_supervisor_goal_lease, release_supervisor_goal_lease
from app.development_supervisor.production_worktree import ensure_goal_worktree_sync, worker_source_repo_root
from app.development_supervisor.service import (
    SupervisorBounds,
    SupervisorError,
    SupervisorResult,
    SupervisorScope,
    WorkBinding,
    instruction_sha256,
    run_supervisor,
)
from app.intelligence_governance import record_execution
from app.jobs.mainai_job_lease import claim_specific_mainai_job
from app.models.execution_envelope import ExecutionAuthorizationEnvelope
from app.models.mainai_execution import MainAIGoal, MainAIGoalStatus, MainAITask, MainAITaskStatus
from app.models.mainai_job import MainAIJobStatus
from app.work_intelligence import bind_strategy_execution, create_strategy

logger = logging.getLogger("mainai.development_supervisor.production_entry")

# The only two statuses `run_supervisor()`'s own `discover_candidates()`/`select_candidate()`
# could ever actually dispatch -- see that function's own status handling. Binding every other
# status would be pure dead weight: `discover_candidates()` already reports an unbound task as
# non-actionable ("execution binding is unavailable") without needing one.
_BINDABLE_TASK_STATUSES = (MainAITaskStatus.ready, MainAITaskStatus.running)

# Deliberately generous relative to SupervisorBounds.max_elapsed_seconds's own default (900s):
# the lease must outlive one full bounded run_supervisor() call with margin, or a merely-slow
# (not dead) worker would have its own lease reclaimed out from under it mid-run.
DEFAULT_SUPERVISOR_LEASE_SECONDS = 1800


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

    `envelope` MUST be the caller's own freshly-read CURRENT envelope (see
    `eligible_authorized_goals()`) -- this function trusts it as-is and does not re-fetch,
    exactly so a caller re-running eligibility on every tick (rather than caching it) is what
    keeps a narrowed/superseded/revoked authorization honored immediately, never one tick
    stale."""
    claim = claim_supervisor_goal_lease(
        db, owner_id=goal.owner_id, goal_id=goal.id, envelope_id=envelope.id, worker_id=worker_id, lease_seconds=lease_seconds
    )
    if claim is None:
        return None
    lease_id, lease_generation = claim

    try:
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
            authority_ref=str(envelope.id),
            authorized_instruction_sha256=instruction_sha256(goal.original_instruction),
            repository_identity=str(repo_root.resolve()),
            allowed_paths=tuple(envelope.authorized_paths or ()),
            allowed_capabilities=tuple(envelope.authorized_capabilities or ()),
            maximum_risk=envelope.authorized_risk,
            provider_spend_authorized=False,
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
            """The real, non-test `WorkBinding.prepare_context` -- claims the job's own
            `mainai_jobs` lease FIRST (before anything else), fencing it against the unrelated
            V0.1 `_advance_mainai_execution_tasks`/`claim_next_mainai_job` poll picking up the
            very same freshly `queued` job this Supervisor call just created via
            `dispatch_ready_task()` and double-executing it through `run_task_execution_job()`.

            A job already `running` (never `queued`) is a RESUME, not a fresh dispatch --
            `run_supervisor()`'s own two-call resume design (see
            test_two_job_chain_and_interruption_resume_are_canonical) calls `prepare_context`
            again for a task it selected but did not finish in an earlier call. This is safe to
            treat as "still ours" WITHOUT re-claiming: `supervisor_goal_leases` already
            guarantees at most one worker runs `run_supervisor()` for this goal at a time, and
            V0.1's own blind dispatch tick is excluded from every envelope-governed goal (see
            app/worker.py's `_advance_mainai_execution_tasks`) -- so a `running` job under this
            goal's own task can only ever be this same logical Supervisor session's own earlier
            claim, never a genuinely competing one. Anything else (still `queued` after a
            failed claim -- a real race; or any other status) is a genuine anomaly and stays a
            hard failure, never silently assumed safe."""
            claimed_generation = claim_specific_mainai_job(db, job_id=job.id, worker_id=worker_id, lease_seconds=lease_seconds)
            if claimed_generation is None:
                db.refresh(job)
                if job.status != MainAIJobStatus.running:
                    raise SupervisorError(f"could not claim mainai_jobs lease for job {job.id}: already claimed elsewhere")
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
        release_supervisor_goal_lease(db, lease_id=lease_id, worker_id=worker_id, lease_generation=lease_generation)
