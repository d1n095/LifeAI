"""Canonical founder-facing work status.

Never infers activity from an assignment existing. `assigned` is not running.
`acknowledged` means a worker claimed the lease (or recorded dispatch) without live execution.
`independently_verified` requires durable verification evidence from a party that is not
the builder of the work. Agent-orchestration self-review is `completed`, not verified.
"""

from __future__ import annotations

import enum
import re
from typing import Any, Iterable

from app.mainai_execution.liveness import TaskLiveness, task_liveness
from app.models.agent_task import AgentTask, AgentTaskStatus
from app.models.mainai_execution import MainAITask, MainAITaskEvent, MainAITaskEventType, MainAITaskStatus
from app.models.mainai_job import MainAIJob, MainAIJobStatus
from app.models.workforce import WorkforceAssignment


class FounderWorkStatus(str, enum.Enum):
    queued = "queued"
    assigned = "assigned"
    acknowledged = "acknowledged"
    running = "running"
    waiting_on_dependency = "waiting_on_dependency"
    failed = "failed"
    completed = "completed"
    independently_verified = "independently_verified"
    cancelled = "cancelled"


ACTIVE_EXECUTION_STATUSES = frozenset({FounderWorkStatus.running.value})
_LIVE_ASSIGNMENT_STATUSES = frozenset({"assigned", "running", "awaiting_verification"})

# Agent-orchestration statuses that still justify a paid dispatch. Anything else is a
# repeated prompt (already in flight, already reviewed, or already published).
REDISPATCHABLE_AGENT_STATUSES = frozenset(
    {AgentTaskStatus.created, AgentTaskStatus.reviewed_needs_correction}
)


_SHA40 = re.compile(r"^[0-9a-f]{40}$")


def is_independent_exact_sha_receipt(receipt: Any, *, expected_sha: str | None = None) -> bool:
    """True only for a durable independent PASS bound to an exact 40-hex SHA.

    Builder self-verification, checkpoint CI/tests, and `verification_passed`
    events are not independent review receipts. A later FAIL/invalidation
    (`revoked`) or a SHA that is not the task commit cannot verify the work.
    """
    if not isinstance(receipt, dict):
        return False
    if receipt.get("revoked") is True:
        return False
    sha = receipt.get("candidate_sha") or receipt.get("sha")
    if not isinstance(sha, str) or _SHA40.fullmatch(sha) is None:
        return False
    if expected_sha is not None and sha != expected_sha:
        return False
    builder = str(receipt.get("builder_identity") or "").strip()
    examiner = str(receipt.get("examiner_identity") or "").strip()
    if not builder or not examiner or builder == examiner:
        return False
    return str(receipt.get("review_result") or "").upper() == "PASS"


def classify_mainai_task(
    task: MainAITask,
    job: MainAIJob | None = None,
    *,
    events: Iterable[MainAITaskEvent] | None = None,
    checkpoint_test_status: str | None = None,
    checkpoint_ci_status: str | None = None,
    independent_receipt: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Map a durable MainAI execution task to founder vocabulary.

    Liveness `idle` while status is `ready` is queued. `pending`/`blocked` wait on a
    dependency. A running task whose lease is dead/stalled is failed, not still running.
    A queued job with `locked_by` is acknowledged, not running.
    """
    liveness = task_liveness(task, job)
    source_status = task.status.value if isinstance(task.status, enum.Enum) else str(task.status)
    event_list = list(events) if events is not None else []
    evidence: dict[str, Any] = {
        "source": "mainai_task",
        "source_status": source_status,
        "liveness": liveness.value,
        "blocker_reason": task.blocker_reason,
        "mainai_job_id": str(task.mainai_job_id) if task.mainai_job_id else None,
        "goal_id": str(task.goal_id),
        "task_id": str(task.id),
        "description": task.description,
        "approval_required": bool(getattr(task, "approval_required", False)),
        "approval_granted": _has_event(event_list, MainAITaskEventType.approval_granted),
        "dispatched": _has_event(event_list, MainAITaskEventType.dispatched),
        "checkpoint_test_status": checkpoint_test_status,
        "checkpoint_ci_status": checkpoint_ci_status,
        "builder_self_verification": _has_event(event_list, MainAITaskEventType.verification_passed)
        and not is_independent_exact_sha_receipt(independent_receipt),
    }

    verified = _task_independently_verified(
        task,
        independent_receipt=independent_receipt,
    )
    if verified and task.status == MainAITaskStatus.completed:
        status = FounderWorkStatus.independently_verified
    elif task.status == MainAITaskStatus.completed:
        status = FounderWorkStatus.completed
    elif task.status == MainAITaskStatus.cancelled:
        status = FounderWorkStatus.cancelled
        evidence["cancelled"] = True
    elif task.status in {MainAITaskStatus.failed, MainAITaskStatus.retryable_failed}:
        status = FounderWorkStatus.failed
    elif liveness in {TaskLiveness.stalled, TaskLiveness.dead}:
        status = FounderWorkStatus.failed
        evidence["failure_class"] = liveness.value
    elif liveness == TaskLiveness.running:
        status = FounderWorkStatus.running
    elif task.status in {
        MainAITaskStatus.pending,
        MainAITaskStatus.blocked,
        MainAITaskStatus.waiting_external,
        MainAITaskStatus.waiting_ci,
    }:
        status = FounderWorkStatus.waiting_on_dependency
    elif _job_acknowledged(job) or evidence["dispatched"]:
        # Claimed or dispatched without a live running lease — accepted, not executing.
        status = FounderWorkStatus.acknowledged
    elif task.status == MainAITaskStatus.ready:
        status = FounderWorkStatus.queued
    else:
        status = FounderWorkStatus.queued

    evidence["independently_verified"] = status == FounderWorkStatus.independently_verified
    if is_independent_exact_sha_receipt(independent_receipt):
        evidence["independent_receipt_sha"] = independent_receipt.get("candidate_sha") or independent_receipt.get(
            "sha"
        )
        evidence["independent_examiner"] = independent_receipt.get("examiner_identity")
    return {"founder_status": status.value, **evidence}


def classify_mainai_job(job: MainAIJob) -> dict[str, Any]:
    source = job.status.value if isinstance(job.status, enum.Enum) else str(job.status)
    locked_by = getattr(job, "locked_by", None)
    if job.status == MainAIJobStatus.queued and locked_by:
        status = FounderWorkStatus.acknowledged
    elif job.status == MainAIJobStatus.queued:
        status = FounderWorkStatus.queued
    elif job.status == MainAIJobStatus.running:
        status = FounderWorkStatus.running
    elif job.status == MainAIJobStatus.paused:
        status = FounderWorkStatus.waiting_on_dependency
    elif job.status == MainAIJobStatus.completed:
        status = FounderWorkStatus.completed
    else:
        status = FounderWorkStatus.failed
    return {
        "founder_status": status.value,
        "source": "mainai_job",
        "source_status": source,
        "job_id": str(job.id),
        "job_type": job.job_type,
        "public_message": job.public_message,
        "error_category": job.error_category,
        "locked_by": locked_by,
        "independently_verified": False,
    }


def classify_workforce_assignment(assignment: WorkforceAssignment) -> dict[str, Any]:
    """Assignment existence is not activity. `assigned` stays assigned until running."""
    source = assignment.status
    verification = assignment.verification_status or "UNVERIFIED"
    evidence = {
        "source": "workforce_assignment",
        "source_status": source,
        "assignment_id": str(assignment.id),
        "profile_id": str(assignment.profile_id),
        "verification_status": verification,
        "revoked": assignment.revoked_at is not None,
    }
    if assignment.revoked_at is not None or source == "failed":
        status = FounderWorkStatus.failed
    elif source == "running":
        status = FounderWorkStatus.running
    elif source == "awaiting_verification":
        status = FounderWorkStatus.waiting_on_dependency
    elif source == "acknowledged":
        status = FounderWorkStatus.acknowledged
    elif source == "completed" and verification == "VERIFIED" and assignment.verifier_profile_id:
        if assignment.verifier_profile_id != assignment.profile_id:
            status = FounderWorkStatus.independently_verified
        else:
            status = FounderWorkStatus.completed
            evidence["unjustified_self_verification"] = True
    elif source == "completed":
        status = FounderWorkStatus.completed
    elif source == "assigned":
        status = FounderWorkStatus.assigned
    else:
        status = FounderWorkStatus.queued
    evidence["independently_verified"] = status == FounderWorkStatus.independently_verified
    return {"founder_status": status.value, **evidence}


def classify_agent_task(task: AgentTask) -> dict[str, Any]:
    source = task.status.value if isinstance(task.status, enum.Enum) else str(task.status)
    if task.status == AgentTaskStatus.created:
        status = FounderWorkStatus.assigned
    elif task.status == AgentTaskStatus.dispatched:
        # Dispatch recorded ownership, not live execution (N4).
        status = FounderWorkStatus.acknowledged
    elif task.status in {
        AgentTaskStatus.result_recorded,
        AgentTaskStatus.reviewed_needs_correction,
        AgentTaskStatus.ready_for_human,
        AgentTaskStatus.pr_prepared,
    }:
        status = FounderWorkStatus.waiting_on_dependency
    elif task.status == AgentTaskStatus.reviewed_rejected:
        status = FounderWorkStatus.failed
    elif task.status in {
        AgentTaskStatus.reviewed_approved,
        AgentTaskStatus.pr_opened,
    }:
        # Self-review / self-proposed PR is not independent verification.
        status = FounderWorkStatus.completed
    else:
        status = FounderWorkStatus.queued
    return {
        "founder_status": status.value,
        "source": "agent_task",
        "source_status": source,
        "task_id": str(task.id),
        "title": task.title,
        "independently_verified": False,
    }


def _job_acknowledged(job: MainAIJob | None) -> bool:
    if job is None:
        return False
    if job.status != MainAIJobStatus.queued:
        return False
    return bool(getattr(job, "locked_by", None))


def _has_event(events: Iterable[MainAITaskEvent], event_type: MainAITaskEventType) -> bool:
    return any(e.event_type == event_type for e in events)


def _task_independently_verified(
    task: MainAITask,
    *,
    independent_receipt: dict[str, Any] | None,
) -> bool:
    if task.status != MainAITaskStatus.completed:
        return False
    expected = None
    if isinstance(independent_receipt, dict):
        expected = independent_receipt.get("expected_sha") or independent_receipt.get("task_commit_sha")
        if not isinstance(expected, str) or _SHA40.fullmatch(expected) is None:
            expected = None
    return is_independent_exact_sha_receipt(independent_receipt, expected_sha=expected)
