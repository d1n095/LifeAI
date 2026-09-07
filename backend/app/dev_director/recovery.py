"""Crash/restart recovery (Milestone 5, Part 2). RESTART != REPLAY.

This package holds no real durable store of its own this round -- "durable" here means
"whatever the caller reconstructs and passes in as the post-restart state" (typically
backed by a real DB elsewhere in a future integration). Documented honestly rather than
faking a persistence layer this round doesn't have.

A job that had ALREADY reached CERTIFIED before the crash remains CERTIFIED -- re-examining
an already-certified artifact is wasted work, not a safety requirement, since certification
is itself the durable proof. A job caught mid-flight is never assumed either failed or
succeeded -- it goes through assess_job_liveness() first. A job whose evidence/verdict
exists but was never applied to its own `state` (a genuine "crashed between artifact-ready
and state-update" scenario) has that pending transition RE-APPLIED from the real evidence,
never silently dropped, and never re-run from scratch either.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

from app.dev_director.fix_loop import JobHeartbeat, JobLivenessAssessment, assess_job_liveness
from app.dev_director.job import transition_job
from app.dev_director.types import (
    ExaminerVerdict,
    ExaminerVerdictRecord,
    Job,
    JobState,
    Program,
    TERMINAL_JOB_STATES,
)

_IN_FLIGHT_STATES = frozenset({JobState.ASSIGNED, JobState.RUNNING, JobState.WAITING_FOR_RESULT, JobState.VERIFYING})


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RecoveryAction(str, Enum):
    NO_ACTION_NEEDED = "NO_ACTION_NEEDED"
    REAPPLY_PENDING_BUILDER_RESULT = "REAPPLY_PENDING_BUILDER_RESULT"
    REAPPLY_PENDING_EXAMINER_VERDICT = "REAPPLY_PENDING_EXAMINER_VERDICT"
    AWAITING_LIVENESS_ASSESSMENT = "AWAITING_LIVENESS_ASSESSMENT"
    REQUIRES_REASSIGNMENT = "REQUIRES_REASSIGNMENT"


@dataclass(frozen=True)
class JobRecoveryDecision:
    job_id: uuid.UUID
    action: RecoveryAction
    detail: str


@dataclass(frozen=True)
class RecoveryPlan:
    program_id: uuid.UUID
    decisions: tuple[JobRecoveryDecision, ...]
    generated_at: datetime


def recover_program_state(
    program: Program,
    jobs: tuple[Job, ...],
    *,
    now: datetime,
    heartbeats: dict[uuid.UUID, JobHeartbeat] | None = None,
    pending_builder_results: dict[uuid.UUID, str] | None = None,  # job_id -> result_sha, arrived but not yet applied to job.state
    pending_examiner_verdicts: dict[uuid.UUID, ExaminerVerdictRecord] | None = None,  # job_id -> verdict, arrived but not yet applied
    silence_threshold=None,
) -> RecoveryPlan:
    """Never blindly replays. Applies real pending evidence where it exists; only falls back
    to a liveness assessment when there's genuinely nothing more concrete to go on."""
    silence_threshold = silence_threshold or timedelta(minutes=5)
    heartbeats = heartbeats or {}
    pending_builder_results = pending_builder_results or {}
    pending_examiner_verdicts = pending_examiner_verdicts or {}

    decisions: list[JobRecoveryDecision] = []
    for job in jobs:
        if job.program_id != program.program_id:
            continue

        if job.state in TERMINAL_JOB_STATES:
            # Includes CERTIFIED -- never re-examined merely because of a restart.
            decisions.append(JobRecoveryDecision(job.job_id, RecoveryAction.NO_ACTION_NEEDED, f"already terminal ({job.state.value})"))
            continue

        if job.state not in _IN_FLIGHT_STATES:
            # PLANNED/READY/BLOCKED/NEEDS_FIX -- not "in flight", the next ordinary tick
            # picks these up naturally, nothing to recover.
            decisions.append(JobRecoveryDecision(job.job_id, RecoveryAction.NO_ACTION_NEEDED, f"not in-flight ({job.state.value}), ordinary tick will handle it"))
            continue

        # Pending examiner verdict takes priority over pending builder result -- if a verdict
        # already arrived, the builder-result question is moot (we're past that stage).
        pending_verdict = pending_examiner_verdicts.get(job.job_id)
        if pending_verdict is not None and job.state == JobState.VERIFYING and job.review_evidence is None:
            decisions.append(JobRecoveryDecision(
                job.job_id, RecoveryAction.REAPPLY_PENDING_EXAMINER_VERDICT,
                f"real examiner verdict ({pending_verdict.verdict.value}) exists but was never applied to job.state -- crashed after exam, before certification",
            ))
            continue

        pending_sha = pending_builder_results.get(job.job_id)
        if pending_sha is not None and job.result_artifact_sha is None:
            decisions.append(JobRecoveryDecision(
                job.job_id, RecoveryAction.REAPPLY_PENDING_BUILDER_RESULT,
                f"real builder result (sha={pending_sha}) exists but was never applied to job.state -- crashed after commit, before state update",
            ))
            continue

        heartbeat = heartbeats.get(job.job_id)
        if heartbeat is None:
            decisions.append(JobRecoveryDecision(job.job_id, RecoveryAction.AWAITING_LIVENESS_ASSESSMENT, "no heartbeat data available -- cannot assess automatically, honest gap"))
            continue

        liveness = assess_job_liveness(job, heartbeat, now=now, silence_threshold=silence_threshold)
        if liveness == JobLivenessAssessment.CONFIRMED_ABANDONED:
            decisions.append(JobRecoveryDecision(job.job_id, RecoveryAction.REQUIRES_REASSIGNMENT, "confirmed abandoned via two-strike liveness check"))
        else:
            decisions.append(JobRecoveryDecision(job.job_id, RecoveryAction.AWAITING_LIVENESS_ASSESSMENT, f"liveness={liveness.value}, not yet confirmed abandoned"))

    return RecoveryPlan(program_id=program.program_id, decisions=tuple(decisions), generated_at=now)


def apply_pending_builder_result(job: Job, *, result_sha: str) -> Job:
    """Real re-application of a pending builder result found during recovery -- never
    re-runs the builder, never drops the evidence."""
    job.result_artifact_sha = result_sha
    if job.state == JobState.RUNNING:
        return transition_job(job, to_state=JobState.VERIFYING, note="recovered: pending builder result re-applied after restart")
    return job


def apply_pending_examiner_verdict(job: Job, *, verdict: ExaminerVerdictRecord) -> Job:
    """Real re-application of a pending examiner verdict found during recovery -- never
    re-runs the exam, never drops the evidence."""
    job.review_evidence = verdict
    if job.state != JobState.VERIFYING:
        return job
    if verdict.verdict == ExaminerVerdict.PASS:
        return transition_job(job, to_state=JobState.CERTIFIED, note="recovered: pending PASS verdict re-applied after restart")
    return transition_job(job, to_state=JobState.NEEDS_FIX, note="recovered: pending non-PASS verdict re-applied after restart")
