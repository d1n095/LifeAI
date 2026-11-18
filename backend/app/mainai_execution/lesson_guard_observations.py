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
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, text
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


# --------------------------------------------------------------------------------------
# Effectiveness rollup (migration 0074's provenance edge, consumed): the measured form of
# "recurring failures lead to improvements in decision rules" — WITHOUT the two dishonest
# shortcuts. It does not call held-guards "caused success" (0058's discipline stands: guard
# evidence is guard evidence), and it does not let an ineffective rule hide: a lesson whose
# own named guard FAILED on a task the lesson demonstrably shaped surfaces as
# needs_reassessment for founder review — a signal, never an automatic mutation of the
# lesson (this module's "SIGNAL PRODUCER != TRUTH WRITER" rule).
#
# The edge matters: observations are counted ONLY where an engineering_lesson_plan_decisions
# row proves the lesson actually shaped that task's plan. A guard that passed on some
# unrelated task is not evidence about the rule — exactly the attribution failure 0058's
# docstring warned the observation table alone could not avoid.
# --------------------------------------------------------------------------------------


@dataclass
class LessonEffectiveness:
    """One lesson's standing, from applied-count (decisions) to guard outcomes
    (observations joined through the provenance edge). All counts are OBSERVATIONS, never
    causal claims."""

    lesson_id: uuid.UUID
    problem: str
    general_rule: str
    regression_test: str | None
    applied_count: int
    guard_held: int
    guard_failed: int
    guard_unusable: int
    guard_not_exercised: int
    keep_ratio: float | None  # held / (held + failed) — None when no verdict was ever reached
    needs_reassessment: bool


def lesson_effectiveness(
    db: Session,
    *,
    owner_id: uuid.UUID,
    lesson_ids: list[uuid.UUID] | None = None,
    limit: int = 100,
) -> list[LessonEffectiveness]:
    """Roll up decisions (migration 0074) joined to guard observations (migration 0058) per
    lesson, for THIS owner's plans. Owner-scoped both by the decisions table's RLS and by
    the explicit filter (defense in depth, the codebase's standing convention).

    `keep_ratio` is deliberately computed over VERDICTS only (held + failed): a guard that
    was not exercised or was unusable says nothing about whether the rule holds, and folding
    those into the denominator would flatter a rule that never runs. `needs_reassessment`
    is triggered ONLY by guard_failed — the one outcome that contradicts the lesson's own
    claim — never by guard_unusable/not_exercised (those indict the target path or the
    evidence, not the rule)."""
    decisions_sql = """
        WITH decisions AS (
            SELECT d.lesson_id, d.task_id
            FROM engineering_lesson_plan_decisions d
            WHERE d.owner_id = :owner
              {lesson_filter}
        )
        SELECT d.lesson_id::text AS lesson_id,
               count(DISTINCT d.task_id)                                AS applied_count,
               count(o.id) FILTER (WHERE o.outcome IN ('guard_held', 'guard_held_task_failed_elsewhere')) AS held,
               count(o.id) FILTER (WHERE o.outcome = 'guard_failed')    AS failed,
               count(o.id) FILTER (WHERE o.outcome = 'guard_unusable')  AS unusable,
               count(o.id) FILTER (WHERE o.outcome = 'guard_not_exercised') AS not_exercised
        FROM decisions d
        LEFT JOIN engineering_lesson_guard_observations o
               ON o.task_id = d.task_id AND o.lesson_id = d.lesson_id AND o.owner_id = :owner
        GROUP BY d.lesson_id
        ORDER BY applied_count DESC, d.lesson_id
        LIMIT :limit
    """
    lesson_filter = ""
    params: dict[str, Any] = {"owner": str(owner_id), "limit": int(limit)}
    if lesson_ids:
        lesson_filter = "AND d.lesson_id IN :lesson_ids"
        params["lesson_ids"] = tuple(str(l) for l in lesson_ids)
    rows = db.execute(text(decisions_sql.format(lesson_filter=lesson_filter)), params).all()
    if not rows:
        return []

    lessons = (
        db.query(EngineeringLesson)
        .filter(EngineeringLesson.id.in_([uuid.UUID(r[0]) for r in rows]))
        .all()
    )
    by_id = {l.id: l for l in lessons}

    out: list[LessonEffectiveness] = []
    for lesson_id_text, applied, held, failed, unusable, not_exercised in rows:
        lesson = by_id.get(uuid.UUID(lesson_id_text))
        if lesson is None:
            continue
        verdicts = int(held) + int(failed)
        out.append(
            LessonEffectiveness(
                lesson_id=lesson.id,
                problem=lesson.problem,
                general_rule=lesson.general_rule,
                regression_test=lesson.regression_test,
                applied_count=int(applied),
                guard_held=int(held),
                guard_failed=int(failed),
                guard_unusable=int(unusable),
                guard_not_exercised=int(not_exercised),
                keep_ratio=(int(held) / verdicts) if verdicts else None,
                needs_reassessment=int(failed) > 0,
            )
        )
    return out


def format_lesson_standing_for_advisory(standing: list[LessonEffectiveness]) -> str:
    """The one-line standing summary the overcorrection advisory appends when it fires, so
    the audit row's readers see not just 'guards dominate this plan' but WHAT those rules'
    own track record is. Lessons with no verdicts yet are named honestly as such."""
    if not standing:
        return ""
    parts = []
    for s in standing:
        if s.needs_reassessment:
            parts.append(
                f"{s.lesson_id}: NEEDS REASSESSMENT — its own guard failed {s.guard_failed}x where applied"
            )
        elif s.guard_held > 0:
            parts.append(f"{s.lesson_id}: guard held {s.guard_held}/{s.guard_held + s.guard_failed} verdicts")
        else:
            parts.append(f"{s.lesson_id}: no guard verdicts yet")
    return " Lesson standing: " + "; ".join(parts) + "."


def recurring_failure_clusters(db: Session, *, owner_id: uuid.UUID) -> list[dict]:
    """DETECTION ONLY: shared-root-cause clusters across DIFFERENT tasks — lessons whose
    guard FAILED on >= 2 DISTINCT tasks and which share an affected_component (the same
    broken subsystem producing repeated guard failures no matter which rule was applied to
    guard it). This is the "identify shared root causes" signal the self-correction loop
    was missing: per-lesson effectiveness already says the rule failed; this says the
    COMPONENT keeps failing under every rule pointed at it, which is evidence about the
    component, not any one lesson.

    Conservative by construction: detection surfaces clusters in the audit trail (and in
    selection summaries for excluded/flagged lessons); it never auto-disputes lessons,
    never mutates anything, and never auto-resolves — resolution is founder/async-tick
    territory (the same boundary lesson_conflicts.py draws). Owner-scoped end to end:
    both observations and the lesson join run under the caller's RLS GUC and explicit
    owner filters.

    A cluster needs >= 2 DISTINCT failed tasks. A lesson that failed twice on the SAME
    task's repeated attempts is a retry pattern, not a cross-task recurrence — exactly the
    single-failure-must-not-suppress discipline, applied at cluster granularity."""
    rows = db.execute(
        text("""
            SELECT l.affected_component,
                   array_agg(DISTINCT l.id::text)                AS lesson_ids,
                   array_agg(DISTINCT o.task_id::text)           AS distinct_failed_tasks,
                   count(DISTINCT o.task_id)                     AS task_count,
                   array_agg(DISTINCT l.general_rule)            AS general_rules
            FROM engineering_lesson_guard_observations o
            JOIN engineering_lessons l ON l.id = o.lesson_id
            WHERE o.owner_id = :owner AND o.outcome = 'guard_failed'
            GROUP BY l.affected_component
            HAVING count(DISTINCT o.task_id) >= 2
            ORDER BY task_count DESC, l.affected_component
        """),
        {"owner": str(owner_id)},
    ).all()

    return [
        {
            "affected_component": component,
            "lesson_ids": [str(lid) for lid in lesson_ids],
            "distinct_failed_tasks": [str(tid) for tid in failed_tasks],
            "task_count": int(task_count),
            "general_rules": list(general_rules),
        }
        for component, lesson_ids, failed_tasks, task_count, general_rules in rows
    ]
