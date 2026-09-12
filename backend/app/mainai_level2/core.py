"""Bounded Level-2 control-plane composition.

This module is deliberately an adapter layer.  The verified runtime remains the
authority for claims, leases and effects; this coordinator owns only program
contracts, observations, review routing and durable recovery evidence.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
import hashlib
import json
from typing import Any, Protocol


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobState(StrEnum):
    READY = "READY"
    ASSIGNED = "ASSIGNED"
    RUNNING = "RUNNING"
    PROGRESSING = "PROGRESSING"
    WAITING = "WAITING"
    IDLE_UNFINISHED = "IDLE_UNFINISHED"
    PARTIAL = "PARTIAL"
    AWAITING_REVIEW = "AWAITING_REVIEW"
    BLOCKED = "BLOCKED"
    STALLED = "STALLED"
    FAILED = "FAILED"
    FIX_REQUIRED = "FIX_REQUIRED"
    VERIFIED = "VERIFIED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    SUPERSEDED = "SUPERSEDED"
    DEAD_LETTER = "DEAD_LETTER"


class ProviderState(StrEnum):
    AVAILABLE = "AVAILABLE"
    EXHAUSTED = "EXHAUSTED"
    UNAVAILABLE = "UNAVAILABLE"
    QUARANTINED = "QUARANTINED"


class BlockerClass(StrEnum):
    NONE = "NONE"
    FOUNDER_REQUIRED = "FOUNDER_REQUIRED"
    EXTERNAL_DEPENDENCY = "EXTERNAL_DEPENDENCY"
    PROVIDER_EXHAUSTED = "PROVIDER_EXHAUSTED"
    LOCAL_REPAIR = "LOCAL_REPAIR"
    INCOMPLETE_WORK = "INCOMPLETE_WORK"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ProgramContract:
    program_id: str
    owner_id: str
    objective: str
    acceptance: tuple[str, ...]
    verification: tuple[str, ...]
    authority_boundary: tuple[str, ...] = ("no_merge", "no_deploy", "bounded_spend")
    founder_only: tuple[str, ...] = ()
    budget_usd: float | None = None
    stop_conditions: tuple[str, ...] = ()


@dataclass
class Job:
    job_id: str
    program_id: str
    owner_id: str
    title: str
    state: JobState = JobState.READY
    dependencies: tuple[str, ...] = ()
    priority: int = 0
    builder_id: str | None = None
    examiner_id: str | None = None
    attempt: int = 0
    sha: str | None = None
    base_sha: str | None = None
    review_sha: str | None = None
    review_passed: bool | None = None
    remaining: tuple[str, ...] = ()
    blocker: BlockerClass = BlockerClass.NONE
    updated_at: str = field(default_factory=_now)


@dataclass(frozen=True)
class Provider:
    provider_id: str
    capabilities: frozenset[str]
    state: ProviderState = ProviderState.AVAILABLE
    authorized: bool = True


@dataclass(frozen=True)
class ResourceRecommendation:
    action: str
    job_id: str
    rationale: str
    authorized: bool = False


class RuntimePort(Protocol):
    """Canonical runtime seam. Implementations own claims, leases and effects."""

    def assign(self, job: Job, agent_id: str) -> str: ...

    def cancel(self, job: Job) -> None: ...

    def release(self, job: Job) -> None: ...


class Journal:
    """Append-only, replayable evidence; never an authority source by itself."""

    def __init__(self, records: list[dict[str, Any]] | None = None):
        self.records = list(records or [])

    def append(self, event: str, **data: Any) -> None:
        safe = {k: v for k, v in data.items() if k not in {"prompt", "secret", "source"}}
        self.records.append({"seq": len(self.records) + 1, "event": event, "at": _now(), **safe})

    def snapshot(self) -> list[dict[str, Any]]:
        return json.loads(json.dumps(self.records))

    @classmethod
    def replay(cls, records: list[dict[str, Any]]) -> "Journal":
        return cls(records)


class Level2ControlPlane:
    """Deterministic unattended controller with explicit authority boundaries."""

    def __init__(self, runtime: RuntimePort, *, journal: Journal | None = None):
        self.runtime = runtime
        self.journal = journal or Journal()
        self.programs: dict[str, ProgramContract] = {}
        self.jobs: dict[str, Job] = {}
        self.providers: dict[str, Provider] = {}
        self.agents: dict[str, str] = {}
        self.continuations = 0
        self.premature_returns = 0
        self.resets = 0
        self.reviews = 0
        self.interruptions = 0
        self.founder_required: list[str] = []

    def add_program(self, contract: ProgramContract) -> None:
        self.programs[contract.program_id] = contract
        self.journal.append("program_created", program_id=contract.program_id, owner_id=contract.owner_id,
                            objective=contract.objective, acceptance=list(contract.acceptance),
                            verification=list(contract.verification), authority_boundary=list(contract.authority_boundary),
                            founder_only=list(contract.founder_only), budget_usd=contract.budget_usd,
                            stop_conditions=list(contract.stop_conditions))

    def add_job(self, job: Job) -> None:
        if job.program_id not in self.programs or self.programs[job.program_id].owner_id != job.owner_id:
            raise ValueError("job owner/program mismatch")
        self.jobs[job.job_id] = job
        self.journal.append("job_created", job_id=job.job_id, program_id=job.program_id,
                            owner_id=job.owner_id, title=job.title, state=job.state.value,
                            dependencies=list(job.dependencies), priority=job.priority, base_sha=job.base_sha,
                            remaining=list(job.remaining))

    def add_provider(self, provider: Provider) -> None:
        self.providers[provider.provider_id] = provider

    def _ready(self, job: Job) -> bool:
        return job.state == JobState.READY and all(self.jobs[d].state == JobState.VERIFIED for d in job.dependencies)

    def next_ready(self, *, owner_id: str, program_id: str) -> Job | None:
        candidates = [j for j in self.jobs.values() if j.owner_id == owner_id and j.program_id == program_id and self._ready(j)]
        return max(candidates, key=lambda j: (j.priority, j.job_id), default=None)

    def assign(self, job_id: str, *, agent_id: str) -> str:
        job = self.jobs[job_id]
        if not self._ready(job):
            raise ValueError("job is not currently ready")
        job.state, job.builder_id, job.attempt = JobState.ASSIGNED, agent_id, job.attempt + 1
        job.updated_at = _now()
        claim = self.runtime.assign(job, agent_id)
        self.journal.append("job_assigned", job_id=job_id, agent_id=agent_id, attempt=job.attempt)
        return claim

    def observe(self, job_id: str, *, state: JobState, remaining: tuple[str, ...] = (), blocker: BlockerClass = BlockerClass.NONE) -> str | None:
        job = self.jobs[job_id]
        if state in {JobState.RUNNING, JobState.PROGRESSING, JobState.WAITING}:
            job.state, job.remaining, job.blocker = state, remaining, blocker
            job.updated_at = _now()
            self.journal.append("observation", job_id=job_id, state=state.value)
            return None
        if state in {JobState.IDLE_UNFINISHED, JobState.PARTIAL} and remaining:
            self.premature_returns += 1
            self.continuations += 1
            job.state, job.remaining = JobState.IDLE_UNFINISHED, remaining
            job.updated_at = _now()
            self.journal.append("continuation_requested", job_id=job_id, remaining=list(remaining))
            return f"continue:{job_id}:{job.attempt}"
        if state == JobState.BLOCKED:
            job.state, job.blocker = state, blocker
            if blocker == BlockerClass.FOUNDER_REQUIRED:
                self.founder_required.append(job_id)
            elif blocker in {BlockerClass.LOCAL_REPAIR, BlockerClass.INCOMPLETE_WORK, BlockerClass.PROVIDER_EXHAUSTED}:
                return self.observe(job_id, state=JobState.IDLE_UNFINISHED, remaining=remaining or ("resolve blocker",))
            return None
        job.state = state
        job.updated_at = _now()
        return None

    def freeze(self, job_id: str, *, sha: str, examiner_id: str) -> None:
        job = self.jobs[job_id]
        if not sha or sha == job.base_sha or not examiner_id or examiner_id == job.builder_id:
            raise ValueError("invalid artifact freeze")
        job.sha, job.examiner_id, job.review_sha = sha, examiner_id, sha
        job.state = JobState.AWAITING_REVIEW
        self.journal.append("artifact_frozen", job_id=job_id, sha=sha, examiner_id=examiner_id)

    def review(self, job_id: str, *, examiner_id: str, sha: str, passed: bool) -> None:
        job = self.jobs[job_id]
        if job.state != JobState.AWAITING_REVIEW or examiner_id != job.examiner_id or sha != job.sha:
            raise ValueError("review is stale, wrong examiner, or wrong SHA")
        self.reviews += 1
        job.review_passed, job.review_sha = passed, sha
        if passed:
            job.state = JobState.VERIFIED
            self.journal.append("review_passed", job_id=job_id, sha=sha)
        else:
            job.state, job.remaining, job.sha = JobState.FIX_REQUIRED, ("address examiner findings",), None
            release = getattr(self.runtime, "release", None)
            if release is not None:
                release(job)
            self.journal.append("review_failed", job_id=job_id, sha=sha)

    def provider_failover(self, provider_id: str, replacement: Provider) -> None:
        old = self.providers[provider_id]
        self.providers[provider_id] = Provider(old.provider_id, old.capabilities, ProviderState.EXHAUSTED, old.authorized)
        self.providers[replacement.provider_id] = replacement
        self.journal.append("provider_failover", old_provider=provider_id, new_provider=replacement.provider_id)

    def checkpoint(self, job_id: str) -> dict[str, Any]:
        job = self.jobs[job_id]
        return {"job_id": job_id, "program_id": job.program_id, "state": job.state.value, "sha": job.sha,
                "remaining": list(job.remaining), "attempt": job.attempt, "authority_boundary": list(self.programs[job.program_id].authority_boundary)}

    def resource_recommendation(self, job_id: str, telemetry: dict[str, Any]) -> ResourceRecommendation:
        """Consume advisory telemetry without turning it into execution authority."""
        if job_id not in self.jobs:
            raise KeyError(job_id)
        if telemetry.get("context_loss_risk") is True:
            action, rationale = "CHECKPOINT", "context loss risk is explicitly reported"
        elif telemetry.get("quota_remaining") is not None and telemetry["quota_remaining"] <= 0:
            action, rationale = "DEFER", "provider quota is exhausted"
        elif telemetry.get("context_utilization", 0) >= 0.9:
            action, rationale = "HANDOFF", "context utilization is high"
        elif telemetry.get("healthy_running") is True:
            action, rationale = "KEEP_CURRENT_AGENT", "productive work should not be interrupted"
        else:
            action, rationale = "CONTINUE_CURRENT_SESSION", "no stronger advisory signal is known"
        recommendation = ResourceRecommendation(action, job_id, rationale, authorized=False)
        self.journal.append("resource_recommendation", job_id=job_id, action=action)
        return recommendation

    def recover(self, records: list[dict[str, Any]]) -> None:
        self.journal = Journal.replay(records)
        self.programs.clear()
        self.jobs.clear()
        for record in records:
            if record.get("event") == "program_created":
                self.programs[record["program_id"]] = ProgramContract(
                    record["program_id"], record["owner_id"], record.get("objective", ""),
                    tuple(record.get("acceptance", ())), tuple(record.get("verification", ())),
                    tuple(record.get("authority_boundary", ())), tuple(record.get("founder_only", ())),
                    record.get("budget_usd"), tuple(record.get("stop_conditions", ())),
                )
            elif record.get("event") == "job_created":
                self.jobs[record["job_id"]] = Job(
                    record["job_id"], record["program_id"], record["owner_id"], record["title"],
                    JobState(record.get("state", JobState.READY.value)), tuple(record.get("dependencies", ())),
                    int(record.get("priority", 0)), base_sha=record.get("base_sha"),
                    remaining=tuple(record.get("remaining", ())),
                )
        # Journal replay is evidence only. Canonical runtime must be re-read before any
        # consequential continuation or assignment; recovery therefore never claims work.
        self.journal.append("recovery_requiring_canonical_reread")

    def founder_brief(self, program_id: str) -> dict[str, Any]:
        program = self.programs[program_id]
        jobs = [j for j in self.jobs.values() if j.program_id == program_id]
        return {"requested": program.objective, "done": [j.job_id for j in jobs if j.state == JobState.VERIFIED],
                "verified": [j.sha for j in jobs if j.state == JobState.VERIFIED and j.sha],
                "remaining": {j.job_id: list(j.remaining) for j in jobs if j.state not in {JobState.VERIFIED, JobState.CANCELLED}},
                "founder_required": list(self.founder_required), "continuations": self.continuations,
                "premature_returns": self.premature_returns, "resets": self.resets,
                "authority_boundary": list(program.authority_boundary)}

    def digest(self) -> str:
        payload = json.dumps({k: vars(v) for k, v in sorted(self.jobs.items())}, default=str, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()


def run_unattended_harness(plane: Level2ControlPlane, *, count: int = 1000) -> dict[str, Any]:
    """Exercise the real coordinator transitions with deterministic provider-free agents.

    The runtime port remains the effect authority; this helper is intentionally suitable for
    CI and fault-injection tests and records no fabricated certification or spend evidence.
    """
    program_id = next(iter(plane.programs))
    owner_id = plane.programs[program_id].owner_id
    for i in range(count):
        job_id = f"soak-{i:04d}"
        plane.add_job(Job(job_id, program_id, owner_id, "bounded unattended unit", priority=i % 7,
                          base_sha="base", remaining=("implement",)))
        plane.assign(job_id, agent_id=f"builder-{i % 3}")
        if i % 29 == 0:
            plane.observe(job_id, state=JobState.PARTIAL, remaining=("finish verification",))
        else:
            plane.observe(job_id, state=JobState.PROGRESSING)
        plane.freeze(job_id, sha=f"sha-{i:04d}", examiner_id=f"examiner-{i % 2}")
        plane.review(job_id, examiner_id=f"examiner-{i % 2}", sha=f"sha-{i:04d}", passed=True)
    return {"jobs": count, "verified": sum(j.state == JobState.VERIFIED for j in plane.jobs.values()),
            "continuations": plane.continuations, "digest": plane.digest()}


def run_multi_provider_harness(plane: Level2ControlPlane) -> dict[str, Any]:
    """Exercise provider loss, bounded continuation, review failure and re-review."""
    program_id = next(iter(plane.programs))
    owner_id = plane.programs[program_id].owner_id
    plane.add_provider(Provider("provider-a", frozenset({"edit"})))
    plane.add_provider(Provider("provider-b", frozenset({"edit", "review"})))
    plane.add_job(Job("provider-job", program_id, owner_id, "provider failover", base_sha="base", remaining=("implement",)))
    plane.assign("provider-job", agent_id="builder-a")
    plane.provider_failover("provider-a", Provider("provider-b", frozenset({"edit", "review"})))
    plane.observe("provider-job", state=JobState.PARTIAL, remaining=("resume after provider loss",))
    plane.freeze("provider-job", sha="provider-sha-a", examiner_id="examiner")
    plane.review("provider-job", examiner_id="examiner", sha="provider-sha-a", passed=False)
    plane.jobs["provider-job"].state = JobState.READY
    plane.jobs["provider-job"].base_sha = "base"
    plane.assign("provider-job", agent_id="builder-b")
    plane.freeze("provider-job", sha="provider-sha-b", examiner_id="examiner-b")
    plane.review("provider-job", examiner_id="examiner-b", sha="provider-sha-b", passed=True)
    return {"verified": plane.jobs["provider-job"].state == JobState.VERIFIED,
            "old_sha_replaced": plane.jobs["provider-job"].sha == "provider-sha-b",
            "continuations": plane.continuations}
