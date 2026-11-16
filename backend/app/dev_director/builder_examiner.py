"""Builder/Examiner separation (Milestone 4) -- the core new mechanism this whole package
exists to add. Generalizes app.workforce.verification's PROVEN collusion-detection pattern
(verifier identity != builder identity, and if two verifiers are required, they must not
equal each other either) to cross-provider identities.

BUILDER != FINAL EXAMINER.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from app.dev_director.types import (
    BuilderAssignment,
    BuilderExaminerCollusionError,
    BuilderResult,
    ExaminerAssignment,
    ExaminerVerdict,
    ExaminerVerdictError,
    ExaminerVerdictRecord,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_builder_assignment(
    *, job_id: uuid.UUID, builder_identity: str, workforce_assignment_ref: uuid.UUID | None = None, external_lease_ref: uuid.UUID | None = None,
) -> BuilderAssignment:
    """Real, enforced XOR: references EITHER a real WorkforceAssignment (local) OR an
    ExternalProviderLease (external) -- never both, never neither."""
    if (workforce_assignment_ref is None) == (external_lease_ref is None):
        raise ValueError("exactly one of workforce_assignment_ref or external_lease_ref must be provided (XOR)")
    return BuilderAssignment(
        assignment_id=uuid.uuid4(), job_id=job_id, builder_identity=builder_identity,
        workforce_assignment_ref=workforce_assignment_ref, external_lease_ref=external_lease_ref,
    )


def submit_builder_result(assignment: BuilderAssignment, *, result_sha: str, branch: str, claimed_completion: bool) -> BuilderResult:
    return BuilderResult(assignment_id=assignment.assignment_id, result_sha=result_sha, branch=branch, builder_identity=assignment.builder_identity, claimed_completion=claimed_completion)


def new_examiner_assignment(
    *, job_id: uuid.UUID, examiner_identity: str, builder_assignment: BuilderAssignment, target_sha: str,
    scope: str = "", known_risks: tuple[str, ...] = (), test_expectations: tuple[str, ...] = (),
    workforce_assignment_ref: uuid.UUID | None = None, external_lease_ref: uuid.UUID | None = None,
) -> ExaminerAssignment:
    """BUILDER != FINAL EXAMINER, structurally enforced here -- the earliest possible point.
    Rejects: same provider/agent identity as the builder (self-certification); the same
    underlying execution lease/assignment reused for both roles (checked by comparing the
    examiner's own eventual lease/assignment reference at verdict-recording time too, see
    record_examiner_verdict() -- this constructor alone cannot see that yet, but it DOES
    reject identity collision here, the same real check
    app.workforce.verification.apply_verification_decision() performs first)."""
    if examiner_identity == builder_assignment.builder_identity:
        raise BuilderExaminerCollusionError(
            f"examiner identity {examiner_identity!r} is the same as the builder's own identity -- BUILDER_CANNOT_SELF_VERIFY"
        )
    if target_sha != target_sha.strip() or not target_sha:
        raise ValueError("target_sha must be a real, exact, non-empty pinned SHA -- never resolved from a mutable branch ref")
    if workforce_assignment_ref is not None and workforce_assignment_ref == builder_assignment.workforce_assignment_ref:
        raise BuilderExaminerCollusionError("examiner cannot reuse the builder's own WorkforceAssignment as its own execution lease")
    if external_lease_ref is not None and external_lease_ref == builder_assignment.external_lease_ref:
        raise BuilderExaminerCollusionError("examiner cannot reuse the builder's own ExternalProviderLease as its own execution lease")
    return ExaminerAssignment(
        examiner_assignment_id=uuid.uuid4(), job_id=job_id, examiner_identity=examiner_identity,
        target_sha=target_sha, scope=scope, known_risks=known_risks, test_expectations=test_expectations,
        workforce_assignment_ref=workforce_assignment_ref, external_lease_ref=external_lease_ref,
    )


def record_examiner_verdict(
    examiner_assignment: ExaminerAssignment, *, builder_assignment: BuilderAssignment, builder_result: BuilderResult,
    verdict: ExaminerVerdict, evidence: tuple[str, ...] = (), reason: str = "",
) -> ExaminerVerdictRecord:
    """Second, defense-in-depth collusion check (mirrors apply_verification_decision()'s own
    two-place check) -- re-verifies at record time, not just at assignment-construction time,
    in case the two were constructed independently. Also enforces the exact-SHA discipline:
    the examiner's own target_sha must match the builder's actual result_sha -- examining
    "whatever the branch currently points to" instead of the frozen SHA is rejected. A PASS
    verdict REQUIRES non-empty evidence."""
    if examiner_assignment.examiner_identity == builder_assignment.builder_identity:
        raise BuilderExaminerCollusionError("examiner identity equals builder identity at verdict-recording time")
    if examiner_assignment.job_id != builder_assignment.job_id:
        raise ValueError("examiner assignment and builder assignment must reference the same job")
    # Defense in depth: re-check the same-execution-lease collision at record time too, not
    # just at ExaminerAssignment construction (in case the two were built independently and
    # only compared here).
    if examiner_assignment.workforce_assignment_ref is not None and examiner_assignment.workforce_assignment_ref == builder_assignment.workforce_assignment_ref:
        raise BuilderExaminerCollusionError("examiner's own execution lease is the same WorkforceAssignment the builder used")
    if examiner_assignment.external_lease_ref is not None and examiner_assignment.external_lease_ref == builder_assignment.external_lease_ref:
        raise BuilderExaminerCollusionError("examiner's own execution lease is the same ExternalProviderLease the builder used")
    if examiner_assignment.target_sha != builder_result.result_sha:
        raise ExaminerVerdictError(
            f"examiner target_sha {examiner_assignment.target_sha!r} does not match the builder's actual result_sha "
            f"{builder_result.result_sha!r} -- an examiner must attack the exact frozen SHA, never a re-resolved branch ref"
        )
    if verdict == ExaminerVerdict.PASS and not evidence:
        raise ExaminerVerdictError("a PASS verdict requires non-empty evidence")
    return ExaminerVerdictRecord(
        examiner_assignment_id=examiner_assignment.examiner_assignment_id, examiner_identity=examiner_assignment.examiner_identity,
        target_sha=examiner_assignment.target_sha, verdict=verdict, evidence=evidence, reason=reason,
    )
