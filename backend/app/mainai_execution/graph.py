"""Task graph readiness selection for the MainAI Execution Loop V0.1 (see app/mainai_execution/
planner.py for how the graph itself is created, and migration 0032's docstring for why cycle
detection lives at plan-creation time, not here)."""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.mainai_execution import (
    TERMINAL_MAINAI_TASK_STATUSES,
    MainAITask,
    MainAITaskDependency,
    MainAITaskEvent,
    MainAITaskEventType,
    MainAITaskStatus,
)

# Terminal-but-not-completed states a dependency can land in that should block (not silently
# stall) a dependent task -- `cancelled` is included because a replan's supersession
# (planner.py's create_plan()) cancels a stale task's still-pending siblings, and anything that
# depended on one of those should become visibly `blocked`, not sit in `pending` forever with
# no diagnostic signal.
_DEPENDENCY_FAILURE_STATUSES = TERMINAL_MAINAI_TASK_STATUSES - {MainAITaskStatus.completed}


def recompute_task_readiness(db: Session, *, goal_id: uuid.UUID) -> list[MainAITask]:
    """Scans every `pending` task in `goal_id`'s current task set and either:
      - promotes it to `ready` if ALL of its dependencies are `completed`, or
      - moves it to `blocked` (with a blocker_reason and a `blocked` event) if ANY dependency
        is `failed`/`cancelled` -- a dependency that will never complete must never leave its
        dependent silently `pending` forever, or
      - leaves it `pending` if dependencies are still in flight.

    Idempotent -- safe to call after plan creation (planner.py's create_plan()) AND after
    every task completion/failure (see executor.py). Returns the tasks newly promoted to
    `ready` this call (the set the caller should consider dispatching)."""
    pending_tasks = (
        db.execute(select(MainAITask).where(MainAITask.goal_id == goal_id, MainAITask.status == MainAITaskStatus.pending))
        .scalars()
        .all()
    )

    newly_ready: list[MainAITask] = []
    for task in pending_tasks:
        dependency_ids = (
            db.execute(select(MainAITaskDependency.depends_on_task_id).where(MainAITaskDependency.task_id == task.id))
            .scalars()
            .all()
        )

        if not dependency_ids:
            task.status = MainAITaskStatus.ready
            db.add(MainAITaskEvent(task_id=task.id, owner_id=task.owner_id, event_type=MainAITaskEventType.ready, detail={}))
            newly_ready.append(task)
            continue

        dep_rows = db.execute(select(MainAITask.id, MainAITask.status).where(MainAITask.id.in_(dependency_ids))).all()

        blocking_dep = next((row for row in dep_rows if row.status in _DEPENDENCY_FAILURE_STATUSES), None)
        if blocking_dep is not None:
            task.status = MainAITaskStatus.blocked
            task.blocker_reason = f"Depends on task {blocking_dep.id}, which is {blocking_dep.status.value}."
            db.add(
                MainAITaskEvent(
                    task_id=task.id,
                    owner_id=task.owner_id,
                    event_type=MainAITaskEventType.blocked,
                    detail={"blocking_task_id": str(blocking_dep.id), "blocking_task_status": blocking_dep.status.value},
                )
            )
            continue

        all_completed = len(dep_rows) == len(dependency_ids) and all(row.status == MainAITaskStatus.completed for row in dep_rows)
        if all_completed:
            task.status = MainAITaskStatus.ready
            db.add(MainAITaskEvent(task_id=task.id, owner_id=task.owner_id, event_type=MainAITaskEventType.ready, detail={}))
            newly_ready.append(task)
        # else: still waiting on at least one in-flight dependency -- stays `pending`.

    if pending_tasks:
        db.flush()
    return newly_ready


def next_ready_task(db: Session, *, owner_id: uuid.UUID) -> MainAITask | None:
    """Picks the single highest-priority `ready` task across all of an owner's goals, for
    dispatch (see executor.py's dispatch_ready_task()). Ties broken by created_at ascending
    (oldest first) -- deterministic, never dependent on incidental row-scan order."""
    return db.execute(
        select(MainAITask)
        .where(MainAITask.owner_id == owner_id, MainAITask.status == MainAITaskStatus.ready)
        .order_by(MainAITask.priority.desc(), MainAITask.created_at.asc())
        .limit(1)
    ).scalar_one_or_none()
