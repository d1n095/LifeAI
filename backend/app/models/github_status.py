"""Founder Alpha R14: the durable, owner-scoped GitHub development-status snapshot.

See migration 0089's module docstring. One row per successful READ of GitHub; never a claim
that anything was merged, deployed or authorized. `payload` is the exact facts GitHub returned
(head SHA, commit subject, open PRs, open issue count, check summary) so a later reader can
say "this was true at 14:02" instead of re-guessing.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class GithubStatusSnapshot(Base):
    __tablename__ = "github_status_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    repo: Mapped[str] = mapped_column(String(256))
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    fetched_at: Mapped[datetime] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
