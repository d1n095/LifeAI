"""The learning loop's missing back-edge: what happened to an applied lesson's own guard?

`lessons.py` applies lessons at plan time (injecting each lesson's `regression_test` as a
`targeted_tests` verification step, recorded durably as `lessons_applied` on the task's
`created` MainAITaskEvent). `lesson_from_verification.py` (#134) writes new lessons from
exhausted, structured verification failures. Between them, nothing ever looked back at whether
an applied lesson's guard held — a lesson's `confidence` could only ever be what its writer
asserted at birth. This module closes that edge:

    lesson applied at plan time  ->  task finalizes with structured verification evidence
    ->  find THAT lesson's OWN target in THAT evidence  ->  append one guard observation

GUARD EVIDENCE != LESSON EFFECTIVENESS, and this module is scoped to the first. Read what
applying a lesson actually does: `apply_lessons_to_verification_plan()` appends the lesson's
`regression_test` to the verification plan and records `lessons_applied`. It does not change
the implementation strategy, the constraints, or the work. So when that target later passes,
the honest reading is

    this lesson's named guard was exercised, and it held in this execution context

and NOT

    the lesson changed how the work was done, or caused the task to succeed

The same guard would have passed on a task the lesson had no influence over whatsoever. A
system that recorded the first and reported the second would be manufacturing confidence about
its own behavior, which is the precise failure this subsystem exists to prevent. So no outcome
here is named for a verdict on the lesson, and `LessonGuardEvidenceStrength` grades how directly
the evidence speaks about the GUARD — it is not a causal attribution confidence and no value,
including `direct`, licenses an effectiveness claim.

A genuine "did this lesson help?" answer needs an edge that does not exist yet: durable
provenance for how a lesson altered the later plan or execution (lesson -> changed planning
decision -> affected step/constraint/strategy -> execution -> comparable outcome evidence).
When that edge exists, these observations become one input to it. They are deliberately not
shaped as a down payment on it.

SIGNAL PRODUCER != TRUTH WRITER. This module only appends `EngineeringLessonGuardObservation`
rows. It never touches `EngineeringLesson.confidence`/`status`/`evidence`. Aggregating
observations into a reviewable signal is a separate, later step, and deliberately not automatic.

Fail-closed about relevance. A task that succeeds is not evidence about every lesson in the
system:
  - no `lessons_applied` provenance for this task  ->  no rows at all
  - evidence without structured `steps` (CI-wait outcomes, exception-path finalize)  ->  no
    rows at all; there is nothing a lesson's guard could have shown up in
  - lesson applied but its own target absent from the evidence  ->  `guard_not_exercised`
  - the task's overall pass/fail NEVER, on its own, produces a positive observation

Every `LessonGuardOutcome` value has a real producer here — see `classify_guard_outcome()`. A
state with no writer is the defect class this lane exists to remove, so none were defined "for
completeness".
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models.lesson_guard_observations import (
    EngineeringLessonGuardObservation,
    LessonGuardEvidenceStrength as Strength,
    LessonGuardOutcome as Outcome,
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
    would be exactly the manufactured relevance this module exists to avoid."""
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


def classify_guard_outcome(
    *, lesson: EngineeringLesson, evidence: dict[str, Any], overall_passed: bool, conflicted: bool
) -> tuple[Outcome, Strength, str, dict[str, Any]]:
    """Pure classifier — returns (outcome, guard evidence strength, reason, evidence excerpt).

    Kept free of session/IO so the whole decision table is directly testable without
    constructing a task finalize, and so the reasoning below is the only place outcomes are
    decided.

    Note the asymmetry, which is intentional rather than an oversight: `guard_failed` and
    `guard_unusable` ARE statements about the lesson (its own prescription did not hold, or is
    not runnable as written), while `guard_held` is only a statement about this execution
    context. Negative guard evidence is genuinely attributable to the lesson; positive guard
    evidence is not. Every reason string below is worded to survive being read literally out of
    the database by something that did not read this docstring."""
    if lesson.superseded_by is not None:
        return (
            Outcome.lesson_superseded,
            Strength.none,
            "Lesson was superseded before this evidence arrived; the observation belongs to the "
            "superseding lesson's own future evidence, not to this one.",
            {},
        )
    if conflicted or lesson.status != EngineeringLessonStatus.active:
        return (
            Outcome.guard_not_exercised,
            Strength.none,
            f"Lesson status is {lesson.status.value} or it is under unresolved conflict review — "
            "its guard must not be scored while its own validity is in question.",
            {},
        )
    if not lesson.regression_test:
        return (
            Outcome.guard_not_exercised,
            Strength.none,
            "Lesson names no regression_test, so it contributed no verification target whose "
            "result could be observed at all.",
            {},
        )

    step = _target_step(evidence, lesson.regression_test)
    if step is None:
        return (
            Outcome.guard_not_exercised,
            Strength.none,
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
                Outcome.guard_held,
                Strength.direct,
                "The lesson's own regression target ran and passed on a task where the lesson was "
                "applied, and the task's verification passed as a whole. This shows the guard was "
                "exercised and held in this context. It does NOT show the lesson changed how the "
                "work was done or caused the task to pass — applying a lesson currently only adds "
                "this target to the verification plan, so the same target would also have passed "
                "on a task the lesson had no influence over.",
                excerpt,
            )
        return (
            Outcome.guard_held_task_failed_elsewhere,
            Strength.partial,
            "The lesson's own regression target held, but the task failed on other verification "
            "steps — this finalize speaks to the guard only, and to nothing about the task outcome.",
            excerpt,
        )

    if detail.get("error") is not None or returncode not in (_PYTEST_TESTS_FAILED, *_PYTEST_TARGET_UNUSABLE):
        return (
            Outcome.guard_not_exercised,
            Strength.none,
            f"The lesson's regression target did not reach a verdict (error={detail.get('error')!r}, "
            f"returncode={returncode!r}) — neither held nor failed on its merits.",
            excerpt,
        )
    if returncode in _PYTEST_TARGET_UNUSABLE:
        return (
            Outcome.guard_unusable,
            Strength.direct,
            f"The lesson's named regression_test is not a usable guard (pytest exit {returncode}: "
            "usage error / nothing collected) — the lesson's own prescription is invalid as written.",
            excerpt,
        )
    return (
        Outcome.guard_failed,
        Strength.direct,
        "The lesson's own regression target ran and failed on a task where the lesson was applied "
        "— the prescribed guard did not hold where the lesson said it would.",
        excerpt,
    )


def record_lesson_guard_observations_from_finalize(
    db: Session,
    *,
    task: MainAITask,
    evidence: dict[str, Any],
    passed: bool,
    job_id: uuid.UUID | None = None,
) -> list[EngineeringLessonGuardObservation]:
    """Append one observation per lesson this task's plan actually applied.

    Called from `_finalize_task_outcome()` for BOTH pass and fail — a lesson's guard holding on
    a successful task is exactly as informative as it failing on a broken one, and only writing
    on failure would bias every lesson's evidence toward the negative.

    No-op unless `evidence` carries structured verification `steps`. CI-wait and exception-path
    finalizes have no step results a lesson's target could appear in, so they produce no rows at
    all rather than a pile of `guard_not_exercised` noise.

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

    written: list[EngineeringLessonGuardObservation] = []
    for lesson in lessons:
        outcome, strength, reason, excerpt = classify_guard_outcome(
            lesson=lesson,
            evidence=evidence,
            overall_passed=passed,
            conflicted=lesson.id in conflicted_ids,
        )
        source_ref = _source_ref(goal_id=task.goal_id, task_id=task.id, job_id=job_id, lesson_id=lesson.id)
        db.execute(
            pg_insert(EngineeringLessonGuardObservation)
            .values(
                id=uuid.uuid4(),
                owner_id=task.owner_id,
                lesson_id=lesson.id,
                task_id=task.id,
                goal_id=task.goal_id,
                job_id=job_id,
                outcome=outcome.value,
                evidence_strength=strength.value,
                relevance_reason=reason,
                guard_target=lesson.regression_test,
                evidence={
                    "task_type": task.task_type,
                    "overall_passed": passed,
                    "attempts": task.attempts,
                    "target_step": excerpt,
                },
                source_ref=source_ref,
            )
            .on_conflict_do_nothing(constraint="uq_engineering_lesson_guard_observations_source_ref")
        )
        row = db.execute(
            select(EngineeringLessonGuardObservation).where(
                EngineeringLessonGuardObservation.source_ref == source_ref
            )
        ).scalar_one_or_none()
        if row is not None:
            written.append(row)
    return written
