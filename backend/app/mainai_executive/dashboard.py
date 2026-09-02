"""Coherent founder-readable executive dashboard backend — evidence only."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.mainai_executive.observability import executive_status_snapshot
from app.mainai_executive.why_graph import list_decision_debt
from app.workforce.kill_switch import get_kill_switch
from app.workforce.org_view import organization_snapshot


def founder_executive_dashboard(
    db: Session,
    *,
    owner_id: uuid.UUID,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Single status model for founder inspection — no chain-of-thought."""
    snap = executive_status_snapshot(db, owner_id=owner_id, session_id=session_id)
    try:
        org = organization_snapshot(db, owner_id=owner_id)
    except Exception:
        org = {"error": "org_unavailable"}
    ks = get_kill_switch(db, owner_id).as_dict()
    debt = list_decision_debt(db, owner_id=owner_id, limit=10)

    return {
        "WHAT_SHE_IS_DOING": snap.get("current_goal"),
        "WHY": (snap.get("last_recovery") or {}).get("was_doing"),
        "WHAT_IS_NEXT": (snap.get("horizon_plan") or [])[:5],
        "WHAT_IS_BLOCKED": [
            r for r in (snap.get("open_risks") or [])
        ] + [i for i in debt["items"] if i.get("needs_founder")],
        "WHAT_SHE_REMEMBERS_AS_CURRENT": {
            "phase": snap.get("current_plan_phase"),
            "candidates": snap.get("executive_work_candidates"),
        },
        "WHAT_SHE_IS_UNSURE_ABOUT": (snap.get("last_recovery") or {}).get("uncertain")
        or (snap.get("assumption_scan") or {}).get("unverified_assumptions"),
        "WHAT_AGENTS_ARE_WORKING": snap.get("active_work"),
        "WHAT_EACH_AGENT_CAN_ACTUALLY_DO": org,
        "WHAT_COST_IS_ACCRUING": {"note": "provider_invoke_disabled", "spend_live": False},
        "WHAT_NEEDS_FOUNDER_APPROVAL": [
            c for c in (snap.get("executive_work_candidates") or []) if c.get("status") == "unreviewed"
        ],
        "WHAT_FAILED_RECENTLY": (snap.get("last_recovery") or {}).get("interruption_point"),
        "WHAT_RECOVERED": snap.get("last_recovery"),
        "WHAT_CHANGED": debt["items"][:5],
        "WHAT_WAS_LEARNED": (snap.get("assumption_scan") or {}).get("lesson_conflict_candidates"),
        "startup_readiness": snap.get("startup_readiness"),
        "kill_switch": ks,
        "chain_of_thought_exposed": False,
        "evidence_basis": "durable_rows_only",
        "authority_state": snap.get("authority_state"),
    }
