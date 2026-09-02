"""Stage R — ROI record model."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class SelfImprovementROIRecord(Base):
    __tablename__ = "self_improvement_roi_records"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    change_ref: Mapped[str] = mapped_column(String(256))
    metrics_before: Mapped[dict] = mapped_column(JSONB, default=dict)
    metrics_after: Mapped[dict] = mapped_column(JSONB, default=dict)
    complexity_cost: Mapped[float] = mapped_column(Float, default=0.0)
    net_roi: Mapped[float] = mapped_column(Float, default=0.0)
    recommendation: Mapped[str] = mapped_column(String(64), default="observe")
    rationale: Mapped[str] = mapped_column(Text, default="")
    idempotency_key: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
