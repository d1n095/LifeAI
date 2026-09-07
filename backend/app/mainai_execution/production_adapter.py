"""Disabled-by-default adapter from the Director contract to canonical ``mainai_jobs``.

This module is intentionally a narrow seam: PostgreSQL ``mainai_jobs`` and its existing lease
service remain authoritative. It does not invoke providers, create routes, or change autonomy
policy. A scheduler may claim work only when explicitly enabled and at autonomy level <= 2.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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


@dataclass(frozen=True)
class ProviderProfile:
    name: str
    capabilities: frozenset[str]
    state: str = "available"
    authorized: bool = True
    disclosure_scope: str = "metadata"


@dataclass(frozen=True)
class ReviewRecord:
    job_id: str
    sha: str
    examiner_id: str
    passed: bool
    reviewed_at: str


class RuntimeOrchestrator:
    """Deterministic, provider-agnostic runtime path used by the Director seam tests.

    Provider profiles are capability and availability observations only.  The substrate
    claim/lease remains the authority and every reassignment creates a fresh attempt.
    """

    def __init__(self, substrate):
        from app.mainai_execution.substrate import DirectorContract
        self.substrate = substrate
        self.director = DirectorContract(substrate)
        self.providers: dict[str, ProviderProfile] = {}
        self.attempts: dict[str, tuple[object, str]] = {}
        self.frozen: dict[str, ArtifactFreeze] = {}
        self.reviews: dict[str, ReviewRecord] = {}
        self.certified: dict[str, str] = {}

    def register_provider(self, profile: ProviderProfile) -> None:
        self.providers[profile.name] = profile

    def submit(self, *, owner_id: str, program: str, required: set[str] | None = None, provider: str | None = None, base_sha: str | None = None, worktree: str | None = None):
        return self.director.submit_job(owner_id=owner_id, program=program, provider=provider, base_sha=base_sha, worktree=worktree, capabilities=tuple(sorted(required or set())), max_active=100)

    def _provider(self, required: set[str], *, examiner: bool = False, disclosure_scope: str = "metadata") -> ProviderProfile:
        for profile in self.providers.values():
            if profile.state != "available" or not profile.authorized:
                continue
            if not required.issubset(profile.capabilities):
                continue
            if examiner and "review" not in profile.capabilities:
                continue
            if disclosure_scope == "full" and profile.disclosure_scope != "full":
                continue
            return profile
        raise ProductionRuntimeError("no compatible authorized provider")

    def claim(self, job_id: str, *, owner_id: str, worker_id: str, required: set[str] | None = None):
        profile = self._provider(required or set())
        claim = self.director.claim_job(job_id, owner_id=owner_id, worker_id=worker_id)
        self.attempts[job_id] = (claim, profile.name)
        return claim, profile

    def failover(self, job_id: str, *, owner_id: str, required: set[str], worker_id: str):
        previous = self.attempts.get(job_id)
        if previous is None:
            raise ProductionRuntimeError("job has no current attempt")
        profile = self._provider(required)
        self.substrate.abandon_stale(now=datetime.now(timezone.utc) + timedelta(seconds=61))
        self.substrate.retry_or_reassign(job_id, new_provider=profile.name)
        claim = self.director.claim_job(job_id, owner_id=owner_id, worker_id=worker_id)
        self.attempts[job_id] = (claim, profile.name)
        return claim, profile, previous[0]

    def freeze(self, *, job_id: str, attempt_id: str, builder_id: str, examiner_id: str, worktree: str, base_sha: str) -> ArtifactFreeze:
        artifact = freeze_artifact(job_id=job_id, attempt_id=attempt_id, builder_id=builder_id, examiner_id=examiner_id, worktree=worktree, base_sha=base_sha)
        self.frozen[job_id] = artifact
        return artifact

    def examine(self, *, job_id: str, examiner_id: str, sha: str, passed: bool) -> ReviewRecord:
        artifact = self.frozen.get(job_id)
        if artifact is None or artifact.sha != sha:
            raise ProductionRuntimeError("review SHA is not the frozen artifact")
        if examiner_id == artifact.builder_id:
            raise ProductionRuntimeError("builder cannot examine its own artifact")
        profile = self.providers.get(examiner_id)
        if profile is None or "review" not in profile.capabilities or not profile.authorized or profile.state != "available":
            raise ProductionRuntimeError("examiner is not authorized")
        record = ReviewRecord(job_id, sha, examiner_id, passed, datetime.now(timezone.utc).isoformat())
        self.reviews[job_id] = record
        if passed:
            self.certified[job_id] = sha
        return record


@dataclass(frozen=True)
class ArtifactFreeze:
    job_id: str
    attempt_id: str
    repository: str
    branch: str
    sha: str
    base_sha: str
    builder_id: str
    examiner_id: str | None
    frozen_at: str


@dataclass(frozen=True)
class TestEvidence:
    command_fingerprint: str
    exit_code: int
    started_at: str
    finished_at: str
    repo_sha: str
    clean: bool
    suite: str
    summary: str


def quarantine_provider_output(text: str, *, limit: int = 4000) -> dict[str, object]:
    """Parse provider prose as bounded evidence only; control instructions are never actions."""
    safe = (text or "")[:limit]
    return {"reported_text": safe, "control_actions": (), "authority": "none"}


def freeze_artifact(*, job_id: str, attempt_id: str, builder_id: str, examiner_id: str | None, worktree: str, base_sha: str, protected_refs=()) -> ArtifactFreeze:
    state = inspect_worktree(worktree, expected_sha=None, protected_refs=protected_refs)
    if state["sha"] == base_sha or not state["clean"]:
        raise ProductionRuntimeError("artifact is not a clean changed commit")
    if examiner_id is not None and examiner_id == builder_id:
        raise ProductionRuntimeError("builder cannot examine its own artifact")
    return ArtifactFreeze(job_id, attempt_id, str(worktree), str(state["branch"]), str(state["sha"]), base_sha, builder_id, examiner_id, datetime.now(timezone.utc).isoformat())


class DependencyEngine:
    """Small exact-SHA dependency gate; higher-level Director owns policy and priorities."""

    @staticmethod
    def validate(graph: dict[str, tuple[str, ...]]) -> None:
        visiting: set[str] = set()
        visited: set[str] = set()
        def visit(node: str):
            if node in visiting:
                raise ProductionRuntimeError("dependency cycle")
            if node in visited:
                return
            visiting.add(node)
            for dep in graph.get(node, ()):
                visit(dep)
            visiting.remove(node)
            visited.add(node)
        for node in graph:
            visit(node)

    @staticmethod
    def satisfied(*, required_sha: str, observed_sha: str, certified: bool) -> bool:
        return certified and required_sha == observed_sha


def offline_policy_allows(action: str, *, autonomy_level: int) -> bool:
    if action in {"merge", "deploy", "secret_change", "authority_expand", "publish"}:
        return False
    return 0 <= autonomy_level <= 2


def provider_can_dispatch(*, provider_state: str, authorized: bool, capabilities: set[str], required: set[str]) -> bool:
    """Capability and authorization are independent gates for scheduler decisions."""
    if provider_state not in {"available"} or not authorized:
        return False
    return required.issubset(capabilities)


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
