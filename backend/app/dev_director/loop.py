"""Continuous execution loop (Milestone 1, Part 2) -- ties Part 1's primitives together into
the founder's own described loop:

job READY -> select builder -> create bounded assignment -> start/submit work -> observe
completion -> validate output SHA -> schedule examiner -> examiner PASS? yes -> CERTIFIED /
no -> NEEDS_FIX -> create bounded fix job -> reattack exact new SHA -> update program ->
select next READY job

No live external provider invocation happens here -- `BuilderAdapter`/`ExaminerAdapter` are
caller-injected Protocols (deterministic fakes in tests; no real adapter implementation is
built or wired this round). Critically, the adapters return only the RAW decision (SHA/
verdict), never a constructed BuilderResult/ExaminerVerdictRecord directly -- this loop
ALWAYS routes through Part 1's real submit_builder_result()/record_examiner_verdict()
functions, so an adapter can never bypass their real collusion/SHA-pinning/evidence checks
by constructing a record itself.

Job.state != CERTIFIED is NEVER treated as "safe to propose a PR for" -- see
git_pr_broker.build_pr_proposal(), the only function in this package that can produce a
PullRequestProposal, which requires state == CERTIFIED.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Protocol

from app.dev_director.builder_examiner import (
    new_builder_assignment,
    new_examiner_assignment,
    record_examiner_verdict,
    submit_builder_result,
)
from app.dev_director.fix_loop import MaxFixAttemptsExceededError, create_fix_job
from app.dev_director.job import next_ready_job, recompute_program_job_index, transition_job
from app.dev_director.program import reserve_from_budget
from app.dev_director.protected import assert_artifact_not_protected
from app.dev_director.provider_lease import new_external_provider_lease
from app.dev_director.provider_selection import select_builder_provider
from app.dev_director.types import (
    BudgetExceededError,
    BuilderAssignment,
    BuilderExaminerCollusionError,
    ExaminerAssignment,
    ExaminerVerdict,
    ExaminerVerdictError,
    Job,
    JobState,
    NoAvailableProvider,
    NoReadyJob,
    Program,
    ProtectedArtifactViolationError,
    ProviderCapabilityProfile,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Reserved job.risk_class values that are blocked-by-default while Program.offline_mode is
# True, per the founder's own closed list -- distinct from the ordinary priority-sort values
# ("low"/"medium"/"high"/"P0"/"critical"), which fall through to the default sort bucket in
# next_ready_job()'s own _RISK_SORT_ORDER and are unaffected here. A real, structural
# enum-set-membership check -- never free-text keyword matching over job prose.
BLOCKED_WHILE_OFFLINE_RISK_CLASSES = frozenset(
    {
        "production_deploy", "destructive_migration", "secret_rotation", "authority_expansion",
        "external_publication", "irreversible",
    }
)


def is_blocked_while_offline(job: Job, program: Program) -> bool:
    if not program.offline_mode:
        return False
    return job.risk_class in BLOCKED_WHILE_OFFLINE_RISK_CLASSES


class TickOutcome(str, Enum):
    NO_WORK_AVAILABLE = "NO_WORK_AVAILABLE"
    AMBIGUOUS_PRIORITY_TIE = "AMBIGUOUS_PRIORITY_TIE"
    BLOCKED_OFFLINE_MODE = "BLOCKED_OFFLINE_MODE"
    BLOCKED_PROTECTED_ARTIFACT = "BLOCKED_PROTECTED_ARTIFACT"
    BLOCKED_BUDGET = "BLOCKED_BUDGET"
    NO_BUILDER_AVAILABLE = "NO_BUILDER_AVAILABLE"
    BUILDER_CRASHED = "BUILDER_CRASHED"
    NO_EXAMINER_AVAILABLE = "NO_EXAMINER_AVAILABLE"
    EXAMINER_CRASHED = "EXAMINER_CRASHED"
    JOB_CERTIFIED = "JOB_CERTIFIED"
    JOB_NEEDS_FIX = "JOB_NEEDS_FIX"
    JOB_NEEDS_FIX_MAX_ATTEMPTS = "JOB_NEEDS_FIX_MAX_ATTEMPTS"
    JOB_SUPERSEDED_MID_RUN = "JOB_SUPERSEDED_MID_RUN"
    AUTHORITY_INVALID_MID_RUN = "AUTHORITY_INVALID_MID_RUN"


@dataclass
class TickResult:
    outcome: TickOutcome
    job_id: uuid.UUID | None = None
    new_job: Job | None = None
    builder_assignment: BuilderAssignment | None = None
    examiner_assignment: ExaminerAssignment | None = None
    detail: str = ""


class BuilderAdapter(Protocol):
    """Returns the RAW builder decision only -- (result_sha, branch, claimed_completion).
    Never constructs a BuilderResult itself; the loop always calls the real
    submit_builder_result()."""

    def build(self, assignment: BuilderAssignment) -> tuple[str, str, bool]: ...


class ExaminerAdapter(Protocol):
    """Returns the RAW examiner decision only -- (verdict, evidence, reason). Never
    constructs an ExaminerVerdictRecord itself; the loop always calls the real
    record_examiner_verdict(), so the real collusion/exact-SHA/non-empty-evidence checks are
    structurally impossible to bypass through an adapter."""

    def examine(self, assignment: ExaminerAssignment) -> tuple[ExaminerVerdict, tuple[str, ...], str]: ...


class AdapterCrashError(Exception):
    """A builder/examiner adapter raised -- caught here and converted to a typed TickResult,
    never propagated raw to corrupt Program/Job state."""


def _select_lease_refs(
    selected: ProviderCapabilityProfile, *, job: Job, workforce_assignment_ref_for: Callable[[Job], uuid.UUID] | None,
) -> tuple[uuid.UUID | None, uuid.UUID | None]:
    """A provider_identity prefixed 'local_agent:' is treated as a real local WorkforceAssignment
    -- the caller must supply `workforce_assignment_ref_for` to resolve it (this package does
    not create real WorkforceAssignment rows, that is app.workforce's own job). Every other
    provider_identity gets a fresh, real ExternalProviderLease via Part 1's own
    new_external_provider_lease()."""
    if selected.provider_identity.startswith("local_agent:"):
        if workforce_assignment_ref_for is None:
            raise ValueError("provider_identity is local_agent-shaped but no workforce_assignment_ref_for resolver was supplied")
        return workforce_assignment_ref_for(job), None
    lease = new_external_provider_lease(
        provider_identity=selected.provider_identity, task_ref=job.task_ref, workspace_ref=f"job:{job.job_id}",
        branch=f"dev-director/{job.job_id}", allowed_tools=(), allowed_files=tuple(job.touches), ttl_seconds=3600,
    )
    return None, lease.lease_id


def run_program_tick(
    program: Program,
    jobs: tuple[Job, ...],
    *,
    builder_candidates: tuple[ProviderCapabilityProfile, ...],
    examiner_candidates: tuple[ProviderCapabilityProfile, ...],
    builder_adapter: BuilderAdapter,
    examiner_adapter: ExaminerAdapter,
    workforce_assignment_ref_for: Callable[[Job], uuid.UUID] | None = None,
    authority_still_valid: Callable[[Job], bool] | None = None,
    is_job_superseded: Callable[[uuid.UUID], bool] | None = None,
) -> TickResult:
    """Safely, idempotently callable repeatedly -- a Program with no new READY work is a
    genuine no-op (NO_WORK_AVAILABLE), never an error. `authority_still_valid`/
    `is_job_superseded` are optional caller-supplied freshness checks (this package holds no
    real authority/durable-store of its own) -- if omitted, those specific adversarial checks
    are skipped, documented honestly rather than faked."""
    job = next_ready_job(jobs)
    if isinstance(job, NoReadyJob):
        if job.ambiguous_candidates:
            return TickResult(outcome=TickOutcome.AMBIGUOUS_PRIORITY_TIE, detail=f"tied candidates: {job.ambiguous_candidates}")
        return TickResult(outcome=TickOutcome.NO_WORK_AVAILABLE)

    if is_blocked_while_offline(job, program):
        return TickResult(outcome=TickOutcome.BLOCKED_OFFLINE_MODE, job_id=job.job_id, detail=f"risk_class={job.risk_class!r} blocked while Program.offline_mode")

    if job.input_artifact_sha:
        try:
            assert_artifact_not_protected(job.input_artifact_sha, program.protected_artifacts, action="modify")
        except ProtectedArtifactViolationError as exc:
            return TickResult(outcome=TickOutcome.BLOCKED_PROTECTED_ARTIFACT, job_id=job.job_id, detail=str(exc))

    if job.budget_usd:
        try:
            reserve_from_budget(program.budget_envelope, amount_usd=job.budget_usd)
        except BudgetExceededError as exc:
            return TickResult(outcome=TickOutcome.BLOCKED_BUDGET, job_id=job.job_id, detail=str(exc))

    selected_builder = select_builder_provider(builder_candidates, job=job, program=program)
    if isinstance(selected_builder, NoAvailableProvider):
        return TickResult(outcome=TickOutcome.NO_BUILDER_AVAILABLE, job_id=job.job_id, detail=selected_builder.reason)

    workforce_ref, external_ref = _select_lease_refs(selected_builder, job=job, workforce_assignment_ref_for=workforce_assignment_ref_for)
    ba = new_builder_assignment(job_id=job.job_id, builder_identity=selected_builder.provider_identity, workforce_assignment_ref=workforce_ref, external_lease_ref=external_ref)
    transition_job(job, to_state=JobState.ASSIGNED)
    transition_job(job, to_state=JobState.RUNNING)

    try:
        result_sha, branch, claimed_completion = builder_adapter.build(ba)
    except Exception as exc:  # noqa: BLE001 -- deliberately broad: any adapter failure must be caught, never corrupt Job state
        return TickResult(outcome=TickOutcome.BUILDER_CRASHED, job_id=job.job_id, builder_assignment=ba, detail=str(exc))

    if is_job_superseded is not None and is_job_superseded(job.job_id):
        # The job moved to SUPERSEDED while the adapter was "in flight" -- the stale result
        # must never be applied.
        return TickResult(outcome=TickOutcome.JOB_SUPERSEDED_MID_RUN, job_id=job.job_id, builder_assignment=ba)

    result = submit_builder_result(ba, result_sha=result_sha, branch=branch, claimed_completion=claimed_completion)
    job.result_artifact_sha = result.result_sha
    transition_job(job, to_state=JobState.VERIFYING)

    if authority_still_valid is not None and not authority_still_valid(job):
        return TickResult(outcome=TickOutcome.AUTHORITY_INVALID_MID_RUN, job_id=job.job_id, builder_assignment=ba, detail="authority_snapshot_ref no longer valid mid-run")

    selected_examiner = select_builder_provider(
        examiner_candidates, job=job, program=program, examiner_role=True, excluding=(selected_builder.provider_identity,),
    )
    if isinstance(selected_examiner, NoAvailableProvider):
        return TickResult(outcome=TickOutcome.NO_EXAMINER_AVAILABLE, job_id=job.job_id, builder_assignment=ba, detail=selected_examiner.reason)

    ea = new_examiner_assignment(job_id=job.job_id, examiner_identity=selected_examiner.provider_identity, builder_assignment=ba, target_sha=result.result_sha)

    try:
        verdict, evidence, reason = examiner_adapter.examine(ea)
    except Exception as exc:  # noqa: BLE001
        return TickResult(outcome=TickOutcome.EXAMINER_CRASHED, job_id=job.job_id, builder_assignment=ba, examiner_assignment=ea, detail=str(exc))

    if is_job_superseded is not None and is_job_superseded(job.job_id):
        return TickResult(outcome=TickOutcome.JOB_SUPERSEDED_MID_RUN, job_id=job.job_id, builder_assignment=ba, examiner_assignment=ea)

    try:
        verdict_record = record_examiner_verdict(ea, builder_assignment=ba, builder_result=result, verdict=verdict, evidence=evidence, reason=reason)
    except (BuilderExaminerCollusionError, ExaminerVerdictError) as exc:
        # A real, structural rejection (collusion or stale-SHA) -- job stays in VERIFYING,
        # never silently advanced.
        return TickResult(outcome=TickOutcome.EXAMINER_CRASHED, job_id=job.job_id, builder_assignment=ba, examiner_assignment=ea, detail=str(exc))

    job.review_evidence = verdict_record

    if verdict_record.verdict == ExaminerVerdict.PASS:
        transition_job(job, to_state=JobState.CERTIFIED)
        recompute_program_job_index(program, (*jobs, job))
        return TickResult(outcome=TickOutcome.JOB_CERTIFIED, job_id=job.job_id, builder_assignment=ba, examiner_assignment=ea)

    transition_job(job, to_state=JobState.NEEDS_FIX)
    try:
        fix_job = create_fix_job(job, verdict=verdict_record)
    except MaxFixAttemptsExceededError as exc:
        recompute_program_job_index(program, (*jobs, job))
        return TickResult(outcome=TickOutcome.JOB_NEEDS_FIX_MAX_ATTEMPTS, job_id=job.job_id, builder_assignment=ba, examiner_assignment=ea, detail=str(exc))

    recompute_program_job_index(program, (*jobs, job, fix_job))
    return TickResult(outcome=TickOutcome.JOB_NEEDS_FIX, job_id=job.job_id, new_job=fix_job, builder_assignment=ba, examiner_assignment=ea)
