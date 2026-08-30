"""End-to-end delegation audit receipt — observable chain for founder inspection."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models.workforce import WorkforceAssignment


@dataclass
class DelegationAuditReceipt:
    receipt_id: str
    created_at: str
    founder_request: str
    selected_department: str
    selected_worker_key: str
    selection_reason: dict
    context_disclosed: list[str]
    context_denied: list[str]
    spend_reservation: dict
    provider_call: dict
    raw_unverified_result: dict
    verification: dict
    performance_update: dict
    assignment_id: str
    owner_id: str
    invariants: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "created_at": self.created_at,
            "founder_request": self.founder_request,
            "selected_department": self.selected_department,
            "selected_worker_key": self.selected_worker_key,
            "selection_reason": self.selection_reason,
            "context_disclosed": self.context_disclosed,
            "context_denied": self.context_denied,
            "spend_reservation": self.spend_reservation,
            "provider_call": self.provider_call,
            "raw_unverified_result": self.raw_unverified_result,
            "verification": self.verification,
            "performance_update": self.performance_update,
            "assignment_id": self.assignment_id,
            "owner_id": self.owner_id,
            "invariants": self.invariants,
        }


def build_audit_receipt(
    db: Session,
    *,
    owner_id: uuid.UUID,
    assignment: WorkforceAssignment,
    founder_request: str,
    selected_department: str,
    selected_worker_key: str,
    selection_reason: dict,
    disclosed_kinds: list[str],
    denied_kinds: list[str],
    spend_reservation: dict,
    provider_call: dict,
    raw_result: dict,
    verification: dict,
    performance_update: dict,
) -> DelegationAuditReceipt:
    if assignment.owner_id != owner_id:
        raise ValueError("owner mismatch on audit receipt")
    return DelegationAuditReceipt(
        receipt_id=str(uuid.uuid4()),
        created_at=datetime.utcnow().isoformat() + "Z",
        founder_request=founder_request,
        selected_department=selected_department,
        selected_worker_key=selected_worker_key,
        selection_reason=dict(selection_reason or {}),
        context_disclosed=list(disclosed_kinds),
        context_denied=list(denied_kinds),
        spend_reservation=dict(spend_reservation or {}),
        provider_call=dict(provider_call or {}),
        raw_unverified_result=dict(raw_result or {}),
        verification=dict(verification or {}),
        performance_update=dict(performance_update or {}),
        assignment_id=str(assignment.id),
        owner_id=str(owner_id),
        invariants=[
            "EXTERNAL_MODEL_OUTPUT_NE_TRUSTED_FACT",
            "DELEGATION_NE_AUTHORIZATION",
            "UNKNOWN_NE_VERIFIED",
            "SAFE_INTERNAL_NE_PROVIDER_ESCALATION",
        ],
    )
