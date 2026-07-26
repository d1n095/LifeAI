"""MainAI Core: agent orchestration API surface (see app/agent_orchestration.py for the
actual logic). Founder-only, like app/routers/memory.py — this dispatches real provider calls
and, when explicitly enabled, real GitHub writes, so it is never exposed to anyone but the
founder."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.agent_orchestration import (
    attempt_auto_merge,
    create_agent_task,
    dispatch_task,
    get_task,
    list_task_events,
    list_tasks,
    prepare_github_pr,
    record_test_results,
    review_task,
)
from app.audit import record_audit
from app.db import get_db
from app.deps import require_founder
from app.models.user import User
from app.providers.base import ProviderError
from app.schemas import (
    AgentTaskCreateIn,
    AgentTaskDetailOut,
    AgentTaskEventOut,
    AgentTaskOut,
    PrepareGithubPrIn,
    RecordTestResultsIn,
)

router = APIRouter(prefix="/api/admin/agents", tags=["agents"], dependencies=[Depends(require_founder)])


@router.post("/tasks", response_model=AgentTaskOut)
def create_task_route(payload: AgentTaskCreateIn, request: Request, db: Session = Depends(get_db), user: User = Depends(require_founder)):
    task = create_agent_task(
        db,
        title=payload.title,
        description=payload.description,
        acceptance_criteria=payload.acceptance_criteria,
        target_files=payload.target_files,
        constraints=payload.constraints,
        required_tests=payload.required_tests,
        source_note_id=payload.source_note_id,
        created_by=user.email,
    )
    record_audit(db, user_id=user.id, action="agent_task_created", entity_type="agent_task", entity_id=str(task.id), detail=task.title, request=request)
    return task


@router.get("/tasks", response_model=list[AgentTaskOut])
def list_tasks_route(db: Session = Depends(get_db)):
    return list_tasks(db)


@router.get("/tasks/{task_id}", response_model=AgentTaskDetailOut)
def get_task_route(task_id: uuid.UUID, db: Session = Depends(get_db)):
    try:
        task = get_task(db, task_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    events = list_task_events(db, task_id)
    return AgentTaskDetailOut(**AgentTaskOut.model_validate(task).model_dump(), events=[AgentTaskEventOut.model_validate(e) for e in events])


@router.post("/tasks/{task_id}/dispatch", response_model=AgentTaskEventOut)
async def dispatch_task_route(task_id: uuid.UUID, request: Request, db: Session = Depends(get_db), user: User = Depends(require_founder)):
    try:
        event = await dispatch_task(db, task_id, dispatched_by=user.email)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ProviderError as exc:
        # chat_with_fallback()'s aggregated failure message can embed a raw provider
        # exception (e.g. an httpx error whose URL carries an API key, see
        # app/providers/gemini_provider.py) — never echo str(exc) here. This is a clean
        # failure boundary, not a local fallback: there is no local reasoning path for the
        # code agent role yet (see app/agent_orchestration.py's module docstring, "Local-first
        # status").
        raise HTTPException(status_code=503, detail="Ingen AI-leverantör kunde nås just nu. Försök igen senare.") from exc
    record_audit(db, user_id=user.id, action="agent_task_dispatched", entity_type="agent_task", entity_id=str(task_id), request=request)
    return event


@router.post("/tasks/{task_id}/test-results", response_model=AgentTaskEventOut)
def record_test_results_route(task_id: uuid.UUID, payload: RecordTestResultsIn, request: Request, db: Session = Depends(get_db), user: User = Depends(require_founder)):
    try:
        event = record_test_results(db, task_id, passed=payload.passed, output=payload.output, recorded_by=user.email)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    record_audit(db, user_id=user.id, action="agent_task_test_results_recorded", entity_type="agent_task", entity_id=str(task_id), detail=str(payload.passed), request=request)
    return event


@router.post("/tasks/{task_id}/review", response_model=AgentTaskEventOut)
async def review_task_route(task_id: uuid.UUID, request: Request, db: Session = Depends(get_db), user: User = Depends(require_founder)):
    try:
        event = await review_task(db, task_id, reviewed_by=user.email)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    record_audit(db, user_id=user.id, action="agent_task_reviewed", entity_type="agent_task", entity_id=str(task_id), detail=event.payload.get("verdict"), request=request)
    return event


@router.post("/tasks/{task_id}/prepare-pr")
async def prepare_pr_route(task_id: uuid.UUID, payload: PrepareGithubPrIn, request: Request, db: Session = Depends(get_db), user: User = Depends(require_founder)):
    try:
        result = await prepare_github_pr(db, task_id, branch_name=payload.branch_name, base_branch=payload.base_branch, prepared_by=user.email)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    record_audit(db, user_id=user.id, action="agent_task_pr_prepared", entity_type="agent_task", entity_id=str(task_id), detail=result.get("mode"), request=request)
    return result


@router.post("/tasks/{task_id}/attempt-merge")
def attempt_merge_route(task_id: uuid.UUID, request: Request, db: Session = Depends(get_db), user: User = Depends(require_founder)):
    """ALWAYS returns merged=false in this slice — see attempt_auto_merge()'s docstring. This
    endpoint exists so the founder UI has something concrete to call and see the exact list of
    unmet conditions, not to actually merge anything."""
    try:
        result = attempt_auto_merge(db, task_id, requested_by=user.email)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    record_audit(db, user_id=user.id, action="agent_task_merge_attempted", entity_type="agent_task", entity_id=str(task_id), detail="blocked", request=request)
    return result
