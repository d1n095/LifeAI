"""Founder-facing MainAI executive business-workflow API.

One interface over the executive engine (app/mainai_business): run a business objective
through the real workforce substrate and read the reassembled project. Founder-only
(`require_founder`) and RLS-scoped like every other MainAI route — owner isolation is
enforced by Postgres, not re-derived here. No provider is invoked; the consequential publish
step runs only when the Founder sets `approve_publish` (fail-closed otherwise).
"""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.deps import require_founder
from app.limiter import limiter
from app.mainai_business.execution import InProcessRunner, ModelBackedRunner
from app.mainai_business.reference import run_ecommerce_opportunity_assessment
from app.mainai_business.view import assemble_business_project_view
from app.models.user import User

settings = get_settings()

router = APIRouter(
    prefix="/api/mainai/business",
    tags=["mainai-business"],
    dependencies=[Depends(require_founder)],
)


class EcommerceAssessmentIn(BaseModel):
    approve_publish: bool = False
    founder_approval_ref: str | None = None
    # "in_process" (deterministic, default) or "model_backed" (real local inference). The
    # model-backed path fails closed on a non-local provider and degrades to honest task
    # failures if the local model is unreachable — it never falls back to a paid provider.
    mode: str = "in_process"


@router.post("/ecommerce-assessment", status_code=status.HTTP_201_CREATED)
@limiter.limit(f"{settings.rate_limit_default_per_minute}/minute")
def run_ecommerce_assessment(
    request: Request,
    payload: EcommerceAssessmentIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    if payload.mode not in ("in_process", "model_backed"):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="mode must be in_process or model_backed")
    runner = ModelBackedRunner() if payload.mode == "model_backed" else InProcessRunner()
    report = run_ecommerce_opportunity_assessment(
        db,
        owner_id=user.id,
        approve_publish=payload.approve_publish,
        founder_approval_ref=payload.founder_approval_ref,
        runner=runner,
    )
    db.commit()
    return report.as_dict()


@router.get("/{workflow_id}")
def get_business_project(
    workflow_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    view = assemble_business_project_view(db, owner_id=user.id, workflow_id=workflow_id)
    if not view["exists"]:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Business workflow not found.")
    return view
