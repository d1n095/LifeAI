"""MainAI staff management decision loop.

NEED → inspect workforce → choose existing if sufficient → else candidate →
sandbox/probation → evidence → activate if justified → monitor → improve → retire.

Does NOT auto-create agents endlessly. Uses complexity budget + ROI.
A new specialist must solve a recurring or material need.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.workforce import WorkforceAgentProfile, WorkforceDelegationRequest
from app.models.workforce_ops import WorkforceLifecycleEvent
from app.workforce.lifecycle import (
    advance_lifecycle,
    detect_need_and_create_candidate,
    run_hiring_pipeline,
)
from app.workforce.performance import get_or_create_rollup, verified_success_rate
from app.workforce.selector import score_candidates


class StaffManagerError(Exception):
    pass


@dataclass(frozen=True)
class StaffingDecision:
    action: str  # use_existing | create_candidate | refuse
    reason: str
    profile_id: uuid.UUID | None
    agent_key: str | None
    roi_score: float | None
    complexity_cost: float
    evidence: dict


# Soft caps — prevent endless hiring.
MAX_ACTIVE_OR_PROBATION_AGENTS = 24
MIN_RECURRING_NEED_COUNT = 3  # material/recurring threshold
MIN_ROI_TO_HIRE = 0.4


def _count_live_agents(db: Session, owner_id: uuid.UUID) -> int:
    rows = db.execute(
        select(WorkforceAgentProfile).where(
            WorkforceAgentProfile.owner_id == owner_id,
            WorkforceAgentProfile.status.in_(("active", "probation", "sandbox", "candidate")),
        )
    ).scalars()
    return sum(1 for _ in rows)


def _need_recurrence(db: Session, owner_id: uuid.UUID, capability: str) -> int:
    """How often this capability was requested recently — durable signal, not vibes."""
    rows = db.execute(
        select(WorkforceDelegationRequest).where(
            WorkforceDelegationRequest.owner_id == owner_id,
            WorkforceDelegationRequest.required_capability == capability,
        )
    ).scalars()
    return sum(1 for _ in rows)


def decide_staffing(
    db: Session,
    *,
    owner_id: uuid.UUID,
    need_capability: str,
    need_summary: str,
    material: bool = False,
    complexity_cost: float = 0.5,
    prefer_local_only: bool = False,
) -> StaffingDecision:
    """Core decision loop. Never creates agents when an existing specialist is sufficient."""
    if complexity_cost < 0 or complexity_cost > 1:
        raise StaffManagerError("complexity_cost must be in [0,1]")

    ranked = score_candidates(
        db,
        owner_id=owner_id,
        required_capability=need_capability,
        prefer_local_only=prefer_local_only,
    )
    # Sufficient = has verified evidence and positive score.
    for cand in ranked:
        profile = db.get(WorkforceAgentProfile, cand.profile_id)
        if profile is None or profile.status not in ("active", "probation"):
            continue
        rate = cand.explanation.get("verified_success_rate")
        jobs = cand.explanation.get("jobs_with_evidence") or 0
        if jobs >= 1 and rate is not None and rate >= 0.5:
            return StaffingDecision(
                action="use_existing",
                reason="existing specialist has verified evidence for capability",
                profile_id=profile.id,
                agent_key=profile.agent_key,
                roi_score=float(rate) - complexity_cost * 0.2,
                complexity_cost=complexity_cost,
                evidence=cand.explanation,
            )
        if profile.status == "active" and need_capability in (profile.capability_tags or []):
            # Active with matching capability but weak evidence — still prefer over hiring.
            return StaffingDecision(
                action="use_existing",
                reason="active specialist matches capability; gather more evidence before hiring",
                profile_id=profile.id,
                agent_key=profile.agent_key,
                roi_score=0.3,
                complexity_cost=complexity_cost,
                evidence=cand.explanation,
            )

    live = _count_live_agents(db, owner_id)
    if live >= MAX_ACTIVE_OR_PROBATION_AGENTS:
        return StaffingDecision(
            action="refuse",
            reason=f"workforce at capacity ({live}>={MAX_ACTIVE_OR_PROBATION_AGENTS}); improve or retire first",
            profile_id=None,
            agent_key=None,
            roi_score=None,
            complexity_cost=complexity_cost,
            evidence={"live_agents": live},
        )

    recurrence = _need_recurrence(db, owner_id, need_capability)
    if not material and recurrence < MIN_RECURRING_NEED_COUNT:
        return StaffingDecision(
            action="refuse",
            reason=(
                f"need not recurring/material yet (seen={recurrence}, "
                f"need>={MIN_RECURRING_NEED_COUNT} or material=True)"
            ),
            profile_id=None,
            agent_key=None,
            roi_score=None,
            complexity_cost=complexity_cost,
            evidence={"recurrence": recurrence},
        )

    # ROI: hiring cost vs expected value — refuse low ROI.
    # Material needs get a stronger prior; complexity_cost taxes the decision.
    roi = min(1.0, (recurrence / 10.0) + (0.55 if material else 0.0)) - complexity_cost * 0.5
    if roi < MIN_ROI_TO_HIRE:
        return StaffingDecision(
            action="refuse",
            reason=f"ROI too low to hire ({roi:.2f} < {MIN_ROI_TO_HIRE})",
            profile_id=None,
            agent_key=None,
            roi_score=roi,
            complexity_cost=complexity_cost,
            evidence={"recurrence": recurrence, "material": material},
        )

    return StaffingDecision(
        action="create_candidate",
        reason="recurring/material need with no sufficient specialist",
        profile_id=None,
        agent_key=None,
        roi_score=roi,
        complexity_cost=complexity_cost,
        evidence={"recurrence": recurrence, "need_summary": need_summary},
    )


def apply_staffing_decision(
    db: Session,
    *,
    owner_id: uuid.UUID,
    decision: StaffingDecision,
    need_capability: str,
    need_summary: str,
    agent_key: str | None = None,
    name: str | None = None,
    agent_type: str = "DOMAIN_SPECIALIST",
) -> StaffingDecision:
    """Execute create_candidate via hiring pipeline stop_at=probation. Never auto-activates."""
    if decision.action != "create_candidate":
        return decision
    key = agent_key or f"auto-{need_capability.replace('_', '-')[:40]}-{uuid.uuid4().hex[:6]}"
    profile = run_hiring_pipeline(
        db,
        owner_id=owner_id,
        agent_key=key,
        name=name or f"Candidate {need_capability}",
        role="specialist",
        agent_type=agent_type,
        capability_tags=[need_capability],
        need_summary=need_summary,
        benchmark_evidence={"pending": True},
        adversarial_evidence={"pending": True},
        stop_at="probation",
    )
    return StaffingDecision(
        action="create_candidate",
        reason=decision.reason + "; created at probation (lowest elevated trust)",
        profile_id=profile.id,
        agent_key=profile.agent_key,
        roi_score=decision.roi_score,
        complexity_cost=decision.complexity_cost,
        evidence={**decision.evidence, "status": profile.status, "authority": False},
    )


def maybe_retire_obsolete(
    db: Session,
    *,
    owner_id: uuid.UUID,
    profile_id: uuid.UUID,
    reason: str,
) -> WorkforceLifecycleEvent:
    """Retire when obsolete — explicit call, not automatic mass retirement."""
    from app.workforce.lifecycle import advance_lifecycle

    profile, event = advance_lifecycle(
        db,
        owner_id=owner_id,
        profile_id=profile_id,
        to_status="retired",
        change_kind="retire",
        change_summary=reason,
        evidence_before={"status": "active"},
        evidence_after={"status": "retired", "reason": reason},
    )
    return event
