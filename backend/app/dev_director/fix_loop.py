"""Fix loop + heartbeat/liveness/abandoned-job handling (Milestone 3, Part 2).

CERTIFIED SHA != BRANCH NAME: a fix job always produces its OWN fresh SHA via its OWN fresh
BuilderAssignment/ExaminerAssignment cycle -- the failed artifact is never retroactively
marked certified, and the fix job never inherits the failed job's result_artifact_sha as if
it were still valid.

Per the founder's own instruction: do not immediately assume failure on silence alone. A
real two-strike pattern -- LIKELY_ABANDONED requires both heartbeat staleness AND artifact
staleness; CONFIRMED_ABANDONED requires a SECOND, later check still showing no progress.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

from app.dev_director.job import new_job, transition_job
from app.dev_director.types import Job, JobState, ExaminerVerdictRecord

MAX_FIX_ATTEMPTS = 3  # mirrors app.autonomous_gap.GapGenerationBounds' proven bounded-generation technique


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MaxFixAttemptsExceededError(ValueError):
    """A poison result that would otherwise trigger an endless fix-loop cycle -- bounded, not
    silently allowed to retry forever."""


def create_fix_job(original_job: Job, *, verdict: ExaminerVerdictRecord) -> Job:
    """The old (failed) job is left for the caller to transition to NEEDS_FIX/SUPERSEDED --
    this function only builds the NEW job. Carries forward the FAILURE evidence as context
    (goal_description references it) but explicitly does NOT copy result_artifact_sha --
    the fix job starts fresh, with no artifact of its own yet, until its OWN builder produces
    one."""
    if original_job.attempt_number >= MAX_FIX_ATTEMPTS:
        raise MaxFixAttemptsExceededError(
            f"job {original_job.job_id} has already had {original_job.attempt_number} attempts "
            f"(max {MAX_FIX_ATTEMPTS}) -- refusing to create another fix job, bounded generation"
        )
    fix_job = new_job(
        program_id=original_job.program_id,
        goal_description=f"FIX for {original_job.job_id}: {verdict.reason or 'examiner FAIL'} (evidence: {', '.join(verdict.evidence) or 'none'})",
        goal_ref=original_job.goal_ref,
        risk_class=original_job.risk_class,
        touches=original_job.touches,
    )
    fix_job.task_ref = original_job.task_ref
    fix_job.attempt_number = original_job.attempt_number + 1
    fix_job.dependencies = ()
    # result_artifact_sha deliberately left None -- a fresh SHA is required from THIS job's
    # own builder; the old, failed SHA is never carried forward as if still valid.
    return fix_job


class JobLivenessAssessment(str, Enum):
    ALIVE = "ALIVE"
    SILENT_BUT_RECENT_PROGRESS = "SILENT_BUT_RECENT_PROGRESS"
    LIKELY_ABANDONED = "LIKELY_ABANDONED"
    CONFIRMED_ABANDONED = "CONFIRMED_ABANDONED"


@dataclass
class JobHeartbeat:
    job_id: uuid.UUID
    last_heartbeat_at: datetime
    last_artifact_change_at: datetime
    # Real two-strike bookkeeping: the timestamp of the FIRST time this heartbeat was
    # assessed as LIKELY_ABANDONED. None until that first strike happens.
    first_likely_abandoned_at: datetime | None = None


def assess_job_liveness(
    job: Job, heartbeat: JobHeartbeat, *, now: datetime, silence_threshold: timedelta,
    confirm_after: timedelta = timedelta(minutes=10),
) -> JobLivenessAssessment:
    """Do NOT immediately assume failure on silence alone. Uses BOTH heartbeat age AND
    artifact-change recency before concluding LIKELY_ABANDONED; requires a SECOND, LATER
    check still showing no progress (via `heartbeat.first_likely_abandoned_at`, which the
    CALLER is responsible for persisting/updating between assessments -- this function is
    pure and does not mutate `heartbeat`, see `record_liveness_strike()`) before
    CONFIRMED_ABANDONED."""
    heartbeat_age = now - heartbeat.last_heartbeat_at
    artifact_age = now - heartbeat.last_artifact_change_at
    if heartbeat_age <= silence_threshold:
        return JobLivenessAssessment.ALIVE
    if artifact_age <= silence_threshold:
        # Silent on heartbeat, but the artifact itself is still changing -- real progress,
        # not abandonment.
        return JobLivenessAssessment.SILENT_BUT_RECENT_PROGRESS
    # Both heartbeat AND artifact are stale -- first strike.
    if heartbeat.first_likely_abandoned_at is None:
        return JobLivenessAssessment.LIKELY_ABANDONED
    if now - heartbeat.first_likely_abandoned_at >= confirm_after:
        return JobLivenessAssessment.CONFIRMED_ABANDONED
    return JobLivenessAssessment.LIKELY_ABANDONED


def record_liveness_strike(heartbeat: JobHeartbeat, *, now: datetime) -> JobHeartbeat:
    """The caller's own job: when assess_job_liveness() returns LIKELY_ABANDONED for the
    FIRST time, call this to record the strike so a LATER assessment can reach
    CONFIRMED_ABANDONED. Idempotent -- calling it again after the first strike is a no-op."""
    if heartbeat.first_likely_abandoned_at is None:
        heartbeat.first_likely_abandoned_at = now
    return heartbeat


def handle_confirmed_abandoned_job(job: Job, *, note: str = "confirmed abandoned -- two-strike liveness check") -> Job:
    """Reuses the EXISTING JobState transition table exactly as Part 1 defined it -- never a
    bypass path, and this function does NOT modify that table. BLOCKED is only a legal
    target from PLANNED/READY/ASSIGNED (see JOB_TRANSITIONS) -- an ASSIGNED-but-never-started
    job genuinely abandoned before real work began is moved there, so a later tick can
    reconsider it. A job already RUNNING/WAITING_FOR_RESULT/VERIFYING when it went silent has
    no legal BLOCKED edge from those states (by Part 1's own design) -- CANCELLED is the only
    safe, honest choice (never FAILED -- a dead job proves nothing about the WORK itself,
    only about the worker, mirroring app.mainai_execution.recovery_takeover.py's own "a dead
    job proves nothing" doctrine). A caller wanting a fresh attempt after cancellation should
    use create_fix_job()-style job creation referencing the same goal_ref, exactly like the
    ordinary fix loop -- abandonment recovery is not a special bypass mechanism."""
    if job.state == JobState.ASSIGNED:
        return transition_job(job, to_state=JobState.BLOCKED, note=note)
    if job.state in (JobState.RUNNING, JobState.WAITING_FOR_RESULT, JobState.VERIFYING):
        return transition_job(job, to_state=JobState.CANCELLED, note=note)
    return job
