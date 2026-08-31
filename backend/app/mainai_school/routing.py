"""Local-first routing — EXTERNAL APIs are teachers, not the permanent brain."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.capability_reality import get_capability_reality
from app.mainai_school.types import CompetenceStatus, RouteDecision
from uuid import UUID


@dataclass(frozen=True)
class RoutingAdvice:
    decision: RouteDecision
    reason: str
    competence: CompetenceStatus
    use_external_teacher: bool
    use_external_as_doer: bool  # should trend toward False over time
    authorized: bool = False  # routing never grants authority


def _map_capability_to_competence(status: str | None, provenance: dict | None) -> CompetenceStatus:
    prov = provenance or {}
    if prov.get("school_competence") in {c.value for c in CompetenceStatus}:
        return CompetenceStatus(prov["school_competence"])
    if status == "verified_available":
        return CompetenceStatus.LOCALLY_VERIFIED
    if status == "configured_unavailable":
        return CompetenceStatus.DEGRADED
    if status == "planned":
        return CompetenceStatus.LEARNING
    return CompetenceStatus.UNTRAINED


def route_local_first(
    db: Session,
    *,
    owner_id: UUID,
    domain: str,
    task_class: str,
    local_confidence: float,
    evidence_jobs: int = 0,
    recent_failures: int = 0,
    new_or_hard_domain: bool = False,
) -> RoutingAdvice:
    """Decide LOCAL vs teacher help. Never treats teacher as required doer by default."""
    key = f"school.{domain}.{task_class}"
    record = get_capability_reality(db, owner_id=owner_id, capability_key=key)
    competence = _map_capability_to_competence(
        record.status if record else None,
        record.provenance if record else None,
    )

    if recent_failures >= 3 or competence in {CompetenceStatus.DEGRADED, CompetenceStatus.RETRAINING}:
        return RoutingAdvice(
            decision=RouteDecision.SUPERVISED_LEARNING,
            reason="degraded_or_repeated_failures",
            competence=competence,
            use_external_teacher=True,
            use_external_as_doer=False,
        )
    if competence == CompetenceStatus.LOCALLY_VERIFIED and evidence_jobs >= 3 and local_confidence >= 0.55:
        return RoutingAdvice(
            decision=RouteDecision.LOCAL_FIRST_PROVEN,
            reason="verified_local_competence_with_evidence",
            competence=competence,
            use_external_teacher=False,
            use_external_as_doer=False,
        )
    if competence in {CompetenceStatus.LOCALLY_COMPETENT, CompetenceStatus.PROBATION} and local_confidence >= 0.5:
        return RoutingAdvice(
            decision=RouteDecision.LOCAL,
            reason="local_competent_or_probation_attempt_first",
            competence=competence,
            use_external_teacher=False,
            use_external_as_doer=False,
        )
    if new_or_hard_domain or competence == CompetenceStatus.UNTRAINED:
        return RoutingAdvice(
            decision=RouteDecision.TEACHER_GUIDED,
            reason="new_or_untrained_domain",
            competence=competence,
            use_external_teacher=True,
            use_external_as_doer=False,  # still local attempt preferred when possible
        )
    if local_confidence < 0.45 or evidence_jobs < 1:
        return RoutingAdvice(
            decision=RouteDecision.LOCAL_THEN_TEACHER_REVIEW,
            reason="insufficient_local_evidence_or_confidence",
            competence=competence,
            use_external_teacher=True,
            use_external_as_doer=False,
        )
    return RoutingAdvice(
        decision=RouteDecision.LOCAL,
        reason="default_local_attempt_first",
        competence=competence,
        use_external_teacher=False,
        use_external_as_doer=False,
    )


def routing_as_dict(advice: RoutingAdvice) -> dict[str, Any]:
    return {
        "decision": advice.decision.value,
        "reason": advice.reason,
        "competence": advice.competence.value,
        "use_external_teacher": advice.use_external_teacher,
        "use_external_as_doer": advice.use_external_as_doer,
        "authorized": False,
        "external_model_is_not_mainai": True,
    }
