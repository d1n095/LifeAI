"""Founder-only, read-only GitHub and LifeAI project-state observations."""

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import require_founder
from app.integrations.github_operations import _get_github_observation, _local_project_state, build_project_snapshot
from app.integrations.integration_analysis import MAX_ANALYZED_PRS, analyze_integration
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


@router.get("/integration-analysis")
@limiter.limit("6/minute")
async def get_integration_analysis(
    request: Request,
    pr: list[int] | None = Query(default=None),
    force_refresh: bool = False,
    include_migration_graph: bool = False,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    """Analyze bounded current PR observations; no authorization or execution is granted."""
    if pr is not None and (len(pr) > MAX_ANALYZED_PRS or any(number <= 0 for number in pr)):
        raise HTTPException(status_code=422, detail="Invalid pull request selection.")
    if include_migration_graph and (pr is None or len(pr) != 1):
        raise HTTPException(status_code=422, detail="Migration graph requires exactly one selected pull request.")
    github = await _get_github_observation(force_refresh)
    local = _local_project_state(db, user.id)
    return await analyze_integration(github, local["assignments"], pr, include_migration_graph)
