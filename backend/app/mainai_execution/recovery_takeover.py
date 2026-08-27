"""V0.2 recovery pipeline stage 5: takeover — the ONE place old and new executors' authority
formally switches over. Orchestrates, in this exact order:

  0. require_recovery_approval() (recovery_approval.py) — the approval gate, checked BEFORE
     any mutation, exactly like dispatch_ready_task()'s own require_task_approval() check.
     Distinct decision from that task-level gate: not "is this task's content allowed to run"
     but "is it safe to let this autonomous pass take over a dead job without a founder
     looking at it first". Only PUSHED_NO_PR/PR_EXISTS require it by default (the dead
     attempt's code is already visible on GitHub) — see that module's own docstring.
  0.5. RECOVERY MUST NEVER INCREASE OR BYPASS AUTHORITY (governance gate, this module):
     locks the goal row (`SELECT ... FOR UPDATE`) FIRST -- the same row
     `app.execution_envelopes.service.authorize_execution_scope()` now also locks before
     creating/superseding an envelope -- then `goal_has_ever_been_envelope_governed()`. If the
     owning goal has EVER had an `execution_authorization_envelopes` row, this function
     refuses to hand the dead job to V0.1's `dispatch_ready_task()` at all and takes the
     `_decline_takeover_for_governed_goal` branch below instead. The lock (not a bare re-SELECT
     immediately before dispatch, which still leaves a window) is what makes this decision
     race-free against a founder authorizing a goal's FIRST-EVER envelope concurrently -- see
     the lock's own inline comment and `authorize_execution_scope()`'s matching docstring for
     the full reasoning, and `_decline_takeover_for_governed_goal`'s own docstring further
     down for the decline branch itself.
  1. reset_task_for_takeover() (executor.py) — the dead job's task, still stuck at `running`,
     moves back to `ready` WITHOUT fabricating a pass/fail verdict (a dead job proves nothing
     about whether the work would have succeeded).
  2. dispatch_ready_task() (executor.py) — the SAME, unmodified V0.1 dispatch function any
     ordinary task uses. Mints a genuinely new `mainai_jobs` row with its own id and its own
     lease_generation, starting fresh exactly like any other job (no parallel lease/fencing
     mechanism invented for it — see migration 0034's own docstring for why task_execution is
     excluded from blind reclaim in the first place). ONLY reached for a NEVER_GOVERNED goal
     — see 0.5 above.
  3. salvage_recovery_record() (recovery_salvage.py, already reviewed/committed) — copies the
     evidence classify_recovery_record() already determined was safe to carry forward, into
     the new job that now exists. Ordering note: the founder's own pipeline list reads
     "salvage -> takeover", which this follows at the conceptual level — classification fully
     determines WHAT to salvage before this function ever runs; only the physical act of
     writing those checkpoints needs the new job's id to exist, which step 2 just created.
  4. mark_job_superseded() (app/jobs/mainai_job_lease.py) — the dead job's honest terminal
     outcome. Re-verifies atomically that it is STILL either (a) `running` with a genuinely
     expired lease, or (b) already terminal (`failed`/`completed`/`cancelled`) while the
     owning task remains stuck `running` (TaskLiveness.dead without finalize) before writing
     anything; refuses (surfacing as TakeoverError here) if that is no longer true — e.g. a
     concurrent takeover already superseded it, or it was somehow not actually dead. This is
     the fencing half of "old executor becomes authoritative-dead": once superseded,
     `status != 'running'` for every check that matters (renew_mainai_job_lease's own WHERE
     clause, the checkpoint fencing in execution_job.py) — a worker that was somehow still
     alive and tries to act on it from this point on is rejected exactly like any other
     stale-lease write.
  5. The recovery record itself moves detected->...->classified->taking_over->taken_over, with
     `takeover_executor`/`takeover_job_id` recorded — a durable, append-only
     (mainai_recovery_events) chain of exactly what happened and when.

GOVERNED DECLINE (0.5 above): for an EVER_GOVERNED goal, steps 2-4 above are replaced by
`_decline_takeover_for_governed_goal()` — the task still returns to `ready` (step 1, unchanged)
but NO new `mainai_jobs` row is minted and NO salvage runs (both are meaningless without a
V0.1-executed job to attach to). The dead job is instead finalized `failed` (no successor to
record) via `mark_job_failed_after_governed_recovery_decline()`, and the recovery record moves
detected->...->classified->taking_over->completed (never `taken_over` — no takeover occurred).
The goal's OWN Supervisor tick (`app.worker.py`'s `_advance_authorized_supervisor_goals`,
driven by `eligible_authorized_goals()`) rediscovers the now-`ready` task on its own schedule
and redispatches it through `prepare_context()`'s real `OperatorContext` binding, re-validated
against whatever the CURRENT active envelope authorizes at THAT later moment — never this dead
attempt's (possibly stale, possibly since-narrowed/revoked) authority. If there is no current
active envelope at that point, `eligible_authorized_goals()` simply will not surface the goal
at all — correctly stopping, not falling back to V0.1, matching the same EVER_GOVERNED +
NO_ACTIVE => STOP invariant PR #154 already established for ordinary V0.1 auto-dispatch."""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.execution_envelopes.service import goal_has_ever_been_envelope_governed
from app.jobs.mainai_job_lease import (
    JobNotDeclinableError,
    JobNotSupersedableError,
    mark_job_failed_after_governed_recovery_decline,
    mark_job_superseded,
)
from app.mainai_execution import executor
from app.mainai_execution.recovery_approval import require_recovery_approval
from app.mainai_execution.recovery_inspector import record_recovery_event
from app.mainai_execution.recovery_salvage import salvage_recovery_record
from app.models.mainai_execution import MainAIGoal, MainAITask
from app.models.mainai_job import MainAIJob
from app.models.mainai_recovery import (
    AUTO_SALVAGEABLE_CLASSIFICATIONS,
    MainAIRecoveryEventType,
    MainAIRecoveryRecord,
    MainAIRecoveryStatus,
)


class TakeoverError(RuntimeError):
    pass


async def execute_takeover(
    db: Session, *, task: MainAITask, goal: MainAIGoal, record: MainAIRecoveryRecord, dispatched_by: str
) -> tuple[MainAIRecoveryRecord, MainAIJob | None]:
    if record.status != MainAIRecoveryStatus.classified:
        raise TakeoverError(f"recovery record {record.id} is not classified (status={record.status}) -- cannot take over.")
    if record.classification not in AUTO_SALVAGEABLE_CLASSIFICATIONS:
        raise TakeoverError(
            f"recovery record {record.id} classification {record.classification} is not auto-salvageable -- refusing takeover."
        )
    # V0.2 approval gate (recovery_approval.py): PUSHED_NO_PR/PR_EXISTS mean the dead
    # attempt's code is already visible on GitHub -- a real external side effect -- so
    # `standard_recovery` requires an explicit founder approval before takeover proceeds,
    # checked here BEFORE any mutation, mirroring dispatch_ready_task()'s own
    # require_task_approval() call at the very top of its function.
    require_recovery_approval(db, record=record)

    dead_job_id = record.job_id

    record_recovery_event(db, record=record, event_type=MainAIRecoveryEventType.takeover_started, detail={"dispatched_by": dispatched_by})

    # FIRST-GOVERNANCE TOCTOU FENCE: lock the goal row BEFORE the EVER_GOVERNED decision and
    # hold it through whichever branch below actually runs (V0.1 dispatch or governed
    # decline) -- see app.execution_envelopes.service.authorize_execution_scope()'s matching
    # lock and docstring for the exact race this closes. A bare re-SELECT immediately before
    # dispatch does NOT close this window (the gap between that SELECT and the dispatch write
    # is itself still racy); only a lock held by BOTH sides across their own consequential
    # transition serializes the two orderings so "governance becomes effective mid-flight of
    # an already-decided legacy dispatch" is structurally impossible.
    db.execute(
        select(MainAIGoal).where(MainAIGoal.id == goal.id, MainAIGoal.owner_id == goal.owner_id).with_for_update()
    ).scalar_one()

    if goal_has_ever_been_envelope_governed(db, owner_id=goal.owner_id, goal_id=goal.id):
        return await _decline_takeover_for_governed_goal(db, task=task, record=record, dead_job_id=dead_job_id)

    executor.reset_task_for_takeover(db, task=task)
    new_job = executor.dispatch_ready_task(db, task=task, goal=goal, dispatched_by=dispatched_by)

    # salvage_recovery_record() requires `record.status == classified` (its own precondition,
    # already reviewed/committed) -- it internally advances classified -> salvaging -> salvaged.
    # This function's own `taking_over`/`taken_over` transitions happen AFTER, once salvage's
    # own work is durably done, so the two functions' status contracts never collide.
    await salvage_recovery_record(db, task=task, goal=goal, record=record, new_job=new_job)

    record.status = MainAIRecoveryStatus.taking_over
    db.add(record)
    db.flush()

    try:
        mark_job_superseded(db, job_id=dead_job_id, superseded_by_job_id=new_job.id)
    except JobNotSupersedableError as exc:
        raise TakeoverError(f"could not fence the dead job {dead_job_id}: {exc}") from exc

    record.status = MainAIRecoveryStatus.taken_over
    record.takeover_executor = dispatched_by
    record.takeover_job_id = new_job.id
    db.add(record)
    db.flush()
    record_recovery_event(
        db, record=record, event_type=MainAIRecoveryEventType.takeover_completed,
        detail={"dead_job_id": str(dead_job_id), "new_job_id": str(new_job.id)},
    )

    record.status = MainAIRecoveryStatus.completed
    record.completed_at = datetime.utcnow()
    db.add(record)
    db.flush()

    return record, new_job


async def _decline_takeover_for_governed_goal(
    db: Session, *, task: MainAITask, record: MainAIRecoveryRecord, dead_job_id
) -> tuple[MainAIRecoveryRecord, None]:
    """RECOVERY MUST NEVER INCREASE OR BYPASS AUTHORITY. This goal has (or has ever had) an
    execution-authorization envelope, so V0.1's generic `dispatch_ready_task()` — which runs a
    `task_execution` job through `app.mainai_execution.execution_job.py`'s own executor, with
    NONE of `SupervisorScope`'s `allowed_paths`/`allowed_capabilities`/`maximum_risk`
    narrowing applied — must never resume it, regardless of whether this call came from the
    automatic dead-agent-recovery tick or a founder's own `POST /tasks/{id}/recover`
    (`execute_takeover()` is the ONE choke point both go through; this decline branch protects
    both callers identically without either needing its own governance check).

    `reset_task_for_takeover()` still runs — same "no fabricated verdict" honesty as an
    ordinary takeover's step 1 — but nothing dispatches the task. `salvage_recovery_record()`
    is skipped entirely: every action it takes (checkpoint copy-forward, worktree rebind) is
    keyed to a NEW `mainai_jobs` row for V0.1's OWN checkpoint-based resume contract
    (execution_job.py), which does not exist and would not be read by anything in this branch
    — Supervisor's own execution path uses a completely separate, goal-scoped checkpoint
    mechanism (`app.development_supervisor.service._checkpoint`/`_latest_state`), so there is
    nothing here for V0.1-shaped salvage to usefully attach to. This intentionally does NOT
    attempt to reconstruct Supervisor authority/checkpoints itself — the goal's own next
    `run_supervisor()` tick already does that correctly; duplicating it here would be a second,
    subtly-different implementation of the same authority-reconstruction logic."""
    executor.reset_task_for_takeover(db, task=task)

    record.status = MainAIRecoveryStatus.taking_over
    db.add(record)
    db.flush()

    try:
        mark_job_failed_after_governed_recovery_decline(db, job_id=dead_job_id)
    except JobNotDeclinableError as exc:
        raise TakeoverError(f"could not fence the dead job {dead_job_id}: {exc}") from exc

    record_recovery_event(
        db, record=record, event_type=MainAIRecoveryEventType.takeover_declined_governed,
        detail={
            "dead_job_id": str(dead_job_id),
            "reason": "goal is execution-envelope-governed; task returned to ready for Supervisor's own governed redispatch, not V0.1",
        },
    )

    record.status = MainAIRecoveryStatus.completed
    record.completed_at = datetime.utcnow()
    db.add(record)
    db.flush()

    return record, None
