"""Bounded consumer for the canonical transactional outbox.

Outbox rows are routing hints only. The consumer never trusts historical owner/content data;
the RecallIndexWorker re-reads canonical state and fences by the source generation.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.personal_recall_outbox import PersonalRecallOutbox
from app.models.personal_recall_outbox_delivery import PersonalRecallOutboxDelivery
from app.personal_recall.workers import ChangeKind, RecallIndexWorker, SourceChange


@dataclass(frozen=True)
class OutboxConsumeResult:
    seen: int
    enqueued: int
    skipped: int


def _kind(source_type: str, event_type: str) -> ChangeKind:
    if source_type == "conversation_message":
        return ChangeKind.NEW_MESSAGE if event_type == "created" else ChangeKind.EDITED_MESSAGE
    if source_type == "durable_memory":
        return ChangeKind.MEMORY_PURGE if event_type == "purged" else ChangeKind.MEMORY_REVOKE if event_type == "revoked" else ChangeKind.MEMORY_REVOKE
    if event_type == "deleted":
        return ChangeKind.DELETED_DOCUMENT
    if event_type == "superseded":
        return ChangeKind.KNOWLEDGE_SUPERSESSION
    return ChangeKind.NEW_DOCUMENT if event_type == "created" else ChangeKind.EDITED_MESSAGE if event_type == "updated" else ChangeKind.NEW_DOCUMENT


def consume_outbox(db: Session, *, owner_id: str, worker: RecallIndexWorker, limit: int = 100) -> OutboxConsumeResult:
    if not 1 <= limit <= 1_000:
        raise ValueError("outbox batch bound is 1..1000")
    rows = db.execute(
        select(PersonalRecallOutbox)
        .where(PersonalRecallOutbox.owner_id == owner_id)
        .where(~select(PersonalRecallOutboxDelivery.event_id).where(PersonalRecallOutboxDelivery.event_id == PersonalRecallOutbox.event_id).exists())
        .order_by(PersonalRecallOutbox.occurred_at, PersonalRecallOutbox.event_id)
        .limit(limit)
    ).scalars().all()
    enqueued = 0
    for row in rows:
        if str(row.owner_id) != owner_id:
            continue
        worker.enqueue(SourceChange(
            event_id=str(row.event_id), owner_id=owner_id, source_id=str(row.source_id),
            kind=_kind(row.source_type, str(row.event_type)), canonical_version=row.canonical_version,
            event_type=str(row.event_type), version_fenced=True,
        ))
        db.add(PersonalRecallOutboxDelivery(event_id=row.event_id, owner_id=row.owner_id, delivered_at=datetime.now(timezone.utc)))
        enqueued += 1
    db.commit()
    return OutboxConsumeResult(len(rows), enqueued, len(rows) - enqueued)


def outbox_status(db: Session, *, owner_id: str, limit: int = 1_000) -> dict[str, int | float | None]:
    if not 1 <= limit <= 10_000:
        raise ValueError("status bound is 1..10000")
    rows = db.execute(
        select(PersonalRecallOutbox.occurred_at)
        .outerjoin(PersonalRecallOutboxDelivery, PersonalRecallOutboxDelivery.event_id == PersonalRecallOutbox.event_id)
        .where(PersonalRecallOutbox.owner_id == owner_id, PersonalRecallOutboxDelivery.event_id.is_(None))
        .order_by(PersonalRecallOutbox.occurred_at)
        .limit(limit)
    ).scalars().all()
    oldest = None
    if rows:
        occurred = rows[0]
        if occurred.tzinfo is None:
            occurred = occurred.replace(tzinfo=timezone.utc)
        oldest = max(0.0, (datetime.now(timezone.utc) - occurred).total_seconds())
    return {"pending_events": len(rows), "oldest_pending_age_seconds": oldest, "batch_capped": int(len(rows) == limit)}
