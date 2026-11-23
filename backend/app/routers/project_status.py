"""Founder Alpha: the project-status read surface.

One founder-only GET that returns the composed project memory (`app/project_status/service.py`):
blueprint, implemented/verified/planned/blocked buckets, goals, capabilities, permission-
controlled GitHub evidence already staged, the LIVE work chain, ideas awaiting a decision, and
the decision journal — plus `next_steps`, which are SUGGESTIONS with evidence and are never
authorized or created.

There is no write method on this router. Ideas become decisions, and evidence becomes work,
only through the existing founder-only endpoints (improvement-proposals, github evidence
authorization, execution envelopes).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from app.deps import get_db, require_founder
from app.models.user import User
from app.project_status.service import build_project_status

router = APIRouter(
    prefix="/api/mainai",
    tags=["mainai-project-status"],
    dependencies=[Depends(require_founder)],
)


@router.get("/project-status")
def read_project_status(
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
) -> dict:
    """The founder's current project memory, derived from the owning systems (read-only)."""
    status = build_project_status(db, owner_id=user.id)
    response.headers["Cache-Control"] = "no-store"
    return status
