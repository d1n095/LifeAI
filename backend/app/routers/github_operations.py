"""Founder-only, read-only GitHub and LifeAI project-state observations."""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import require_founder
from app.integrations.github_operations import build_project_snapshot
from app.limiter import limiter
from app.models.user import User

router = APIRouter(
    prefix="/api/github-operations",
    tags=["github-operations"],
    dependencies=[Depends(require_founder)],
)


@router.get("/snapshot")
@limiter.limit("6/minute")
async def get_project_snapshot(
    request: Request,
    force_refresh: bool = False,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    """Return a versioned, read-only project snapshot.

    GitHub observations include explicit freshness, exact head SHAs, source URLs and bounded
    pagination metadata. Cached data is labeled `cached_fresh` or `stale`, never `live`.
    PR comments are untrusted plain text. Recommendations are evidence-linked suggestions;
    this endpoint cannot dispatch, merge, or modify anything.
    """
    return await build_project_snapshot(db, owner_id=user.id, force_refresh=force_refresh)
