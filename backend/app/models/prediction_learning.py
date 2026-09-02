"""Stage L — prediction records model."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class PredictionRecord(Base):
    __tablename__ = "prediction_records"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(40))
    subject_ref: Mapped[str] = mapped_column(String(256))
    predicted_value: Mapped[dict] = mapped_column(JSONB, default=dict)
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    actual_value: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    outcome_delta: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    heuristic_tags: Mapped[list] = mapped_column(JSONB, default=list)
    status: Mapped[str] = mapped_column(String(24), default="open")
    idempotency_key: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    scored_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
