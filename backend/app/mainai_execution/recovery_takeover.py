"""V0.2 recovery pipeline stage 5: takeover — the ONE place old and new executors' authority
formally switches over. Orchestrates, in this exact order:

  1. reset_task_for_takeover() (executor.py) — the dead job's task, still stuck at `running`,
     moves back to `ready` WITHOUT fabricating a pass/fail verdict (a dead job proves nothing
     about whether the work would have succeeded).
  2. dispatch_ready_task() (executor.py) — the SAME, unmodified V0.1 dispatch function any
     ordinary task uses. Mints a genuinely new `mainai_jobs` row with its own id and its own
     lease_generation, starting fresh exactly like any other job (no parallel lease/fencing
     mechanism invented for it — see migration 0034's own docstring for why task_execution is
     excluded from blind reclaim in the first place).
  3. salvage_recovery_record() (recovery_salvage.py, already reviewed/committed) — copies the
     evidence classify_recovery_record() already determined was safe to carry forward, into
     the new job that now exists. Ordering note: the founder's own pipeline list reads
     "salvage -> takeover", which this follows at the conceptual level — classification fully
     determines WHAT to salvage before this function ever runs; only the physical act of
     writing those checkpoints needs the new job's id to exist, which step 2 just created.
  4. mark_job_superseded() (app/jobs/mainai_job_lease.py) — the dead job's honest terminal
     outcome. Re-verifies atomically that it is STILL `running` with a genuinely expired lease
     before writing anything; refuses (surfacing as TakeoverError here) if that is no longer
     true — e.g. a concurrent takeover already superseded it, or it was somehow not actually
     dead. This is the fencing half of "old executor becomes authoritative-dead": once
     superseded, `status != 'running'` for every check that matters (renew_mainai_job_lease's
     own WHERE clause, the checkpoint fencing in execution_job.py) — a worker that was somehow
     still alive and tries to act on it from this point on is rejected exactly like any other
     stale-lease write.
  5. The recovery record itself moves detected->...->classified->taking_over->taken_over, with
     `takeover_executor`/`takeover_job_id` recorded — a durable, append-only
     (mainai_recovery_events) chain of exactly what happened and when."""

from datetime import datetime

from sqlalchemy.orm import Session

from app.jobs.mainai_job_lease import JobNotSupersedableError, mark_job_superseded
from app.mainai_execution import executor
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
) -> tuple[MainAIRecoveryRecord, MainAIJob]:
    if record.status != MainAIRecoveryStatus.classified:
        raise TakeoverError(f"recovery record {record.id} is not classified (status={record.status}) -- cannot take over.")
    if record.classification not in AUTO_SALVAGEABLE_CLASSIFICATIONS:
        raise TakeoverError(
            f"recovery record {record.id} classification {record.classification} is not auto-salvageable -- refusing takeover."
        )

    dead_job_id = record.job_id

    record_recovery_event(db, record=record, event_type=MainAIRecoveryEventType.takeover_started, detail={"dispatched_by": dispatched_by})

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
