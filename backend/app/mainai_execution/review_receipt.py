"""Exact-SHA independent review receipts for the first development cycle.

Uses the existing append-only `MainAIVerificationRecord` model. These records are evidence,
not execution authority: they do not grant push, merge, deploy, provider spend, or Track B
reviewer runtime.

Ordinary MainAI runtime (`mainai_app`) cannot INSERT verification rows (migration 0080).
A Founder-supplied receipt, or another privileged authorized examiner path, records the
attestation. Path A only *evaluates* a stored receipt against the exact publication identity.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.mainai_verification_registry.service import (
    BLOCKED,
    FAIL,
    PASS,
    VerificationRegistryError,
    record_verification_attestation,
)
from app.models.mainai_verification import MainAIVerificationRecord

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

FIRST_CYCLE_REVIEW_COMPONENT = "path_a.first_cycle.pr_review"
PATH_A_BUILDER_IDENTITY = "path_a.builder"

STATE_STOP_BEFORE_MERGE = "stop_before_merge"
STATE_INDEPENDENT_REVIEW_BLOCKED = "independent_review_blocked"

_ALLOWED_EXAMINER_CLASSES = frozenset({"founder", "external_agent", "authorized_readonly_examiner"})
_FORBIDDEN_REVIEWER_AUTHORITY = frozenset({"push", "merge", "deploy", "grant", "track_b"})


class ReviewReceiptError(ValueError):
    """Fail-closed review-receipt construction or evaluation error."""


def canonical_hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def instruction_hash(instruction: str) -> str:
    return hashlib.sha256((instruction or "").encode("utf-8")).hexdigest()


def output_hash(*, repository: str, pr_number: int, head_sha: str) -> str:
    return canonical_hash({"repository": repository, "pr_number": int(pr_number), "head_sha": head_sha})


def envelope_authorization_hash(envelope_context: dict[str, Any] | None) -> str:
    return canonical_hash(envelope_context or {})


def candidate_id_for(*, repository: str, pr_number: int) -> str:
    return f"github:{repository}#{int(pr_number)}"


def _require_sha(value: str, *, field: str) -> str:
    if not _SHA_RE.fullmatch(value or ""):
        raise ReviewReceiptError(f"{field} must be an exact 40-character lowercase git SHA")
    return value


def _require_independent(builder_identity: str, reviewer_identity: str) -> None:
    if not (builder_identity or "").strip() or not (reviewer_identity or "").strip():
        raise ReviewReceiptError("builder and reviewer identity are required")
    if builder_identity == reviewer_identity:
        raise ReviewReceiptError("reviewer and builder identities must be distinct")


def publication_truth(*, first_cycle_state: str, review_ok: bool = False, ci_passed: bool = False) -> dict[str, Any]:
    """Terminal first-cycle truth. Push, draft PR, CI, and review are never 'merged'."""
    return {
        "merged": False,
        "automatic_merge": False,
        "automatic_deploy": False,
        "push_is_not_merged": True,
        "draft_pr_is_not_merged": True,
        "ci_success_is_not_merged": True,
        "positive_review_is_not_merged": True,
        "ci_passed": ci_passed,
        "review_ok": review_ok,
        "first_cycle_state": first_cycle_state,
        "awaiting_founder_merge": first_cycle_state == STATE_STOP_BEFORE_MERGE,
        "subsequent_autonomous_job": False,
    }


@dataclass(frozen=True)
class ReviewEvaluation:
    ok: bool
    reason: str
    first_cycle_state: str
    record: MainAIVerificationRecord | None
    merged: bool = False
    truth: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.merged:
            raise ReviewReceiptError("independent review must never report merged=true")


def record_first_cycle_review_receipt(
    db: Session,
    *,
    repository: str,
    pr_number: int,
    head_sha: str,
    approved_base: str,
    builder_identity: str,
    reviewer_identity: str,
    review_result: str,
    evidence_sha: str,
    instruction_hash_value: str,
    output_hash_value: str,
    envelope_context: dict[str, Any] | None,
    evidence_summary: str,
    owner_id: UUID | None = None,
    examiner_class: str = "founder",
    reviewed_at: datetime | None = None,
    test_evidence_refs: list[Any] | None = None,
) -> MainAIVerificationRecord:
    """Privileged Founder/examiner insert. Runtime Path A must not call this as mainai_app."""
    _require_sha(head_sha, field="head_sha")
    _require_sha(approved_base, field="approved_base")
    _require_sha(evidence_sha, field="evidence_sha")
    _require_independent(builder_identity, reviewer_identity)
    if not repository.strip():
        raise ReviewReceiptError("repository is required")
    if int(pr_number) <= 0:
        raise ReviewReceiptError("pr_number must be a positive GitHub PR number")
    if examiner_class not in _ALLOWED_EXAMINER_CLASSES:
        raise ReviewReceiptError("examiner_class is not an authorized read-only examination path")
    if review_result not in {PASS, FAIL, BLOCKED}:
        raise ReviewReceiptError("review_result must be PASS, FAIL, or BLOCKED")
    expected_output = output_hash(repository=repository, pr_number=int(pr_number), head_sha=head_sha)
    if output_hash_value != expected_output:
        raise ReviewReceiptError("output_hash does not bind the exact repository/PR/head SHA")
    scope = {
        "repository": repository,
        "pr_number": int(pr_number),
        "approved_base": approved_base,
        "evidence_sha": evidence_sha,
        "instruction_hash": instruction_hash_value,
        "output_hash": output_hash_value,
        "envelope": envelope_context or {},
        "reviewer_authority": [],
        "forbidden_reviewer_authority": sorted(_FORBIDDEN_REVIEWER_AUTHORITY),
    }
    provenance = {
        "path": "founder_supplied_or_authorized_readonly_examiner",
        "envelope": envelope_context or {},
        "authorization_context": {
            "component_id": FIRST_CYCLE_REVIEW_COMPONENT,
            "builder_identity": builder_identity,
            "reviewer_identity": reviewer_identity,
            "examiner_class": examiner_class,
        },
        "track_b": "off",
    }
    try:
        return record_verification_attestation(
            db,
            owner_id=owner_id,
            component_id=FIRST_CYCLE_REVIEW_COMPONENT,
            candidate_id=candidate_id_for(repository=repository, pr_number=int(pr_number)),
            candidate_sha=head_sha,
            candidate_tree_identity=approved_base,
            builder_identity=builder_identity,
            examiner_identity=reviewer_identity,
            examiner_class=examiner_class,
            independence_relationship="founder_or_distinct_examiner",
            identity_assurance="asserted",
            review_type="independent_review",
            review_result=review_result,
            reviewed_at=reviewed_at,
            verification_scope=scope,
            evidence_summary=evidence_summary,
            test_evidence_refs=test_evidence_refs or [],
            source_provenance=provenance,
        )
    except VerificationRegistryError as exc:
        raise ReviewReceiptError(str(exc)) from exc


def _scope(record: MainAIVerificationRecord) -> dict[str, Any]:
    scope = record.verification_scope if isinstance(record.verification_scope, dict) else {}
    return scope


def _blocked(reason: str, *, record: MainAIVerificationRecord | None = None, ci_passed: bool = True) -> ReviewEvaluation:
    truth = publication_truth(first_cycle_state=STATE_INDEPENDENT_REVIEW_BLOCKED, review_ok=False, ci_passed=ci_passed)
    return ReviewEvaluation(
        ok=False,
        reason=reason,
        first_cycle_state=STATE_INDEPENDENT_REVIEW_BLOCKED,
        record=record,
        merged=False,
        truth=truth,
    )


def evaluate_first_cycle_review_receipt(
    db: Session | None,
    *,
    repository: str,
    pr_number: int,
    head_sha: str,
    approved_base: str,
    builder_identity: str,
    instruction_hash_value: str,
    output_hash_value: str,
    envelope_context: dict[str, Any] | None,
    owner_id: UUID | None = None,
) -> ReviewEvaluation:
    """Return GO only for a verifiable exact-SHA independent PASS. Never reports merged."""
    if db is None:
        return _blocked("no review receipt; first cycle must STOP")
    if not _SHA_RE.fullmatch(head_sha or "") or not _SHA_RE.fullmatch(approved_base or ""):
        return _blocked("publication identity is not an exact SHA; independent review cannot complete")
    if int(pr_number) <= 0 or not repository.strip():
        return _blocked("publication identity is incomplete; independent review cannot complete")
    if not (builder_identity or "").strip():
        return _blocked("builder identity is required")

    expected_id = candidate_id_for(repository=repository, pr_number=int(pr_number))
    expected_output = output_hash(repository=repository, pr_number=int(pr_number), head_sha=head_sha)
    expected_envelope = envelope_authorization_hash(envelope_context)
    stmt = select(MainAIVerificationRecord).where(
        MainAIVerificationRecord.component_id == FIRST_CYCLE_REVIEW_COMPONENT,
        MainAIVerificationRecord.candidate_id == expected_id,
        MainAIVerificationRecord.candidate_sha == head_sha,
    )
    if owner_id is None:
        stmt = stmt.where(MainAIVerificationRecord.owner_id.is_(None))
    else:
        stmt = stmt.where(or_(MainAIVerificationRecord.owner_id.is_(None), MainAIVerificationRecord.owner_id == owner_id))
    record = db.execute(
        stmt.order_by(MainAIVerificationRecord.reviewed_at.desc(), MainAIVerificationRecord.created_at.desc())
    ).scalars().first()
    if record is None:
        return _blocked("no review receipt; first cycle must STOP")

    if record.builder_identity != builder_identity:
        return _blocked("receipt builder identity does not match the builder", record=record)
    if record.examiner_identity == record.builder_identity or record.examiner_identity == builder_identity:
        return _blocked("reviewer and builder identities must be distinct", record=record)
    if record.candidate_tree_identity != approved_base:
        return _blocked("review receipt approved base does not match", record=record)
    if record.examiner_class not in _ALLOWED_EXAMINER_CLASSES:
        return _blocked("examiner_class is not an authorized read-only examination path", record=record)

    scope = _scope(record)
    if scope.get("repository") != repository or int(scope.get("pr_number") or 0) != int(pr_number):
        return _blocked("forged or mismatched repository/PR binding", record=record)
    if scope.get("instruction_hash") != instruction_hash_value:
        return _blocked("instruction_hash does not match the goal instruction", record=record)
    if scope.get("output_hash") != output_hash_value or output_hash_value != expected_output:
        return _blocked("output_hash does not match the exact PR head", record=record)
    if envelope_authorization_hash(scope.get("envelope") if isinstance(scope.get("envelope"), dict) else None) != expected_envelope:
        return _blocked("envelope/authorization context does not match", record=record)
    if any(item in _FORBIDDEN_REVIEWER_AUTHORITY for item in (scope.get("reviewer_authority") or [])):
        return _blocked("review receipt must not grant reviewer push/merge/deploy authority", record=record)
    if not isinstance(scope.get("evidence_sha"), str) or not _SHA_RE.fullmatch(scope["evidence_sha"]):
        return _blocked("evidence SHA is missing or not exact", record=record)

    if record.review_result in {FAIL, BLOCKED}:
        return _blocked(f"negative or inconclusive review: {record.review_result}", record=record)
    if record.review_result != PASS:
        return _blocked(f"inconclusive review: {record.review_result}", record=record)

    truth = publication_truth(first_cycle_state=STATE_STOP_BEFORE_MERGE, review_ok=True, ci_passed=True)
    return ReviewEvaluation(
        ok=True,
        reason="approved independent review; STOP before merge",
        first_cycle_state=STATE_STOP_BEFORE_MERGE,
        record=record,
        merged=False,
        truth=truth,
    )
