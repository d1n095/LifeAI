import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, String, Text
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
