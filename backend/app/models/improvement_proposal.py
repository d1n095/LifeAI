"""Round 10 M7: the improvement-proposal persistence model (migration 0076).

An improvement proposal is a REVIEW ARTIFACT: MainAI recognized a recurring failure or an
opportunity, wrote down the why/benefit/risk, and STOPPED. Executing it is the founder's
decision through this service's decision methods — nothing else can move the status. See
the migration docstring for the authorization boundary."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class ImprovementStatus(str, enum.Enum):
    proposed = "proposed"
    accepted = "accepted"
    rejected = "rejected"
    implemented = "implemented"
    reverted = "reverted"


class ImprovementSource(str, enum.Enum):
    recurring_failure_cluster = "recurring_failure_cluster"
    lesson_recurrence = "lesson_recurrence"
    founder_observation = "founder_observation"
    manual = "manual"


class MainAIImprovementProposal(Base):
    __tablename__ = "mainai_improvement_proposals"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    source_type: Mapped[ImprovementSource] = mapped_column(Enum(ImprovementSource), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    benefit_estimate: Mapped[str | None] = mapped_column(Text, nullable=True)
    risk_estimate: Mapped[str | None] = mapped_column(Text, nullable=True)
    duplicate_key: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[ImprovementStatus] = mapped_column(
        Enum(ImprovementStatus), default=ImprovementStatus.proposed, nullable=False
    )
    proposed_by: Mapped[str] = mapped_column(String(64), default="mainai", nullable=False)
    decision_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    outcome_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
