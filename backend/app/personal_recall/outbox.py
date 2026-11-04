"""Bounded consumer for the canonical transactional outbox.

Outbox rows are routing hints only. The consumer never trusts historical owner/content data;
the RecallIndexWorker re-reads canonical state and fences by the source generation.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import sqlite3
import uuid

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


@dataclass(frozen=True)
class OutboxProcessResult:
    attempted: int
    delivered: int
    retrying: int
    dead_lettered: int
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
        db.add(PersonalRecallOutboxDelivery(event_id=row.event_id, owner_id=row.owner_id, delivered_at=datetime.now(timezone.utc), state="delivered", retryable=True))
        enqueued += 1
    db.commit()
    return OutboxConsumeResult(len(rows), enqueued, len(rows) - enqueued)


def _backoff(attempt: int, event_id: str, *, cap_seconds: int = 3_600) -> float:
    base = min(cap_seconds, 5 * (2 ** max(0, attempt - 1)))
    jitter = int(hashlib.sha256(event_id.encode()).hexdigest()[:4], 16) / 0xFFFF
    return min(cap_seconds, base * (0.9 + (0.2 * jitter)))


def _failure_class(exc: BaseException) -> tuple[str, bool, str]:
    if isinstance(exc, (OSError, TimeoutError, ConnectionError, sqlite3.OperationalError)):
        return type(exc).__name__, True, "temporary infrastructure failure"
    if isinstance(exc, PermissionError):
        return type(exc).__name__, False, "authority or owner violation"
    if isinstance(exc, ValueError):
        return type(exc).__name__, False, "malformed or unsupported event"
    return type(exc).__name__, True, "unknown failure; bounded retry"


def process_outbox_batch(db: Session, *, owner_id: str, worker: RecallIndexWorker, limit: int = 100,
                         now: datetime | None = None, max_attempts: int = 5, lease_seconds: int = 60) -> OutboxProcessResult:
    """Claim due events fairly, process them, and persist retry/dead-letter state.

    Claim is short and lease-based; worker execution happens outside the DB transaction. A
    crashed worker leaves an expired lease, so another worker can converge without duplicate
    disclosure. Delivery remains idempotent by event_id and canonical generation.
    """
    if not 1 <= limit <= 1_000 or not 1 <= max_attempts <= 100 or not 1 <= lease_seconds <= 3_600:
        raise ValueError("invalid outbox processing bounds")
    checked_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    rows = db.execute(
        select(PersonalRecallOutbox)
        .where(PersonalRecallOutbox.owner_id == owner_id)
        .order_by(PersonalRecallOutbox.occurred_at, PersonalRecallOutbox.event_id)
        .limit(limit)
    ).scalars().all()
    result = [0, 0, 0, 0, 0]
    for row in rows:
        delivery = db.get(PersonalRecallOutboxDelivery, row.event_id)
        if delivery is not None:
            if delivery.state in ("delivered", "dead_letter"):
                result[4] += 1
                continue
            if delivery.next_attempt_at is not None and delivery.next_attempt_at > checked_at:
                result[4] += 1
                continue
            if delivery.lease_until is not None and delivery.lease_until > checked_at:
                result[4] += 1
                continue
        else:
            delivery = PersonalRecallOutboxDelivery(event_id=row.event_id, owner_id=row.owner_id, attempt_count=0, state="pending", retryable=True)
            db.add(delivery)
        token = uuid.uuid4().hex
        delivery.attempt_count = int(delivery.attempt_count or 0) + 1
        delivery.last_attempt_at = checked_at
        delivery.lease_owner = token
        delivery.lease_until = checked_at + timedelta(seconds=lease_seconds)
        db.commit()
        result[0] += 1
        try:
            worker.enqueue(SourceChange(event_id=str(row.event_id), owner_id=owner_id, source_id=str(row.source_id), kind=_kind(row.source_type, str(row.event_type)), canonical_version=row.canonical_version, event_type=str(row.event_type), version_fenced=True))
            worker.run_once()
        except BaseException as exc:
            error_class, retryable, reason = _failure_class(exc)
            delivery = db.get(PersonalRecallOutboxDelivery, row.event_id)
            if delivery is None or delivery.lease_owner != token:
                result[4] += 1
                continue
            delivery.error_class = error_class
            delivery.last_error = reason
            delivery.retryable = retryable
            delivery.lease_owner = None
            delivery.lease_until = None
            if retryable and delivery.attempt_count < max_attempts:
                delivery.state = "retrying"
                delivery.next_attempt_at = checked_at + timedelta(seconds=_backoff(delivery.attempt_count, str(row.event_id)))
                result[2] += 1
            else:
                delivery.state = "dead_letter"
                delivery.blocked_reason = reason
                delivery.next_attempt_at = None
                result[3] += 1
            db.commit()
            continue
        delivery = db.get(PersonalRecallOutboxDelivery, row.event_id)
        if delivery is not None and delivery.lease_owner == token:
            delivery.state = "delivered"
            delivery.delivered_at = checked_at
            delivery.next_attempt_at = None
            delivery.lease_owner = None
            delivery.lease_until = None
            delivery.last_error = None
            db.commit()
            result[1] += 1
    return OutboxProcessResult(*result)


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


def retry_status(db: Session, *, owner_id: str) -> dict[str, int | float | None]:
    rows = db.execute(select(PersonalRecallOutboxDelivery).where(PersonalRecallOutboxDelivery.owner_id == owner_id)).scalars().all()
    counts = {"pending": 0, "retrying": 0, "delivered": 0, "dead_letter": 0, "attempts": 0}
    oldest = None
    for row in rows:
        counts[row.state] = counts.get(row.state, 0) + 1
        counts["attempts"] += int(row.attempt_count or 0)
        if row.state in ("pending", "retrying") and row.last_attempt_at is not None:
            age = (datetime.now(timezone.utc) - row.last_attempt_at.replace(tzinfo=timezone.utc)).total_seconds()
            oldest = age if oldest is None else max(oldest, age)
    return {**counts, "oldest_active_age_seconds": oldest}
