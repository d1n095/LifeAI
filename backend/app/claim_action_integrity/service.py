"""Evidence-derived claim/action state.

MODEL OUTPUT != FACT.  This module never performs the represented action and never turns
verification into permission.  Trusted control-plane adapters record immutable observations;
callers may only use language at or below the effective state derived here.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.claim_action_integrity import (
    ActionState,
    ClaimActionEvidence,
    ClaimActionReceipt,
    ClaimState,
    EvidenceSourceType,
    VerificationState,
)


class ClaimActionIntegrityError(ValueError):
    pass


_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_ACTOR_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._:@/-]{2,127}$")
_MUTABLE_EVIDENCE_MAX_AGE = timedelta(minutes=5)
_PERMISSION_AUTHORITY_ISSUERS = frozenset({"founder-capability-authority"})
_TRUSTED_SOURCES = {
    EvidenceSourceType.github.value,
    EvidenceSourceType.database.value,
    EvidenceSourceType.filesystem.value,
    EvidenceSourceType.ci.value,
    EvidenceSourceType.test_runner.value,
    EvidenceSourceType.deployment_provider.value,
    EvidenceSourceType.task_execution_ledger.value,
    EvidenceSourceType.verification_registry.value,
    EvidenceSourceType.external_service.value,
    EvidenceSourceType.permission_authority.value,
}
_SELF_SOURCES = {
    EvidenceSourceType.agent_self_report.value,
    EvidenceSourceType.mainai_generated_text.value,
}
_CLAIM_RANK = {
    ClaimState.unknown.value: 0,
    ClaimState.intended.value: 1,
    ClaimState.requested.value: 2,
    ClaimState.dispatched.value: 3,
    ClaimState.running.value: 4,
    ClaimState.completed.value: 5,
    ClaimState.externally_observed.value: 6,
    ClaimState.independently_verified.value: 7,
    ClaimState.certified.value: 8,
    ClaimState.merged.value: 9,
    ClaimState.deployed.value: 10,
    ClaimState.activated.value: 11,
    ClaimState.failed.value: 12,
}


@dataclass(frozen=True)
class ClaimAssessment:
    claimable: bool
    requested_state: str
    effective_state: str
    verification_state: str
    reasons: tuple[str, ...]
    evidence_id: UUID | None
    allowed_language: str


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _digest_evidence(
    *, owner_id: UUID, execution_id: str, subject_key: str, action_key: str,
    source_type: str, source_ref: str, artifact_sha: str | None, payload: dict[str, Any],
    observed_at: datetime,
) -> str:
    body = {
        "owner_id": str(owner_id), "execution_id": execution_id, "subject_key": subject_key,
        "action_key": action_key, "source_type": source_type, "source_ref": source_ref,
        "artifact_sha": artifact_sha, "payload": payload,
        "observed_at": _as_aware(observed_at).isoformat(),
    }
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def record_evidence(
    db: Session,
    *,
    owner_id: UUID,
    execution_id: str,
    subject_key: str,
    action_key: str,
    source_type: str,
    source_ref: str,
    payload: dict[str, Any],
    recorded_by: str,
    artifact_sha: str | None = None,
    observed_at: datetime | None = None,
    expires_at: datetime | None = None,
) -> ClaimActionEvidence:
    """Record an observation without treating the writer's claim as proof.

    `authoritative` is derived, never caller-controlled.  MainAI text and agent self-report are
    useful provenance, but structurally non-authoritative.
    """
    if source_type not in {item.value for item in EvidenceSourceType}:
        raise ClaimActionIntegrityError("unknown evidence source type")
    if not execution_id.strip() or not subject_key.strip() or not action_key.strip() or not source_ref.strip():
        raise ClaimActionIntegrityError("execution, subject, action, and source reference are required")
    if not isinstance(payload, dict):
        raise ClaimActionIntegrityError("evidence payload must be an object")
    if artifact_sha is not None and not _SHA_RE.fullmatch(artifact_sha):
        raise ClaimActionIntegrityError("artifact_sha must be an exact lowercase 40-character git SHA")
    observed = observed_at or _utc_now()
    fact_mutability = _evidence_mutability(action_key, payload)
    payload = {**payload, "fact_mutability": fact_mutability}
    if fact_mutability == "mutable_snapshot":
        governed_expiry = _as_aware(observed) + _MUTABLE_EVIDENCE_MAX_AGE
        expires_at = min(_as_aware(expires_at), governed_expiry) if expires_at else governed_expiry
    elif expires_at is not None:
        raise ClaimActionIntegrityError("immutable evidence must not carry mutable-state expiry")
    if expires_at is not None and _as_aware(expires_at) <= _as_aware(observed):
        raise ClaimActionIntegrityError("evidence expiry must be later than observation")
    digest = _digest_evidence(
        owner_id=owner_id, execution_id=execution_id, subject_key=subject_key,
        action_key=action_key, source_type=source_type, source_ref=source_ref,
        artifact_sha=artifact_sha, payload=payload, observed_at=observed,
    )
    existing = db.execute(
        select(ClaimActionEvidence).where(ClaimActionEvidence.payload_digest == digest)
    ).scalar_one_or_none()
    if existing is not None:
        if existing.owner_id != owner_id:
            raise ClaimActionIntegrityError("evidence digest belongs to another owner")
        return existing
    row = ClaimActionEvidence(
        owner_id=owner_id, execution_id=execution_id, subject_key=subject_key,
        action_key=action_key, source_type=source_type, source_ref=source_ref,
        artifact_sha=artifact_sha, authoritative=source_type in _TRUSTED_SOURCES,
        payload=payload, payload_digest=digest, observed_at=observed,
        expires_at=expires_at, recorded_by=recorded_by,
    )
    db.add(row)
    db.flush()
    return row


def _evidence_mutability(action_key: str, payload: dict[str, Any]) -> str:
    """Classify centrally; callers cannot make mutable state immortal by omission."""
    if action_key in {"branch_push", "activation", "database_observation", "generic", "permission_grant"}:
        return "mutable_snapshot"
    if action_key == "deployment":
        return "mutable_snapshot" if payload.get("evidence_semantics") == "current_state" else "immutable_fact"
    if payload.get("evidence_semantics") == "current_state":
        return "mutable_snapshot"
    if action_key == "test_run" and payload.get("status") not in {"passed", "failed"}:
        return "mutable_snapshot"
    return "immutable_fact"


def _negative(payload: dict[str, Any]) -> bool:
    return (
        payload.get("passed") is False
        or payload.get("success") is False
        or payload.get("verified") is False
        or str(payload.get("status", "")).lower() in {"failed", "failure", "error", "rejected", "cancelled"}
    )


def _validate_evidence(
    db: Session,
    *,
    evidence: ClaimActionEvidence,
    owner_id: UUID,
    execution_id: str,
    subject_key: str,
    action_key: str,
    artifact_sha: str | None,
    now: datetime,
) -> tuple[str, str, tuple[str, ...]]:
    reasons: list[str] = []
    if evidence.owner_id != owner_id:
        reasons.append("owner_mismatch")
    if evidence.execution_id != execution_id:
        reasons.append("execution_id_mismatch")
    if evidence.subject_key != subject_key or evidence.action_key != action_key:
        reasons.append("subject_or_action_mismatch")
    if artifact_sha is not None and evidence.artifact_sha != artifact_sha:
        reasons.append("artifact_sha_mismatch")
    if evidence.source_type in _SELF_SOURCES or not evidence.authoritative:
        reasons.append("self_report_or_non_authoritative_source")
    mutability = evidence.payload.get("fact_mutability")
    if mutability not in {"immutable_fact", "mutable_snapshot"}:
        reasons.append("evidence_mutability_missing_or_invalid")
    if mutability == "mutable_snapshot" and evidence.expires_at is None:
        reasons.append("mutable_snapshot_expiry_missing")
    if evidence.expires_at is not None and _as_aware(evidence.expires_at) <= _as_aware(now):
        reasons.append("stale_evidence")
    later = db.execute(
        select(ClaimActionEvidence).where(
            ClaimActionEvidence.owner_id == owner_id,
            ClaimActionEvidence.execution_id == execution_id,
            ClaimActionEvidence.subject_key == subject_key,
            ClaimActionEvidence.action_key == action_key,
            ClaimActionEvidence.artifact_sha == evidence.artifact_sha,
            ClaimActionEvidence.observed_at > evidence.observed_at,
        )
    ).scalars().all()
    if any(row.authoritative and row.source_type not in _SELF_SOURCES for row in later):
        reasons.append("superseded_by_newer_authoritative_evidence")

    if reasons:
        verification = VerificationState.stale.value if "stale_evidence" in reasons else (
            VerificationState.conflicting.value if "superseded_by_newer_authoritative_evidence" in reasons else VerificationState.invalid.value
        )
        return ClaimState.unknown.value, verification, tuple(reasons)

    if _negative(evidence.payload):
        return (
            ClaimState.failed.value,
            VerificationState.externally_observed.value,
            ("negative_evidence_bound",),
        )

    p = evidence.payload
    source = evidence.source_type
    target = ClaimState.externally_observed.value
    verification = VerificationState.externally_observed.value

    if action_key == "branch_push":
        if source != EvidenceSourceType.github.value or p.get("pushed") is not True or not p.get("branch"):
            reasons.append("github_remote_push_proof_required")
        if p.get("remote_sha") != evidence.artifact_sha or p.get("local_sha") != evidence.artifact_sha:
            reasons.append("local_remote_sha_mismatch")
    elif action_key == "test_run":
        if source not in {EvidenceSourceType.test_runner.value, EvidenceSourceType.ci.value}:
            reasons.append("test_runner_or_ci_proof_required")
        if p.get("status") != "passed" or not isinstance(p.get("passed"), int) or p.get("failed") != 0:
            reasons.append("passing_test_counts_required")
        if not p.get("command") or not isinstance(p.get("environment"), dict):
            reasons.append("test_command_and_environment_required")
        if p.get("execution_id") != execution_id or p.get("artifact_sha") != evidence.artifact_sha:
            reasons.append("test_run_binding_mismatch")
        if source == EvidenceSourceType.ci.value and p.get("independent") is True:
            target = ClaimState.independently_verified.value
            verification = VerificationState.independently_verified.value
    elif action_key == "certification":
        if artifact_sha is None:
            reasons.append("caller_candidate_sha_required")
        if source != EvidenceSourceType.verification_registry.value or p.get("review_result") != "PASS":
            reasons.append("independent_registry_pass_required")
        builder_actor = _canonical_actor_id(p.get("builder_actor_id"))
        examiner_actor = _canonical_actor_id(p.get("examiner_actor_id"))
        if p.get("candidate_sha") != artifact_sha or evidence.artifact_sha != artifact_sha:
            reasons.append("exact_candidate_sha_required")
        if builder_actor is None or examiner_actor is None or builder_actor == examiner_actor:
            reasons.append("exact_sha_independent_examiner_required")
        target = ClaimState.certified.value
        verification = VerificationState.certified.value
    elif action_key == "merge":
        if source != EvidenceSourceType.github.value or p.get("merged") is not True or not p.get("merge_sha"):
            reasons.append("github_merge_proof_required")
        target = ClaimState.merged.value
    elif action_key == "deployment":
        semantics = p.get("evidence_semantics")
        if source != EvidenceSourceType.deployment_provider.value or not p.get("deployment_id"):
            reasons.append("deployment_provider_success_required")
        if semantics == "historical_event":
            if p.get("status") != "succeeded" or not p.get("completed_at"):
                reasons.append("historical_deployment_event_fields_required")
            target = ClaimState.completed.value
        elif semantics == "current_state":
            if p.get("status") != "deployed" or not p.get("provider_observation_id") or not p.get("observed_at"):
                reasons.append("current_deployment_snapshot_fields_required")
            target = ClaimState.deployed.value
        else:
            reasons.append("deployment_evidence_semantics_required")
        if p.get("artifact_sha") != evidence.artifact_sha:
            reasons.append("deployment_artifact_mismatch")
    elif action_key == "activation":
        if source not in {EvidenceSourceType.deployment_provider.value, EvidenceSourceType.database.value, EvidenceSourceType.external_service.value}:
            reasons.append("activation_authority_source_required")
        if p.get("activated") is not True or not p.get("deployment_id"):
            reasons.append("explicit_activation_observation_required")
        if p.get("artifact_sha") != evidence.artifact_sha:
            reasons.append("activation_artifact_mismatch")
        target = ClaimState.activated.value
    elif action_key == "agent_completion":
        if source != EvidenceSourceType.task_execution_ledger.value or p.get("status") != "completed":
            reasons.append("completed_task_ledger_record_required")
        if (
            str(p.get("owner_id")) != str(owner_id)
            or p.get("execution_id") != execution_id
            or p.get("subject_key") != subject_key
            or p.get("action_key") != action_key
            or not p.get("job_id")
            or not p.get("result_artifact_id")
            or not p.get("observed_at")
        ):
            reasons.append("bound_task_ledger_terminal_record_required")
    else:
        if source not in {EvidenceSourceType.database.value, EvidenceSourceType.external_service.value, EvidenceSourceType.task_execution_ledger.value}:
            reasons.append("authoritative_observation_required")
        if source == EvidenceSourceType.database.value and (
            str(p.get("owner_id")) != str(owner_id)
            or p.get("execution_id") != execution_id
            or p.get("subject_key") != subject_key
            or p.get("action_key") != action_key
            or p.get("observed_state") not in {"completed", "failed"}
            or not p.get("observation_id")
            or not p.get("observed_at")
        ):
            reasons.append("specific_database_state_proof_required")
        if source == EvidenceSourceType.external_service.value and (
            str(p.get("owner_id")) != str(owner_id)
            or p.get("execution_id") != execution_id
            or p.get("subject_key") != subject_key
            or p.get("action_key") != action_key
            or p.get("observed_state") not in {"completed", "failed"}
            or not p.get("provider_id")
            or not p.get("provider_observation_id")
            or not p.get("observed_at")
            or p.get("evidence_semantics") not in {"historical_event", "current_state"}
            or (p.get("evidence_semantics") == "historical_event" and not p.get("completed_at"))
        ):
            reasons.append("specific_external_service_state_proof_required")
        if source == EvidenceSourceType.task_execution_ledger.value and (
            str(p.get("owner_id")) != str(owner_id)
            or p.get("execution_id") != execution_id
            or p.get("subject_key") != subject_key
            or p.get("action_key") != action_key
            or p.get("observed_state") not in {"completed", "failed"}
            or not p.get("job_id")
            or not p.get("result_artifact_id")
            or not p.get("observed_at")
        ):
            reasons.append("specific_task_ledger_state_proof_required")

    if reasons:
        return ClaimState.unknown.value, VerificationState.invalid.value, tuple(reasons)
    return target, verification, ("evidence_bound",)


def _parse_evidence_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return _as_aware(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def _validate_permission_grant(
    db: Session,
    *,
    grant: ClaimActionEvidence | None,
    owner_id: UUID,
    execution_id: str,
    subject_key: str,
    action_key: str,
    permitted_action: dict[str, Any],
    now: datetime,
) -> None:
    if grant is None or grant.source_type != EvidenceSourceType.permission_authority.value or not grant.authoritative:
        raise ClaimActionIntegrityError("permission grant must come from the configured permission authority")
    p = grant.payload
    issued_at = _parse_evidence_time(p.get("issued_at"))
    expires_at = _parse_evidence_time(p.get("expires_at"))
    required = (
        grant.owner_id == owner_id
        and grant.execution_id == execution_id
        and grant.subject_key == subject_key
        and grant.action_key == "permission_grant"
        and str(p.get("owner_id")) == str(owner_id)
        and p.get("principal") == permitted_action.get("principal")
        and p.get("execution_id") == execution_id
        and p.get("resource") == subject_key
        and p.get("action") == action_key
        and p.get("issuer_authority") == permitted_action.get("authority_ref")
        and p.get("granted") is True
        and p.get("revoked") is False
        and isinstance(p.get("grant_id"), str) and bool(p["grant_id"].strip())
        and p.get("issuer_authority") in _PERMISSION_AUTHORITY_ISSUERS
        and isinstance(p.get("limits"), dict)
        and isinstance(p.get("scopes"), list) and action_key in p["scopes"]
        and issued_at is not None and expires_at is not None
        and issued_at <= now < expires_at
        and grant.expires_at is not None and now < _as_aware(grant.expires_at)
    )
    if not required:
        raise ClaimActionIntegrityError("permission grant evidence is invalid, expired, or out of scope")
    revocations = db.execute(
        select(ClaimActionEvidence).where(
            ClaimActionEvidence.owner_id == owner_id,
            ClaimActionEvidence.execution_id == execution_id,
            ClaimActionEvidence.action_key == "permission_grant",
            ClaimActionEvidence.source_type == EvidenceSourceType.permission_authority.value,
            ClaimActionEvidence.observed_at >= grant.observed_at,
        )
    ).scalars().all()
    if any(
        row.authoritative
        and row.payload.get("grant_id") == p["grant_id"]
        and row.payload.get("revoked") is True
        for row in revocations
    ):
        raise ClaimActionIntegrityError("permission grant has been revoked")


def _canonical_actor_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    canonical = value.strip().casefold()
    return canonical if _ACTOR_ID_RE.fullmatch(canonical) else None


def _has_request_receipt(
    db: Session, *, owner_id: UUID, execution_id: str, subject_key: str, action_key: str,
) -> bool:
    return db.execute(
        select(ClaimActionReceipt.id).where(
            ClaimActionReceipt.owner_id == owner_id,
            ClaimActionReceipt.execution_id == execution_id,
            ClaimActionReceipt.subject_key == subject_key,
            ClaimActionReceipt.action_key == action_key,
            ClaimActionReceipt.declared_state == ClaimState.requested.value,
            ClaimActionReceipt.action_state == ActionState.requested.value,
        ).limit(1)
    ).scalar_one_or_none() is not None


def _allowed_language(state: str, subject_key: str) -> str:
    if state == ClaimState.intended.value:
        return f"I intend to perform {subject_key}."
    if state == ClaimState.requested.value:
        return f"I requested {subject_key}; completion is not verified."
    if state == ClaimState.failed.value:
        return f"{subject_key} failed."
    if state in {ClaimState.unknown.value, ClaimState.dispatched.value, ClaimState.running.value}:
        return f"The current externally verified state of {subject_key} is {state}."
    return f"Evidence supports {subject_key} at state {state}."


def assess_claim(
    db: Session,
    *,
    owner_id: UUID,
    execution_id: str,
    subject_key: str,
    action_key: str,
    requested_state: str,
    artifact_sha: str | None = None,
    evidence_id: UUID | None = None,
    now: datetime | None = None,
) -> ClaimAssessment:
    if requested_state not in _CLAIM_RANK:
        raise ClaimActionIntegrityError("unknown claim state")
    baseline = requested_state if requested_state in {ClaimState.intended.value, ClaimState.unknown.value} else ClaimState.unknown.value
    verification = VerificationState.unverified.value
    reasons: tuple[str, ...] = ("proof_absent",)
    evidence = None
    if evidence_id is not None:
        evidence = db.get(ClaimActionEvidence, evidence_id)
        if evidence is None:
            reasons = ("evidence_not_found",)
        else:
            proven, verification, reasons = _validate_evidence(
                db, evidence=evidence, owner_id=owner_id, execution_id=execution_id,
                subject_key=subject_key, action_key=action_key, artifact_sha=artifact_sha,
                now=now or _utc_now(),
            )
            if requested_state == ClaimState.failed.value:
                baseline = proven if proven == ClaimState.failed.value else ClaimState.unknown.value
            elif _CLAIM_RANK[requested_state] <= _CLAIM_RANK[proven]:
                baseline = requested_state
            else:
                baseline = proven
    claimable = requested_state == baseline and not any(
        r for r in reasons if r not in {"evidence_bound", "negative_evidence_bound"}
    )
    if requested_state == ClaimState.requested.value and evidence_id is None and _has_request_receipt(
        db, owner_id=owner_id, execution_id=execution_id,
        subject_key=subject_key, action_key=action_key,
    ):
        baseline = ClaimState.requested.value
        reasons = ("request_receipt_bound",)
        claimable = True
    if requested_state in {ClaimState.intended.value, ClaimState.unknown.value} and evidence_id is None:
        claimable = True
    return ClaimAssessment(
        claimable=claimable, requested_state=requested_state, effective_state=baseline,
        verification_state=verification, reasons=reasons,
        evidence_id=evidence.id if evidence else evidence_id,
        allowed_language=_allowed_language(baseline, subject_key),
    )


def require_claimable(db: Session, **kwargs: Any) -> ClaimAssessment:
    assessment = assess_claim(db, **kwargs)
    if not assessment.claimable:
        raise ClaimActionIntegrityError(
            f"claim cannot be upgraded to {assessment.requested_state}: {','.join(assessment.reasons)}; "
            f"effective={assessment.effective_state}"
        )
    return assessment


def record_receipt(
    db: Session,
    *,
    owner_id: UUID,
    execution_id: str,
    subject_key: str,
    action_key: str,
    declared_state: str,
    action_state: str,
    declared_action: dict[str, Any],
    permitted_action: dict[str, Any],
    executed_action: dict[str, Any],
    observed_result: dict[str, Any],
    authority_snapshot: dict[str, Any],
    created_by: str,
    artifact_sha: str | None = None,
    evidence_id: UUID | None = None,
    predecessor_receipt_id: UUID | None = None,
    now: datetime | None = None,
) -> ClaimActionReceipt:
    if action_state not in {item.value for item in ActionState}:
        raise ClaimActionIntegrityError("unknown action state")
    latest = db.execute(
        select(ClaimActionReceipt).where(
            ClaimActionReceipt.owner_id == owner_id,
            ClaimActionReceipt.execution_id == execution_id,
            ClaimActionReceipt.subject_key == subject_key,
            ClaimActionReceipt.action_key == action_key,
        ).order_by(ClaimActionReceipt.created_at.desc(), ClaimActionReceipt.id.desc())
    ).scalars().first()
    if latest is not None and predecessor_receipt_id != latest.id:
        raise ClaimActionIntegrityError("predecessor must be the latest receipt; stale receipt chain rejected")
    if latest is None and predecessor_receipt_id is not None:
        raise ClaimActionIntegrityError("predecessor does not belong to this receipt chain")

    requires_permission = bool(executed_action) or action_state in {
        ActionState.dispatched.value, ActionState.running.value, ActionState.completed.value,
    }
    if requires_permission:
        if permitted_action.get("granted") is not True or not permitted_action.get("authority_ref"):
            raise ClaimActionIntegrityError("execution state requires explicit scoped permission evidence")
        scopes = permitted_action.get("scopes")
        if not isinstance(scopes, list) or action_key not in scopes:
            raise ClaimActionIntegrityError("permission does not cover this action")
        try:
            grant_evidence_id = UUID(str(permitted_action.get("grant_evidence_id")))
        except (TypeError, ValueError):
            raise ClaimActionIntegrityError("permission requires genuine grant evidence") from None
        grant = db.get(ClaimActionEvidence, grant_evidence_id)
        _validate_permission_grant(
            db,
            grant=grant,
            owner_id=owner_id,
            execution_id=execution_id,
            subject_key=subject_key,
            action_key=action_key,
            permitted_action=permitted_action,
            now=_as_aware(now or _utc_now()),
        )

    if declared_state == ClaimState.requested.value and action_state == ActionState.requested.value and evidence_id is None:
        assessment = ClaimAssessment(
            claimable=True, requested_state=declared_state, effective_state=ClaimState.requested.value,
            verification_state=VerificationState.unverified.value,
            reasons=("request_receipt_recorded",), evidence_id=None,
            allowed_language=_allowed_language(ClaimState.requested.value, subject_key),
        )
    else:
        assessment = assess_claim(
            db, owner_id=owner_id, execution_id=execution_id, subject_key=subject_key,
            action_key=action_key, requested_state=declared_state, artifact_sha=artifact_sha,
            evidence_id=evidence_id, now=now,
        )
    receipt = ClaimActionReceipt(
        owner_id=owner_id, execution_id=execution_id, subject_key=subject_key,
        action_key=action_key, declared_action=declared_action,
        permitted_action=permitted_action, executed_action=executed_action,
        observed_result=observed_result, declared_state=declared_state,
        effective_state=assessment.effective_state, action_state=action_state,
        verification_state=assessment.verification_state, evidence_id=evidence_id,
        predecessor_receipt_id=predecessor_receipt_id,
        authority_snapshot=authority_snapshot,
        explanation=";".join(assessment.reasons), created_by=created_by,
    )
    db.add(receipt)
    db.flush()
    return receipt
