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
        "startup_readiness": _startup_readiness(),
        "kill_switch": _kill_switch_status(db, owner_id),
    }


def _startup_readiness() -> dict[str, Any]:
    """Best-effort after #234 — never invents READY_FOR_SERIOUS_AUTONOMOUS_RUN."""
    try:
        from app.mainai_startup_readiness import evaluate_startup_readiness

        # claude_reviews_satisfied=None → UNKNOWN for higher levels (fail closed)
        report = evaluate_startup_readiness(claude_reviews_satisfied=None)
        return report.as_dict() if hasattr(report, "as_dict") else {
            "level": report.level.value,
            "source": "mainai_startup_readiness",
            "notes": getattr(report, "notes", None),
        }
    except Exception as exc:  # noqa: BLE001 — observability must not fail the cycle
        return {"level": "UNKNOWN", "error": type(exc).__name__, "source": "unavailable"}


def _kill_switch_status(db: Session, owner_id: uuid.UUID) -> dict[str, Any]:
    try:
        from app.workforce.kill_switch import get_global_kill_switch, get_kill_switch

        state = get_kill_switch(db, owner_id)
        global_state = get_global_kill_switch(db)
        active = bool(state.active or global_state.active)
        reason = state.reason if state.active else (global_state.reason if global_state.active else None)
        return {
            "active": active,
            "reason": reason,
            "global_active": bool(global_state.active),
            "source": "workforce.kill_switch",
        }
    except Exception as exc:  # noqa: BLE001
        return {"active": False, "error": type(exc).__name__, "source": "unavailable"}
