"""Memory Threads index durable history without copying canonical content."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class ThreadClassificationBasis(str, enum.Enum):
    manual = "manual"
    deterministic = "deterministic"
    inferred = "inferred"
    unknown = "unknown"


class ThreadState(str, enum.Enum):
    active = "active"
    dormant = "dormant"
    completed = "completed"
    superseded = "superseded"
    archived = "archived"


class MemoryThread(Base):
    __tablename__ = "memory_threads"
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    manual_label: Mapped[str | None] = mapped_column(String(256), nullable=True)
    system_label: Mapped[str | None] = mapped_column(String(256), nullable=True)
    classification_basis: Mapped[str] = mapped_column(String(24), default="unknown")
    state: Mapped[str] = mapped_column(String(24), default="active")
    idempotency_key: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_activity_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow
    )


class MemoryThreadMember(Base):
    __tablename__ = "memory_thread_members"
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    thread_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("memory_threads.id", ondelete="CASCADE"),
        index=True,
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    member_kind: Mapped[str] = mapped_column(String(48))
    member_ref_id: Mapped[str] = mapped_column(String(256))
    membership_basis: Mapped[str] = mapped_column(String(40), default="unknown")
    classification_basis: Mapped[str] = mapped_column(String(24), default="unknown")
    state: Mapped[str] = mapped_column(String(24), default="active")
    provenance: Mapped[dict] = mapped_column(JSON, default=dict)
    source_occurred_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    source_time_basis: Mapped[str] = mapped_column(String(24), default="unknown")
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    added_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    valid_from: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class MemoryThreadRelationship(Base):
    __tablename__ = "memory_thread_relationships"
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    from_thread_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("memory_threads.id", ondelete="CASCADE")
    )
    to_thread_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("memory_threads.id", ondelete="CASCADE")
    )
    relationship_type: Mapped[str] = mapped_column(String(24))
    basis: Mapped[str] = mapped_column(String(24), default="unknown")
    provenance: Mapped[dict] = mapped_column(JSON, default=dict)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class MemoryThreadEvent(Base):
    __tablename__ = "memory_thread_events"
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    thread_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("memory_threads.id", ondelete="CASCADE"),
        index=True,
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    member_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("memory_thread_members.id", ondelete="SET NULL"),
        nullable=True,
    )
    relationship_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("memory_thread_relationships.id", ondelete="SET NULL"),
        nullable=True,
    )
    event_type: Mapped[str] = mapped_column(String(32))
    actor_type: Mapped[str] = mapped_column(String(24), default="unknown")
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, index=True
    )
