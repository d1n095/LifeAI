"""Engineering lesson / safety memory foundation (migration 0032's `engineering_lessons` —
see app/models/mainai_execution.py's EngineeringLesson for the full column rationale,
including why this is deliberately its OWN table, not RLS-protected, and never auto-promoted
to founder-approved truth). Two things live here:

  - record_lesson()/lookup_lessons(): plain CRUD-ish persistence and tag-based lookup, with
    provenance (source_type/source_ref) mandatory at the model level (NOT NULL columns) — there
    is no code path in this module that can create an unsourced lesson.
  - apply_lessons_to_verification_plan(): the one place a lesson is allowed to actually DO
    something, wired into app/mainai_execution/planner.py's create_plan(). A lesson whose
    `applies_to` tags include a task's `task_type` and which names a `regression_test` gets that
    regression test added to the task's `verification_plan` if it isn't already covered — real
    influence on planning, not a loose AI-generated summary with no source and no effect. Every
    task whose verification_plan was touched this way gets a `lessons_applied` list recorded on
    its `created` MainAITaskEvent (see planner.py) so the provenance survives in the durable
    event history, not just in this function's return value."""

import uuid

from sqlalchemy import String, cast, select
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Session

from app.models.mainai_execution import EngineeringLesson, EngineeringLessonConfidence, EngineeringLessonStatus


def record_lesson(
    db: Session,
    *,
    problem: str,
    root_cause: str,
    affected_component: str,
    severity,
    evidence: str,
    fix: str,
    general_rule: str,
    applies_to: list[str],
    source_type: str,
    source_ref: str,
    created_by: str,
    first_seen_at,
    regression_test: str | None = None,
    confidence: EngineeringLessonConfidence = EngineeringLessonConfidence.likely,
) -> EngineeringLesson:
    lesson = EngineeringLesson(
        status=EngineeringLessonStatus.active,
        problem=problem,
        root_cause=root_cause,
        affected_component=affected_component,
        severity=severity,
        evidence=evidence,
        fix=fix,
        regression_test=regression_test,
        general_rule=general_rule,
        applies_to=applies_to,
        source_type=source_type,
        source_ref=source_ref,
        first_seen_at=first_seen_at,
        confidence=confidence,
        created_by=created_by,
    )
    db.add(lesson)
    db.flush()
    return lesson


def lookup_lessons(db: Session, *, applies_to_any: list[str]) -> list[EngineeringLesson]:
    """Active lessons tagged with ANY of `applies_to_any` — uses the GIN-indexed jsonb `?|`
    "any array element matches" operator (migration 0032's own ix_engineering_lessons_applies_to
    index exists specifically for this query shape), not a Python-side scan."""
    if not applies_to_any:
        return []
    # The `?|` jsonb operator requires a real Postgres text[] on its right-hand side -- without
    # this explicit ARRAY(String) cast, SQLAlchemy binds the Python list as a JSON-encoded
    # string parameter instead, which Postgres then rejects (DataError: operator does not
    # exist: jsonb ?| text).
    tags = cast(applies_to_any, ARRAY(String))
    return (
        db.execute(
            select(EngineeringLesson)
            .where(EngineeringLesson.status == EngineeringLessonStatus.active, EngineeringLesson.applies_to.op("?|")(tags))
            .order_by(EngineeringLesson.severity.desc(), EngineeringLesson.created_at.desc())
        )
        .scalars()
        .all()
    )


def apply_lessons_to_verification_plan(db: Session, *, task_type: str, verification_plan: list[dict]) -> tuple[list[dict], list[uuid.UUID]]:
    """Returns (possibly-augmented verification_plan, ids of lessons that actually changed
    it) -- called once per planned task from planner.py's create_plan(), never from the
    executor (a lesson influences what gets PLANNED, not what an already-dispatched task does
    on the fly, which would be an untracked runtime behavior change instead of a durable,
    reviewable planning decision)."""
    lessons = lookup_lessons(db, applies_to_any=[task_type])
    existing_targets = {step.get("target") for step in verification_plan if step.get("kind") == "targeted_tests"}

    augmented = list(verification_plan)
    applied_lesson_ids: list[uuid.UUID] = []
    for lesson in lessons:
        if not lesson.regression_test or lesson.regression_test in existing_targets:
            continue
        augmented.append({"kind": "targeted_tests", "target": lesson.regression_test})
        existing_targets.add(lesson.regression_test)
        applied_lesson_ids.append(lesson.id)

    return augmented, applied_lesson_ids
