"""Refuse duplicate live assignments, repeated paid prompts, and cyclic task graphs.

Durable state is the source of truth. An open request, a live assignment, or an already
dispatched agent task is enough to refuse a second grant/prompt for the same work.
A stored dependency cycle is never assignable.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.mainai_founder_ops.status import REDISPATCHABLE_AGENT_STATUSES
from app.mainai_founder_ops.workflow import detect_dependency_cycles, load_dependency_edges, task_ids_in_cycles
from app.models.agent_task import AgentTask
from app.models.workforce import WorkforceAssignment, WorkforceDelegationRequest


class DuplicateWorkError(Exception):
    """Second live assignment or second prompt for work that is already in flight."""

    def __init__(self, message: str, *, code: str, existing_id: str | None = None):
        super().__init__(message)
        self.code = code
        self.existing_id = existing_id


class CircularDependencyError(DuplicateWorkError):
    """Assignment refused because the task sits in a stored dependency cycle."""


LIVE_ASSIGNMENT_STATUSES = ("assigned", "acknowledged", "running", "awaiting_verification")
OPEN_REQUEST_STATUSES = ("open", "resolving", "assigned")


def live_assignment_for_task(
    db: Session, *, owner_id: uuid.UUID, task_id: uuid.UUID
) -> WorkforceAssignment | None:
    return db.execute(
        select(WorkforceAssignment)
        .join(
            WorkforceDelegationRequest,
            WorkforceAssignment.delegation_request_id == WorkforceDelegationRequest.id,
        )
        .where(
            WorkforceAssignment.owner_id == owner_id,
            WorkforceAssignment.revoked_at.is_(None),
            WorkforceAssignment.status.in_(LIVE_ASSIGNMENT_STATUSES),
            WorkforceDelegationRequest.task_id == task_id,
        )
        .order_by(WorkforceAssignment.created_at.asc())
        .limit(1)
    ).scalars().first()


def open_request_for_task(
    db: Session, *, owner_id: uuid.UUID, task_id: uuid.UUID
) -> WorkforceDelegationRequest | None:
    return db.execute(
        select(WorkforceDelegationRequest)
        .where(
            WorkforceDelegationRequest.owner_id == owner_id,
            WorkforceDelegationRequest.task_id == task_id,
            WorkforceDelegationRequest.status.in_(OPEN_REQUEST_STATUSES),
        )
        .order_by(WorkforceDelegationRequest.created_at.asc())
        .limit(1)
    ).scalars().first()


def assert_no_circular_dependency(db: Session, *, owner_id: uuid.UUID, task_id: uuid.UUID | None) -> None:
    if task_id is None:
        return
    cycles = detect_dependency_cycles(load_dependency_edges(db, owner_id=owner_id))
    if str(task_id) in task_ids_in_cycles(cycles):
        matching = next(cycle for cycle in cycles if str(task_id) in cycle)
        raise CircularDependencyError(
            f"circular_dependency: task {task_id} sits in cycle {matching}",
            code="circular_dependency",
            existing_id=str(task_id),
        )


def assert_no_duplicate_task_assignment(
    db: Session,
    *,
    owner_id: uuid.UUID,
    task_id: uuid.UUID | None,
    excluding_request_id: uuid.UUID | None = None,
) -> None:
    """Fail closed if this MainAI task already has an open request or live assignment."""
    if task_id is None:
        return
    assert_no_circular_dependency(db, owner_id=owner_id, task_id=task_id)
    live = live_assignment_for_task(db, owner_id=owner_id, task_id=task_id)
    if live is not None:
        raise DuplicateWorkError(
            f"duplicate_live_assignment: task {task_id} already has live assignment {live.id}",
            code="duplicate_live_assignment",
            existing_id=str(live.id),
        )
    existing = open_request_for_task(db, owner_id=owner_id, task_id=task_id)
    if existing is not None and existing.id != excluding_request_id:
        raise DuplicateWorkError(
            f"duplicate_open_request: task {task_id} already has request {existing.id}",
            code="duplicate_open_request",
            existing_id=str(existing.id),
        )


def assert_agent_dispatch_allowed(task: AgentTask) -> None:
    """Refuse a second paid code-agent prompt unless the task is created or needs correction."""
    if task.status in REDISPATCHABLE_AGENT_STATUSES:
        return
    raise DuplicateWorkError(
        f"duplicate_dispatch: agent task {task.id} is already {task.status.value}",
        code="duplicate_dispatch",
        existing_id=str(task.id),
    )
