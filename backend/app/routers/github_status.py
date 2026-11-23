"""Founder Alpha R14: the founder-only GitHub development-status surface.

GET reads the current picture (never talks to GitHub). POST /refresh performs the one read.
Both are founder-only and no-store; neither pushes, merges or comments — `app/github_status`
has no write path at all.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from app.deps import get_db, require_founder
from app.github_status.service import dev_status, refresh
from app.models.user import User

router = APIRouter(
    prefix="/api/mainai",
    tags=["mainai-github-status"],
    dependencies=[Depends(require_founder)],
)


@router.get("/github-status")
def read_github_status(
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    return dev_status(db, owner_id=user.id)


@router.post("/github-status/refresh")
async def refresh_github_status(
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    return await refresh(db, owner_id=user.id)
