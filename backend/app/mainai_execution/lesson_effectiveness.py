"""The learning loop's missing back-edge: did applying a lesson actually do anything?

`lessons.py` applies lessons at plan time (injecting each lesson's `regression_test` as a
`targeted_tests` verification step, recorded durably as `lessons_applied` on the task's
`created` MainAITaskEvent). `lesson_from_verification.py` (#134) writes new lessons from
exhausted, structured verification failures. Between them, nothing ever looked back at whether
an applied lesson's own guard held — a lesson's `confidence` could only ever be what its writer
asserted at birth. This module closes that edge:

    lesson applied at plan time  ->  task finalizes with structured verification evidence
    ->  find THAT lesson's OWN target in THAT evidence  ->  append one effectiveness observation

SIGNAL PRODUCER != TRUTH WRITER. This module only appends `EngineeringLessonEffectiveness`
rows. It never touches `EngineeringLesson.confidence`/`status`/`evidence`. Aggregating
observations into a reviewable confidence signal is a separate, later step, and deliberately
not automatic.

Causality discipline, which is the entire risk here. A task that succeeds is not evidence that
every lesson in the system works:
  - no `lessons_applied` provenance for this task  ->  no rows at all
  - evidence without structured `steps` (CI-wait outcomes, exception-path finalize)  ->  no
    rows at all; there is nothing a lesson's guard could have shown up in
  - lesson applied but its own target absent from the evidence  ->  `insufficient_evidence`
  - the task's overall pass/fail NEVER, on its own, produces `reinforced`

Every `LessonEffectivenessOutcome` value has a real producer here — see
`classify_lesson_outcome()`. A state with no writer is the defect class this lane exists to
remove, so none were defined "for completeness".
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models.lesson_effectiveness import (
    EngineeringLessonEffectiveness,
    LessonEffectivenessAttributionConfidence as Confidence,
    LessonEffectivenessOutcome as Outcome,
)
from app.models.mainai_execution import (
    EngineeringLesson,
    EngineeringLessonStatus,
    MainAITask,
    MainAITaskEvent,
    MainAITaskEventType,
)

# pytest's own documented exit codes. 1 is "tests ran and failed" — a real verdict about the
# guarded behavior. 4 (usage error) and 5 (no tests collected) mean the named target is not a
# usable guard at all, which is a statement about the LESSON, not about the code it guards.
_PYTEST_TESTS_FAILED = 1
_PYTEST_TARGET_UNUSABLE = (4, 5)


def _source_ref(*, goal_id: uuid.UUID, task_id: uuid.UUID, job_id: uuid.UUID | None, lesson_id: uuid.UUID) -> str:
    job_part = str(job_id) if job_id is not None else "no_job"
    return f"goal:{goal_id}/task:{task_id}/job:{job_part}/lesson:{lesson_id}"


def applied_lesson_ids(db: Session, *, task: MainAITask) -> list[uuid.UUID]:
    """The lessons this task's plan actually applied, read back from the durable `created`
    MainAITaskEvent that `planner.py`/`plan_insertion.py` wrote at plan time.

    Deliberately NOT re-derived by calling `lookup_lessons()` again now: lessons recorded
    AFTER this task was planned never influenced it, and attributing this outcome to them
    would be exactly the manufactured causality this module exists to avoid."""
    event = db.execute(
        select(MainAITaskEvent)
        .where(MainAITaskEvent.task_id == task.id, MainAITaskEvent.event_type == MainAITaskEventType.created)
        .order_by(MainAITaskEvent.created_at.asc())
    ).scalars().first()
    if event is None or not isinstance(event.detail, dict):
        return []
    raw = event.detail.get("lessons_applied")
    if not isinstance(raw, list):
        return []
    out: list[uuid.UUID] = []
    for item in raw:
        try:
            out.append(uuid.UUID(str(item)))
        except (ValueError, AttributeError):
            continue
    return out


def _target_step(evidence: dict[str, Any], target: str) -> dict[str, Any] | None:
    """The verification step for this lesson's OWN regression target, if it ran at all."""
    for step in evidence.get("steps") or []:
        if not isinstance(step, dict) or step.get("kind") != "targeted_tests":
            continue
        detail = step.get("detail")
        if isinstance(detail, dict) and detail.get("target") == target:
            return step
    return None


def classify_lesson_outcome(
    *, lesson: EngineeringLesson, evidence: dict[str, Any], overall_passed: bool, conflicted: bool
) -> tuple[Outcome, Confidence, str, dict[str, Any]]:
    """Pure classifier — returns (outcome, attribution confidence, reason, evidence excerpt).

    Kept free of session/IO so the whole decision table is directly testable without
    constructing a task finalize, and so the reasoning below is the only place outcomes are
    decided."""
    if lesson.superseded_by is not None:
        return (
            Outcome.superseded,
            Confidence.low,
            "Lesson was superseded before this evidence arrived; the observation belongs to the "
            "superseding lesson's own future evidence, not to this one.",
            {},
        )
    if conflicted or lesson.status != EngineeringLessonStatus.active:
        return (
            Outcome.insufficient_evidence,
            Confidence.low,
            f"Lesson status is {lesson.status.value} or it is under unresolved conflict review — "
            "its guard must not be scored while its own validity is in question.",
            {},
        )
    if not lesson.regression_test:
        return (
            Outcome.insufficient_evidence,
            Confidence.low,
            "Lesson names no regression_test, so it contributed no verification target whose "
            "result could be attributed to it.",
            {},
        )

    step = _target_step(evidence, lesson.regression_test)
    if step is None:
        return (
            Outcome.insufficient_evidence,
            Confidence.low,
            "Lesson was applied at plan time but its regression target does not appear in this "
            "finalize's verification evidence — an unrelated outcome is not evidence about it.",
            {},
        )

    detail = step.get("detail") if isinstance(step.get("detail"), dict) else {}
    excerpt = {
        k: detail[k] for k in ("target", "returncode", "error", "timeout_seconds") if k in detail
    }
    returncode = detail.get("returncode")

    if step.get("passed") is True:
        if overall_passed:
            return (
                Outcome.reinforced,
                Confidence.high,
                "The lesson's own regression target ran and passed on a task where the lesson was "
                "applied, and the task's verification passed as a whole.",
                excerpt,
            )
        return (
            Outcome.context_specific,
            Confidence.medium,
            "The lesson's own regression target held, but the task failed on other verification "
            "steps — this finalize supports the lesson's guard only, not the task outcome.",
            excerpt,
        )

    if detail.get("error") is not None or returncode not in (_PYTEST_TESTS_FAILED, *_PYTEST_TARGET_UNUSABLE):
        return (
            Outcome.insufficient_evidence,
            Confidence.low,
            f"The lesson's regression target did not reach a verdict (error={detail.get('error')!r}, "
            f"returncode={returncode!r}) — neither held nor failed on its merits.",
            excerpt,
        )
    if returncode in _PYTEST_TARGET_UNUSABLE:
        return (
            Outcome.contradicted,
            Confidence.high,
            f"The lesson's named regression_test is not a usable guard (pytest exit {returncode}: "
            "usage error / nothing collected) — the lesson's own prescription is invalid as written.",
            excerpt,
        )
    return (
        Outcome.weakened,
        Confidence.high,
        "The lesson's own regression target ran and failed on a task where the lesson was applied "
        "— the prescribed guard did not hold where the lesson said it would.",
        excerpt,
    )


def record_lesson_effectiveness_from_finalize(
    db: Session,
    *,
    task: MainAITask,
    evidence: dict[str, Any],
    passed: bool,
    job_id: uuid.UUID | None = None,
) -> list[EngineeringLessonEffectiveness]:
    """Append one observation per lesson this task's plan actually applied.

    Called from `_finalize_task_outcome()` for BOTH pass and fail — a lesson's guard holding on
    a successful task is exactly as informative as it failing on a broken one, and only writing
    on failure would bias every lesson's evidence toward the negative.

    No-op unless `evidence` carries structured verification `steps`. CI-wait and exception-path
    finalizes have no step results a lesson's target could appear in, so they produce no rows at
    all rather than a pile of `insufficient_evidence` noise.

    Never raises into the caller's finalize transaction: inserts use ON CONFLICT DO NOTHING on
    `source_ref` instead of select-then-insert, so a concurrent or replayed finalize can never
    turn a duplicate observation into an IntegrityError that rolls back the task's own terminal
    state. Does not commit — the caller owns that transaction."""
    if not isinstance(evidence, dict) or not isinstance(evidence.get("steps"), list):
        return []

    lesson_ids = applied_lesson_ids(db, task=task)
    if not lesson_ids:
        return []

    lessons = db.execute(
        select(EngineeringLesson).where(EngineeringLesson.id.in_(lesson_ids))
    ).scalars().all()
    if not lessons:
        return []

    from app.mainai_execution.lesson_conflicts import find_conflict_candidate_pairs

    conflicted_ids: set[uuid.UUID] = set()
    for a, b in find_conflict_candidate_pairs(db, lessons=list(lessons)):
        conflicted_ids.add(a.id)
        conflicted_ids.add(b.id)

    written: list[EngineeringLessonEffectiveness] = []
    for lesson in lessons:
        outcome, confidence, reason, excerpt = classify_lesson_outcome(
            lesson=lesson,
            evidence=evidence,
            overall_passed=passed,
            conflicted=lesson.id in conflicted_ids,
        )
        source_ref = _source_ref(goal_id=task.goal_id, task_id=task.id, job_id=job_id, lesson_id=lesson.id)
        db.execute(
            pg_insert(EngineeringLessonEffectiveness)
            .values(
                id=uuid.uuid4(),
                owner_id=task.owner_id,
                lesson_id=lesson.id,
                task_id=task.id,
                goal_id=task.goal_id,
                job_id=job_id,
                outcome=outcome.value,
                attribution_confidence=confidence.value,
                relevance_reason=reason,
                verification_target=lesson.regression_test,
                evidence={
                    "task_type": task.task_type,
                    "overall_passed": passed,
                    "attempts": task.attempts,
                    "target_step": excerpt,
                },
                source_ref=source_ref,
            )
            .on_conflict_do_nothing(constraint="uq_engineering_lesson_effectiveness_source_ref")
        )
        row = db.execute(
            select(EngineeringLessonEffectiveness).where(
                EngineeringLessonEffectiveness.source_ref == source_ref
            )
        ).scalar_one_or_none()
        if row is not None:
            written.append(row)
    return written
