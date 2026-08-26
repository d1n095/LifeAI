"""Durable wake edges for provider-planning waits (spend park + WAITING_PROVIDER backoff).

Park/release writers live in development_supervisor; this module owns reconsideration so
founder authorization commits and worker ticks can recover the same way after a crash
between grant and wake.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.jobs.retry import compute_backoff_seconds
from app.mainai_execution.executor import _lock_task
from app.models.mainai_execution import MainAITask, MainAITaskEvent, MainAITaskEventType, MainAITaskStatus

# Event.detail["reason"] — the ONLY durable classifiers for these parks (never English substring).
PROVIDER_SPEND_PARK_REASON = "provider_spend_not_authorized_park"
WAITING_PROVIDER_BACKOFF_REASON = "waiting_provider_defer_backoff"
WAITING_PROVIDER_EXHAUSTED_REASON = "waiting_provider_defer_exhausted"

PROVIDER_SPEND_PARK_BLOCKER = (
    "provider-assisted planning is not authorized for this scope"
)
WAITING_PROVIDER_BLOCKER = "provider planning is unavailable"

# Bounded WAITING_PROVIDER retries before leaving blocked without a clock (founder/outage review).
WAITING_PROVIDER_MAX_BACKOFFS = 8
WAITING_PROVIDER_BACKOFF_BASE_SECONDS = 30.0
WAITING_PROVIDER_BACKOFF_CAP_SECONDS = 900.0


def _latest_block_or_retry_reason(db: Session, *, task: MainAITask) -> str | None:
    event = db.execute(
        select(MainAITaskEvent)
        .where(
            MainAITaskEvent.task_id == task.id,
            MainAITaskEvent.owner_id == task.owner_id,
            MainAITaskEvent.event_type.in_(
                [MainAITaskEventType.blocked, MainAITaskEventType.retry_scheduled]
            ),
        )
        .order_by(MainAITaskEvent.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if event is None:
        return None
    reason = (event.detail or {}).get("reason")
    return reason if isinstance(reason, str) else None


def is_provider_spend_park(db: Session, *, task: MainAITask) -> bool:
    if task.status != MainAITaskStatus.blocked:
        return False
    reason = _latest_block_or_retry_reason(db, task=task)
    if reason == PROVIDER_SPEND_PARK_REASON:
        return True
    return task.blocker_reason == PROVIDER_SPEND_PARK_BLOCKER


def is_waiting_provider_backoff_park(db: Session, *, task: MainAITask) -> bool:
    if task.status != MainAITaskStatus.blocked:
        return False
    reason = _latest_block_or_retry_reason(db, task=task)
    return reason in {WAITING_PROVIDER_BACKOFF_REASON, WAITING_PROVIDER_EXHAUSTED_REASON}


def count_waiting_provider_backoffs(db: Session, *, task: MainAITask) -> int:
    events = (
        db.execute(
            select(MainAITaskEvent).where(
                MainAITaskEvent.task_id == task.id,
                MainAITaskEvent.owner_id == task.owner_id,
                MainAITaskEvent.event_type == MainAITaskEventType.blocked,
            )
        )
        .scalars()
        .all()
    )
    return sum(
        1
        for event in events
        if (event.detail or {}).get("reason") == WAITING_PROVIDER_BACKOFF_REASON
    )


def compute_waiting_provider_retry_at(*, prior_backoffs: int, now: datetime | None = None) -> datetime | None:
    """None means exhausted — stay blocked with no clock until founder/outage intervention."""
    if prior_backoffs >= WAITING_PROVIDER_MAX_BACKOFFS:
        return None
    now = now or datetime.utcnow()
    delay = compute_backoff_seconds(
        prior_backoffs,
        base=WAITING_PROVIDER_BACKOFF_BASE_SECONDS,
        cap=WAITING_PROVIDER_BACKOFF_CAP_SECONDS,
    )
    return now + timedelta(seconds=delay)


def wake_tasks_blocked_for_provider_spend(
    db: Session,
    *,
    owner_id: uuid.UUID,
    goal_id: uuid.UUID,
    execution_envelope_id: uuid.UUID,
) -> list[MainAITask]:
    """Reconsider tasks parked specifically for missing provider-spend on this goal.

    Requires a still-active grant for the SAME owner+goal+envelope (caller verifies grant;
    this only filters tasks). Cross-owner / wrong-goal / unrelated blockers are never woken.
    Idempotent: already-ready tasks are skipped.
    """
    from app.models.provider_spend import (
        ProviderSpendAuthorization,
        ProviderSpendAuthorizationStatus,
    )

    grant = db.execute(
        select(ProviderSpendAuthorization).where(
            ProviderSpendAuthorization.owner_id == owner_id,
            ProviderSpendAuthorization.goal_id == goal_id,
            ProviderSpendAuthorization.execution_envelope_id == execution_envelope_id,
            ProviderSpendAuthorization.status == ProviderSpendAuthorizationStatus.active.value,
        )
    ).scalar_one_or_none()
    if grant is None:
        return []
    if grant.expires_at is not None and grant.expires_at <= datetime.utcnow():
        return []

    candidates = (
        db.execute(
            select(MainAITask).where(
                MainAITask.owner_id == owner_id,
                MainAITask.goal_id == goal_id,
                MainAITask.status == MainAITaskStatus.blocked,
            )
        )
        .scalars()
        .all()
    )
    woken: list[MainAITask] = []
    for task in candidates:
        if not is_provider_spend_park(db, task=task):
            continue
        locked = _lock_task(db, task.id)
        if locked.status != MainAITaskStatus.blocked:
            continue
        if not is_provider_spend_park(db, task=locked):
            continue
        locked.status = MainAITaskStatus.ready
        locked.blocker_reason = None
        locked.next_retry_at = None
        db.add(
            MainAITaskEvent(
                task_id=locked.id,
                owner_id=locked.owner_id,
                event_type=MainAITaskEventType.retry_scheduled,
                detail={
                    "reason": "provider_spend_grant_wake",
                    "authorization_id": str(grant.id),
                    "execution_envelope_id": str(execution_envelope_id),
                    "attempts": locked.attempts,
                },
            )
        )
        woken.append(locked)
    if woken:
        db.flush()
    return woken


def wake_due_waiting_provider_backoff_tasks(
    db: Session, *, limit: int = 20
) -> list[MainAITask]:
    """Worker clock: blocked WAITING_PROVIDER parks whose next_retry_at has elapsed → ready."""
    now = datetime.utcnow()
    due = (
        db.execute(
            select(MainAITask)
            .where(
                MainAITask.status == MainAITaskStatus.blocked,
                MainAITask.next_retry_at.isnot(None),
                MainAITask.next_retry_at <= now,
            )
            .order_by(MainAITask.next_retry_at.asc(), MainAITask.id.asc())
            .limit(limit)
        )
        .scalars()
        .all()
    )
    woken: list[MainAITask] = []
    for task in due:
        if not is_waiting_provider_backoff_park(db, task=task):
            continue
        if _latest_block_or_retry_reason(db, task=task) == WAITING_PROVIDER_EXHAUSTED_REASON:
            continue
        locked = _lock_task(db, task.id)
        if locked.status != MainAITaskStatus.blocked:
            continue
        if locked.next_retry_at is None or locked.next_retry_at > now:
            continue
        if not is_waiting_provider_backoff_park(db, task=locked):
            continue
        locked.status = MainAITaskStatus.ready
        locked.blocker_reason = None
        locked.next_retry_at = None
        db.add(
            MainAITaskEvent(
                task_id=locked.id,
                owner_id=locked.owner_id,
                event_type=MainAITaskEventType.retry_scheduled,
                detail={
                    "reason": "waiting_provider_backoff_wake",
                    "attempts": locked.attempts,
                },
            )
        )
        woken.append(locked)
    if woken:
        db.flush()
    return woken


def reconcile_provider_spend_parks_for_active_grants(
    db: Session, *, limit: int = 50
) -> list[MainAITask]:
    """Crash-recovery: any active grant may wake matching parks even if authorize's wake was lost."""
    from app.models.provider_spend import (
        ProviderSpendAuthorization,
        ProviderSpendAuthorizationStatus,
    )

    now = datetime.utcnow()
    grants = (
        db.execute(
            select(ProviderSpendAuthorization)
            .where(ProviderSpendAuthorization.status == ProviderSpendAuthorizationStatus.active.value)
            .order_by(ProviderSpendAuthorization.created_at.asc())
            .limit(limit)
        )
        .scalars()
        .all()
    )
    woken: list[MainAITask] = []
    for grant in grants:
        if grant.expires_at is not None and grant.expires_at <= now:
            continue
        woken.extend(
            wake_tasks_blocked_for_provider_spend(
                db,
                owner_id=grant.owner_id,
                goal_id=grant.goal_id,
                execution_envelope_id=grant.execution_envelope_id,
            )
        )
    return woken
