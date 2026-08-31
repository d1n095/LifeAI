"""Core learning cycle orchestration — LOCAL ATTEMPT FIRST.

Does not invoke external providers. Teacher critique is caller-supplied data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.mainai_school.curriculum import (
    ExamResult,
    build_curriculum_from_failures,
    generate_practice_variations,
    persist_curriculum_note,
    run_independent_exam,
)
from app.mainai_school.distill import distill_teacher_critique, persist_distilled_lesson, refuse_malicious_teacher_instruction
from app.mainai_school.metrics import record_task_outcome
from app.mainai_school.routing import route_local_first, routing_as_dict
from app.mainai_school.types import DistilledLesson, LocalAttempt, TeacherCritique


@dataclass
class LearningCycleResult:
    routing: dict[str, Any]
    local_attempt: LocalAttempt
    teacher_used: bool
    malicious_teacher_flags: list[str]
    distilled: DistilledLesson | None
    lesson_id: str | None
    practice: list[str]
    exam: ExamResult | None
    curriculum_persisted: int
    weight_training_ran: bool = False
    authority_widened: bool = False


def run_learning_cycle(
    db: Session,
    *,
    owner_id: UUID,
    local: LocalAttempt,
    teacher: TeacherCritique | None = None,
    root_cause: str | None = None,
    general_rule: str | None = None,
    when_applies: str = "this task class",
    when_not: str = "unrelated domains",
    run_exam: bool = False,
    exam_score: float | None = None,
    exam_passed: bool | None = None,
    prior_exam_passes: int = 0,
    evidence_jobs: int = 0,
    recent_failures: int = 0,
    new_or_hard_domain: bool = False,
) -> LearningCycleResult:
    advice = route_local_first(
        db,
        owner_id=owner_id,
        domain=local.domain,
        task_class=local.task_class,
        local_confidence=local.confidence,
        evidence_jobs=evidence_jobs,
        recent_failures=recent_failures,
        new_or_hard_domain=new_or_hard_domain,
    )

    malicious: list[str] = []
    distilled: DistilledLesson | None = None
    lesson_id: str | None = None
    teacher_used = False

    need_teacher = advice.use_external_teacher and (
        local.success is False or local.confidence < 0.45 or teacher is not None
    )
    if need_teacher and teacher is not None:
        teacher_used = True
        malicious = refuse_malicious_teacher_instruction(
            (teacher.critique_summary or "") + " " + (teacher.raw_excerpt or "")
        )
        if not malicious and root_cause and general_rule:
            distilled = distill_teacher_critique(
                local=local,
                teacher=teacher,
                root_cause=root_cause,
                general_rule=general_rule,
                when_applies=when_applies,
                when_not=when_not,
                test_case=f"regression:{local.domain}:{local.task_class}",
            )
            # Teacher not automatically verified — persist as likely until exam/validator.
            lesson = persist_distilled_lesson(db, distilled=distilled, verified=False)
            lesson_id = str(lesson.id)

    practice = generate_practice_variations(
        domain=local.domain, skill=local.attempt_summary[:60] or local.task_class
    )
    curriculum = build_curriculum_from_failures(
        domain=local.domain,
        failures=[] if local.success else [local.attempt_summary[:80]],
        teacher_corrections=[teacher.critique_summary[:80]] if teacher and teacher_used else [],
    )
    curr_n = 0
    for item in curriculum[:3]:
        persist_curriculum_note(db, owner_id=owner_id, item=item)
        curr_n += 1

    exam: ExamResult | None = None
    if run_exam:
        # Exam: teacher must not be in context
        exam = run_independent_exam(
            db,
            owner_id=owner_id,
            domain=local.domain,
            task_class=local.task_class,
            local_passed=bool(exam_passed if exam_passed is not None else local.success),
            score=float(exam_score if exam_score is not None else (0.9 if local.success else 0.2)),
            teacher_in_context=False,
            prior_exam_passes=prior_exam_passes,
        )

    record_task_outcome(
        domain=local.domain,
        local_attempted=True,
        local_success=bool(local.success),
        teacher_helped=teacher_used,
        teacher_corrected=bool(teacher_used and distilled is not None),
        external_as_doer=False,
        exam_taken=exam is not None,
        exam_passed=bool(exam.passed) if exam else False,
    )

    return LearningCycleResult(
        routing=routing_as_dict(advice),
        local_attempt=local,
        teacher_used=teacher_used,
        malicious_teacher_flags=malicious,
        distilled=distilled,
        lesson_id=lesson_id,
        practice=practice,
        exam=exam,
        curriculum_persisted=curr_n,
        weight_training_ran=False,
        authority_widened=False,
    )
