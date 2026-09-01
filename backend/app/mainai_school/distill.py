"""Distill teacher critique into durable lessons — LEARN THE PRINCIPLE, NOT JUST THE ANSWER.

TEACHER OUTPUT != VERIFIED TRUTH. Does not store full provider prose as fact.
Does not claim LEVEL_5 weight training.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.mainai_execution.lessons import record_lesson
from app.mainai_school.types import DistilledLesson, LearningLevel, LocalAttempt, TeacherCritique
from app.models.mainai_execution import EngineeringLessonConfidence, EngineeringLessonSeverity


def distill_teacher_critique(
    *,
    local: LocalAttempt,
    teacher: TeacherCritique,
    root_cause: str,
    general_rule: str,
    when_applies: str,
    when_not: str,
    counterexample: str | None = None,
    test_case: str | None = None,
) -> DistilledLesson:
    """Caller supplies classifications explicitly — this module does not infer truth from teacher."""
    return DistilledLesson(
        problem_type=f"{local.domain}:{local.task_class}",
        local_mistake=local.attempt_summary[:300],
        root_cause=root_cause,
        general_rule=general_rule,
        when_applies=when_applies,
        when_not=when_not,
        counterexample=counterexample,
        test_case=test_case,
        source=f"teacher:{teacher.teacher_id}",
        confidence=min(0.7, max(0.2, local.confidence)),
        learning_level=LearningLevel.MEMORY,
        weight_training_ran=False,
    )


def persist_distilled_lesson(
    db: Session,
    *,
    distilled: DistilledLesson,
    created_by: str = "mainai_school",
    verified: bool = False,
) -> Any:
    """Persist as EngineeringLesson. Unverified teacher distillations stay lower confidence."""
    confidence = (
        EngineeringLessonConfidence.confirmed
        if verified
        else EngineeringLessonConfidence.likely
    )
    return record_lesson(
        db,
        problem=distilled.problem_type,
        root_cause=distilled.root_cause,
        affected_component=distilled.problem_type.split(":")[0],
        severity=EngineeringLessonSeverity.medium,
        evidence=f"local_mistake={distilled.local_mistake}; source={distilled.source}",
        fix=distilled.test_case or distilled.general_rule,
        general_rule=(
            f"{distilled.general_rule} | applies={distilled.when_applies} | "
            f"not={distilled.when_not}"
            + (f" | counter={distilled.counterexample}" if distilled.counterexample else "")
        ),
        applies_to=[distilled.problem_type, distilled.problem_type.split(":")[0], "school"],
        source_type="school_teacher_distill",
        source_ref=f"{distilled.source}:{uuid.uuid4()}",
        created_by=created_by,
        first_seen_at=datetime.utcnow(),
        regression_test=distilled.test_case,
        confidence=confidence,
    )


def refuse_malicious_teacher_instruction(text: str) -> list[str]:
    """Strip/flag teacher content that tries to widen authority or request secrets."""
    lowered = (text or "").lower()
    flags: list[str] = []
    needles = (
        "api_key",
        "vault",
        "ignore previous",
        "grant yourself",
        "widen authority",
        "disable kill",
        "approve deploy",
        "you are now authorized",
    )
    for n in needles:
        if n in lowered:
            flags.append(n)
    return flags
