"""Department capability evidence view — no promotion without durable evidence."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.workforce import WorkforceAgentProfile, WorkforcePerformanceRollup
from app.workforce.first_team import FIRST_TEAM_SPECS, bootstrap_first_team, inspect_first_team
from app.workforce.performance import verified_success_rate


def department_capability_ledger(db: Session, *, owner_id: uuid.UUID) -> list[dict[str, Any]]:
    """Per-department inspectable status. Does NOT promote candidates automatically."""
    bootstrap_first_team(db, owner_id=owner_id)
    by_key = {
        a.agent_key: a
        for a in db.execute(
            select(WorkforceAgentProfile).where(WorkforceAgentProfile.owner_id == owner_id)
        ).scalars()
        if (a.provenance or {}).get("first_team")
    }
    out: list[dict[str, Any]] = []
    for spec in FIRST_TEAM_SPECS:
        profile = by_key.get(spec.agent_key)
        rollups = []
        if profile is not None:
            rollups = list(
                db.execute(
                    select(WorkforcePerformanceRollup).where(
                        WorkforcePerformanceRollup.owner_id == owner_id,
                        WorkforcePerformanceRollup.profile_id == profile.id,
                    )
                ).scalars()
            )
        verified_caps = []
        unverified_caps = list(spec.capability_tags)
        last_success = None
        last_failure = None
        for r in rollups:
            rate = verified_success_rate(r)
            if rate is not None and rate > 0 and int(r.verified_success) > 0:
                verified_caps.append(r.capability_tag)
                if r.capability_tag in unverified_caps:
                    unverified_caps.remove(r.capability_tag)
            if int(r.verified_success) > 0:
                last_success = r.capability_tag
            if int(r.verified_failure) > 0:
                last_failure = r.capability_tag
        # Never claim verified_caps without rollup evidence.
        out.append(
            {
                "department": spec.name,
                "agent_key": spec.agent_key,
                "status": profile.status if profile else "missing",
                "candidate": (profile.status == "candidate") if profile else True,
                "runtime_backed": spec.runtime not in ("none",),
                "runtime": spec.runtime,
                "provider_model": spec.provider_model_available,
                "verified_capabilities": verified_caps,
                "unverified_capabilities": unverified_caps,
                "last_successful_evidence": last_success,
                "last_failure": last_failure,
                "cost_profile": profile.cost_class if profile else "unknown",
                "trust_status": profile.trust_zone if profile else spec.trust_zone,
                "evidence_proves_capability": bool(verified_caps),
                "blocked_reason": spec.blocked_reason if not verified_caps else None,
                "do_not_promote_without_evidence": True,
            }
        )
    return out
