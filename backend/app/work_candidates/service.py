"""Life Work Candidates -- the staging layer between structured project understanding
(`app.project_entities`) and real, governed MainAI work (`app.mainai_execution.planner.
create_goal()`). See migration 0055's own module docstring for the full architecture.

Hard rule, structural not just documented: `record_work_candidate()` NEVER creates a
`MainAIGoal`, directly or indirectly. The ONLY function in this module that can is
`authorize_work_candidate()`, and it ALWAYS requires the caller to supply `authorized_by`
explicitly -- the candidate's own `classifier_confidence` (a fact about how strongly the
source `ProjectEntity` was itself evidenced) is never silently treated as founder
authorization. `authorize_work_candidate()` does not reimplement goal creation or duplicate
`create_goal()`'s own approval-policy semantics -- it calls that exact function, the same one
`app/routers/mainai_execution.py`'s `Depends(require_founder)`-gated route already uses, so a
work candidate can never bypass whatever authorization boundary wraps real goal creation
elsewhere in the system. This module only decides WHETHER to call it and WITH WHAT ARGUMENTS,
never WHETHER THE CALLER IS ALLOWED TO -- that remains entirely the caller's own
responsibility, exactly like `create_goal()` itself already documents."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.mainai_execution.planner import create_goal
from app.models.mainai_execution import MainAIGoalRiskLevel
from app.models.project_entities import ProjectEntity
from app.models.work_candidate import WorkCandidate


class WorkCandidateError(ValueError):
    pass


def _same(row: WorkCandidate, values: dict[str, Any]) -> WorkCandidate:
    differing = [key for key, value in values.items() if getattr(row, key) != value]
    if differing:
        raise WorkCandidateError(f"idempotency key reused with different fields: {', '.join(sorted(differing))}")
    return row


def record_work_candidate(
    db: Session,
    *,
    owner_id: uuid.UUID,
    source_entity_id: uuid.UUID,
    title: str,
    idempotency_key: str,
    rationale: str | None = None,
    dependencies: list[Any] | None = None,
    priority: str = "medium",
    classifier_strategy: str = "unknown",
    classifier_confidence: float | None = None,
    provenance: dict[str, Any] | None = None,
) -> WorkCandidate:
    """Records ONE work candidate -- never a claim that this work is authorized, only a claim
    that a signal producer noticed a piece of structured project understanding that might be
    actionable. Never raises for "the candidate turned out to be noise" -- that judgment
    happens later, explicitly, via `dismiss_work_candidate()`/`authorize_work_candidate()`,
    never here.

    Fails closed BEFORE any write if `source_entity_id` does not structurally belong to
    `owner_id` -- a clear, typed error here, with the database's own composite FK (migration
    0056) as the final backstop."""

    entity = db.execute(select(ProjectEntity).where(ProjectEntity.id == source_entity_id, ProjectEntity.owner_id == owner_id)).scalar_one_or_none()
    if entity is None:
        raise WorkCandidateError(f"source_entity_id={source_entity_id} does not belong to owner_id={owner_id}")

    values: dict[str, Any] = dict(
        source_entity_id=source_entity_id, title=title, rationale=rationale, dependencies=dependencies or [],
        priority=priority, classifier_strategy=classifier_strategy, classifier_confidence=classifier_confidence,
        provenance=provenance or {},
    )
    existing = db.execute(
        select(WorkCandidate).where(WorkCandidate.owner_id == owner_id, WorkCandidate.idempotency_key == idempotency_key)
    ).scalar_one_or_none()
    if existing:
        return _same(existing, values)

    row = WorkCandidate(owner_id=owner_id, idempotency_key=idempotency_key, status="unreviewed", **values)
    db.add(row)
    db.flush()
    return row


def dismiss_work_candidate(db: Session, *, owner_id: uuid.UUID, candidate_id: uuid.UUID, reason: str) -> WorkCandidate:
    """An explicit "this candidate is not worth pursuing" outcome -- never deletes the row,
    so the same non-candidate is not re-surfaced for review indefinitely without a durable
    record that it was already considered."""

    row = db.execute(
        select(WorkCandidate).where(WorkCandidate.id == candidate_id, WorkCandidate.owner_id == owner_id).with_for_update()
    ).scalar_one_or_none()
    if row is None:
        raise WorkCandidateError("work candidate is missing or belongs to another owner")
    if row.status != "unreviewed":
        raise WorkCandidateError(f"work candidate is already {row.status}, not unreviewed")
    row.status = "dismissed"
    row.dismissed_reason = reason
    row.updated_at = datetime.utcnow()
    db.flush()
    return row


def authorize_work_candidate(
    db: Session,
    *,
    owner_id: uuid.UUID,
    candidate_id: uuid.UUID,
    authorized_by: str,
    title: str | None = None,
    risk_level: MainAIGoalRiskLevel = MainAIGoalRiskLevel.low,
    approval_policy: str = "standard_repo_work",
) -> tuple[WorkCandidate, Any]:
    """The ONLY path from a work candidate to real, executable MainAI work -- DERIVED WORK
    CANDIDATE != AUTHORIZED WORK != EXECUTABLE WORK enforced here, not just documented.
    `authorized_by` is ALWAYS the caller's own explicit assertion (this function has no
    default, and never reads it off the candidate's own classifier fields) -- a good
    inference existing does not grant execution authority. Delegates the actual `MainAIGoal`
    row to `create_goal()` itself -- this function never constructs one directly, so any
    future change to goal-creation semantics (approval policy, risk gating) applies here
    automatically, not as a second, potentially-diverging implementation."""

    row = db.execute(
        select(WorkCandidate).where(WorkCandidate.id == candidate_id, WorkCandidate.owner_id == owner_id).with_for_update()
    ).scalar_one_or_none()
    if row is None:
        raise WorkCandidateError("work candidate is missing or belongs to another owner")
    if row.status != "unreviewed":
        raise WorkCandidateError(f"work candidate is already {row.status}, not unreviewed")

    goal = create_goal(
        db, owner_id=owner_id, title=title or row.title, original_instruction=row.rationale or row.title,
        created_by=authorized_by, risk_level=risk_level, approval_policy=approval_policy,
    )
    db.flush()
    row.status = "authorized"
    row.authorized_goal_id = goal.id
    row.updated_at = datetime.utcnow()
    db.flush()
    return row, goal


def get_work_candidate(db: Session, *, owner_id: uuid.UUID, candidate_id: uuid.UUID) -> WorkCandidate | None:
    return db.execute(select(WorkCandidate).where(WorkCandidate.id == candidate_id, WorkCandidate.owner_id == owner_id)).scalar_one_or_none()


def list_work_candidates(db: Session, *, owner_id: uuid.UUID, status: str | None = None) -> list[WorkCandidate]:
    stmt = select(WorkCandidate).where(WorkCandidate.owner_id == owner_id)
    if status is not None:
        stmt = stmt.where(WorkCandidate.status == status)
    return list(db.execute(stmt.order_by(WorkCandidate.observed_at)).scalars().all())


def list_unreviewed_work_candidates(db: Session, *, owner_id: uuid.UUID) -> list[WorkCandidate]:
    return list_work_candidates(db, owner_id=owner_id, status="unreviewed")
