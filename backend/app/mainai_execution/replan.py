"""MainAI V0.3 -- minimal, real automatic replan trigger. V0.1's planner.py already fully
implements the MECHANICS of a replan (create_plan() called again for a goal that already has
an active plan supersedes it, cancels its still-non-terminal tasks, and starts a fresh,
versioned plan) -- what V0.1 never had was anything that DECIDED when a replan is actually
warranted and DID it unattended. This module is exactly that decision + trigger, reusing
planner.py's own propose_plan_via_ai()/create_plan() completely unchanged.

Approval escalation is not a new mechanism either: a replan produces brand-new MainAITask rows
with brand-new ids, and app/mainai_execution/approval.py's require_task_approval() looks up
`approval_granted` MainAITaskEvent rows by the task's OWN id -- so a prior approval can
structurally never apply to a replanned task, regardless of whether its risk increased. This is
the same "new plan version = new tasks = old approval never carries over" property
docs/MAINAI_EXECUTION_LOOP_V0_1.md's planner section already established; V0.3 relies on it
rather than reimplementing it."""

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.mainai_execution.planner import create_plan, propose_plan_via_ai
from app.models.mainai_execution import (
    MainAIGoal,
    MainAIPlan,
    MainAIPlanStatus,
    MainAITask,
    MainAITaskEvent,
    MainAITaskEventType,
    MainAITaskStatus,
)

logger = logging.getLogger("mainai.execution")

# Bounds the UNATTENDED replan tick only -- a founder's own explicit replan/re-plan action
# (app.routers.mainai_execution's propose_and_create_plan route) calls create_plan() directly,
# never through find_replan_trigger(), so founder authority to keep iterating on a goal by hand
# is never limited by this constant. Matches app.safe_planner.service's own max_replans=3
# precedent for the same "give up after N automatic attempts" shape, in a different module.
MAX_AUTO_REPLANS = 3


def find_replan_trigger(db: Session, *, goal: MainAIGoal) -> MainAITask | None:
    """Returns the first permanently `failed` task (attempts exhausted -- see
    execution_job.py's `_finalize_task_outcome()`) in `goal`'s CURRENTLY ACTIVE plan, or None
    if nothing warrants a replan. Deliberately scoped to the active plan only: a failed task
    from an already-superseded plan version was already handled by THAT plan's own
    supersession (planner.py's create_plan()), and re-triggering on it again would be
    pointless -- its own replan already happened.

    `blocked` tasks are deliberately NOT a trigger here -- they are a downstream CONSEQUENCE of
    a failed dependency (app/mainai_execution/graph.py's recompute_task_readiness()), not an
    independent signal; the failed task itself is the one real root cause worth replanning
    around.

    BOUNDED (V1 readiness fix): `goal.current_plan_version - 1` is the number of automatic
    replans already performed for this goal (the very first plan, version 1, is not itself a
    replan). Once that count reaches MAX_AUTO_REPLANS, this returns None even if a failed task
    exists -- without this bound, a genuinely unworkable goal (references a nonexistent file,
    requires a permanently-unavailable capability, or a model that keeps proposing the same
    broken approach) would replan forever, once per worker tick, indefinitely: every iteration
    is a real, billed propose_plan_via_ai() call, with no counter or escalation anywhere in
    this path to ever stop it or surface it to a founder. Refusing here is enough on its own --
    the failed task stays `failed` (not silently swallowed or retried), so the worker's own
    _finalize_mainai_execution_goals tick closes the goal to MainAIGoalStatus.failed with a
    real final_outcome report on its very next pass (record_final_report() already treats
    `failed` as terminal), turning an invisible infinite loop into a concrete, founder-
    discoverable outcome with no new schema or escalation mechanism needed."""
    if goal.current_plan_version - 1 >= MAX_AUTO_REPLANS:
        return None
    active_plan = db.execute(
        select(MainAIPlan).where(MainAIPlan.goal_id == goal.id, MainAIPlan.status == MainAIPlanStatus.active)
    ).scalar_one_or_none()
    if active_plan is None:
        return None
    return (
        db.execute(
            select(MainAITask)
            .where(MainAITask.plan_id == active_plan.id, MainAITask.status == MainAITaskStatus.failed)
            .order_by(MainAITask.completed_at.asc())
        )
        .scalars()
        .first()
    )


async def trigger_replan(db: Session, *, goal: MainAIGoal, failed_task: MainAITask, dispatched_by: str) -> MainAIPlan:
    """Proposes a fresh plan (a real AI call, same provider chain every other planning step
    already uses) and persists it via create_plan() -- which handles supersession, stale-task
    cancellation, and re-promoting dependency-free tasks to `ready` entirely on its own,
    unchanged from V0.1. Records a `replanned` event on the failed task that triggered this --
    the durable link between "this specific failure" and "this specific new plan version"."""
    tasks, provider, model = await propose_plan_via_ai(db, goal=goal)
    plan = create_plan(
        db,
        goal=goal,
        rationale=f"Automatic replan after task {failed_task.id} ({failed_task.task_type}) failed permanently: {failed_task.description[:200]}",
        tasks=tasks,
        created_by=dispatched_by,
    )
    db.add(
        MainAITaskEvent(
            task_id=failed_task.id,
            owner_id=failed_task.owner_id,
            event_type=MainAITaskEventType.replanned,
            detail={"new_plan_version": plan.version, "provider": provider, "model": model},
        )
    )
    db.flush()
    return plan
