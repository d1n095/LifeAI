"""Semantic evidence validator — EVIDENCE EXISTS != CLAIM PROVEN.

Shared rule for capability_reality, school routing, readiness, workforce.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.intelligence_governance import IntelligenceEvidence


@dataclass(frozen=True)
class EvidenceSupportResult:
    supports: bool
    reasons: tuple[str, ...]
    evidence_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "supports": self.supports,
            "reasons": list(self.reasons),
            "evidence_id": self.evidence_id,
            "evidence_exists_is_not_claim_proven": True,
        }


_FAIL_MARKERS = frozenset(
    {
        "failed",
        "failure",
        "rejected",
        "error",
        "invalid",
        "false",
        "denied",
        "unverified",
    }
)


def _payload_values(payload: dict[str, Any], key: str) -> tuple[str, ...]:
    value = payload.get(key)
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple, set)):
        return tuple(str(v) for v in value if v is not None)
    return (str(value),)


def _subject_matches(payload: dict[str, Any], *, subject_key: str) -> tuple[bool, str | None]:
    """Exact subject binding only; substring matches are evidence inflation."""
    candidates = (
        _payload_values(payload, "capability_key")
        + _payload_values(payload, "subject")
        + _payload_values(payload, "subject_key")
        + _payload_values(payload, "capability")
    )
    if not candidates:
        return False, "missing_subject_key"
    if subject_key in candidates:
        return True, None
    return False, "subject_key_mismatch"


def _payload_positive(payload: dict[str, Any] | None) -> tuple[bool, str | None]:
    if not isinstance(payload, dict):
        return False, "payload_not_object"
    # Explicit failures
    for key in ("passed", "success", "verified", "ok"):
        if key in payload and payload[key] is False:
            return False, f"payload_{key}_false"
    status = str(payload.get("status") or payload.get("result") or "").lower()
    if status in _FAIL_MARKERS:
        return False, f"payload_status_{status}"
    # Require at least one positive signal for verification-like evidence
    positives = []
    for key in ("passed", "success", "verified", "ok"):
        if payload.get(key) is True:
            positives.append(key)
    if payload.get("outcome") in ("passed", "success", "verified"):
        positives.append("outcome")
    if not positives and "passed" not in payload and "success" not in payload:
        # Presence-only payload without outcome → not supporting
        return False, "no_positive_outcome_in_payload"
    if not positives:
        return False, "no_positive_outcome_in_payload"
    return True, None


def evidence_supports_claim(
    db: Session,
    *,
    owner_id: UUID,
    subject_key: str,
    proposition: str,
    evidence_id: UUID | None,
    allowed_kinds: set[str] | None = None,
    require_deterministic: bool = False,
    max_age: timedelta | None = None,
    now: datetime | None = None,
    evidence_row: IntelligenceEvidence | None = None,
) -> EvidenceSupportResult:
    """Return whether evidence SUPPORTS the proposition for this owner/subject.

    Hard rules:
    EVIDENCE EXISTS != CLAIM PROVEN
    EVIDENCE OWNER MATCH != CLAIM PROVEN
    TEST RAN != TEST PASSED
    OLD SUCCESS + NEW FAILURE != CURRENT VERIFIED SUCCESS (caller must pass latest evidence)
    """
    reasons: list[str] = []
    if evidence_id is None and evidence_row is None:
        return EvidenceSupportResult(False, ("missing_evidence_id",))

    row = evidence_row
    if row is None:
        row = db.execute(
            select(IntelligenceEvidence).where(
                IntelligenceEvidence.id == evidence_id,
                IntelligenceEvidence.owner_id == owner_id,
            )
        ).scalar_one_or_none()
    if row is None:
        return EvidenceSupportResult(
            False, ("evidence_not_found_or_wrong_owner",), str(evidence_id) if evidence_id else None
        )

    if row.owner_id != owner_id:
        return EvidenceSupportResult(False, ("owner_mismatch",), str(row.id))

    if allowed_kinds and row.evidence_kind not in allowed_kinds:
        reasons.append("evidence_kind_not_allowed")

    if require_deterministic and not bool(row.deterministic):
        reasons.append("not_deterministic")

    # Subject/proposition relevance via explicit payload fields only.
    # `foo in bar` accepted send_email_v2 as send_email and parent capabilities as
    # child capabilities; source_ref-only passed=True was also a bare bypass.
    payload = row.payload if isinstance(row.payload, dict) else {}
    subject_ok, subject_reason = _subject_matches(payload, subject_key=subject_key)
    if not subject_ok:
        reasons.append(subject_reason or "subject_or_proposition_not_tied_to_evidence")

    evidence_prop = payload.get("proposition")
    if evidence_prop is not None and str(evidence_prop) != proposition:
        reasons.append("proposition_mismatch")

    positive, fail_reason = _payload_positive(payload)
    if not positive:
        reasons.append(fail_reason or "not_positive")

    if max_age is not None:
        now = now or datetime.utcnow()
        created = getattr(row, "created_at", None)
        if created is not None and (now - created) > max_age:
            reasons.append("stale_evidence")

    # Invalidated/superseded markers in payload
    if payload.get("invalidated") is True or payload.get("superseded") is True:
        reasons.append("evidence_invalidated_or_superseded")
    if payload.get("rejected") is True:
        reasons.append("evidence_rejected")

    if reasons:
        return EvidenceSupportResult(False, tuple(reasons), str(row.id))
    return EvidenceSupportResult(True, ("supports",), str(row.id))


def require_supporting_evidence_for_verified(
    db: Session,
    *,
    owner_id: UUID,
    capability_key: str,
    verification_evidence_id: UUID | None,
    success: bool | None,
) -> EvidenceSupportResult:
    """Gate for status=verified_available."""
    if success is False:
        return EvidenceSupportResult(False, ("success_false_cannot_verify",))
    return evidence_supports_claim(
        db,
        owner_id=owner_id,
        subject_key=capability_key,
        proposition="verified_available",
        evidence_id=verification_evidence_id,
        allowed_kinds={"test_run_result", "verification_result", "deterministic_check", "exam_result", "capability_probe"},
    )
