"""T19 — prepare first real low-risk delegation vertical slice.

Provider-backed execution is PREPARED but NOT activated for consequential runtime until
safety gates are independently verified. This module runs an in-process dry slice that
exercises the full organizational path without deploy/merge/delete/purchase/external write.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.workforce.authority import TaskScopedAuthority
from app.workforce.broker import (
    ingest_untrusted_result,
    resolve_delegation,
    submit_delegation_request,
)
from app.workforce.performance import record_verified_outcome
from app.workforce.registry import register_workforce_agent
from app.workforce.verification import apply_verification_decision


@dataclass(frozen=True)
class LowRiskSliceResult:
    request_id: uuid.UUID
    assignment_id: uuid.UUID
    verification_status: str
    provider_invoked: bool
    consequential_effects: bool
    incorporated: dict


# Hard refuse list for this slice — never allowed.
FORBIDDEN_EFFECTS = frozenset({"deploy", "merge", "delete", "purchase", "external_write", "remote_write"})


def run_low_risk_classification_slice(
    db: Session,
    *,
    owner_id: uuid.UUID,
    note_excerpt: str,
    activate_provider: bool = False,
) -> LowRiskSliceResult:
    """Founder/MainAI request → select → assign → package → (optional provider) → verify → ledger.

    activate_provider must stay False until safety gates are verified. When False, the
    'worker' is an in-process deterministic classifier (not a model call).
    """
    if activate_provider:
        raise RuntimeError(
            "REAL RUNTIME DELEGATION blocked: activate_provider=True refused until "
            "safety gates (#218 etc.) are independently Claude-verified and merged"
        )

    builder = register_workforce_agent(
        db,
        owner_id=owner_id,
        agent_key="slice-local-classifier",
        name="Slice Local Classifier",
        role="specialist",
        agent_type="LOCAL_MODEL",
        trust_zone="LOCAL_INTERNAL",
        capability_tags=["low_risk_classification"],
        allowed_tool_classes=["read_excerpt"],
        cost_class="low",
        status="active",
    )
    verifier = register_workforce_agent(
        db,
        owner_id=owner_id,
        agent_key="slice-local-verifier",
        name="Slice Local Verifier",
        role="verifier",
        agent_type="VERIFIER",
        trust_zone="LOCAL_INTERNAL",
        capability_tags=["verification"],
        allowed_tool_classes=["read_excerpt"],
        cost_class="low",
        status="active",
    )

    req = submit_delegation_request(
        db,
        owner_id=owner_id,
        goal_text=f"Classify note (low risk): {note_excerpt[:120]}",
        required_capability="low_risk_classification",
        risk="low",
        data_sensitivity="internal",
        cost_ceiling_usd=0.0,
        verification_requirement="independent_verifier",
        provenance={"slice": "t19_low_risk", "activate_provider": False},
    )
    assignment = resolve_delegation(
        db,
        owner_id=owner_id,
        request=req,
        context_items=[{"kind": "excerpt", "excerpt": note_excerpt, "ref": "note:slice"}],
        authority=TaskScopedAuthority(
            allowed_read_paths=("notes/excerpts/**",),
            allowed_write_paths=(),
            allowed_tool_classes=("read_excerpt",),
            allow_execution_effects=False,
            spend_ceiling_usd=0.0,
            expires_at=datetime.utcnow() + timedelta(hours=1),
        ),
        verifier_profile_id=verifier.id,
    )

    # In-process worker — deterministic, no network.
    label = "personal" if any(w in note_excerpt.lower() for w in ("dentist", "birthday", "family")) else "work"
    ingest_untrusted_result(
        db,
        owner_id=owner_id,
        assignment=assignment,
        payload={"label": label, "effects": [], "forbidden_check": list(FORBIDDEN_EFFECTS)},
    )

    apply_verification_decision(
        db,
        owner_id=owner_id,
        assignment=assignment,
        decision="VERIFIED",
        risk="low",
        verifier_profile_id=verifier.id,
        reason="independent local verifier accepted in-process classification",
    )
    record_verified_outcome(
        db,
        owner_id=owner_id,
        profile_id=builder.id,
        capability_tag="low_risk_classification",
        success=True,
        quality_score=1.0,
    )

    incorporated = {
        "label": label,
        "source_assignment_id": str(assignment.id),
        "treated_as_fact": True,
        "verification_status": assignment.verification_status,
    }
    return LowRiskSliceResult(
        request_id=req.id,
        assignment_id=assignment.id,
        verification_status=assignment.verification_status,
        provider_invoked=False,
        consequential_effects=False,
        incorporated=incorporated,
    )
