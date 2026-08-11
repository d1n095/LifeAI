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
from app.mainai_execution.recovery_approval import RecoveryApprovalRequiredError, grant_recovery_approval
from app.mainai_execution.recovery_classifier import classify_recovery_record
from app.mainai_execution.recovery_inspector import get_or_create_recovery_record, inspect_recovery_record
from app.mainai_execution.recovery_takeover import TakeoverError, execute_takeover
from app.models.mainai_execution import (
    EngineeringLesson,
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
from app.models.mainai_recovery import AUTO_SALVAGEABLE_CLASSIFICATIONS, MainAIRecoveryRecord, MainAIRecoveryStatus
from app.models.mainai_wait import MainAITaskWait
from app.models.user import User
from app.schemas import (
    EngineeringLessonOut,
    MainAICheckpointOut,
    MainAIGoalCreateIn,
    MainAIGoalDetailOut,
    MainAIGoalOut,
    MainAIPlanOut,
    MainAIRecoveryRecordOut,
    MainAITaskDetailOut,
    MainAITaskEventOut,
    MainAITaskOut,
    MainAITaskWaitOut,
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


@router.get("/goals/{goal_id}/plans", response_model=list[MainAIPlanOut])
def list_goal_plans(goal_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require_founder)):
    """V0.3: every plan version this goal has ever had, oldest first -- the founder-facing
    replan history. `create_plan()` (planner.py) never deletes a superseded plan, only marks
    it `superseded` and versions a new one (app/mainai_execution/replan.py's automatic trigger
    or a founder-driven POST /goals/{id}/plan both go through the same function), so this is a
    real, complete history, not a reconstruction."""
    _get_goal_or_404(db, goal_id)
    return list(db.execute(select(MainAIPlan).where(MainAIPlan.goal_id == goal_id).order_by(MainAIPlan.version)).scalars())


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
        executor.cancel_task(db, task=task, cancelled_by=user.email, cancelled_by_id=user.id, reason="Cancelled by founder.")
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


# ---------------------------------------------------------------- V0.2: dead-agent recovery
#
# Deliberately minimal, matching V0.1's own "read/act, nothing exposed that isn't already a
# real durable operation" discipline. /tasks/{id}/recover is the founder-driven entry point
# that runs the real pipeline (detect -> inspect -> classify -> takeover if safe and
# approved), mirroring how POST /goals/{id}/plan is the one entry point that actually runs
# propose+create for a plan. V0.3 (app/worker.py's `_advance_mainai_execution_auto_recovery`)
# ALSO drives the same four functions unattended for a dead task_execution job's lease expiry
# -- this endpoint remains the one a founder can call directly, e.g. to force an immediate
# check rather than waiting for the next poll cycle.


@router.get("/tasks/{task_id}/recovery", response_model=list[MainAIRecoveryRecordOut])
def list_task_recovery_records(task_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require_founder)):
    _get_task_or_404(db, task_id)
    return list(
        db.execute(select(MainAIRecoveryRecord).where(MainAIRecoveryRecord.task_id == task_id).order_by(MainAIRecoveryRecord.created_at)).scalars()
    )


# ---------------------------------------------------------------- V0.3: external waits (CI etc.)


@router.get("/tasks/{task_id}/waits", response_model=list[MainAITaskWaitOut])
def list_task_waits(task_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require_founder)):
    """V0.3: every durable external-wait record (app/mainai_execution/ci_wait.py) this task has
    ever gone through -- what it's waiting for, its current status (pending/satisfied/failed/
    timed_out/cancelled), and the last poll's evidence. A task can wait more than once across
    its lifetime, so this is always the full list, oldest first."""
    _get_task_or_404(db, task_id)
    return list(db.execute(select(MainAITaskWait).where(MainAITaskWait.task_id == task_id).order_by(MainAITaskWait.created_at)).scalars())


@router.post("/tasks/{task_id}/recover", response_model=MainAIRecoveryRecordOut, status_code=status.HTTP_201_CREATED)
@limiter.limit(f"{settings.rate_limit_default_per_minute}/minute")
async def recover_task(request: Request, task_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require_founder)):
    """Runs the real recovery pipeline for a task's CURRENT `mainai_job_id` -- detect, inspect,
    classify, and (only if the classification is auto-salvageable AND no founder approval is
    outstanding) takeover. Safe to call on a task whose job is not actually dead: every
    underlying primitive (mark_job_superseded()'s row-locked re-check, the classifier's own
    evidence-only judgment) already fails closed on its own, so this endpoint adds no
    additional liveness pre-check of its own -- it would only ever duplicate a guard that
    already exists at the real point of consequence.

    Returns the recovery record either way -- a caller inspects `classification`/`status`/
    `manual_review_required` to know what happened: NOTHING_DONE..VERIFIED_WORK with no
    approval needed completes the takeover immediately; PUSHED_NO_PR/PR_EXISTS come back still
    `classified` awaiting POST /recovery/{id}/approve; CONFLICTED_STATE/UNSAFE_TO_AUTO_RECOVER
    come back `manual_review_required=True` and are never auto-continued."""
    task = _get_task_or_404(db, task_id)
    if task.mainai_job_id is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Task has no dispatched job to recover.")
    goal = _get_goal_or_404(db, task.goal_id)
    job = db.get(MainAIJob, task.mainai_job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")

    record = get_or_create_recovery_record(db, task=task, job=job)
    db.commit()
    record = await inspect_recovery_record(db, task=task, job=job, record=record)
    db.commit()
    record = classify_recovery_record(db, record=record)
    db.commit()

    if record.status == MainAIRecoveryStatus.classified and record.classification in AUTO_SALVAGEABLE_CLASSIFICATIONS:
        try:
            record, _new_job = await execute_takeover(db, task=task, goal=goal, record=record, dispatched_by=user.email)
            db.commit()
        except (RecoveryApprovalRequiredError, TakeoverError):
            db.rollback()  # only the takeover's own uncommitted work rolls back -- classify already committed above

    return record


@router.post("/recovery/{record_id}/approve", response_model=MainAIRecoveryRecordOut)
@limiter.limit(f"{settings.rate_limit_default_per_minute}/minute")
def approve_recovery(request: Request, record_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require_founder)):
    """Grants the V0.2 approval gate (recovery_approval.py) for one recovery record -- the
    dead attempt's code is already visible on GitHub (PUSHED_NO_PR/PR_EXISTS), so takeover
    does not continue on its own until a founder explicitly does this. Does not itself trigger
    a takeover -- call POST /tasks/{task_id}/recover again afterwards, same as approving a
    task (POST /tasks/{id}/approve) never dispatches it itself."""
    record = db.get(MainAIRecoveryRecord, record_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Recovery record not found.")
    grant_recovery_approval(db, record=record, approved_by=user.email)
    db.commit()
    return record


# ---------------------------------------------------------------- V0.3: engineering lessons


@router.get("/lessons", response_model=list[EngineeringLessonOut])
def list_lessons(status_filter: str | None = None, limit: int = 100, offset: int = 0, db: Session = Depends(get_db), user: User = Depends(require_founder)):
    """V0.3: founder-wide read-only view of engineering lessons (app/models/mainai_execution.py's
    EngineeringLesson) -- including `disputed` ones once app/mainai_execution/lesson_conflicts.py's
    mark_conflict() has run. Not RLS-protected / not owner-scoped, same as the model itself,
    but still requires `require_founder` like every other endpoint in this router. `status_filter`
    lets a founder ask specifically for `disputed` lessons needing a decision, without pulling
    the whole (potentially large) founder-wide table."""
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    query = select(EngineeringLesson).order_by(EngineeringLesson.created_at.desc())
    if status_filter is not None:
        from app.models.mainai_execution import EngineeringLessonStatus

        try:
            query = query.where(EngineeringLesson.status == EngineeringLessonStatus(status_filter))
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"Invalid status '{status_filter}'.") from exc
    return list(db.execute(query.limit(limit).offset(offset)).scalars())
