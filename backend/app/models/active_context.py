"""Active Context is a selection/index layer over existing durable source truth."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class ContextBasis(str, enum.Enum):
    deterministic = "deterministic"
    manual = "manual"
    inferred = "inferred"
    unknown = "unknown"


class ContextAuthority(str, enum.Enum):
    founder = "founder"
    repeated_founder_preference = "repeated_founder_preference"
    deterministic_source = "deterministic_source"
    inferred_pattern = "inferred_pattern"
    ai_interpretation = "ai_interpretation"
    unknown = "unknown"


class ContextMemberState(str, enum.Enum):
    active = "active"
    pinned = "pinned"
    suppressed = "suppressed"
    stale = "stale"
    superseded = "superseded"


class ActiveContextSet(Base):
    __tablename__ = "active_context_sets"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    label: Mapped[str | None] = mapped_column(String(256), nullable=True)
    anchor_type: Mapped[str] = mapped_column(String(48))
    anchor_ref: Mapped[str] = mapped_column(String(256))
    subject_basis: Mapped[str] = mapped_column(String(24), default=ContextBasis.unknown.value)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    refreshed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ActiveContextMember(Base):
    __tablename__ = "active_context_members"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    context_set_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("active_context_sets.id", ondelete="CASCADE"), index=True)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    object_type: Mapped[str] = mapped_column(String(48))
    object_ref: Mapped[str] = mapped_column(String(256))
    inclusion_reason: Mapped[str] = mapped_column(String(48))
    relevance_basis: Mapped[str] = mapped_column(String(24))
    authority: Mapped[str] = mapped_column(String(40), default=ContextAuthority.unknown.value)
    rank: Mapped[int] = mapped_column(Integer, default=0)
    state: Mapped[str] = mapped_column(String(24), default=ContextMemberState.active.value)
    activation_path: Mapped[list] = mapped_column(JSON, default=list)
    source_provenance: Mapped[dict] = mapped_column(JSON, default=dict)
    added_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_activated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    valid_from: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ActiveContextEvent(Base):
    """Append-only audit of manual controls and deterministic refreshes."""

    __tablename__ = "active_context_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    context_set_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("active_context_sets.id", ondelete="CASCADE"), index=True)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    member_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("active_context_members.id", ondelete="SET NULL"), nullable=True)
    action: Mapped[str] = mapped_column(String(32))
    actor_type: Mapped[str] = mapped_column(String(32))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
