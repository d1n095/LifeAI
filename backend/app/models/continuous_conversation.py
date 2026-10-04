"""Continuous founder↔MainAI conversation tables (migrations 0089 + 0090)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, ForeignKeyConstraint, JSON, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class FounderCanonicalConversation(Base):
    __tablename__ = "founder_canonical_conversations"

    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("conversation_id", name="uq_founder_canonical_conversations_conversation"),
        ForeignKeyConstraint(
            ["conversation_id", "owner_id"],
            ["conversations.id", "conversations.user_id"],
            name="founder_canonical_conversations_conversation_owner_fkey",
            ondelete="RESTRICT",
        ),
    )


class FounderConversationEvent(Base):
    """Append-only inbound/outbound/internal turn log. Not a permission grant."""

    __tablename__ = "founder_conversation_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    message_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("messages.id", ondelete="SET NULL"), nullable=True)
    direction: Mapped[str] = mapped_column(String(16))
    kind: Mapped[str] = mapped_column(String(64))
    suppressed: Mapped[bool] = mapped_column(Boolean, default=False)
    interrupt: Mapped[bool] = mapped_column(Boolean, default=False)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    excerpt: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    __table_args__ = (
        ForeignKeyConstraint(
            ["conversation_id", "owner_id"],
            ["conversations.id", "conversations.user_id"],
            name="founder_conversation_events_conversation_owner_fkey",
            ondelete="CASCADE",
        ),
    )


class FounderConversationCompaction(Base):
    """Compacted memory. COMPACTION != DELETION. SUMMARY != SOURCE. MEMORY != AUTHORITY."""

    __tablename__ = "founder_conversation_compactions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    summary_text: Mapped[str] = mapped_column(Text, default="")
    source_message_ids: Mapped[list] = mapped_column(JSONB, default=list)
    source_provenance: Mapped[list] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    __table_args__ = (
        ForeignKeyConstraint(
            ["conversation_id", "owner_id"],
            ["conversations.id", "conversations.user_id"],
            name="founder_conversation_compactions_conversation_owner_fkey",
            ondelete="RESTRICT",
        ),
    )


class FounderConversationDecision(Base):
    """Decisions with supersession. Newer source truth wins; old history is kept."""

    __tablename__ = "founder_conversation_decisions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    message_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("messages.id", ondelete="SET NULL"), nullable=True)
    topic: Mapped[str] = mapped_column(String(128), default="")
    statement: Mapped[str] = mapped_column(Text, default="")
    identifiers: Mapped[dict] = mapped_column(JSONB, default=dict)
    superseded: Mapped[bool] = mapped_column(Boolean, default=False)
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    __table_args__ = (
        ForeignKeyConstraint(
            ["conversation_id", "owner_id"],
            ["conversations.id", "conversations.user_id"],
            name="founder_conversation_decisions_conversation_owner_fkey",
            ondelete="RESTRICT",
        ),
    )


class FounderConversationProvenance(Base):
    """Pointers from compacted memory back to original turns and exact identifiers."""

    __tablename__ = "founder_conversation_provenance"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    message_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("messages.id", ondelete="SET NULL"), nullable=True)
    kind: Mapped[str] = mapped_column(String(32), default="")
    value: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("owner_id", "message_id", "kind", "value", name="uq_founder_conversation_provenance_pointer"),
        ForeignKeyConstraint(
            ["conversation_id", "owner_id"],
            ["conversations.id", "conversations.user_id"],
            name="founder_conversation_provenance_conversation_owner_fkey",
            ondelete="RESTRICT",
        ),
    )


class FounderWorkspaceLease(Base):
    """Exclusive workspace ownership. SHA sharing allowed. Workspace sharing forbidden."""

    __tablename__ = "founder_workspace_leases"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    agent_key: Mapped[str] = mapped_column(String(64), default="")
    branch: Mapped[str] = mapped_column(String(256), default="")
    task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    execution_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    worktree_path: Mapped[str] = mapped_column(Text, default="")
    mutability: Mapped[str] = mapped_column(String(32), default="mutable_builder")
    shared_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
