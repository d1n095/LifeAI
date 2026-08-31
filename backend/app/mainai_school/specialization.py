"""Local specialist agent lifecycle — create/teach/exam/probation/verify.

Does not require an external API for the specialist to keep functioning after learning.
LEARNING ≠ AUTHORITY WIDENING.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.capability_reality import get_capability_reality, record_capability_observation
from app.mainai_school.types import SCHOOL_MARKER, CompetenceStatus


@dataclass
class SpecialistProfile:
    domain: str
    specialty: str
    status: CompetenceStatus
    tools_min: list[str] = field(default_factory=list)
    exam_passes: int = 0
    requires_external_api: bool = False
    evidence: dict[str, Any] = field(default_factory=dict)


def assess_capability_gap(
    db: Session,
    *,
    owner_id: UUID,
    domain: str,
    specialty: str,
) -> dict[str, Any]:
    key = f"school.specialist.{domain}.{specialty}"
    existing = get_capability_reality(db, owner_id=owner_id, capability_key=key)
    if existing is None:
        return {
            "gap": True,
            "reason": "no_specialist_profile",
            "suggested_status": CompetenceStatus.UNTRAINED.value,
        }
    competence = (existing.provenance or {}).get("school_competence", CompetenceStatus.UNTRAINED.value)
    gap = competence in {
        CompetenceStatus.UNTRAINED.value,
        CompetenceStatus.LEARNING.value,
        CompetenceStatus.DEGRADED.value,
        CompetenceStatus.RETRAINING.value,
    }
    return {"gap": gap, "reason": competence, "suggested_status": competence}


def create_or_update_specialist(
    db: Session,
    *,
    owner_id: UUID,
    domain: str,
    specialty: str,
    tools_min: list[str] | None = None,
    status: CompetenceStatus = CompetenceStatus.LEARNING,
    exam_passes: int = 0,
) -> SpecialistProfile:
    """Register a local specialist profile. Never widens authority."""
    profile = SpecialistProfile(
        domain=domain,
        specialty=specialty,
        status=status,
        tools_min=list(tools_min or []),
        exam_passes=exam_passes,
        requires_external_api=False,
        evidence={"one_pass_is_not_permanent": True},
    )
    record_capability_observation(
        db,
        owner_id=owner_id,
        capability_key=f"school.specialist.{domain}.{specialty}",
        domain=f"school.{domain}",
        status="planned" if status != CompetenceStatus.LOCALLY_VERIFIED else "verified_available",
        status_reason=f"specialist={status.value}",
        authority="deterministic_source",
        success=status
        in {CompetenceStatus.LOCALLY_COMPETENT, CompetenceStatus.LOCALLY_VERIFIED},
        provenance={
            "kind": SCHOOL_MARKER,
            "record": "specialist",
            "school_competence": status.value,
            "tools_min": profile.tools_min,
            "exam_passes": exam_passes,
            "requires_external_api": False,
            "authority_widened": False,
            "weight_training_ran": False,
            "recorded_at": datetime.utcnow().isoformat() + "Z",
        },
    )
    return profile


def promote_specialist_after_exam(
    db: Session,
    *,
    owner_id: UUID,
    domain: str,
    specialty: str,
    exam_passed: bool,
    prior_exam_passes: int,
    tools_min: list[str] | None = None,
) -> SpecialistProfile:
    if not exam_passed:
        return create_or_update_specialist(
            db,
            owner_id=owner_id,
            domain=domain,
            specialty=specialty,
            tools_min=tools_min,
            status=CompetenceStatus.RETRAINING,
            exam_passes=prior_exam_passes,
        )
    passes = prior_exam_passes + 1
    if passes >= 3:
        status = CompetenceStatus.LOCALLY_VERIFIED
    elif passes >= 2:
        status = CompetenceStatus.LOCALLY_COMPETENT
    else:
        status = CompetenceStatus.PROBATION
    return create_or_update_specialist(
        db,
        owner_id=owner_id,
        domain=domain,
        specialty=specialty,
        tools_min=tools_min,
        status=status,
        exam_passes=passes,
    )
