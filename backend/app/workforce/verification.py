"""Verification pipeline (T14) — risk/task-scoped policy.

Builder cannot self-verify when independence is required.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from app.evidence_claim import evidence_supports_claim
from app.models.workforce import WorkforceAssignment
from app.models.workforce_ops import WorkforceVerificationDecision
from app.workforce.broker import VerificationError


@dataclass(frozen=True)
class VerificationPolicy:
    risk: str
    require_independent_verifier: bool
    require_two_agent_agreement: bool
    require_test_evidence: bool
    require_deterministic_validator: bool
    require_founder_approval: bool
    allowed_decisions_without_extra: tuple[str, ...] = ("UNVERIFIED", "CHECKED", "REJECTED", "SUPERSEDED")

    def as_dict(self) -> dict:
        return {
            "risk": self.risk,
            "require_independent_verifier": self.require_independent_verifier,
            "require_two_agent_agreement": self.require_two_agent_agreement,
            "require_test_evidence": self.require_test_evidence,
            "require_deterministic_validator": self.require_deterministic_validator,
            "require_founder_approval": self.require_founder_approval,
        }


def policy_for_risk(risk: str) -> VerificationPolicy:
    risk = (risk or "low").lower()
    if risk == "high":
        return VerificationPolicy(
            risk="high",
            require_independent_verifier=True,
            require_two_agent_agreement=True,
            require_test_evidence=True,
            require_deterministic_validator=True,
            require_founder_approval=True,
        )
    if risk == "medium":
        return VerificationPolicy(
            risk="medium",
            require_independent_verifier=True,
            require_two_agent_agreement=False,
            require_test_evidence=True,
            require_deterministic_validator=False,
            require_founder_approval=False,
        )
    return VerificationPolicy(
        risk="low",
        require_independent_verifier=True,
        require_two_agent_agreement=False,
        require_test_evidence=False,
        require_deterministic_validator=False,
        require_founder_approval=False,
    )


def apply_verification_decision(
    db: Session,
    *,
    owner_id: uuid.UUID,
    assignment: WorkforceAssignment,
    decision: str,
    risk: str = "low",
    verifier_profile_id: uuid.UUID | None = None,
    second_verifier_profile_id: uuid.UUID | None = None,
    test_evidence_ref: str | None = None,
    deterministic_validator: str | None = None,
    founder_approval_ref: str | None = None,
    agreement: bool | None = None,
    reason: str = "",
) -> WorkforceVerificationDecision:
    if assignment.owner_id != owner_id:
        raise VerificationError("owner mismatch")
    if decision not in ("UNVERIFIED", "CHECKED", "VERIFIED", "REJECTED", "SUPERSEDED"):
        raise VerificationError(f"invalid decision: {decision}")

    policy = policy_for_risk(risk)

    if decision == "VERIFIED":
        if policy.require_independent_verifier:
            if verifier_profile_id is None:
                raise VerificationError("independent verifier required")
            if verifier_profile_id == assignment.profile_id:
                raise VerificationError("BUILDER_CANNOT_SELF_VERIFY")
            if (
                assignment.verifier_profile_id is not None
                and verifier_profile_id != assignment.verifier_profile_id
            ):
                raise VerificationError("verifier does not match assignment requirement")
        if policy.require_two_agent_agreement:
            if second_verifier_profile_id is None:
                raise VerificationError("two-agent agreement required")
            if second_verifier_profile_id in (assignment.profile_id, verifier_profile_id):
                raise VerificationError("second verifier must be independent of builder and first verifier")
            if agreement is not True:
                raise VerificationError("two-agent agreement not confirmed")
        if policy.require_test_evidence:
            # EVIDENCE ROW EXISTS != VERIFIED. A truthy string is not evidence -- it must
            # name a real IntelligenceEvidence row, owned by this owner, that actually
            # supports THIS assignment (never another task's evidence), with a genuine
            # positive/current outcome. Reuses the same shared gate as capability_reality
            # (app.evidence_claim) rather than reinventing evidence semantics here.
            if not test_evidence_ref:
                raise VerificationError("test evidence required for this risk")
            try:
                evidence_uuid = uuid.UUID(str(test_evidence_ref))
            except (ValueError, AttributeError, TypeError):
                raise VerificationError("test_evidence_ref must reference a real evidence record") from None
            support = evidence_supports_claim(
                db,
                owner_id=owner_id,
                subject_key=str(assignment.id),
                proposition="verified_available",
                evidence_id=evidence_uuid,
            )
            if not support.supports:
                raise VerificationError(f"test evidence does not support this assignment: {', '.join(support.reasons)}")
        if policy.require_deterministic_validator and not deterministic_validator:
            raise VerificationError("deterministic validator required for this risk")
        if policy.require_founder_approval and not founder_approval_ref:
            raise VerificationError("founder approval required for this risk")

    # Collusion: same agent as builder cannot be either verifier.
    for vid in (verifier_profile_id, second_verifier_profile_id):
        if vid is not None and vid == assignment.profile_id and decision == "VERIFIED":
            raise VerificationError("BUILDER_CANNOT_SELF_VERIFY")

    row = WorkforceVerificationDecision(
        owner_id=owner_id,
        assignment_id=assignment.id,
        decision=decision,
        policy_snapshot=policy.as_dict(),
        verifier_profile_id=verifier_profile_id,
        second_verifier_profile_id=second_verifier_profile_id,
        test_evidence_ref=test_evidence_ref,
        deterministic_validator=deterministic_validator,
        founder_approval_ref=founder_approval_ref,
        agreement=agreement,
        reason=reason,
        provenance={"result_treated_as_data_until_verified": True},
    )
    db.add(row)

    assignment.verification_status = decision
    assignment.updated_at = datetime.utcnow()
    if decision == "VERIFIED":
        assignment.status = "completed"
        assignment.completed_at = datetime.utcnow()
    elif decision == "REJECTED":
        assignment.status = "failed"
        assignment.completed_at = datetime.utcnow()
    elif decision == "SUPERSEDED":
        assignment.status = "superseded"
        assignment.completed_at = datetime.utcnow()
    elif decision == "CHECKED":
        assignment.status = "awaiting_verification"
    db.flush()
    return row
