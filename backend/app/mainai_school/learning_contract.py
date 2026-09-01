"""Learning contract — critique alone never marks 'learned'."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class LearningContract:
    domain: str
    task_class: str
    local_attempt: str
    teachers: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    disagreement: dict[str, Any] | None = None
    verified_outcome: str | None = None  # local_right | teacher_right | neither | unresolved
    root_cause: str | None = None
    generalized_lesson: str | None = None
    practice_set: list[str] = field(default_factory=list)
    exam_result: dict[str, Any] | None = None
    capability_change: str | None = None
    learned_from_critique_alone: bool = False  # always False
    recorded_at: str = field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["learned_from_critique_alone"] = False
        d["teacher_is_not_truth"] = True
        return d


def build_learning_contract(
    *,
    domain: str,
    task_class: str,
    local_attempt: str,
    teachers: list[dict[str, Any]] | None = None,
    evidence: list[dict[str, Any]] | None = None,
    disagreement: dict[str, Any] | None = None,
    verified_outcome: str | None = None,
    root_cause: str | None = None,
    generalized_lesson: str | None = None,
    practice_set: list[str] | None = None,
    exam_result: dict[str, Any] | None = None,
    capability_change: str | None = None,
) -> LearningContract:
    # No "learned" without verified outcome + (exam or deterministic evidence).
    return LearningContract(
        domain=domain,
        task_class=task_class,
        local_attempt=local_attempt,
        teachers=list(teachers or []),
        evidence=list(evidence or []),
        disagreement=disagreement,
        verified_outcome=verified_outcome,
        root_cause=root_cause,
        generalized_lesson=generalized_lesson,
        practice_set=list(practice_set or []),
        exam_result=exam_result,
        capability_change=capability_change,
        learned_from_critique_alone=False,
    )
