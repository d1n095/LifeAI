import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, FetchedValue, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class MessageRole(str, enum.Enum):
    user = "user"
    assistant = "assistant"
    system = "system"


class MessageStatus(str, enum.Enum):
    """Distinguishes "this row is exactly what it claims to be" from "an assistant reply was
    attempted and failed" — see app/routers/chat.py's module docstring for the full failure-
    boundary this exists to fix. A user message is always `succeeded` (it's just saved, never
    generated). An assistant message is `succeeded` once real content exists, or `failed` when
    a provider attempt produced nothing — `error_category` is only meaningful in that case."""

    succeeded = "succeeded"
    failed = "failed"


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    title: Mapped[str] = mapped_column(String(256), default="Ny konversation")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("conversations.id"))
    role: Mapped[MessageRole] = mapped_column(Enum(MessageRole))
    content: Mapped[str] = mapped_column(Text)
    provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source_document_ids: Mapped[str | None] = mapped_column(Text, nullable=True)  # comma-separated UUIDs
    status: Mapped[MessageStatus] = mapped_column(Enum(MessageStatus), default=MessageStatus.succeeded)
    # Set only on assistant rows — the user message this is a reply to. A partial unique index
    # (migration 0016) enforces at most one assistant reply per user message: retrying an
    # existing failed attempt updates that same row instead of creating a duplicate.
    in_reply_to_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("messages.id"), nullable=True)
    # Set only when status=failed — one of VerificationResult's values (see
    # app/providers/verification.py), never a raw exception string.
    error_category: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    # S1B (migration 0030, docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md §4.9): the total order
    # `created_at` alone cannot provide — two messages written in the same microsecond, or
    # across a non-monotonic clock, are otherwise unorderable. Assigned by the database, never
    # by this class or by any caller: migration 0030's `messages_assign_sequence_number`
    # BEFORE INSERT trigger fills it in per conversation, and a second trigger rejects any
    # later attempt to change or clear an already-assigned value. `FetchedValue()` is what
    # tells SQLAlchemy that (a) it must NOT send this column on INSERT and (b) it must read
    # the trigger's result back via RETURNING, so a freshly-added Message object carries the
    # real ordinal without an explicit refresh.
    #
    # Still `nullable=True` on purpose — this is the EXPAND half of the expand/contract plan.
    # Historical rows stay NULL until the durable `message_sequence_backfill` job
    # (app/rag/message_sequence_backfill.py) has numbered them; only then can the separate
    # CONTRACT migration add NOT NULL. Read paths must therefore not assume it is set yet:
    # `ORDER BY sequence_number` becomes correct for a conversation only once
    # `count_unsequenced_messages()` reports 0 for it.
    sequence_number: Mapped[int | None] = mapped_column(Integer, nullable=True, server_default=FetchedValue())
