"""Job (work queue) + hash-chained event log + priority/dependency/conflict engine
(Milestone 2). Mirrors app.mainai_execution.graph's real dependency-aware dispatch algorithm
(promote only when all dependencies satisfied; explicit blocked-with-reason on a failed
dependency; deterministic tie-break), generalized to cross-goal, Director-layer Jobs."""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone

from app.dev_director.types import (
    JOB_TRANSITIONS,
    TERMINAL_JOB_STATES,
    ConflictReport,
    Job,
    JobEvent,
    JobState,
    JobTransitionError,
    NoReadyJob,
    Program,
)

_GENESIS_HASH = "0" * 64


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_job(*, program_id: uuid.UUID, goal_description: str | None = None, goal_ref: uuid.UUID | None = None, risk_class: str = "low", touches: tuple[str, ...] = ()) -> Job:
    return Job(job_id=uuid.uuid4(), program_id=program_id, state=JobState.PLANNED, created_at=_utcnow(), goal_description=goal_description, goal_ref=goal_ref, risk_class=risk_class, touches=touches)


def _event_hash(event: JobEvent) -> str:
    payload = "|".join([
        str(event.event_id), str(event.job_id), event.from_state.value if event.from_state else "",
        event.to_state.value, event.note, event.prev_hash,
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def transition_job(job: Job, *, to_state: JobState, note: str = "") -> Job:
    """Fail-closed, table-validated -- same discipline as app.life_intents.service's
    LIFE_INTENT_TRANSITIONS. NEEDS_FIX can only reach CERTIFIED by passing back through
    VERIFYING again (see JOB_TRANSITIONS) -- never grandfathers an old verdict."""
    if job.state == to_state:
        return job  # same-state no-op, no event -- mirrors transition_intent()'s own convention
    if to_state not in JOB_TRANSITIONS.get(job.state, set()):
        raise JobTransitionError(f"cannot transition job {job.job_id} from {job.state.value} to {to_state.value}")
    prev_hash = job.history[-1].this_hash if job.history else _GENESIS_HASH
    event = JobEvent(event_id=uuid.uuid4(), job_id=job.job_id, from_state=job.state, to_state=to_state, note=note, prev_hash=prev_hash)
    event.this_hash = _event_hash(event)
    job.history = (*job.history, event)
    job.state = to_state
    if to_state == JobState.RUNNING and job.started_at is None:
        job.started_at = _utcnow()
    if to_state in TERMINAL_JOB_STATES:
        job.completed_at = _utcnow()
    return job


def verify_job_event_chain_intact(job: Job) -> bool:
    prev = _GENESIS_HASH
    for event in job.history:
        if event.prev_hash != prev:
            return False
        if _event_hash(event) != event.this_hash:
            return False
        prev = event.this_hash
    return True


# --- Program job index (derived view, never independent truth). ---------------------------


def recompute_program_job_index(program: Program, jobs: tuple[Job, ...]) -> Program:
    """The ONLY function that may set Program.queued_job_ids/blocked_job_ids/
    completed_job_ids/failed_job_ids -- always DERIVED from real Job.state values, never let
    to drift independently."""
    own_jobs = tuple(j for j in jobs if j.program_id == program.program_id)
    program.queued_job_ids = tuple(j.job_id for j in own_jobs if j.state in (JobState.PLANNED, JobState.READY, JobState.ASSIGNED, JobState.RUNNING, JobState.WAITING_FOR_RESULT, JobState.VERIFYING, JobState.NEEDS_FIX))
    program.blocked_job_ids = tuple(j.job_id for j in own_jobs if j.state == JobState.BLOCKED)
    program.completed_job_ids = tuple(j.job_id for j in own_jobs if j.state == JobState.CERTIFIED)
    program.failed_job_ids = tuple(j.job_id for j in own_jobs if j.state in (JobState.FAILED, JobState.CANCELLED))
    program.updated_at = _utcnow()
    return program


# --- Priority / dependency / conflict engine. ----------------------------------------------

_RISK_SORT_ORDER = {"P0": 0, "critical": 0, "high": 1, "P1": 1, "medium": 2, "low": 3}


def _sort_key(job: Job) -> tuple[int, datetime]:
    return (_RISK_SORT_ORDER.get(job.risk_class, 4), job.created_at)


def next_ready_job(jobs: tuple[Job, ...]) -> Job | NoReadyJob:
    """A Job is READY only when every dependency is itself CERTIFIED (terminally successful)
    and it is not blocked_by anything unresolved. P0/critical sorts before P1/medium before
    low; deterministic tie-break on created_at. A genuine tie (identical sort key) is NEVER
    silently broken by an arbitrary pick -- returns NoReadyJob with the tied candidates."""
    by_id = {j.job_id: j for j in jobs}
    candidates = []
    for job in jobs:
        if job.state != JobState.READY:
            continue
        if job.blocked_by:
            continue
        if any(by_id.get(dep) is None or by_id[dep].state != JobState.CERTIFIED for dep in job.dependencies):
            continue
        candidates.append(job)
    if not candidates:
        return NoReadyJob()
    candidates.sort(key=_sort_key)
    best_key = _sort_key(candidates[0])
    tied = [c for c in candidates if _sort_key(c) == best_key]
    if len(tied) > 1:
        return NoReadyJob(ambiguous_candidates=tuple(j.job_id for j in tied))
    return candidates[0]


def detect_job_conflicts(jobs: tuple[Job, ...]) -> tuple[ConflictReport, ...]:
    """Honest, simple declared-overlap check -- not real static diff analysis (a foundation-
    stage limitation, documented here, not silently overclaimed). Flags two READY-or-RUNNING
    jobs whose declared `touches` sets overlap."""
    active_states = {JobState.READY, JobState.ASSIGNED, JobState.RUNNING}
    active = [j for j in jobs if j.state in active_states and j.touches]
    reports: list[ConflictReport] = []
    for i, a in enumerate(active):
        for b in active[i + 1:]:
            overlap = tuple(sorted(set(a.touches) & set(b.touches)))
            if overlap:
                reports.append(ConflictReport(job_a=a.job_id, job_b=b.job_id, overlapping=overlap, reason="declared touches overlap"))
    return tuple(reports)
