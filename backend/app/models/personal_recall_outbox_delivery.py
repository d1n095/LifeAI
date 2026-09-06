from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class PersonalRecallOutboxDelivery(Base):
    __tablename__ = "personal_recall_outbox_delivery"

    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("personal_recall_outbox.event_id", ondelete="CASCADE"), primary_key=True)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    delivered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempt_count: Mapped[int] = mapped_column(Integer, default=1)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
