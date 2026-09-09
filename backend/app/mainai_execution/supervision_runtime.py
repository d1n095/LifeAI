"""Canonical PostgreSQL supervision adapter and fail-closed evaluation helpers."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.mainai_job import MainAIJob, MainAIJobStatus
from app.models.mainai_supervision import MainAIBudgetReservation, MainAISupervisionAgent, MainAISupervisionMessage


class CompletionDecision(StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL_CONTINUE = "PARTIAL_CONTINUE"
    VERIFY_REQUIRED = "VERIFY_REQUIRED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"
    SUPERSEDED = "SUPERSEDED"


@dataclass(frozen=True)
class CompletionFacts:
    scope_complete: bool
    p0_remaining: int
    tests_passed: bool
    exact_sha: bool
    clean_worktree: bool
    artifact_frozen: bool
    attempt_current: bool
    lease_current: bool
    dependency_ready: bool
    examiner_required: bool = False
    examiner_passed: bool = False
    cancelled: bool = False
    superseded: bool = False


def evaluate_completion(facts: CompletionFacts) -> CompletionDecision:
    if facts.cancelled:
        return CompletionDecision.CANCELLED
    if facts.superseded:
        return CompletionDecision.SUPERSEDED
    if not facts.attempt_current or not facts.lease_current:
        return CompletionDecision.FAILED
    if not facts.tests_passed or not facts.exact_sha or not facts.clean_worktree:
        return CompletionDecision.FAILED
    if not facts.dependency_ready:
        return CompletionDecision.BLOCKED
    if facts.examiner_required and not facts.examiner_passed:
        return CompletionDecision.VERIFY_REQUIRED
    if facts.p0_remaining or not facts.scope_complete:
        return CompletionDecision.PARTIAL_CONTINUE
    if not facts.artifact_frozen:
        return CompletionDecision.VERIFY_REQUIRED
    return CompletionDecision.COMPLETE


class Liveness(StrEnum):
    HEALTHY = "HEALTHY"
    LONG_RUNNING = "LONG_RUNNING"
    IDLE = "IDLE"
    HUNG = "HUNG"
    DISCONNECTED = "DISCONNECTED"


def classify_liveness(*, heartbeat_at: datetime, progress_changed: bool, process_alive: bool, now: datetime | None = None, idle_after: timedelta = timedelta(minutes=5), hung_after: timedelta = timedelta(minutes=30)) -> Liveness:
    now = now or datetime.now(timezone.utc)
    age = now - heartbeat_at
    if not process_alive or age > hung_after:
        return Liveness.DISCONNECTED if not process_alive else Liveness.HUNG
    if age > idle_after and not progress_changed:
        return Liveness.IDLE
    return Liveness.HEALTHY if progress_changed else Liveness.LONG_RUNNING


@dataclass(frozen=True)
class BlockerEvidence:
    local_tools_available: bool | None = None
    provider_alternative_available: bool | None = None
    worktree_safe: bool | None = None
    authority_current: bool | None = None
    external_available: bool | None = None
    founder_only: bool = False


def validate_blocker(reason: str, evidence: BlockerEvidence) -> tuple[bool, str]:
    if evidence.founder_only:
        return True, "founder-only authority is required"
    if evidence.authority_current is False:
        return True, "authority is absent or expired"
    if evidence.local_tools_available is False or evidence.worktree_safe is False:
        return False, "local repair is required before escalation"
    if evidence.provider_alternative_available:
        return False, "authorized alternative provider exists"
    if evidence.external_available is False:
        return True, "external dependency is unavailable"
    if any(v is None for v in (evidence.local_tools_available, evidence.external_available)):
        return False, "blocker evidence is incomplete; inspect before escalation"
    return False, reason[:256]


class PostgresSupervisionStore:
    """Durable supervision observations/messages; MainAIJob remains authority."""

    def __init__(self, session: Session):
        self.session = session

    def observe(self, *, agent_id: str, owner_id: uuid.UUID, state: str, process_nonce: str, heartbeat_at: datetime, job_id: uuid.UUID | None = None, attempt_id: str | None = None, provider: str | None = None, progress_key: str | None = None, pid: int | None = None) -> None:
        row = self.session.get(MainAISupervisionAgent, agent_id)
        if row is None:
            row = MainAISupervisionAgent(agent_id=agent_id, owner_id=owner_id, state=state, process_nonce=process_nonce, heartbeat_at=heartbeat_at, updated_at=heartbeat_at, job_id=job_id, attempt_id=attempt_id, provider=provider, progress_key=progress_key, pid=pid)
            self.session.add(row)
        else:
            if row.owner_id != owner_id:
                raise PermissionError("agent owner mismatch")
            row.state, row.process_nonce, row.heartbeat_at, row.updated_at = state, process_nonce, heartbeat_at, heartbeat_at
            row.job_id, row.attempt_id, row.provider, row.progress_key, row.pid = job_id, attempt_id, provider, progress_key, pid
        self.session.flush()

    def _canonical_job(self, owner_id: uuid.UUID, job_id: uuid.UUID) -> MainAIJob:
        job = self.session.scalar(select(MainAIJob).where(MainAIJob.id == job_id, MainAIJob.owner_id == owner_id).with_for_update())
        if job is None:
            raise LookupError("job is not owned by caller")
        if job.status in {MainAIJobStatus.cancelled, MainAIJobStatus.completed, MainAIJobStatus.failed, MainAIJobStatus.superseded}:
            raise RuntimeError("job is not current")
        return job

    def continuation(self, *, owner_id: uuid.UUID, job_id: uuid.UUID, kind: str, reason: str, sequence: int, attempt_id: str | None = None) -> MainAISupervisionMessage:
        if sequence < 1 or not reason or len(reason) > 1000:
            raise ValueError("bounded continuation metadata required")
        job = self._canonical_job(owner_id, job_id)
        key = uuid.uuid5(uuid.NAMESPACE_URL, f"{owner_id}:{job.id}:{kind}:{sequence}")
        existing = self.session.get(MainAISupervisionMessage, key)
        if existing:
            return existing
        row = MainAISupervisionMessage(id=key, owner_id=owner_id, job_id=job.id, attempt_id=attempt_id, kind=kind, reason=reason, sequence=sequence, created_at=datetime.now(timezone.utc))
        self.session.add(row)
        try:
            self.session.flush()
        except IntegrityError:
            self.session.rollback()
            return self.session.scalar(select(MainAISupervisionMessage).where(MainAISupervisionMessage.owner_id == owner_id, MainAISupervisionMessage.job_id == job.id, MainAISupervisionMessage.kind == kind, MainAISupervisionMessage.sequence == sequence))
        return row

    def eligible_jobs(self, owner_id: uuid.UUID, limit: int = 1) -> list[MainAIJob]:
        return list(self.session.scalars(select(MainAIJob).where(MainAIJob.owner_id == owner_id, MainAIJob.status == MainAIJobStatus.queued).order_by(MainAIJob.created_at, MainAIJob.id).limit(limit)))

    def expire_reservations(self, now: datetime | None = None) -> int:
        now = now or datetime.now(timezone.utc)
        result = self.session.execute(update(MainAIBudgetReservation).where(MainAIBudgetReservation.state.in_(("reserved", "bound")), MainAIBudgetReservation.expires_at <= now).values(state="expired"))
        return int(result.rowcount or 0)

    def supervise_idle(self, *, owner_id: uuid.UUID, agent_id: str, reason: str = "continue authorized work") -> list[MainAISupervisionMessage]:
        """Make one fenced decision from canonical PostgreSQL state."""
        agent = self.session.scalar(select(MainAISupervisionAgent).where(MainAISupervisionAgent.agent_id == agent_id, MainAISupervisionAgent.owner_id == owner_id).with_for_update())
        if agent is None or agent.state != "IDLE":
            return []
        if agent.job_id is not None:
            return [self.continuation_for_agent(owner_id=owner_id, agent_id=agent_id, kind="CONTINUE_SAME_JOB", reason=reason, sequence=1)]
        ready = self.eligible_jobs(owner_id, limit=1)
        if not ready:
            return []
        return [self.continuation(owner_id=owner_id, job_id=ready[0].id, kind="START_NEXT_READY_JOB", reason="idle agent has canonical READY work", sequence=1)]

    def continuation_for_agent(self, *, owner_id: uuid.UUID, agent_id: str, kind: str, reason: str, sequence: int) -> MainAISupervisionMessage:
        agent = self.session.scalar(select(MainAISupervisionAgent).where(MainAISupervisionAgent.agent_id == agent_id, MainAISupervisionAgent.owner_id == owner_id).with_for_update())
        if agent is None or agent.job_id is None:
            raise LookupError("agent has no current canonical job")
        return self.continuation(owner_id=owner_id, job_id=agent.job_id, kind=kind, reason=reason, sequence=sequence, attempt_id=agent.attempt_id)

    def deliver(self, sender, *, owner_id: uuid.UUID, limit: int = 100, max_attempts: int = 5) -> tuple[int, int, int]:
        if not 1 <= limit <= 1000 or not 1 <= max_attempts <= 20:
            raise ValueError("invalid delivery bounds")
        now = datetime.now(timezone.utc)
        rows = list(self.session.scalars(select(MainAISupervisionMessage).where(MainAISupervisionMessage.owner_id == owner_id, MainAISupervisionMessage.state.in_(("pending", "retrying")), (MainAISupervisionMessage.next_attempt_at.is_(None)) | (MainAISupervisionMessage.next_attempt_at <= now)).order_by(MainAISupervisionMessage.created_at).limit(limit).with_for_update(skip_locked=True)))
        delivered = dead = retrying = 0
        for row in rows:
            row.attempts += 1
            try:
                sender({"message_id": str(row.id), "owner_id": str(row.owner_id), "job_id": str(row.job_id), "kind": row.kind, "attempt_id": row.attempt_id, "reason": row.reason, "sequence": row.sequence})
            except Exception:
                if row.attempts >= max_attempts:
                    row.state, row.blocked_reason = "dead_letter", "delivery retry budget exhausted"
                    dead += 1
                else:
                    row.state = "retrying"
                    row.next_attempt_at = now + timedelta(seconds=min(3600, 2 ** row.attempts))
                    retrying += 1
            else:
                row.state, row.delivered_at = "delivered", now
                delivered += 1
        self.session.flush()
        return delivered, dead, retrying


class CostClassification(StrEnum):
    VERIFIED = "VERIFIED"
    BOUNDED_ESTIMATE = "BOUNDED_ESTIMATE"
    UNCERTAIN = "UNCERTAIN"
    INVALID = "INVALID"


def classify_cost(*, reserved: float, reported: float | None, prompt_tokens: int | None = None, completion_tokens: int | None = None, unit_price: float | None = None) -> CostClassification:
    if reserved < 0 or reported is not None and reported < 0:
        return CostClassification.INVALID
    if reported is None:
        return CostClassification.UNCERTAIN
    if reported > reserved + 1e-9:
        return CostClassification.INVALID
    if prompt_tokens is not None and completion_tokens is not None and unit_price is not None:
        estimated = (prompt_tokens + completion_tokens) * unit_price
        if estimated > reserved + 1e-9:
            return CostClassification.INVALID
        return CostClassification.VERIFIED if abs(reported - estimated) <= max(0.01, reserved * 0.05) else CostClassification.BOUNDED_ESTIMATE
    return CostClassification.BOUNDED_ESTIMATE
