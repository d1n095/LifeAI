"""MainAI Runtime Truthfulness and Durable Job Foundation — the Job API (see migration 0025,
app/models/mainai_job.py, app/rag/mainai_jobs_service.py). Every endpoint requires
`require_founder` (this system is founder-only today — see app/deps.py) and operates through
the ordinary RLS-scoped session, so owner isolation is enforced by Postgres itself, not
re-derived here (see app/rls.py's `mainai_jobs_isolation`/`mainai_job_events_isolation`/
`mainai_job_proposals_isolation` policies).

"Admin" read access (list_all_jobs_admin below) is structurally separate from the normal
per-owner endpoints — its own route, its own response shape that includes owner_id — even
though in THIS founder-only system both are gated by the exact same `require_founder`
dependency (there is no broader user base yet for the distinction to bind against). See
docs/MAINAI_JOB_RUNTIME.md's threat model section for why this is an honest, documented
limitation rather than a real second privilege tier today.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.deps import require_founder
from app.limiter import limiter
from app.config import get_settings
from app.db import get_db, migration_engine
from app.mainai_runtime_contract import CapabilityUnavailableError
from app.models.mainai_job import MainAIJobProposal
from app.models.user import User
from app.rag import mainai_jobs_service as service
from app.schemas import (
    MainAIJobCreateIn,
    MainAIJobDetailOut,
    MainAIJobEventOut,
    MainAIJobOut,
    MainAIJobProposalOut,
)

settings = get_settings()

router = APIRouter(prefix="/api/mainai/jobs", tags=["mainai-jobs"], dependencies=[Depends(require_founder)])


@router.post("", response_model=MainAIJobOut, status_code=status.HTTP_201_CREATED)
@limiter.limit(f"{settings.rate_limit_default_per_minute}/minute")
def create_job(
    request: Request,
    payload: MainAIJobCreateIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    try:
        job = service.create_job(
            db,
            owner_id=user.id,
            job_type=payload.job_type,
            input_refs=payload.input_refs,
            created_by="founder",
            idempotency_key=payload.idempotency_key,
            request=request,
        )
    except CapabilityUnavailableError as exc:
        # `reason` is machine-readable (founder re-review round, PR #36) so a caller/UI can
        # tell "not_implemented" (this job_type doesn't exist) apart from "not_configured"
        # (implemented, but no provider is wired up yet -- founder-fixable, not a code gap)
        # instead of one indistinguishable 409 string.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"message": str(exc), "reason": exc.reason}) from exc
    except service.IdempotencyConflictError as exc:
        # Distinct `reason` from CapabilityUnavailableError's 409 above -- founder re-review
        # round (PR #36), fourth pass: reusing an idempotency_key with a materially different
        # job_type/input_refs is its own, clearly-labeled conflict, never silently accepted as
        # if it were the same request and never confused with "this job_type is unavailable".
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"message": str(exc), "reason": "idempotency_conflict"}) from exc
    except service.InvalidInputRefsError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return job


@router.get("", response_model=list[MainAIJobOut])
def list_jobs(limit: int = 50, offset: int = 0, db: Session = Depends(get_db), user: User = Depends(require_founder)):
    return service.list_jobs(db, limit=limit, offset=offset)


@router.get("/{job_id}", response_model=MainAIJobDetailOut)
def get_job(job_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require_founder)):
    try:
        job = service.get_job(db, job_id)
        events = service.list_job_events(db, job_id)
    except service.JobNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.") from exc
    return MainAIJobDetailOut(**MainAIJobOut.model_validate(job).model_dump(), events=[MainAIJobEventOut.model_validate(e) for e in events])


@router.get("/{job_id}/proposals", response_model=list[MainAIJobProposalOut])
def list_job_proposals(job_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require_founder)):
    try:
        service.get_job(db, job_id)  # raises JobNotFoundError under RLS — 404, never a leak
    except service.JobNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.") from exc
    return list(
        db.execute(select(MainAIJobProposal).where(MainAIJobProposal.job_id == job_id).order_by(MainAIJobProposal.created_at.asc())).scalars()
    )


@router.post("/{job_id}/cancel", response_model=MainAIJobOut)
@limiter.limit(f"{settings.rate_limit_default_per_minute}/minute")
def cancel_job(job_id: uuid.UUID, request: Request, db: Session = Depends(get_db), user: User = Depends(require_founder)):
    try:
        return service.request_cancel(db, job_id, requested_by=user.id, request=request)
    except service.JobNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.") from exc
    except service.InvalidJobTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post("/{job_id}/retry", response_model=MainAIJobOut)
@limiter.limit(f"{settings.rate_limit_default_per_minute}/minute")
def retry_job(job_id: uuid.UUID, request: Request, db: Session = Depends(get_db), user: User = Depends(require_founder)):
    try:
        return service.retry_job(db, job_id, requested_by=user.id, request=request)
    except service.JobNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.") from exc
    except service.InvalidJobTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.get("/admin/all", response_model=list[MainAIJobOut])
def list_all_jobs_admin(limit: int = 100, offset: int = 0, user: User = Depends(require_founder)):
    """Deliberately bypasses per-owner RLS scoping (see module docstring) — runs on the
    superuser/migration connection, exactly like app/worker.py's claim step, since it must see
    jobs across every owner. `require_founder` is the only gate available in this founder-only
    system; a future multi-tenant system would need a REAL admin role check here, not just
    this same dependency, before this endpoint could safely stay this way — see
    docs/MAINAI_JOB_RUNTIME.md's threat model."""
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    with migration_engine.connect() as conn:
        rows = conn.execute(
            text("SELECT * FROM mainai_jobs ORDER BY created_at DESC, id DESC LIMIT :limit OFFSET :offset"),
            {"limit": limit, "offset": offset},
        ).mappings().all()
    return [MainAIJobOut(**dict(row)) for row in rows]
