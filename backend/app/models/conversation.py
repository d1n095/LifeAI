import enum
import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Enum, FetchedValue, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
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
    # (app/rag/backfill/message_sequence.py) has numbered them; only then can the separate
    # CONTRACT migration add NOT NULL. Read paths must therefore not assume it is set yet:
    # `ORDER BY sequence_number` becomes correct for a conversation only once
    # `count_unsequenced_messages()` reports 0 for it.
    sequence_number: Mapped[int | None] = mapped_column(Integer, nullable=True, server_default=FetchedValue())


class ChatRequestClaim(Base):
    """Durable idempotency claim for one logical POST /api/chat send.

    Keyed by (owner_id, client_message_id). The row is committed in the SAME transaction
    as the user message, before any provider call; a retry with the same key can therefore
    replay the existing reply (or get 409 while the first attempt is unresolved) instead of
    creating a second user message and a second billable provider call.

    Owner-scoped under FORCE RLS (migration 0090), like conversations/messages. It carries
    no provider authority and no budget state; those remain in their existing owners.
    """

    __tablename__ = "chat_request_claims"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    client_message_id: Mapped[str] = mapped_column(String(128), nullable=False)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    user_message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("owner_id", "client_message_id", name="uq_chat_request_claims_owner_client"),
    )


# Durable chat provider-attempt states (migration 0091). `reserved` means the budget hold
# exists but the provider invocation marker was never written — the only state from which a
# release is provably safe. `invoking`/`held` may have reached the provider and require
# evidence-backed reconciliation, never a blind release.
CHAT_ATTEMPT_UNRESOLVED_STATES = frozenset(
    {"reserved", "invoking", "held", "settlement_pending", "release_pending"}
)


class ChatProviderAttempt(Base):
    """Durable identity for one billable chat attempt and its budget reservation.

    The reservation itself lives in the existing ComputeBudgetStore (Redis/in-memory); this
    row stores exactly enough to find and finalize it after a timeout, crash or restart:
    the reservation identities, the provider/model, and a durable invocation boundary.
    It never stores prompt/reply text and carries no provider authority of its own.

    Owner-scoped under FORCE RLS (migration 0091). A `reserved` row (no invocation marker)
    is the only state that can be released without provider-side evidence; every other
    unresolved state is settled conservatively or from recorded usage.
    """

    __tablename__ = "chat_provider_attempts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    user_message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    assistant_message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    # Includes the per-attempt inflight nonce; unique per attempt, so a retry can never
    # reuse a finalized reservation identity.
    source_ref: Mapped[str] = mapped_column(String(256), nullable=False, unique=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    # None until invocation begins; "pre_egress" only for a proven pre-invocation failure;
    # "ambiguous" once the provider may have been reached; "observed" after real usage.
    effect_boundary: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Exact ComputeBudgetStore reservation identities (subject, period keys, id, amount).
    reservations: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # Safe evidence only: usage counts, error category, reconciliation reference. Never
    # prompt/reply content, API keys or raw provider responses.
    evidence: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    reserved_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reserved_cost_micros: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
