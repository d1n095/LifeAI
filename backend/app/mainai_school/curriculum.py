"""Curriculum + practice + exam — evidence-based competence, no fake training claims."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.capability_reality import get_capability_reality, record_capability_observation
from app.founder_memory import record_founder_memory
from app.mainai_school.types import SCHOOL_MARKER, CompetenceStatus, LearningLevel


@dataclass
class CurriculumItem:
    domain: str
    skill: str
    priority: float
    reason: str
    practice_queue: list[str] = field(default_factory=list)


@dataclass
class ExamResult:
    domain: str
    task_class: str
    passed: bool
    teacher_helped: bool
    score: float
    evidence: dict[str, Any]
    competence_after: CompetenceStatus


def build_curriculum_from_failures(
    *,
    domain: str,
    failures: list[str],
    teacher_corrections: list[str],
    founder_value: float = 0.5,
    api_cost_pressure: float = 0.5,
) -> list[CurriculumItem]:
    """Active learning: weak skills from real failures — not invented gaps."""
    items: list[CurriculumItem] = []
    seen: set[str] = set()
    for raw in failures + [f"corr:{c}" for c in teacher_corrections]:
        skill = raw[:80]
        if skill in seen:
            continue
        seen.add(skill)
        priority = 0.4 + 0.3 * founder_value + 0.2 * api_cost_pressure
        if raw.startswith("corr:"):
            priority += 0.15
        items.append(
            CurriculumItem(
                domain=domain,
                skill=skill,
                priority=min(1.0, priority),
                reason="failure_or_correction",
                practice_queue=generate_practice_variations(domain=domain, skill=skill),
            )
        )
    items.sort(key=lambda x: x.priority, reverse=True)
    return items


def generate_practice_variations(*, domain: str, skill: str, n: int = 4) -> list[str]:
    """Deterministic variations — not exact memorization of one answer."""
    base = skill.strip() or "general"
    templates = [
        f"{domain}: variation of '{base}' with different schema",
        f"{domain}: '{base}' under restart mid-operation",
        f"{domain}: '{base}' with two concurrent actors",
        f"{domain}: '{base}' adversarial edge case",
        f"{domain}: '{base}' with missing evidence",
        f"{domain}: '{base}' after founder correction wording",
    ]
    return templates[: max(1, min(n, len(templates)))]


def run_independent_exam(
    db: Session,
    *,
    owner_id: UUID,
    domain: str,
    task_class: str,
    local_passed: bool,
    score: float,
    teacher_in_context: bool,
    prior_exam_passes: int = 0,
) -> ExamResult:
    """Exam mode: teacher must NOT help. One pass != permanent competence."""
    if teacher_in_context:
        return ExamResult(
            domain=domain,
            task_class=task_class,
            passed=False,
            teacher_helped=True,
            score=0.0,
            evidence={"reason": "exam_invalid_teacher_leakage"},
            competence_after=CompetenceStatus.LEARNING,
        )

    key = f"school.{domain}.{task_class}"
    existing = get_capability_reality(db, owner_id=owner_id, capability_key=key)
    prior = (existing.provenance or {}).get("school_competence") if existing else None

    if not local_passed or score < 0.7:
        new_status = CompetenceStatus.LEARNING
        if prior in {CompetenceStatus.LOCALLY_VERIFIED.value, CompetenceStatus.LOCALLY_COMPETENT.value}:
            new_status = CompetenceStatus.DEGRADED
        _record_competence(
            db,
            owner_id=owner_id,
            domain=domain,
            task_class=task_class,
            competence=new_status,
            evidence={"exam": "failed", "score": score},
        )
        return ExamResult(
            domain=domain,
            task_class=task_class,
            passed=False,
            teacher_helped=False,
            score=score,
            evidence={"exam": "failed"},
            competence_after=new_status,
        )

    # Require multiple passes for LOCALLY_VERIFIED
    passes = prior_exam_passes + 1
    if passes >= 3 and score >= 0.85:
        competence = CompetenceStatus.LOCALLY_VERIFIED
    elif passes >= 2:
        competence = CompetenceStatus.LOCALLY_COMPETENT
    else:
        competence = CompetenceStatus.PROBATION

    _record_competence(
        db,
        owner_id=owner_id,
        domain=domain,
        task_class=task_class,
        competence=competence,
        evidence={"exam": "passed", "score": score, "passes": passes},
    )
    return ExamResult(
        domain=domain,
        task_class=task_class,
        passed=True,
        teacher_helped=False,
        score=score,
        evidence={"exam": "passed", "passes": passes, "one_pass_is_not_permanent": True},
        competence_after=competence,
    )


def _record_competence(
    db: Session,
    *,
    owner_id: UUID,
    domain: str,
    task_class: str,
    competence: CompetenceStatus,
    evidence: dict[str, Any],
) -> None:
    # Map to capability_reality statuses — never invent verified_available without exam path above.
    status_map = {
        CompetenceStatus.UNTRAINED: "unknown",
        CompetenceStatus.LEARNING: "planned",
        CompetenceStatus.SUPERVISED: "planned",
        CompetenceStatus.PROBATION: "planned",
        # School competence is NOT capability_reality verified_available without
        # intelligence evidence that supports the claim (EVIDENCE EXISTS != PROVEN).
        CompetenceStatus.LOCALLY_COMPETENT: "planned",
        CompetenceStatus.LOCALLY_VERIFIED: "planned",
        CompetenceStatus.DEGRADED: "configured_unavailable",
        CompetenceStatus.RETRAINING: "planned",
    }
    # LOCALLY_* stored in provenance.school_competence — never inflate verified_available.
    record_capability_observation(
        db,
        owner_id=owner_id,
        capability_key=f"school.{domain}.{task_class}",
        domain=f"school.{domain}",
        status=status_map[competence],
        status_reason=f"school_competence={competence.value}",
        authority="deterministic_source",
        confidence=evidence.get("score"),
        success=bool(evidence.get("exam") == "passed"),
        provenance={
            "kind": SCHOOL_MARKER,
            "school_competence": competence.value,
            "learning_level": LearningLevel.SPECIALIZATION.value,
            "weight_training_ran": False,
            "evidence": evidence,
            "recorded_at": datetime.utcnow().isoformat() + "Z",
            "not_capability_verified_available": True,
        },
    )


def persist_curriculum_note(
    db: Session,
    *,
    owner_id: UUID,
    item: CurriculumItem,
) -> Any:
    return record_founder_memory(
        db,
        owner_id=owner_id,
        note_type="observation",
        content=f"[school curriculum] {item.domain}:{item.skill}",
        idempotency_key=f"school-curr:{uuid.uuid4()}",
        authority="deterministic_source",
        basis="deterministic",
        provenance={
            "kind": SCHOOL_MARKER,
            "record": "curriculum",
            "domain": item.domain,
            "skill": item.skill,
            "priority": item.priority,
            "practice_queue": item.practice_queue,
            "authorized": False,
        },
    )
