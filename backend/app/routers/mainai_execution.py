"""MainAI Execution Loop V0.1 — the minimal read/act API surface (see
app/mainai_execution/*, migration 0032). Founder-only (`require_founder`), same convention as
app/routers/mainai_jobs.py — owner isolation is enforced by Postgres RLS itself through the
ordinary session, never re-derived here.

Deliberately minimal, matching the founder's own explicit required list: create goal, read
goal, read current plan, list/read tasks, inspect liveness/checkpoint/verification/approvals/
blockers, read final report, grant/reject approval. The one addition beyond that literal list
is `POST /goals/{id}/plan` (AI-propose + persist a plan) -- without it there would be no way
for a goal created through this API to ever get a plan at all, since nothing else in V0.1
exposes planning over HTTP. Dispatch is NOT exposed here: app/worker.py's own
_advance_mainai_execution_tasks() poll-cycle tick already dispatches every `ready` task
automatically (see that function's own docstring) -- a manual "dispatch now" endpoint would
just be racing the worker's next poll cycle for no real benefit."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.deps import require_founder
from app.limiter import limiter
from app.mainai_execution import executor, final_report, planner
from app.mainai_execution.executor import TaskNotCancellableError, TaskNotRetryableError
from app.mainai_execution.liveness import task_liveness
from app.mainai_execution.planner import PlanCycleError, PlanValidationError
from app.models.mainai_execution import (
    MainAICheckpoint,
    MainAIGoal,
    MainAIPlan,
    MainAIPlanStatus,
    MainAITask,
    MainAITaskDependency,
    MainAITaskEvent,
    MainAITaskEventType,
)
from app.models.mainai_job import MainAIJob
from app.models.user import User
from app.schemas import (
    MainAICheckpointOut,
    MainAIGoalCreateIn,
    MainAIGoalDetailOut,
    MainAIGoalOut,
    MainAIPlanOut,
    MainAITaskDetailOut,
    MainAITaskEventOut,
    MainAITaskOut,
)

settings = get_settings()

router = APIRouter(prefix="/api/mainai/execution", tags=["mainai-execution"], dependencies=[Depends(require_founder)])


def _get_goal_or_404(db: Session, goal_id: uuid.UUID) -> MainAIGoal:
    goal = db.get(MainAIGoal, goal_id)
    if goal is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Goal not found.")
    return goal


def _get_task_or_404(db: Session, task_id: uuid.UUID) -> MainAITask:
    task = db.get(MainAITask, task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found.")
    return task


def _task_out(db: Session, task: MainAITask) -> MainAITaskOut:
    job = db.get(MainAIJob, task.mainai_job_id) if task.mainai_job_id is not None else None
    out = MainAITaskOut.model_validate(task)
    out.liveness = task_liveness(task, job).value
    return out


@router.post("/goals", response_model=MainAIGoalOut, status_code=status.HTTP_201_CREATED)
@limiter.limit(f"{settings.rate_limit_default_per_minute}/minute")
def create_goal(request: Request, payload: MainAIGoalCreateIn, db: Session = Depends(get_db), user: User = Depends(require_founder)):
    from app.models.mainai_execution import MainAIGoalRiskLevel

    try:
        risk_level = MainAIGoalRiskLevel(payload.risk_level)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"Invalid risk_level '{payload.risk_level}'.") from exc

    goal = planner.create_goal(
        db,
        owner_id=user.id,
        title=payload.title,
        original_instruction=payload.original_instruction,
        created_by="founder",
        risk_level=risk_level,
        approval_policy=payload.approval_policy,
    )
    db.commit()
    return goal


@router.get("/goals", response_model=list[MainAIGoalOut])
def list_goals(limit: int = 50, offset: int = 0, db: Session = Depends(get_db), user: User = Depends(require_founder)):
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    return list(db.execute(select(MainAIGoal).order_by(MainAIGoal.created_at.desc()).limit(limit).offset(offset)).scalars())


@router.get("/goals/{goal_id}", response_model=MainAIGoalDetailOut)
def get_goal(goal_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require_founder)):
    goal = _get_goal_or_404(db, goal_id)
    plan = db.execute(select(MainAIPlan).where(MainAIPlan.goal_id == goal_id, MainAIPlan.status == MainAIPlanStatus.active)).scalar_one_or_none()
    tasks = db.execute(select(MainAITask).where(MainAITask.goal_id == goal_id).order_by(MainAITask.created_at)).scalars().all()
    return MainAIGoalDetailOut(**MainAIGoalOut.model_validate(goal).model_dump(), plan=plan, tasks=[_task_out(db, t) for t in tasks])


@router.post("/goals/{goal_id}/plan", response_model=MainAIPlanOut, status_code=status.HTTP_201_CREATED)
@limiter.limit(f"{settings.rate_limit_default_per_minute}/minute")
async def propose_and_create_plan(request: Request, goal_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require_founder)):
    goal = _get_goal_or_404(db, goal_id)
    try:
        specs, _provider, _model = await planner.propose_plan_via_ai(db, goal=goal)
        plan = planner.create_plan(db, goal=goal, rationale="AI-proposed plan", tasks=specs, created_by="founder")
    except PlanValidationError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    except PlanCycleError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    db.commit()
    return plan


@router.get("/goals/{goal_id}/report")
def get_goal_report(goal_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require_founder)):
    goal = _get_goal_or_404(db, goal_id)
    try:
        report = final_report.record_final_report(db, goal=goal)
    except final_report.GoalNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Goal not found.") from exc
    db.commit()
    return report


@router.get("/tasks/{task_id}", response_model=MainAITaskDetailOut)
def get_task(task_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require_founder)):
    task = _get_task_or_404(db, task_id)
    events = db.execute(select(MainAITaskEvent).where(MainAITaskEvent.task_id == task_id).order_by(MainAITaskEvent.created_at.desc())).scalars().all()
    checkpoints = db.execute(select(MainAICheckpoint).where(MainAICheckpoint.task_id == task_id).order_by(MainAICheckpoint.created_at.desc())).scalars().all()
    depends_on = db.execute(select(MainAITaskDependency.depends_on_task_id).where(MainAITaskDependency.task_id == task_id)).scalars().all()
    approval_granted = any(e.event_type == MainAITaskEventType.approval_granted for e in events)
    return MainAITaskDetailOut(
        **_task_out(db, task).model_dump(),
        events=[MainAITaskEventOut.model_validate(e) for e in events],
        checkpoints=[MainAICheckpointOut.model_validate(c) for c in checkpoints],
        depends_on=list(depends_on),
        approval_granted=approval_granted,
    )


@router.get("/goals/{goal_id}/tasks", response_model=list[MainAITaskOut])
def list_goal_tasks(goal_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require_founder)):
    _get_goal_or_404(db, goal_id)
    tasks = db.execute(select(MainAITask).where(MainAITask.goal_id == goal_id).order_by(MainAITask.created_at)).scalars().all()
    return [_task_out(db, t) for t in tasks]


@router.post("/tasks/{task_id}/approve", response_model=MainAITaskDetailOut)
@limiter.limit(f"{settings.rate_limit_default_per_minute}/minute")
def approve_task(request: Request, task_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require_founder)):
    from app.mainai_execution.approval import grant_task_approval

    task = _get_task_or_404(db, task_id)
    grant_task_approval(db, task=task, approved_by=user.email)
    db.commit()
    return get_task(task_id, db=db, user=user)


@router.post("/tasks/{task_id}/reject", response_model=MainAITaskDetailOut)
@limiter.limit(f"{settings.rate_limit_default_per_minute}/minute")
def reject_task(request: Request, task_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require_founder)):
    """"Reject" an approval-pending (or otherwise not-yet-running) task -- there is no
    separate approval-denial state in the closed vocabulary; rejecting a task that will never
    be approved is, correctly, the same durable outcome as cancelling it (see
    executor.cancel_task()'s own docstring for exactly which statuses this applies to -- a
    `running` task cannot be rejected this way, same honest limitation as /cancel below)."""
    task = _get_task_or_404(db, task_id)
    try:
        executor.cancel_task(db, task=task, cancelled_by=user.email, reason="Approval rejected by founder.")
    except TaskNotCancellableError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    db.commit()
    return get_task(task_id, db=db, user=user)


@router.post("/tasks/{task_id}/cancel", response_model=MainAITaskDetailOut)
@limiter.limit(f"{settings.rate_limit_default_per_minute}/minute")
def cancel_task(request: Request, task_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require_founder)):
    task = _get_task_or_404(db, task_id)
    try:
        executor.cancel_task(db, task=task, cancelled_by=user.email, reason="Cancelled by founder.")
    except TaskNotCancellableError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    db.commit()
    return get_task(task_id, db=db, user=user)


@router.post("/tasks/{task_id}/retry", response_model=MainAITaskDetailOut)
@limiter.limit(f"{settings.rate_limit_default_per_minute}/minute")
def retry_task(request: Request, task_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require_founder)):
    task = _get_task_or_404(db, task_id)
    try:
        executor.retry_task(db, task=task)
    except TaskNotRetryableError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    db.commit()
    return get_task(task_id, db=db, user=user)
