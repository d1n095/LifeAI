"""Verification pipeline (T14) — risk/task-scoped policy.

Builder cannot self-verify when independence is required.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models.workforce import WorkforceAssignment
from app.models.workforce import WorkforceDelegationRequest
from app.models.intelligence_governance import IntelligenceEvidence
from app.models.workforce_ops import WorkforceVerificationDecision, WorkforceVerificationEvidenceBinding
from app.evidence_claim import evidence_supports_claim
from app.workforce.broker import VerificationError

_MAX_HIGH_RISK_EVIDENCE_AGE = timedelta(days=7)


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


def _evidence_payload_assignment_id(payload: dict) -> str | None:
    for key in ("assignment_id", "workforce_assignment_id"):
        value = payload.get(key)
        if value:
            return str(value)
    return None


def _bind_high_risk_evidence_to_assignment(
    db: Session,
    *,
    owner_id: uuid.UUID,
    assignment: WorkforceAssignment,
    evidence: IntelligenceEvidence,
    capability_key: str,
) -> None:
    """Durably consume one high-risk evidence row for one exact assignment.

    The INSERT's unique constraints close both sequential and concurrent replay. A retry for
    the same assignment/evidence is idempotent; a different assignment cannot consume the
    same evidence row.
    """
    payload = evidence.payload if isinstance(evidence.payload, dict) else {}
    bound_assignment_id = _evidence_payload_assignment_id(payload)
    if bound_assignment_id != str(assignment.id):
        raise VerificationError("high-risk evidence is not bound to this assignment")

    stmt = (
        pg_insert(WorkforceVerificationEvidenceBinding)
        .values(
            owner_id=owner_id,
            assignment_id=assignment.id,
            evidence_id=evidence.id,
            evidence_execution_id=evidence.execution_id,
            capability_key=capability_key,
            binding_kind="high_risk_assignment_verification",
            provenance={
                "evidence_for_execution_ne_evidence_for_another_execution": True,
                "assignment_bound_in_evidence_payload": True,
                "verified_ne_authorized": True,
            },
        )
        .on_conflict_do_nothing()
        .returning(WorkforceVerificationEvidenceBinding.id)
    )
    inserted_id = db.execute(stmt).scalar_one_or_none()
    if inserted_id is not None:
        return

    existing = db.execute(
        select(WorkforceVerificationEvidenceBinding).where(
            WorkforceVerificationEvidenceBinding.owner_id == owner_id,
            WorkforceVerificationEvidenceBinding.evidence_id == evidence.id,
        )
    ).scalar_one_or_none()
    if existing is not None and existing.assignment_id == assignment.id:
        return
    if existing is not None:
        raise VerificationError("high-risk evidence already consumed by another assignment")
    raise VerificationError("high-risk evidence consumption failed closed")


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
        if policy.require_test_evidence and not test_evidence_ref:
            raise VerificationError("test evidence required for this risk")
        if policy.require_test_evidence:
            try:
                evidence_id = uuid.UUID(str(test_evidence_ref))
            except (TypeError, ValueError):
                raise VerificationError("test_evidence_ref must be an IntelligenceEvidence id")
            request = db.get(WorkforceDelegationRequest, assignment.delegation_request_id)
            if request is None or request.owner_id != owner_id:
                raise VerificationError("delegation request missing or owner mismatch")
            evidence_row = db.execute(
                select(IntelligenceEvidence).where(
                    IntelligenceEvidence.id == evidence_id,
                    IntelligenceEvidence.owner_id == owner_id,
                )
            ).scalar_one_or_none()
            support = evidence_supports_claim(
                db,
                owner_id=owner_id,
                subject_key=request.required_capability,
                proposition="workforce_verification",
                evidence_id=evidence_id,
                allowed_kinds={"test_run_result", "verification_result", "deterministic_check", "exam_result"},
                require_deterministic=policy.require_deterministic_validator,
                max_age=_MAX_HIGH_RISK_EVIDENCE_AGE if risk == "high" else None,
                evidence_row=evidence_row,
            )
            if not support.supports:
                raise VerificationError("test evidence does not support assignment: " + ",".join(support.reasons))
            if evidence_row is None:
                raise VerificationError("test evidence not found")
            if risk == "high":
                _bind_high_risk_evidence_to_assignment(
                    db,
                    owner_id=owner_id,
                    assignment=assignment,
                    evidence=evidence_row,
                    capability_key=request.required_capability,
                )
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
