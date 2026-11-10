"""Session-start context API — a read-only, owner-scoped project context for the start/resume of
a Life session. Thin boundary over app.session_context.assemble_session_context (no new store).
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_current_user
from app.models.user import User
from app.session_context import assemble_session_context

router = APIRouter(prefix="/api/session", tags=["session"])


@router.get("/context")
async def get_session_context(
    session_id: str = "default",
    token_budget: int = 4000,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """Assemble the authenticated user's compact, source-grounded session-start context. Owner-
    scoped (never another user's data). Read-only: it authorizes nothing and writes nothing; a
    `blocked`/`BLOCKED` readiness means the session must not act on the Founder's behalf until the
    named source/violation is resolved."""
    bundle = assemble_session_context(
        db, owner_id=user.id, session_id=session_id, token_budget=token_budget
    )
    return bundle.to_dict()
