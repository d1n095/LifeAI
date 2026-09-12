"""Founder Program Truth -- composes the existing, already-real
`app.mainai_executive.dashboard.founder_executive_dashboard()` (WHAT_SHE_IS_DOING/WHY/
WHAT_IS_NEXT/WHAT_IS_BLOCKED/WHAT_SHE_IS_UNSURE_ABOUT/WHAT_CHANGED/WHAT_WAS_LEARNED/...) with
this package's own new completion/gap/evidence views into one founder-facing truth payload. See
docs/mainai_v2/MAINAI_COGNITIVE_CONTROL_PLANE_RECONCILIATION.md for the architecture decision.

MAINAI SHOULD COMPRESS COMPLEXITY FOR THE FOUNDER, NOT TRANSFER IT: every field below answers
ONE plain question from the founder's own named list, with a pointer to the real evidence
behind it -- never raw internal state, never chain-of-thought."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.mainai_executive.dashboard import founder_executive_dashboard
from app.mainai_vision.completion import assess_program_completion


def founder_program_truth(db: Session, *, owner_id: uuid.UUID, session_id: str | None = None) -> dict[str, Any]:
    """Single, coherent status payload answering the founder's own named questions. Never
    invents an answer: any field this package cannot yet derive for real is explicitly `None`/
    `"UNKNOWN"`, matching `dashboard.founder_executive_dashboard()`'s own
    `evidence_basis: "durable_rows_only"` discipline exactly."""

    dashboard = founder_executive_dashboard(db, owner_id=owner_id, session_id=session_id)
    completion = assess_program_completion(db, owner_id=owner_id)

    return {
        "WHERE_ARE_WE": dashboard.get("WHAT_SHE_IS_DOING"),
        "PERCENT_GENUINELY_COMPLETE": completion.overall_percent,
        "WHY": dashboard.get("WHY"),
        "COMPLETION_DEFINITION": completion.definition,
        "DENOMINATOR": completion.denominator,
        "NODE_COUNT": completion.node_count,
        "PER_DIMENSION_COMPLETION": completion.per_dimension_fraction,
        "WHAT_IS_STILL_MISSING": dashboard.get("WHAT_IS_BLOCKED"),
        "WHAT_IS_VERIFIED": {
            entity_id: maturity
            for entity_id, maturity in completion.node_maturity.items()
            if maturity in ("INDEPENDENTLY_REVIEWED", "INTEGRATED", "ACTIVATED", "PRODUCTION_PROVEN", "MAINTAINED")
        },
        "WHAT_IS_ONLY_IMPLEMENTED": {
            entity_id: maturity
            for entity_id, maturity in completion.node_maturity.items()
            if maturity in ("IMPLEMENTED", "UNIT_TESTED", "INTEGRATION_TESTED", "ROBUSTNESS_TESTED")
        },
        "WHAT_IS_ASSUMPTION": dashboard.get("WHAT_SHE_IS_UNSURE_ABOUT"),
        "WHAT_IS_BLOCKING": dashboard.get("WHAT_IS_BLOCKED"),
        "WHAT_SHOULD_HAPPEN_NEXT": (dashboard.get("WHAT_IS_NEXT") or [None])[0] if dashboard.get("WHAT_IS_NEXT") else None,
        "WHAT_WAS_LEARNED": dashboard.get("WHAT_WAS_LEARNED"),
        "not_100_percent_means_nothing_more_can_be_improved": False,
        "chain_of_thought_exposed": False,
        "evidence_basis": "durable_rows_only",
        "authority_state": dashboard.get("authority_state"),
        "kill_switch": dashboard.get("kill_switch"),
    }
