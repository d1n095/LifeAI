"""Memory truth claim receipts — see migration 0063 and
docs/MAINAI_INSPECTABLE_MEMORY_FOUNDATION.md.

A row here is NEVER canonical memory content. Canonical memory stays in
founder_memory_notes / candidate_learning_signals / work_candidates /
engineering_lessons / project_entities. This table only records what MainAI
claimed about that state so the claim can be checked against reality.
"""

import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

MEMORY_TRUTH_STATES = ("said", "stored", "planned", "implemented", "verified")
MEMORY_TRUTH_TARGET_KINDS = (
    "founder_memory_note",
    "candidate_learning_signal",
    "work_candidate",
    "engineering_lesson",
    "project_entity",
    "mainai_task",
    "mainai_goal",
    "memory_truth_claim",
)


class MemoryTruthClaim(Base):
    """Durable receipt for a claim MainAI makes about her own memory/work state."""

    __tablename__ = "memory_truth_claims"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    claim_text: Mapped[str] = mapped_column(Text)
    claimed_state: Mapped[str] = mapped_column(String(24))
    target_kind: Mapped[str] = mapped_column(String(64))
    target_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    verified_result: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    provenance: Mapped[dict] = mapped_column(JSON, default=dict)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
