from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.mainai_verification import MainAIVerificationRecord

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
PASS = "PASS"
FAIL = "FAIL"
BLOCKED = "BLOCKED"


class VerificationRegistryError(ValueError):
    pass


def _require_sha(candidate_sha: str) -> None:
    if not _SHA_RE.fullmatch(candidate_sha):
        raise VerificationRegistryError("candidate_sha must be an exact 40-character lowercase git SHA")


def _require_independent(builder_identity: str, examiner_identity: str) -> None:
    if not builder_identity.strip() or not examiner_identity.strip():
        raise VerificationRegistryError("builder and examiner identity are required")
    if builder_identity == examiner_identity:
        raise VerificationRegistryError("builder self-certification cannot establish independent verification")


def record_verification_attestation(
    db: Session,
    *,
    component_id: str,
    candidate_sha: str,
    builder_identity: str,
    examiner_identity: str,
    review_result: str,
    owner_id: UUID | None = None,
    candidate_id: str | None = None,
    candidate_tree_identity: str | None = None,
    examiner_class: str = "external_agent",
    independence_relationship: str = "different_agent",
    identity_assurance: str = "asserted",
    review_type: str = "independent_review",
    reviewed_at: datetime | None = None,
    verification_scope: dict[str, Any] | None = None,
    evidence_summary: str = "",
    test_evidence_refs: list[Any] | None = None,
    source_provenance: dict[str, Any] | None = None,
    supersedes_verification_id: UUID | None = None,
    invalidates_verification_id: UUID | None = None,
) -> MainAIVerificationRecord:
    _require_sha(candidate_sha)
    _require_independent(builder_identity, examiner_identity)
    if review_result not in {PASS, FAIL, BLOCKED}:
        raise VerificationRegistryError("review_result must be PASS, FAIL, or BLOCKED")
    if not component_id.strip():
        raise VerificationRegistryError("component_id is required")
    if not evidence_summary.strip():
        raise VerificationRegistryError("evidence_summary is required")
    record = MainAIVerificationRecord(
        owner_id=owner_id,
        component_id=component_id,
        candidate_id=candidate_id or f"{component_id}:{candidate_sha}",
        candidate_sha=candidate_sha,
        candidate_tree_identity=candidate_tree_identity,
        builder_identity=builder_identity,
        examiner_identity=examiner_identity,
        examiner_class=examiner_class,
        independence_relationship=independence_relationship,
        identity_assurance=identity_assurance,
        review_type=review_type,
        review_result=review_result,
        reviewed_at=reviewed_at or datetime.now(timezone.utc),
        verification_scope=verification_scope or {},
        evidence_summary=evidence_summary,
        test_evidence_refs=test_evidence_refs or [],
        source_provenance=source_provenance or {},
        supersedes_verification_id=supersedes_verification_id,
        invalidates_verification_id=invalidates_verification_id,
    )
    db.add(record)
    db.flush()
    return record


def find_independent_pass(
    db: Session | None,
    *,
    component_id: str,
    candidate_sha: str | None,
    builder_identity: str,
    owner_id: UUID | None = None,
) -> MainAIVerificationRecord | None:
    if db is None or candidate_sha is None:
        return None
    _require_sha(candidate_sha)
    stmt = select(MainAIVerificationRecord).where(
        MainAIVerificationRecord.component_id == component_id,
        MainAIVerificationRecord.candidate_sha == candidate_sha,
        MainAIVerificationRecord.review_result == PASS,
        MainAIVerificationRecord.builder_identity == builder_identity,
        MainAIVerificationRecord.examiner_identity != builder_identity,
    )
    if owner_id is None:
        stmt = stmt.where(MainAIVerificationRecord.owner_id.is_(None))
    else:
        stmt = stmt.where(or_(MainAIVerificationRecord.owner_id.is_(None), MainAIVerificationRecord.owner_id == owner_id))
    return db.execute(stmt.order_by(MainAIVerificationRecord.reviewed_at.desc(), MainAIVerificationRecord.created_at.desc())).scalars().first()
