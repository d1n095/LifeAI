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

import logging
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.development_operator.service import DEVELOPMENT_CAPABILITIES, LOCAL_EXECUTION, LOCAL_WRITE, READ_ONLY
from app.mainai_execution.planner import create_goal
from app.models.mainai_execution import MainAIGoalRiskLevel
from app.models.project_entities import ProjectEntity
from app.models.work_candidate import WorkCandidate

logger = logging.getLogger(__name__)

# task_type may SUGGEST a proposed scope, it may never AUTHORIZE one (see migration 0057's
# own module docstring) -- entity_type is this module's own closest analogue to a task_type
# signal, so it is what the auto-proposal heuristic below uses to suggest capabilities. Paths
# are deliberately NEVER suggested here (see app.execution_envelopes.service.
# propose_execution_scope()'s own docstring on why an empty, honest proposal beats a guess).
#
# CAPABILITY VOCABULARY: these MUST be real app.development_operator.service.
# DEVELOPMENT_CAPABILITIES keys -- the exact vocabulary run_supervisor()'s own validate_scope()
# checks scope.allowed_capabilities against (app/development_supervisor/service.py). An
# earlier version of this dict used a coarser, made-up vocabulary ("repo_read"/"repo_edit"/
# "run_tests") that did not correspond to anything DEVELOPMENT_CAPABILITIES actually
# recognizes -- a founder authorizing that proposal exactly as suggested would have produced
# an envelope validate_scope() rejects outright. Found by adversarial review of PR #148
# (app/development_supervisor/production_entry.py, the first real consumer of these
# proposals), fixed here rather than by inventing a silent proposal -> capability translation
# layer at authorization time: proposal vocabulary must stay non-authoritative and the founder
# must be able to see, in the proposal itself, the EXACT capability strings that would apply
# if authorized -- not a coarse label a second, hidden mapping later expands.
#
# Derived from DEVELOPMENT_CAPABILITIES' own tier labels (never hand-typed) so a future
# capability added to READ_ONLY/LOCAL_WRITE/LOCAL_EXECUTION is automatically covered, and
# REMOTE_WRITE ("push_branch") can never end up here by construction -- real remote pushes
# remain a separate, not-yet-authorized capability throughout this whole mission.
_READ_ONLY_CAPABILITIES = tuple(sorted(name for name, tier in DEVELOPMENT_CAPABILITIES.items() if tier == READ_ONLY))
_READ_AND_LOCAL_WORK_CAPABILITIES = _READ_ONLY_CAPABILITIES + tuple(
    sorted(name for name, tier in DEVELOPMENT_CAPABILITIES.items() if tier in (LOCAL_WRITE, LOCAL_EXECUTION))
)

_PROPOSED_CAPABILITIES_BY_ENTITY_TYPE = {
    "task_reference": _READ_AND_LOCAL_WORK_CAPABILITIES,
    "decision": _READ_AND_LOCAL_WORK_CAPABILITIES,
    "idea": _READ_ONLY_CAPABILITIES,
}


class WorkCandidateError(ValueError):
    pass


def _propose_execution_scope_if_actionable(db: Session, *, owner_id: uuid.UUID, candidate: WorkCandidate, goal: Any, entity: ProjectEntity) -> None:
    """Purely observational, same doctrine as app/project_entities/service.py's own
    work-candidate wiring one level up the chain: never changes authorize_work_candidate()'s
    own result, never raises into the caller. Writes ONLY to execution_scope_proposals -- a
    staging table nothing treats as execution authority -- never creates an
    ExecutionAuthorizationEnvelope. See migration 0057's own module docstring:
    PROPOSED_SCOPE != AUTHORIZED_SCOPE.

    Uses a SAVEPOINT (db.begin_nested()), not a top-level commit/rollback, for the exact same
    reason app/project_entities/service.py's own analogous helper does: authorize_work_
    candidate() itself never commits (leaves that to its own caller), so a plain commit/
    rollback here would either surprise-commit the caller's still-open transaction or, on
    failure, roll back the goal authorization this is supposed to be a side effect OF."""

    proposed_capabilities = _PROPOSED_CAPABILITIES_BY_ENTITY_TYPE.get(entity.entity_type)
    if proposed_capabilities is None:
        return
    try:
        from app.execution_envelopes import propose_execution_scope

        savepoint = db.begin_nested()
        try:
            propose_execution_scope(
                db, owner_id=owner_id, goal_id=goal.id, idempotency_key=f"work-candidate-authorization:{candidate.id}",
                proposed_capabilities=list(proposed_capabilities), proposed_risk=goal.risk_level.value,
                proposal_reasoning=f"Derived from WorkCandidate {candidate.id} (ProjectEntity {entity.id}, entity_type={entity.entity_type}).",
                proposal_strategy="work_candidate_authorization_v1",
                provenance={"work_candidate_id": str(candidate.id), "project_entity_id": str(entity.id), "entity_type": entity.entity_type},
            )
            savepoint.commit()
        except Exception:
            savepoint.rollback()
            raise
    except Exception:
        logger.warning("failed to propose execution scope for goal %s (non-fatal)", goal.id, exc_info=True)


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

    entity = db.execute(select(ProjectEntity).where(ProjectEntity.id == row.source_entity_id, ProjectEntity.owner_id == owner_id)).scalar_one_or_none()
    if entity is not None:
        _propose_execution_scope_if_actionable(db, owner_id=owner_id, candidate=row, goal=goal, entity=entity)

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
