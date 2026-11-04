"""Verification pipeline (T14) — risk/task-scoped policy.

Builder cannot self-verify when independence is required.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.evidence_claim import evidence_supports_claim
from app.models.intelligence_governance import IntelligenceEvidence, IntelligenceExecution
from app.models.workforce import WorkforceAssignment
from app.models.workforce import WorkforceDelegationRequest
from app.models.workforce_ops import WorkforceVerificationDecision
from app.workforce.broker import VerificationError

_MAX_HIGH_RISK_EVIDENCE_AGE = timedelta(days=7)


def _current_authority_epochs(db: Session, *, owner_id: uuid.UUID) -> dict[str, int]:
    rows = db.execute(
        text(
            """
            SELECT scope_key, epoch
            FROM workforce_authority_epoch
            WHERE scope_key IN ('GLOBAL', :owner_scope)
            """
        ),
        {"owner_scope": str(owner_id)},
    ).mappings().all()
    by_scope = {row["scope_key"]: int(row["epoch"] or 0) for row in rows}
    return {
        "global_authority_epoch": by_scope.get("GLOBAL", 0),
        "owner_authority_epoch": by_scope.get(str(owner_id), 0),
    }


def _payload_uuid(payload: dict, key: str) -> uuid.UUID | None:
    value = payload.get(key)
    if value in (None, ""):
        return None
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        raise VerificationError(f"{key} must be a UUID")


def _validate_high_risk_execution_evidence(
    db: Session,
    *,
    owner_id: uuid.UUID,
    assignment: WorkforceAssignment,
    request: WorkforceDelegationRequest,
    evidence_id: uuid.UUID,
) -> None:
    evidence = db.execute(
        select(IntelligenceEvidence).where(
            IntelligenceEvidence.id == evidence_id,
            IntelligenceEvidence.owner_id == owner_id,
        )
    ).scalar_one_or_none()
    if evidence is None:
        raise VerificationError("test evidence not found for owner")
    payload = evidence.payload if isinstance(evidence.payload, dict) else {}

    if _payload_uuid(payload, "assignment_id") != assignment.id:
        raise VerificationError("test evidence is not bound to this assignment")
    if _payload_uuid(payload, "delegation_request_id") != assignment.delegation_request_id:
        raise VerificationError("test evidence is not bound to this delegation request")
    if _payload_uuid(payload, "execution_id") != evidence.execution_id:
        raise VerificationError("test evidence is not bound to this execution")

    execution = db.execute(
        select(IntelligenceExecution).where(
            IntelligenceExecution.id == evidence.execution_id,
            IntelligenceExecution.owner_id == owner_id,
        )
    ).scalar_one_or_none()
    if execution is None:
        raise VerificationError("test evidence execution not found for owner")
    if request.task_id is not None and execution.task_id != request.task_id:
        raise VerificationError("test evidence execution is not for this work item")

    epochs = _current_authority_epochs(db, owner_id=owner_id)
    for key, current in epochs.items():
        if payload.get(key) != current:
            raise VerificationError(f"test evidence {key} is stale or missing")


def _existing_verified_evidence_claim(
    db: Session, *, owner_id: uuid.UUID, evidence_ref: str
) -> WorkforceVerificationDecision | None:
    return db.execute(
        select(WorkforceVerificationDecision).where(
            WorkforceVerificationDecision.owner_id == owner_id,
            WorkforceVerificationDecision.decision == "VERIFIED",
            WorkforceVerificationDecision.test_evidence_ref == evidence_ref,
        )
    ).scalar_one_or_none()


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
    high_risk_evidence_ref: str | None = None

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
            if not test_evidence_ref:
                raise VerificationError("test evidence required for this risk")
            try:
                evidence_id = uuid.UUID(str(test_evidence_ref))
            except (TypeError, ValueError):
                raise VerificationError("test_evidence_ref must be an IntelligenceEvidence id")
            evidence_ref = str(evidence_id)
            request = db.get(WorkforceDelegationRequest, assignment.delegation_request_id)
            if request is None or request.owner_id != owner_id:
                raise VerificationError("delegation request missing or owner mismatch")
            support = evidence_supports_claim(
                db,
                owner_id=owner_id,
                subject_key=request.required_capability,
                proposition="workforce_verification",
                evidence_id=evidence_id,
                allowed_kinds={"test_run_result", "verification_result", "deterministic_check", "exam_result"},
                require_deterministic=policy.require_deterministic_validator,
                max_age=_MAX_HIGH_RISK_EVIDENCE_AGE if policy.risk == "high" else None,
            )
            if not support.supports:
                raise VerificationError("test evidence does not support assignment: " + ",".join(support.reasons))
            if policy.risk == "high":
                _validate_high_risk_execution_evidence(
                    db,
                    owner_id=owner_id,
                    assignment=assignment,
                    request=request,
                    evidence_id=evidence_id,
                )
                high_risk_evidence_ref = evidence_ref
                test_evidence_ref = evidence_ref
        if policy.require_deterministic_validator and not deterministic_validator:
            raise VerificationError("deterministic validator required for this risk")
        if policy.require_founder_approval and not founder_approval_ref:
            raise VerificationError("founder approval required for this risk")
        if high_risk_evidence_ref is not None:
            existing = _existing_verified_evidence_claim(
                db, owner_id=owner_id, evidence_ref=high_risk_evidence_ref
            )
            if existing is not None:
                if existing.assignment_id == assignment.id:
                    return existing
                raise VerificationError("test evidence already verifies another assignment")

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
    savepoint = db.begin_nested()
    try:
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
        savepoint.commit()
        return row
    except IntegrityError as exc:
        savepoint.rollback()
        if (
            decision == "VERIFIED"
            and policy.risk == "high"
            and test_evidence_ref
            and (
                getattr(getattr(getattr(exc, "orig", None), "diag", None), "constraint_name", None)
                == "uq_workforce_verified_test_evidence_ref"
                or "uq_workforce_verified_test_evidence_ref" in str(exc)
            )
        ):
            existing = _existing_verified_evidence_claim(
                db, owner_id=owner_id, evidence_ref=test_evidence_ref
            )
            if existing is not None and existing.assignment_id == assignment.id:
                return existing
            raise VerificationError("test evidence already verifies another assignment")
        raise
