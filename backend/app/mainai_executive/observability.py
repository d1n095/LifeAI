"""Founder-facing executive observability — no chain-of-thought, evidence/provenance only."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.mainai_executive.continuity import load_continuity_checkpoint, resume_summary
from app.models.work_candidate import WorkCandidate
from app.models.workforce import WorkforceAssignment, WorkforceDelegationRequest


def executive_status_snapshot(
    db: Session,
    *,
    owner_id: uuid.UUID,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Inspectable status without raw-table browsing."""
    checkpoint = None
    recovery = None
    if session_id:
        checkpoint = load_continuity_checkpoint(db, owner_id=owner_id, session_id=session_id)
        if checkpoint:
            recovery = resume_summary(checkpoint)

    candidates = list(
        db.execute(
            select(WorkCandidate)
            .where(WorkCandidate.owner_id == owner_id)
            .order_by(WorkCandidate.created_at.desc())
            .limit(20)
        ).scalars()
    )
    exec_cands = [
        c
        for c in candidates
        if c.classifier_strategy == "executive_lookaround_v1"
        or (isinstance(c.provenance, dict) and c.provenance.get("scan_step") == "bounded_candidate_generation")
    ]
    if session_id and checkpoint and checkpoint.work_candidate_ids:
        wanted = set(checkpoint.work_candidate_ids)
        session_cands = [c for c in exec_cands if str(c.id) in wanted]
        if session_cands:
            exec_cands = session_cands

    active_assignments = list(
        db.execute(
            select(WorkforceAssignment).where(
                WorkforceAssignment.owner_id == owner_id,
                WorkforceAssignment.status.in_(("assigned", "running", "awaiting_verification")),
            )
        ).scalars()
    )
    open_requests = list(
        db.execute(
            select(WorkforceDelegationRequest).where(
                WorkforceDelegationRequest.owner_id == owner_id,
                WorkforceDelegationRequest.status.in_(("open", "resolving", "assigned")),
            )
        ).scalars()
    )

    return {
        "session_id": session_id,
        "current_goal": (checkpoint.founder_request if checkpoint else None),
        "current_plan_phase": (checkpoint.phase.value if checkpoint else None),
        "horizon_plan": (checkpoint.horizon_items if checkpoint else []),
        "active_work": [
            {"id": str(a.id), "status": a.status, "profile_id": str(a.profile_id)}
            for a in active_assignments[:10]
        ],
        "waiting_or_open_delegation": [
            {"id": str(r.id), "capability": r.required_capability, "status": r.status}
            for r in open_requests[:10]
        ],
        "executive_work_candidates": [
            {
                "id": str(c.id),
                "title": c.title,
                "priority": c.priority,
                "status": c.status,
                "authorized": False,  # candidates are never authority
            }
            for c in exec_cands[:15]
        ],
        "authority_state": {
            "executive_holds_execution_authority": False,
            "notes": (checkpoint.authority_notes if checkpoint else ["NO_ACTIVE_EXECUTIVE_SESSION"]),
        },
        "last_recovery": recovery,
        "open_risks": [
            "PROVIDER_INVOKE_DISABLED_UNTIL_CLAUDE_GATES",
            "EXECUTIVE_CYCLE_IS_PLANNING_ONLY",
        ],
        "chain_of_thought_exposed": False,
        "evidence_basis": "durable_rows_only",
    }
