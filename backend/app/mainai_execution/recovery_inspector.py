"""V0.2 recovery pipeline, stage 1-2: detect (get_or_create_recovery_record) and inspect
(inspect_recovery_record). Gathers a durable EVIDENCE SNAPSHOT for one dead/stalled
`mainai_jobs` attempt — never decides what to DO with that evidence. That is
recovery_classifier.py's job, kept deliberately separate, mirroring the same separation V0.1
already keeps between execution (execution_job.py) and verification (verify.py): a component
that both gathers facts and acts on them is exactly the shape that hides "trust me, it's
fine" bugs.

Evidence sources, each read-only, each independently falsifiable by a later re-inspection:
  - mainai_checkpoints (via checkpoint.py's existing latest_checkpoint_for_step) — was a
    work_result ever durably recorded for this exact job? was the GitHub push finalized?
  - mainai_task_worktrees + worktree.py's own trust boundary — does an isolated checkout
    exist for this job, and does its on-disk marker still prove it belongs to this task
    (verify_worktree_ownership())? If so, does it have uncommitted changes, and what is its
    local HEAD?
  - GitHub (read-only: get_ref, list_pull_requests_for_head) — does the task's branch exist
    on the remote, and if so, is there already a PR for it?
  - mainai_task_events — did verification ever actually pass or fail for this task?
  - engineering_lessons (via lessons.py's existing lookup_lessons(), V0.2 addition) — what
    past, already-recorded incidents (any source, not just prior recoveries) are tagged with
    this task's task_type? Pure read-only lookup, same "influences what a human/the planner
    decides, never itself decides anything" discipline lessons.py's own docstring already
    establishes — recovery/takeover never re-plans a task (any regression test a lesson
    already added to task.verification_plan at ORIGINAL planning time stays in effect for a
    takeover automatically, since the same task row is reused), so this exists purely to give
    a human reviewing a blocked/manual-review record the same context a founder would have to
    otherwise look up separately.

Anything that cannot be read cleanly (more than one worktree row claims the same job, a git
status call fails, a PR lookup errors) is recorded as an `unsafe_reason` and the record is
parked at `blocked`/`manual_review_required=True` immediately — never guessed past. This is
the fail-closed behavior the founder's instruction requires ("Om worktree/branch/commit inte
säkert kan bindas till rätt task+attempt: fail closed -> manual_review_required")."""

from datetime import datetime

from sqlalchemy.orm import Session

from app.integrations.github_client import GitHubClient, GitHubClientError
from app.mainai_execution.checkpoint import latest_checkpoint_for_step
from app.mainai_execution.lessons import lookup_lessons
from app.mainai_execution.worktree import fetch_remote_branch, is_ancestor, task_branch_name, verify_worktree_ownership, worktree_git_status
from app.models.mainai_execution import MainAITask, MainAITaskEvent, MainAITaskEventType
from app.models.mainai_job import MainAIJob
from app.models.mainai_recovery import (
    MainAIRecoveryEvent,
    MainAIRecoveryEventType,
    MainAIRecoveryRecord,
    MainAIRecoveryStatus,
    MainAITaskWorktree,
)


def record_recovery_event(
    db: Session, *, record: MainAIRecoveryRecord, event_type: MainAIRecoveryEventType, detail: dict | None = None
) -> MainAIRecoveryEvent:
    event = MainAIRecoveryEvent(
        recovery_record_id=record.id,
        task_id=record.task_id,
        owner_id=record.owner_id,
        event_type=event_type,
        detail=detail or {},
    )
    db.add(event)
    db.flush()
    return event


def get_or_create_recovery_record(db: Session, *, task: MainAITask, job: MainAIJob) -> MainAIRecoveryRecord:
    """Stage 1: detect. Idempotent per job — a job dies exactly once; a later takeover attempt
    that itself dies gets its OWN job_id and therefore its own, separate recovery record
    (never reuses this one)."""
    existing = db.query(MainAIRecoveryRecord).filter(MainAIRecoveryRecord.job_id == job.id).one_or_none()
    if existing is not None:
        return existing

    record = MainAIRecoveryRecord(
        task_id=task.id,
        owner_id=task.owner_id,
        job_id=job.id,
        detected_at=datetime.utcnow(),
        status=MainAIRecoveryStatus.detected,
    )
    db.add(record)
    db.flush()
    record_recovery_event(
        db, record=record, event_type=MainAIRecoveryEventType.dead_detected, detail={"job_id": str(job.id)}
    )
    return record


async def inspect_recovery_record(db: Session, *, task: MainAITask, job: MainAIJob, record: MainAIRecoveryRecord) -> MainAIRecoveryRecord:
    """Stage 2: inspect. Idempotent — a record already past `detected` is returned unchanged,
    never re-inspected (evidence is a snapshot of what was true at inspection time, not
    continuously re-derived; a fresh recovery pass for a NEW dead job gets its own record)."""
    if record.status != MainAIRecoveryStatus.detected:
        return record

    record.status = MainAIRecoveryStatus.inspecting
    db.add(record)
    db.flush()
    record_recovery_event(db, record=record, event_type=MainAIRecoveryEventType.recovery_started)

    evidence: dict = {}
    unsafe_reasons: list[str] = []

    # -- checkpoints --
    work_checkpoint = latest_checkpoint_for_step(db, task_id=task.id, job_id=job.id, step="work_result")
    finalize_checkpoint = latest_checkpoint_for_step(db, task_id=task.id, job_id=job.id, step="finalized")
    evidence["has_work_result_checkpoint"] = work_checkpoint is not None
    evidence["has_finalized_checkpoint"] = finalize_checkpoint is not None

    # -- worktree --
    worktree_rows = db.query(MainAITaskWorktree).filter(MainAITaskWorktree.job_id == job.id).all()
    if len(worktree_rows) > 1:
        unsafe_reasons.append("multiple_worktree_rows_for_job")
    worktree = worktree_rows[0] if len(worktree_rows) == 1 else None
    evidence["worktree_exists"] = worktree is not None
    if worktree is not None:
        ownership_ok = verify_worktree_ownership(worktree)
        evidence["worktree_ownership_verified"] = ownership_ok
        evidence["worktree_current_commit"] = worktree.current_commit
        evidence["worktree_branch"] = worktree.branch
        if ownership_ok:
            try:
                status = worktree_git_status(worktree)
                evidence["worktree_has_uncommitted_changes"] = status["has_uncommitted_changes"]
                evidence["worktree_local_head_sha"] = status["local_head_sha"]
            except Exception as exc:  # noqa: BLE001 -- any git failure here is evidence, not a crash
                unsafe_reasons.append(f"worktree_git_status_failed: {exc}")

    # -- remote GitHub state --
    branch = task_branch_name(task.id)
    evidence["branch_name"] = branch
    client = GitHubClient()
    if client.is_configured():
        try:
            remote_sha = await client.get_ref(branch)
            evidence["remote_branch_exists"] = True
            evidence["remote_branch_sha"] = remote_sha
        except GitHubClientError:
            evidence["remote_branch_exists"] = False
            evidence["remote_branch_sha"] = None

        if evidence["remote_branch_exists"]:
            try:
                repo_owner = client.settings.github_repo.split("/", 1)[0]
                prs = await client.list_pull_requests_for_head(head=f"{repo_owner}:{branch}", base="claude/det-kommer-mer-879lcm", state="all")
                evidence["pr_exists"] = bool(prs)
                evidence["pr_numbers"] = [pr.get("number") for pr in prs]
            except GitHubClientError as exc:
                unsafe_reasons.append(f"pr_lookup_failed: {exc}")
        else:
            evidence["pr_exists"] = False
            evidence["pr_numbers"] = []
    else:
        # GitHub not configured -- genuinely unknown, never coerced to False (that would read
        # as "confirmed no PR" when it is really "never checked").
        evidence["remote_branch_exists"] = None
        evidence["pr_exists"] = None
        evidence["pr_numbers"] = None

    # -- true divergence check (not just a SHA mismatch, which is also what a completely
    # normal "haven't pushed yet" state looks like) --
    if (
        evidence.get("worktree_ownership_verified")
        and evidence.get("worktree_local_head_sha")
        and evidence.get("remote_branch_exists")
        and evidence.get("remote_branch_sha")
        and evidence["remote_branch_sha"] != evidence["worktree_local_head_sha"]
    ):
        try:
            fetch_remote_branch(worktree)
            ancestor = is_ancestor(worktree, evidence["remote_branch_sha"])
            evidence["remote_sha_is_ancestor_of_local"] = ancestor
            if ancestor is None:
                unsafe_reasons.append("remote_sha_not_present_in_local_worktree")
        except Exception as exc:  # noqa: BLE001 -- ancestry check failure is evidence, not a crash
            unsafe_reasons.append(f"ancestry_check_failed: {exc}")

    # -- task-level verification outcome --
    verification_event_types = {
        row[0]
        for row in db.query(MainAITaskEvent.event_type)
        .filter(
            MainAITaskEvent.task_id == task.id,
            MainAITaskEvent.event_type.in_(
                [MainAITaskEventType.verification_passed, MainAITaskEventType.verification_failed]
            ),
        )
        .all()
    }
    evidence["verification_passed"] = MainAITaskEventType.verification_passed in verification_event_types
    evidence["verification_failed"] = MainAITaskEventType.verification_failed in verification_event_types

    # V0.2 engineering-lessons reuse: pure lookup, same read-only "influences what a human or
    # the planner decides, never itself decides anything" discipline lessons.py's own docstring
    # already establishes for planner.py's create_plan() -- recovery/takeover never re-plans a
    # task (reset_task_for_takeover() reuses the SAME task row, so any regression tests a
    # lesson already added to task.verification_plan at ORIGINAL planning time are already in
    # effect for a takeover automatically). What was missing is VISIBILITY: a founder doing
    # manual review of a blocked/CONFLICTED_STATE record had no way to see whether this
    # task_type has known past incidents at all without a separate query. Surfacing them here
    # means the recovery record's own durable evidence -- and therefore the final report's
    # recovery_history -- carries that context, never a fabricated one (an empty list is
    # exactly as truthful as a populated one).
    applicable_lessons = lookup_lessons(db, applies_to_any=[task.task_type])
    evidence["applicable_lessons"] = [
        {
            "lesson_id": str(lesson.id),
            "problem": lesson.problem,
            "general_rule": lesson.general_rule,
            "severity": lesson.severity.value,
        }
        for lesson in applicable_lessons
    ]

    record.evidence = evidence
    if unsafe_reasons:
        record.status = MainAIRecoveryStatus.blocked
        record.blocker = "; ".join(unsafe_reasons)
        record.manual_review_required = True
    else:
        record.status = MainAIRecoveryStatus.inspected
    db.add(record)
    db.flush()

    record_recovery_event(
        db, record=record, event_type=MainAIRecoveryEventType.recovery_inspected, detail={"unsafe_reasons": unsafe_reasons}
    )
    if unsafe_reasons:
        record_recovery_event(
            db, record=record, event_type=MainAIRecoveryEventType.manual_review_required, detail={"reasons": unsafe_reasons}
        )
    return record
