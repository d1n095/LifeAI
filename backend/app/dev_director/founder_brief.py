"""Founder Brief (Milestone 6, Part 2). COMPOSES per-goal report data across every goal in a
Program's window -- does NOT import app.mainai_execution.final_report (keeping this
package's own independence from real production modules), it only accepts that module's
OUTPUT SHAPE as a caller-supplied input parameter (a tuple of dict-shaped per-goal reports).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from app.dev_director.types import BudgetEnvelope, Job, JobState, Program

if TYPE_CHECKING:
    from app.dev_director.git_pr_broker import PullRequestProposal
    from app.dev_director.recovery import RecoveryPlan


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class FounderBrief:
    program_id: uuid.UUID
    since: datetime
    generated_at: datetime
    jobs_started: int
    jobs_completed: int  # non-terminal-but-active -> CERTIFIED transition count in window
    jobs_certified: int
    jobs_failed: int
    jobs_blocked: int
    p0_count: int
    p1_count: int
    bugs_found: tuple[str, ...]
    provider_usage_summary: dict[str, int]
    cost_summary: BudgetEnvelope
    branches_and_shas: tuple[tuple[str, str], ...]  # (branch, sha) pairs, from CERTIFIED jobs only
    pr_proposals: tuple["PullRequestProposal", ...]
    actions_awaiting_founder: tuple[str, ...]
    security_incidents: tuple[str, ...]  # opaque references only -- never imports Guardian/Sentinel
    recovery_events: tuple[str, ...]
    goal_reports: tuple[dict, ...] = field(default_factory=tuple)  # the caller-supplied, unmodified per-goal report shapes


def generate_founder_brief(
    program: Program,
    jobs: tuple[Job, ...],
    *,
    since: datetime,
    goal_reports: tuple[dict, ...] = (),
    pr_proposals: tuple["PullRequestProposal", ...] = (),
    security_incidents: tuple[str, ...] = (),
    recovery_plan: "RecoveryPlan | None" = None,
) -> "FounderBrief":
    """Pure aggregation over durable, already-real Job/Program state -- never fabricates a
    summary sentence, never invents a count. `goal_reports` is accepted AS-IS from the caller
    (the real app.mainai_execution.final_report.generate_goal_report()'s own output shape,
    this module never re-derives or second-guesses it)."""
    own_jobs = tuple(j for j in jobs if j.program_id == program.program_id)
    started = tuple(j for j in own_jobs if j.created_at >= since)
    certified = tuple(j for j in own_jobs if j.state == JobState.CERTIFIED and (j.completed_at is None or j.completed_at >= since))
    failed = tuple(j for j in own_jobs if j.state in (JobState.FAILED, JobState.CANCELLED) and (j.completed_at is None or j.completed_at >= since))
    blocked = tuple(j for j in own_jobs if j.state == JobState.BLOCKED)
    needs_founder = tuple(f"job {j.job_id}: {j.next_action or j.state.value}" for j in own_jobs if j.state in (JobState.BLOCKED, JobState.NEEDS_FIX))

    p0 = sum(1 for j in own_jobs if j.test_evidence is not None and j.test_evidence.p0_count > 0)
    p1 = sum(1 for j in own_jobs if j.test_evidence is not None and j.test_evidence.p1_count > 0)
    bugs = tuple(
        reason
        for j in own_jobs
        if j.review_evidence is not None and j.review_evidence.verdict.value == "FAIL"
        for reason in (j.review_evidence.reason,)
        if reason
    )

    provider_usage: dict[str, int] = {}
    branches_and_shas: list[tuple[str, str]] = []
    for j in certified:
        if j.review_evidence is not None:
            provider_usage[j.review_evidence.examiner_identity] = provider_usage.get(j.review_evidence.examiner_identity, 0) + 1
        if j.result_artifact_sha:
            branches_and_shas.append((j.test_evidence.branch if j.test_evidence else "", j.result_artifact_sha))

    recovery_events = tuple(f"{d.job_id}: {d.action.value} -- {d.detail}" for d in (recovery_plan.decisions if recovery_plan else ()))

    return FounderBrief(
        program_id=program.program_id, since=since, generated_at=_utcnow(),
        jobs_started=len(started), jobs_completed=len(certified), jobs_certified=len(certified),
        jobs_failed=len(failed), jobs_blocked=len(blocked), p0_count=p0, p1_count=p1, bugs_found=bugs,
        provider_usage_summary=provider_usage, cost_summary=program.budget_envelope,
        branches_and_shas=tuple(branches_and_shas), pr_proposals=pr_proposals,
        actions_awaiting_founder=needs_founder, security_incidents=security_incidents,
        recovery_events=recovery_events, goal_reports=goal_reports,
    )
