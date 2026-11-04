"""Canonical PostgreSQL execution-event outbox; payload is routing/evidence metadata only."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.mainai_execution_event import MainAIExecutionEvent


def append_execution_event(db: Session, *, owner_id: uuid.UUID, job_id: uuid.UUID, event_type: str, attempt_id: str | None = None, metadata: dict | None = None, event_id: uuid.UUID | None = None) -> MainAIExecutionEvent:
    safe = {str(key): value for key, value in (metadata or {}).items() if key in {"sha", "base_sha", "branch", "failure_class", "provider", "lease_generation", "reason", "scope"}}
    if len(str(safe)) > 4_000:
        raise ValueError("execution event metadata exceeds bound")
    identity = event_id or uuid.uuid4()
    existing = db.get(MainAIExecutionEvent, identity, populate_existing=True)
    if existing is not None:
        if existing.owner_id != owner_id or existing.job_id != job_id or existing.event_type != event_type:
            raise ValueError("execution event identity collision")
        return existing
    event = MainAIExecutionEvent(id=identity, owner_id=owner_id, job_id=job_id, event_type=event_type, attempt_id=attempt_id, event_metadata=safe, created_at=datetime.now(timezone.utc))
    db.add(event)
    return event


def deliver_execution_events(db: Session, *, owner_id: uuid.UUID, handler, limit: int = 100, max_attempts: int = 5) -> tuple[int, int, int]:
    if not 1 <= limit <= 1_000 or not 1 <= max_attempts <= 20:
        raise ValueError("invalid delivery bounds")
    rows = db.execute(select(MainAIExecutionEvent).where(MainAIExecutionEvent.owner_id == owner_id, MainAIExecutionEvent.state.in_(("pending", "retrying")), (MainAIExecutionEvent.next_attempt_at.is_(None) | (MainAIExecutionEvent.next_attempt_at <= datetime.now(timezone.utc)))).order_by(MainAIExecutionEvent.created_at, MainAIExecutionEvent.id).limit(limit)).scalars().all()
    delivered = dead = retrying = 0
    for event in rows:
        try:
            handler({"event_id": str(event.id), "owner_id": str(event.owner_id), "job_id": str(event.job_id), "event_type": event.event_type, "attempt_id": event.attempt_id, "metadata": dict(event.event_metadata or {})})
        except Exception:
            event.attempts += 1
            if event.attempts >= max_attempts:
                event.state = "dead_letter"
                event.blocked_reason = "bounded delivery failure"
                dead += 1
            else:
                event.state = "retrying"
                event.next_attempt_at = datetime.now(timezone.utc) + timedelta(seconds=min(3_600, 2 ** event.attempts))
                retrying += 1
            continue
        event.state = "delivered"
        event.delivered_at = datetime.now(timezone.utc)
        event.attempts += 1
        delivered += 1
    db.commit()
    return delivered, dead, retrying
