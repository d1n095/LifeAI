"""V0.2 recovery pipeline, stage 4: salvage. Copies durable evidence from the DEAD job
(`record.job_id`) forward to a NEW job (`new_job.id`, already dispatched by the caller via the
existing `dispatch_ready_task()` -- see recovery_takeover.py) so V0.1's OWN existing resume/
checkpoint contract (checkpoint.py's `latest_checkpoint_for_step()`,
run_task_execution_job()'s own `work_result`/`finalized` step lookups) picks it up naturally.
This module never invents a parallel resume mechanism — it only ever writes checkpoints and
worktree ownership records in the exact shape execution_job.py already reads.

Only ever called for a classification in `AUTO_SALVAGEABLE_CLASSIFICATIONS`
(app/models/mainai_recovery.py) — CONFLICTED_STATE and UNSAFE_TO_AUTO_RECOVER never reach
here, per the founder's explicit "fail closed -> manual_review_required" instruction.

Per classification:
  - NOTHING_DONE: no-op. The new attempt starts genuinely fresh (correct — there is nothing
    honest to salvage).
  - Any classification where the dead job's `work_result` checkpoint exists
    (CHECKPOINTED_WORK, VERIFIED_WORK, PUSHED_NO_PR, PR_EXISTS): that checkpoint is copied
    forward verbatim under the new job_id. This is real, previously-computed, already-durable
    work — recomputing it would waste a real (possibly expensive, possibly
    non-deterministic) provider call for no benefit. Duplicate side-effect prevention
    (verification): if the dead job ALSO durably recorded a `verification` checkpoint (only
    ever written for a genuine PASS — see execution_job.py) for that exact work_result, it is
    copied forward too, so the new attempt does not silently re-run the task's real
    verification_plan side effects (e.g. a `targeted_tests` subprocess pytest invocation) a
    second time against content that already, provably, passed.
  - PUSHED_NO_PR / PR_EXISTS specifically ALSO get a `finalized` checkpoint synthesized from
    the inspector's own evidence (the real branch name, the fact that `remote_branch_exists`
    was independently confirmed via GitHub). This directly closes the exact gap
    `_finalize_repo_edit()`'s own docstring admits (execution_job.py): without it, a resume
    with no `finalized` checkpoint re-runs `_finalize_repo_edit()` from scratch and creates a
    second, redundant commit with the same file contents on top of the branch's real tip.
    Branch/commit salvage safety check: `evidence["remote_branch_sha"]` is a SNAPSHOT taken
    at inspection time, and salvage can run an arbitrary amount of time later (an operator
    re-running a stuck recovery, a delayed takeover retry) -- the founder's explicit
    "remote ancestry måste verifieras, aldrig antas" invariant means salvage must re-confirm
    the branch's tip live, immediately before minting a checkpoint that will make the new job
    skip `_finalize_repo_edit()` entirely. If the branch has since disappeared or its tip has
    moved (force-pushed by something outside this system, merged and deleted, etc.), the
    inspected state is no longer provably true and salvage refuses to synthesize the
    `finalized` checkpoint -- see `_verify_branch_unchanged_since_inspection()` below.
  - LOCAL_UNCOMMITTED_WORK / LOCAL_COMMITTED_NOT_PUSHED: the worktree (if it still exists and
    its ownership still verifies) is rebound to the new job via
    `worktree.rebind_worktree_to_job()` — a durable, auditable ownership transfer. Honesty
    note (also called out in the final V0.2 REAL/STUBBED/LIMITED list): V0.1's
    `_handle_repo_edit()` does not yet consult `mainai_task_worktrees` at all — it still
    writes to the shared checkout. Rebinding the worktree here correctly preserves the ATTEMPT
    that made local git progress and prevents it from being silently orphaned/overwritten by
    the new job's own dispatch, but actually re-entering and continuing IN that worktree
    requires execution_job.py to be wired to worktree.py, which is future integration work,
    not silently claimed as already happening here."""

from sqlalchemy.orm import Session

from app.integrations.github_client import GitHubClient, GitHubClientError
from app.mainai_execution.checkpoint import latest_checkpoint_for_step, record_checkpoint
from app.mainai_execution.recovery_inspector import record_recovery_event
from app.mainai_execution.worktree import BASE_BRANCH, rebind_worktree_to_job, verify_worktree_ownership
from app.models.mainai_execution import MainAIGoal, MainAITask
from app.models.mainai_job import MainAIJob
from app.models.mainai_recovery import (
    AUTO_SALVAGEABLE_CLASSIFICATIONS,
    MainAIRecoveryEventType,
    MainAIRecoveryRecord,
    MainAIRecoveryStatus,
    MainAITaskWorktree,
    RecoveryClassification,
)


class SalvageError(RuntimeError):
    pass


_PUSHED_CLASSIFICATIONS = frozenset({RecoveryClassification.pushed_no_pr, RecoveryClassification.pr_exists})
_LOCAL_WORKTREE_CLASSIFICATIONS = frozenset(
    {RecoveryClassification.local_uncommitted_work, RecoveryClassification.local_committed_not_pushed}
)


async def _verify_branch_unchanged_since_inspection(record: MainAIRecoveryRecord, *, branch: str) -> None:
    """Live re-confirmation, immediately before salvage trusts it, that the branch's remote
    tip is still exactly the commit `evidence["remote_branch_sha"]` recorded at inspection
    time. Salvage can run an arbitrary amount of time after inspection (a stuck recovery
    re-run, a delayed takeover retry), so the branch may have been deleted, merged, or its
    tip moved by something outside this system in the meantime. Any drift means the inspected
    state is no longer provably true, and per the founder's "remote ancestry måste verifieras,
    aldrig antas" invariant, salvage must refuse to act on it rather than assume it still
    holds."""
    evidence = record.evidence or {}
    expected_sha = evidence.get("remote_branch_sha")
    client = GitHubClient()
    if not client.is_configured():
        raise SalvageError(f"GitHub is not configured -- cannot re-verify branch '{branch}' before salvage.")
    try:
        live_sha = await client.get_ref(branch)
    except GitHubClientError as exc:
        raise SalvageError(
            f"branch '{branch}' no longer exists on the remote (was present at inspection) -- "
            "refusing to salvage state that can no longer be proven true."
        ) from exc
    if live_sha != expected_sha:
        raise SalvageError(
            f"branch '{branch}' tip has moved since inspection (was {expected_sha}, now {live_sha}) -- "
            "refusing to salvage state that can no longer be proven true."
        )


async def salvage_recovery_record(
    db: Session, *, task: MainAITask, goal: MainAIGoal, record: MainAIRecoveryRecord, new_job: MainAIJob
) -> MainAIRecoveryRecord:
    if record.status != MainAIRecoveryStatus.classified:
        raise SalvageError(f"recovery record {record.id} is not classified (status={record.status}) -- cannot salvage.")
    if record.classification not in AUTO_SALVAGEABLE_CLASSIFICATIONS:
        raise SalvageError(f"recovery record {record.id} classification {record.classification} is not auto-salvageable.")

    record.status = MainAIRecoveryStatus.salvaging
    db.add(record)
    db.flush()
    record_recovery_event(db, record=record, event_type=MainAIRecoveryEventType.salvage_started, detail={"classification": record.classification.value})

    evidence = record.evidence or {}
    actions_taken: list[str] = []

    if evidence.get("has_work_result_checkpoint"):
        old_work_checkpoint = latest_checkpoint_for_step(db, task_id=task.id, job_id=record.job_id, step="work_result")
        if old_work_checkpoint is not None:
            record_checkpoint(
                db, task=task, goal=goal, job_id=new_job.id, step="work_result",
                data={"work_result": old_work_checkpoint.executor_state["work_result"]},
            )
            actions_taken.append("copied_work_result_checkpoint")

            # Duplicate side-effect prevention (verification): a "verification" checkpoint is
            # only ever paired with the EXACT work_result it was checked against
            # (execution_job.py records it immediately after that work_result durably passed)
            # -- so it is only ever safe to copy forward here alongside that same work_result,
            # never on its own. Only present for a job that ran after this checkpoint step
            # existed; an older dead job simply has none to copy, and the new attempt correctly
            # falls back to a real, honest re-verification rather than inventing one.
            if evidence.get("verification_passed"):
                old_verification_checkpoint = latest_checkpoint_for_step(db, task_id=task.id, job_id=record.job_id, step="verification")
                if old_verification_checkpoint is not None:
                    record_checkpoint(
                        db, task=task, goal=goal, job_id=new_job.id, step="verification",
                        data={"verification": old_verification_checkpoint.executor_state["verification"]},
                    )
                    actions_taken.append("copied_verification_checkpoint")

    if record.classification in _PUSHED_CLASSIFICATIONS:
        branch = evidence.get("branch_name") or evidence.get("worktree_branch")
        if branch and evidence.get("remote_branch_exists"):
            await _verify_branch_unchanged_since_inspection(record, branch=branch)
            finalize_info = {
                "branch": branch,
                "base_branch": BASE_BRANCH,
                "proposed": False,
                "salvaged_from_recovery_record": str(record.id),
            }
            record_checkpoint(db, task=task, goal=goal, job_id=new_job.id, step="finalized", data={"finalize_info": finalize_info})
            actions_taken.append("synthesized_finalized_checkpoint")

    if record.classification in _LOCAL_WORKTREE_CLASSIFICATIONS:
        worktree = db.query(MainAITaskWorktree).filter(MainAITaskWorktree.job_id == record.job_id).one_or_none()
        if worktree is not None and verify_worktree_ownership(worktree):
            rebind_worktree_to_job(db, worktree, new_job_id=new_job.id, new_lease_generation=1)
            actions_taken.append("rebound_worktree")
        else:
            actions_taken.append("worktree_unavailable_no_local_salvage")

    record.status = MainAIRecoveryStatus.salvaged
    record.salvage_action = ",".join(actions_taken) if actions_taken else "none"
    db.add(record)
    db.flush()
    record_recovery_event(db, record=record, event_type=MainAIRecoveryEventType.salvage_completed, detail={"actions": actions_taken})
    return record
