"""Bounded self-improvement cycle. Observe → propose → prepare. Never grant authority or deploy."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from app.agent_coordination.durable import DurableCoordinationService

FIRST_CYCLE_GO = False


def _iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def run_bounded_self_improvement_cycle(
    service: DurableCoordinationService,
    *,
    owner_id: str,
    observed_gaps: list[dict[str, Any]],
    authorized_scope: str,
    actor: str = "mainai",
) -> dict[str, Any]:
    """One bounded cycle. Proposals are not goals, not grants, not deploys."""
    if actor not in {"mainai", "founder"}:
        return {
            "accepted": False,
            "reason": "untrusted actor cannot start self-improvement",
            "authority_expanded": False,
            "deployed": False,
            "first_cycle_go": FIRST_CYCLE_GO,
        }
    store = service._operational_handover.setdefault("self_improve", {"cycles": [], "lessons": []})
    proposals = []
    for gap in observed_gaps:
        if gap.get("provider_idea") and not gap.get("evidence_backed"):
            continue
        proposals.append(
            {
                "proposal_id": str(uuid.uuid4()),
                "gap": gap.get("description") or gap.get("gap"),
                "scope": authorized_scope,
                "priority": int(gap.get("priority") or 50),
                "delegate_to_capability": gap.get("capability") or "engineering",
                "requires_independent_review": True,
                "authority_expanded": False,
                "deployed": False,
            }
        )
    cycle = {
        "cycle_id": str(uuid.uuid4()),
        "owner_id": str(owner_id),
        "observed": len(observed_gaps),
        "proposals": sorted(proposals, key=lambda row: row["priority"]),
        "delegated_engineering": False,
        "tested": False,
        "independently_verified": False,
        "lessons_preserved": False,
        "candidate_prepared": False,
        "authority_expanded": False,
        "stop_bypassed": False,
        "budget_expanded": False,
        "protections_removed": False,
        "deployed": False,
        "code_prep_is_not_deploy": True,
        "created_at": _iso(),
        "first_cycle_go": FIRST_CYCLE_GO,
    }
    store["cycles"].append(cycle)
    return {
        "accepted": True,
        "cycle": cycle,
        "next": "independent_review_then_founder_disposition" if proposals else "no_evidence_backed_gap",
        "first_cycle_go": FIRST_CYCLE_GO,
        "vendor_delivery_claimed": False,
    }


def preserve_verified_lesson(
    service: DurableCoordinationService,
    *,
    cycle_id: str,
    lesson: str,
    independently_verified: bool,
) -> dict[str, Any]:
    if not independently_verified:
        return {"accepted": False, "reason": "unverified lesson is not preserved as governing", "first_cycle_go": FIRST_CYCLE_GO}
    store = service._operational_handover.setdefault("self_improve", {"cycles": [], "lessons": []})
    record = {
        "lesson_id": str(uuid.uuid4()),
        "cycle_id": cycle_id,
        "lesson": lesson,
        "authorization_still_valid": False,
        "created_at": _iso(),
    }
    store["lessons"].append(record)
    for cycle in store["cycles"]:
        if cycle.get("cycle_id") == cycle_id:
            cycle["lessons_preserved"] = True
    return {"accepted": True, "lesson": record, "first_cycle_go": FIRST_CYCLE_GO}
