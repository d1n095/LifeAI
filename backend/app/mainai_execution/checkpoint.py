"""Durable checkpoint/resume (migration 0032's `mainai_checkpoints`, append-only — see that
table's own trigger). A checkpoint is the ONLY thing a resumed execution attempt is allowed to
trust for "was this step already done" — never conversation/model memory, which does not
survive a worker process crash, and never an in-memory variable, which is gone the instant the
process dies.

The concrete crash this closes: a worker calls an LLM provider, gets a real (possibly
expensive, possibly non-deterministic) response, and then the process dies before that
response's resulting work is durably finished. Without a checkpoint, the only recovery is
`retry_task()` starting the WHOLE task over — a second AI call, a second local write, and (once
`github_write_enabled` is on) a second GitHub commit for work that was already correctly done
once. record_checkpoint()/latest_checkpoint_for_step() let
app/mainai_execution/execution_job.py's run_task_execution_job() recognize, on resume, that a
given mainai_jobs attempt already computed `work_result` and skip straight to verification
instead of repeating the call — see that module's own docstring for exactly where this is
wired in."""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.mainai_execution import MainAICheckpoint, MainAIGoal, MainAITask


def record_checkpoint(db: Session, *, task: MainAITask, goal: MainAIGoal, job_id: uuid.UUID, step: str, data: dict) -> MainAICheckpoint:
    """`job_id` and `step` live inside `executor_state` (the table has no dedicated columns for
    either — see migration 0032's own comment on that column) rather than as separate table
    columns, so latest_checkpoint_for_step() below is the one place that needs to know this
    shape. `plan_version` is captured from `goal` at write time — a checkpoint records what was
    true when it was written, never re-derived later from a goal that may have since been
    replanned."""
    checkpoint = MainAICheckpoint(
        goal_id=task.goal_id,
        task_id=task.id,
        owner_id=task.owner_id,
        plan_version=goal.current_plan_version,
        executor_state={"job_id": str(job_id), "step": step, **data},
    )
    db.add(checkpoint)
    db.flush()
    return checkpoint


def latest_checkpoint_for_step(db: Session, *, task_id: uuid.UUID, job_id: uuid.UUID, step: str) -> MainAICheckpoint | None:
    """The read side of the resume contract: the most recent checkpoint row for THIS EXACT
    (task, job, step) triple, or None if that step has never been durably completed for this
    attempt. Deliberately scoped to `job_id` — a checkpoint from a PREVIOUS attempt (an earlier
    mainai_jobs row for the same task, e.g. after a genuine retry_task()) must never be reused
    as if it were this attempt's own state; each dispatch is its own job_id and starts this
    lookup fresh."""
    rows = (
        db.execute(select(MainAICheckpoint).where(MainAICheckpoint.task_id == task_id).order_by(MainAICheckpoint.created_at.desc()))
        .scalars()
        .all()
    )
    job_id_str = str(job_id)
    for row in rows:
        if row.executor_state.get("job_id") == job_id_str and row.executor_state.get("step") == step:
            return row
    return None
