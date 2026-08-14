"""S1C message-source historical backfill (PR #60 provisional proposal,
docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md §4.8/§8): creates a `message_source_units` row for
every existing `messages` row that doesn't have one yet.

Simpler than app/rag/backfill/message_sequence.py's numbering backfill on purpose: this is a
per-message find-or-create (app/rag/message_source.py's
`get_or_create_message_source_unit`), not an ordinal assignment, so there is no conflict class
to detect and no advisory lock to take — the SAVEPOINT-based find-or-create is already safe
under concurrency by construction (proven by S1A's own equivalent for documents). "No
message_source_units row yet" is itself the restart marker, exactly the same reasoning
message_sequence.py's own docstring gives for `sequence_number IS NULL` — a crash mid-run loses
at most the in-flight message, and re-running simply finds it again.

No AI, no provider, no network — pure SQL + the deterministic find-or-create primitive.
"""

import logging
import uuid
from dataclasses import dataclass
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.conversation import Conversation, Message
from app.models.memory_source_unit import MessageSourceUnit
from app.rag.message_source import MessageSourceLocator, get_or_create_message_source_unit

logger = logging.getLogger("mainai.message_source_backfill")


@dataclass
class MessageSourceBackfillResult:
    processed: int = 0
    created: int = 0


def count_messages_without_source_unit(db: Session, owner_id: uuid.UUID) -> int:
    return (
        db.query(Message)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .outerjoin(MessageSourceUnit, MessageSourceUnit.message_id == Message.id)
        .filter(Conversation.user_id == owner_id, MessageSourceUnit.memory_source_id.is_(None))
        .count()
    )


def candidate_message_ids(db: Session, owner_id: uuid.UUID, limit: int) -> list[uuid.UUID]:
    stmt = (
        select(Message.id)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .outerjoin(MessageSourceUnit, MessageSourceUnit.message_id == Message.id)
        .where(Conversation.user_id == owner_id, MessageSourceUnit.memory_source_id.is_(None))
        .order_by(Message.created_at, Message.id)
        .limit(limit)
    )
    return [row[0] for row in db.execute(stmt).all()]


def backfill_message_source_units(
    db: Session,
    owner_id: uuid.UUID,
    *,
    max_batches: int = 200,
    batch_size: int = 200,
    before_batch_commit: Callable[[], None] | None = None,
) -> MessageSourceBackfillResult:
    """Runs up to `max_batches` batches of `batch_size` messages each, committing after every
    batch (same discipline as app/rag/backfill/memory_source.py's own batched backfill) — a
    crash or lease loss between batches loses only the in-flight batch, not the whole run.
    Returns truthfully: if candidates remain when the cap is hit, the caller (the job handler)
    must say so explicitly rather than claim completion."""
    result = MessageSourceBackfillResult()
    for _ in range(max_batches):
        ids = candidate_message_ids(db, owner_id, batch_size)
        if not ids:
            break
        for message_id in ids:
            message = db.get(Message, message_id)
            if message is None:
                continue
            conversation = db.get(Conversation, message.conversation_id)
            if conversation is None or conversation.user_id != owner_id:
                # Defensive: the candidate query already scopes by owner_id; unreachable under
                # normal operation, but never silently attach a source to the wrong owner.
                logger.warning("message_source backfill: skipping message %s — conversation ownership mismatch", message_id)
                continue
            get_or_create_message_source_unit(
                db,
                MessageSourceLocator(
                    owner_id=owner_id,
                    message_id=message.id,
                    conversation_id=conversation.id,
                    role=message.role,
                    observed_at=message.created_at,
                    content_text=message.content,
                ),
            )
            result.created += 1
            result.processed += 1
        # A durable-job caller supplies a lease-fencing write here. It runs in the SAME
        # transaction as the source-unit inserts, immediately before commit: if ownership was
        # reclaimed while this batch was running, the failed fence rolls the whole batch back
        # instead of allowing a stale worker's domain writes to escape before its next progress
        # update. Pure/manual backfills intentionally need no job lease and omit the callback.
        if before_batch_commit is not None:
            before_batch_commit()
        db.commit()
    return result
