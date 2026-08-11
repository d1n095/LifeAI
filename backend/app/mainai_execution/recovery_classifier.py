"""V0.2 recovery pipeline, stage 3: classify. Deterministic, evidence-only — reads
`record.evidence` (written once by recovery_inspector.py) and never re-queries the DB, the
filesystem, or GitHub itself. This is the founder's own A-I vocabulary
(app/models/mainai_recovery.py's `RecoveryClassification`), each code mapped to the exact
evidence fields recovery_inspector.py populates.

Priority order (checked top to bottom, first match wins) reflects "how far did this attempt
actually get", with one deliberate exception: CONFLICTED_STATE is checked FIRST, above
everything else, because a genuine remote divergence makes every other signal untrustworthy
(a locally-verified diff sitting on top of a worktree whose branch has since diverged from
origin is not safely "still just needs a push" — see recovery_inspector.py's ancestry check,
`is_ancestor()`, for how this is told apart from the completely normal case of "just hasn't
been pushed yet").

VERIFIED_WORK sits below the git-state codes on purpose: for a `repo_edit` task the git
evidence is strictly more concrete and further along than "verification passed" alone (which
happens BEFORE the push step in V0.1's own flow — see execution_job.py's
run_task_execution_job()). VERIFIED_WORK is the correct top signal for task types that never
touch git at all (`read_only_audit`, `run_tests`), where no worktree/branch evidence exists.

Hardening-pass finding (P0, real production correctness/approval-bypass bug): the SHA-equality
check below (`remote_sha == local_head`) was the ONLY signal `_classify()` used to detect
PUSHED_NO_PR/PR_EXISTS -- but `local_head` (`worktree_local_head_sha`) is populated exclusively
from a `mainai_task_worktrees` row, and `execution_job.py`'s actual production `repo_edit`
handler (`_handle_repo_edit()`/`_finalize_repo_edit()`) does NOT create one -- it still writes
to the shared checkout and pushes via the GitHub Git Data API (see
docs/MAINAI_DEAD_AGENT_RECOVERY_V0_2.md's LIMITED section). For every real dead `repo_edit`
job today, `local_head` is therefore always None, so this branch could NEVER be reached no
matter how far the dead job actually got -- a dead job that had ALREADY, genuinely pushed to
GitHub fell all the way through to CHECKPOINTED_WORK (auto-salvageable, no approval needed),
silently bypassing the one gate (require_recovery_approval()) that exists specifically because
PUSHED_NO_PR/PR_EXISTS mean real code is already live on GitHub, and risking a real duplicate
commit when the salvaged work_result re-ran `_finalize_repo_edit()`. Fixed by also accepting
`has_finalized_checkpoint` (real, durably recorded by execution_job.py itself immediately after
a confirmed successful push, entirely independent of whether a worktree exists) together with
a live-confirmed `remote_branch_exists` as an equally valid proof that the push already
happened -- using only evidence recovery_inspector.py already gathers, never a new signal."""

from app.mainai_execution.recovery_inspector import record_recovery_event
from app.models.mainai_recovery import AUTO_SALVAGEABLE_CLASSIFICATIONS, MainAIRecoveryEventType, MainAIRecoveryRecord, MainAIRecoveryStatus, RecoveryClassification


def _classify(evidence: dict) -> RecoveryClassification:
    if evidence.get("remote_sha_is_ancestor_of_local") is False:
        return RecoveryClassification.conflicted_state

    remote_exists = evidence.get("remote_branch_exists")
    local_head = evidence.get("worktree_local_head_sha")
    remote_sha = evidence.get("remote_branch_sha")

    worktree_confirms_push = remote_exists and remote_sha is not None and remote_sha == local_head
    checkpoint_confirms_push = bool(remote_exists) and bool(evidence.get("has_finalized_checkpoint"))
    if worktree_confirms_push or checkpoint_confirms_push:
        if evidence.get("pr_exists"):
            return RecoveryClassification.pr_exists
        return RecoveryClassification.pushed_no_pr

    # `worktree_current_commit` is set ONLY by commit_worktree_changes() actually creating a
    # new commit -- unlike `worktree_local_head_sha` (which is always non-None once a
    # worktree exists at all, since HEAD points at the base-SHA checkout commit even with zero
    # new commits), this is a real signal that local work beyond the base was committed.
    if evidence.get("worktree_current_commit"):
        return RecoveryClassification.local_committed_not_pushed

    if evidence.get("worktree_ownership_verified") and evidence.get("worktree_has_uncommitted_changes"):
        return RecoveryClassification.local_uncommitted_work

    if evidence.get("verification_passed"):
        return RecoveryClassification.verified_work

    if evidence.get("has_work_result_checkpoint"):
        return RecoveryClassification.checkpointed_work

    return RecoveryClassification.nothing_done


def classify_recovery_record(db, *, record: MainAIRecoveryRecord) -> MainAIRecoveryRecord:
    """Idempotent: a record not yet `inspected` cannot be classified (returned unchanged); a
    record already `classified` or later is also returned unchanged (a classification is a
    snapshot judgment, never silently recomputed against evidence that has not itself
    changed)."""
    if record.status != MainAIRecoveryStatus.inspected:
        return record

    classification = _classify(record.evidence or {})
    record.classification = classification
    record.status = MainAIRecoveryStatus.classified
    if classification not in AUTO_SALVAGEABLE_CLASSIFICATIONS:
        record.manual_review_required = True
    db.add(record)
    db.flush()

    record_recovery_event(
        db, record=record, event_type=MainAIRecoveryEventType.recovery_classified, detail={"classification": classification.value}
    )
    if classification not in AUTO_SALVAGEABLE_CLASSIFICATIONS:
        record_recovery_event(
            db, record=record, event_type=MainAIRecoveryEventType.manual_review_required, detail={"classification": classification.value}
        )
    return record
