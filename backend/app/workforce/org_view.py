"""Human-readable organization view (T17) — inspectable backend foundation, not UI polish."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.workforce import (
    WorkforceAgentProfile,
    WorkforceAssignment,
    WorkforceDelegationRequest,
    WorkforcePerformanceRollup,
    WorkforceTeam,
)
from app.workforce.performance import verified_success_rate


def organization_snapshot(db: Session, *, owner_id: uuid.UUID) -> dict:
    agents = list(
        db.execute(
            select(WorkforceAgentProfile).where(WorkforceAgentProfile.owner_id == owner_id)
        ).scalars()
    )
    teams = list(db.execute(select(WorkforceTeam).where(WorkforceTeam.owner_id == owner_id)).scalars())
    open_assignments = list(
        db.execute(
            select(WorkforceAssignment).where(
                WorkforceAssignment.owner_id == owner_id,
                WorkforceAssignment.status.in_(("assigned", "running", "awaiting_verification")),
            )
        ).scalars()
    )
    recent_requests = list(
        db.execute(
            select(WorkforceDelegationRequest)
            .where(WorkforceDelegationRequest.owner_id == owner_id)
            .order_by(WorkforceDelegationRequest.created_at.desc())
            .limit(20)
        ).scalars()
    )
    rollups = list(
        db.execute(
            select(WorkforcePerformanceRollup).where(WorkforcePerformanceRollup.owner_id == owner_id)
        ).scalars()
    )

    return {
        "executive": "MainAI",
        "model": "Founder → MainAI → Workforce Manager → Lead/Department → Specialists → Temporary Subagents",
        "invariants": [
            "AGENT_ROLE_NE_AUTHORITY",
            "DELEGATION_NE_AUTHORIZATION",
            "EXTERNAL_MODEL_OUTPUT_NE_TRUSTED_FACT",
            "CONFIDENCE_NE_PERFORMANCE_EVIDENCE",
        ],
        "agents": [
            {
                "id": str(a.id),
                "agent_key": a.agent_key,
                "name": a.name,
                "role": a.role,
                "agent_type": a.agent_type,
                "trust_zone": a.trust_zone,
                "provider_type": a.provider_type,
                "provider_model_id": a.provider_model_id,
                "capabilities": list(a.capability_tags or []),
                "allowed_tool_classes": list(a.allowed_tool_classes or []),
                "status": a.status,
                "cost_class": a.cost_class,
                "risk_tier": a.risk_tier,
            }
            for a in agents
        ],
        "teams": [
            {
                "id": str(t.id),
                "name": t.name,
                "pattern": t.pattern,
                "members": list(t.member_profile_ids or []),
                "status": t.status,
            }
            for t in teams
        ],
        "current_assignments": [
            {
                "id": str(x.id),
                "profile_id": str(x.profile_id),
                "status": x.status,
                "verification_status": x.verification_status,
                "selection_score": x.selection_score,
                "revoked_at": x.revoked_at.isoformat() if x.revoked_at else None,
                "expires_at": x.expires_at.isoformat() if x.expires_at else None,
            }
            for x in open_assignments
        ],
        "recent_delegations": [
            {
                "id": str(r.id),
                "required_capability": r.required_capability,
                "risk": r.risk,
                "status": r.status,
                "selection_explanation": r.selection_explanation,
            }
            for r in recent_requests
        ],
        "performance": [
            {
                "profile_id": str(p.profile_id),
                "capability_tag": p.capability_tag,
                "verified_success": p.verified_success,
                "verified_failure": p.verified_failure,
                "verified_success_rate": verified_success_rate(p),
                "security_violations": p.security_violations,
                "authority_violations": p.authority_violations,
                "provider_cost_usd_sum": p.provider_cost_usd_sum,
            }
            for p in rollups
        ],
        "founder_controls": [
            "disable_workforce_agent",
            "retire_workforce_agent",
            "cancel_assignment",
            "restrict via allowed_tool_classes / trust_zone on re-register",
        ],
    }
