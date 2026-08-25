import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, ForeignKeyConstraint, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class SupervisorGoalLease(Base):
    """The durable mutual-exclusion primitive for `run_supervisor()` production invocation --
    see migration 0059's own module docstring for the full rationale (why this is a NEW,
    narrow lease rather than reusing `mainai_jobs`' one-shot claim machinery, and why it
    mirrors `AgentScopeLease`'s exact fencing shape). Records nothing about WHAT happened
    during a Supervisor run -- that remains `mainai_checkpoints`/`SupervisorResult`, entirely
    unchanged -- only WHO currently holds the exclusive right to call `run_supervisor()` for a
    given goal, and until when."""

    __tablename__ = "supervisor_goal_leases"
    __table_args__ = (
        ForeignKeyConstraint(
            ["goal_id", "owner_id"], ["mainai_goals.id", "mainai_goals.owner_id"], ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["envelope_id", "owner_id"], ["execution_authorization_envelopes.id", "execution_authorization_envelopes.owner_id"], ondelete="SET NULL",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    goal_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    envelope_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    worker_id: Mapped[str] = mapped_column(String(128))
    lease_generation: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(16), default="active")
    acquired_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    released_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
