"""Disabled-by-default adapter from the Director contract to canonical ``mainai_jobs``.

This module is intentionally a narrow seam: PostgreSQL ``mainai_jobs`` and its existing lease
service remain authoritative. It does not invoke providers, create routes, or change autonomy
policy. A scheduler may claim work only when explicitly enabled and at autonomy level <= 2.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import uuid

from sqlalchemy.orm import Session

from app.jobs.mainai_job_lease import JobLeaseLostError, claim_next_mainai_job, renew_mainai_job_lease
from app.jobs.service import mark_completed, mark_failed, record_claimed
from app.mainai_execution.substrate import CompletionEnvelope, FailureClass, inspect_worktree
from app.models.mainai_job import MainAIJob, MainAIJobErrorCategory, MainAIJobStatus


PROTECTED_REFS = frozenset({"#245", "818dfb732da47901eb5ae06ffdd9c829fe00c4c5", "main", "master"})


class ProductionRuntimeError(RuntimeError):
    pass


class AutonomyLevel(StrEnum):
    MANUAL = "0"
    ISOLATED = "1"
    EXAMINED = "2"
    MERGE = "3"
    DEPLOY = "4"


@dataclass(frozen=True)
class CanonicalJobMap:
    job_id: uuid.UUID
    owner_id: uuid.UUID
    program: str
    status: str
    attempt_worker: str | None
    lease_generation: int
    provider: str | None
    worktree: str | None
    branch: str | None
    base_sha: str | None
    result_sha: str | None


@dataclass(frozen=True)
class ProductionClaim:
    job_id: uuid.UUID
    owner_id: uuid.UUID
    worker_id: str
    lease_generation: int
    attempt_id: str


def canonical_job_map(job: MainAIJob, *, attempt_id: str | None = None) -> CanonicalJobMap:
    """Map the canonical row without creating a second lifecycle truth."""
    output = job.output_refs or []
    result_sha = next((str(ref.get("sha")) for ref in output if isinstance(ref, dict) and ref.get("sha")), None)
    return CanonicalJobMap(job.id, job.owner_id, job.job_type, job.status.value, job.locked_by, int(job.lease_generation), job.provider, None, None, None, result_sha)


class ProductionExecutionAdapter:
    def __init__(self, db: Session):
        self.db = db

    def claim_next(self, *, worker_id: str, lease_seconds: int = 60) -> ProductionClaim | None:
        claimed = claim_next_mainai_job(self.db, worker_id, lease_seconds)
        if claimed is None:
            return None
        job_id, owner_id, generation = claimed
        job = self.db.get(MainAIJob, job_id, populate_existing=True)
        if job is None or job.status is not MainAIJobStatus.running or job.locked_by != worker_id or job.lease_generation != generation:
            raise ProductionRuntimeError("canonical claim could not be revalidated")
        attempt_id = f"{worker_id}:{generation}:{uuid.uuid4()}"
        record_claimed(self.db, job, worker_id=worker_id, lease_generation=generation)
        return ProductionClaim(job_id, owner_id, worker_id, generation, attempt_id)

    def heartbeat(self, claim: ProductionClaim, *, lease_seconds: int = 60) -> None:
        renew_mainai_job_lease(self.db, claim.job_id, claim.worker_id, claim.lease_generation, lease_seconds)
        self.db.commit()

    def complete(self, claim: ProductionClaim, envelope: CompletionEnvelope) -> None:
        if envelope.evidence.job_id != str(claim.job_id) or envelope.evidence.attempt_id != claim.attempt_id:
            raise ProductionRuntimeError("completion attempt does not match canonical claim")
        if envelope.reported_sha != envelope.evidence.observed_sha or not envelope.evidence.changed or not envelope.evidence.clean:
            raise ProductionRuntimeError("completion evidence is not grounded in repository state")
        job = self._job(claim)
        mark_completed(self.db, job, worker_id=claim.worker_id, lease_generation=claim.lease_generation, public_message="Execution evidence accepted.")

    def fail(self, claim: ProductionClaim, *, failure_class: FailureClass) -> None:
        category = {
            FailureClass.TRANSIENT: MainAIJobErrorCategory.transient_io,
            FailureClass.PROVIDER_LIMIT: MainAIJobErrorCategory.capability_unavailable,
            FailureClass.AUTHORITY_REVOKED: MainAIJobErrorCategory.cancelled_by_owner,
            FailureClass.PROCESS_LOST: MainAIJobErrorCategory.timeout,
        }.get(failure_class, MainAIJobErrorCategory.permanent)
        mark_failed(self.db, self._job(claim), worker_id=claim.worker_id, lease_generation=claim.lease_generation, error_category=category)

    def _job(self, claim: ProductionClaim) -> MainAIJob:
        job = self.db.get(MainAIJob, claim.job_id, populate_existing=True)
        if job is None or job.owner_id != claim.owner_id or job.locked_by != claim.worker_id or job.lease_generation != claim.lease_generation or job.status is not MainAIJobStatus.running:
            raise JobLeaseLostError(claim.job_id, claim.worker_id, claim.lease_generation)
        return job


class SafeScheduler:
    """Opt-in scheduler; scheduling never changes authority, autonomy or protected refs."""

    def __init__(self, adapter: ProductionExecutionAdapter, *, enabled: bool = False, autonomy_level: AutonomyLevel = AutonomyLevel.ISOLATED):
        self.adapter = adapter
        self.enabled = enabled
        self.autonomy_level = autonomy_level

    def tick(self, *, worker_id: str, lease_seconds: int = 60) -> ProductionClaim | None:
        if not self.enabled:
            raise ProductionRuntimeError("scheduler is disabled")
        if self.autonomy_level in (AutonomyLevel.MERGE, AutonomyLevel.DEPLOY):
            raise ProductionRuntimeError("scheduler cannot grant merge or deploy autonomy")
        return self.adapter.claim_next(worker_id=worker_id, lease_seconds=lease_seconds)


def validate_protected_ref(branch: str | None, sha: str | None) -> None:
    if branch in PROTECTED_REFS or sha in PROTECTED_REFS:
        raise ProductionRuntimeError("protected ref is read-only")


def validate_worktree_for_claim(path: str, *, expected_sha: str | None, branch: str | None) -> dict[str, object]:
    validate_protected_ref(branch, expected_sha)
    return inspect_worktree(path, expected_sha=expected_sha, protected_refs=PROTECTED_REFS)
