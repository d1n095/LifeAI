"""Founder-only, read-only surface over `app.agent_coordination.founder_intelligence` -- "who is
working, who is blocked, who is finished, and does any of it duplicate something else" in plain
language. No write path exists in this module, same discipline as `app.routers.github_status`.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session

from app.agent_coordination.founder_intelligence import founder_development_summary
from app.db import get_db
from app.deps import require_founder
from app.models.user import User

router = APIRouter(
    prefix="/api/founder",
    tags=["founder-development-status"],
    dependencies=[Depends(require_founder)],
)


@router.get("/development-status")
def read_development_status(
    response: Response,
    known: str | None = Query(
        default=None,
        description="Comma-separated assignment_id:signature pairs the caller already showed; "
        "unchanged rows are omitted from the response.",
    ),
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    known_signatures: dict[str, str] = {}
    if known:
        for pair in known.split(","):
            if ":" not in pair:
                continue
            assignment_id, _, signature = pair.partition(":")
            known_signatures[assignment_id] = signature

    summaries = founder_development_summary(db, owner_id=user.id, known_signatures=known_signatures)
    return {
        "assignments": [
            {
                "assignment_id": str(s.assignment_id),
                "agent_key": s.agent_key,
                "plain_status": s.plain_status,
                "canonical_status": s.canonical_status,
                "branch": s.branch,
                "head_sha": s.head_sha,
                "upstream_pr_ref": s.upstream_pr_ref,
                "ci_conclusion": s.ci_conclusion,
                "block_explanation": s.block_explanation,
                "duplicate_warning": s.duplicate_warning,
                "signature": s.signature,
            }
            for s in summaries
        ]
    }
