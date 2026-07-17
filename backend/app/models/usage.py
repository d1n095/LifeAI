import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class UsageLog(Base):
    """Append-only cost/usage record. One row per successful provider call."""

    __tablename__ = "usage_log"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # SET NULL, not CASCADE: cost/usage is an audit-style record and must outlive the
    # conversation it came from — deleting a chat thread must never quietly erase what it
    # cost. Same reasoning applies to user_id if a user account is ever hard-deleted later.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True
    )
    role: Mapped[str] = mapped_column(String(32))  # "chat" | "embedding"
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(128))
    prompt_tokens: Mapped[int] = mapped_column(default=0)
    completion_tokens: Mapped[int] = mapped_column(default=0)
    # NULL when pricing for this provider/model isn't in app/providers/pricing.py yet —
    # never a fabricated 0, so the admin usage view can distinguish "free" from "unknown".
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 6), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
