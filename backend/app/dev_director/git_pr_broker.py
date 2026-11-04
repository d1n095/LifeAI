"""Git/worktree/artifact broker contract (Milestone 4, Part 2) -- interface-only, no live
git push or `gh pr create` anywhere in this module (verified structurally, see
test_no_live_push_or_gh_calls_anywhere_in_package in the test file, which uses the exact
same AST-based technique app.attachment_chamber's own no-forbidden-calls test already
established -- real code inspection, never a docstring/comment string match).

`GitWorktreeBrokerContract`'s method signatures mirror the REAL, already-working functions in
app.development_supervisor.production_worktree (`_run_git`, `goal_branch_name`,
`goal_worktree_path`, `ensure_goal_worktree_sync`, `reset_goal_worktree_to_clean_head`) --
cited here as the pattern a future real implementation should follow, NOT imported (this
package stays isolated from that real production module, per the reconciliation doc).

`build_pr_proposal()` DOES real, useful work assembling proposal data -- it just never
calls anything that would actually push/open a PR.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol

from app.dev_director.protected import assert_artifact_not_protected
from app.dev_director.types import (
    CompletionEvidence,
    ExaminerVerdictRecord,
    Job,
    JobState,
    ProtectedArtifact,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class GitWorktreeBrokerContract(Protocol):
    """Interface-only -- no implementation this round. A future real implementation should
    mirror app.development_supervisor.production_worktree's real functions (cited by name in
    this module's docstring, never imported here)."""

    def create_worktree(self, *, branch: str, base_sha: str) -> str: ...
    def create_branch(self, *, name: str, from_sha: str) -> None: ...
    def read_current_sha(self, *, branch: str) -> str: ...
    def commit(self, *, message: str, paths: tuple[str, ...]) -> str: ...
    def diff(self, *, base_sha: str, head_sha: str) -> tuple[str, ...]: ...
    def run_tests(self, *, commands: tuple[str, ...]) -> bool: ...
    def compare_artifacts(self, *, sha_a: str, sha_b: str) -> tuple[str, ...]: ...
    def rollback(self, *, to_sha: str) -> None: ...


class PullRequestProposalError(ValueError):
    pass


@dataclass(frozen=True)
class PullRequestProposal:
    """What a PR WOULD contain -- never actually opened. No live gh/git-push call exists
    anywhere in this package."""

    proposal_id: uuid.UUID
    program_id: uuid.UUID
    job_id: uuid.UUID
    title: str
    description: str
    branch: str
    base_sha: str
    head_sha: str
    changed_files: tuple[str, ...]
    test_evidence: CompletionEvidence
    examiner_verdict: ExaminerVerdictRecord
    created_at: datetime = field(default_factory=_utcnow)


def build_pr_proposal(
    job: Job, *, examiner_verdict: ExaminerVerdictRecord, evidence: CompletionEvidence,
    protected_artifacts: tuple[ProtectedArtifact, ...] = (), title: str | None = None,
) -> PullRequestProposal:
    """The ONLY function in this package capable of producing a PullRequestProposal. Requires
    Job.state == CERTIFIED (never "safe to propose a PR for" on anything less). Requires the
    examiner verdict's OWN target_sha (the exact examined SHA) to match `job.result_artifact_sha`
    -- a branch moving after the exam (someone pushed a new commit to the same branch name
    post-certification) must NOT silently get proposed as if it were the examined artifact.
    Also rejects if the job's own certified SHA is itself a protected ref."""
    if job.state != JobState.CERTIFIED:
        raise PullRequestProposalError(f"job {job.job_id} is {job.state.value}, not CERTIFIED -- CERTIFIED is the only safe-to-propose signal")
    if examiner_verdict.target_sha != job.result_artifact_sha:
        raise PullRequestProposalError(
            f"examiner verdict was pinned to {examiner_verdict.target_sha!r}, but the job's own current "
            f"result_artifact_sha is {job.result_artifact_sha!r} -- the branch moved after the exam, or a "
            "stale verdict is being reused; refusing to propose a PR for an artifact that was never actually examined"
        )
    assert_artifact_not_protected(job.result_artifact_sha or "", protected_artifacts, action="merge")
    return PullRequestProposal(
        proposal_id=uuid.uuid4(), program_id=job.program_id, job_id=job.job_id,
        title=title or (job.goal_description or f"Job {job.job_id}"),
        description=f"Certified by {examiner_verdict.examiner_identity}. Evidence: {', '.join(examiner_verdict.evidence) or 'none'}. Reason: {examiner_verdict.reason}",
        branch=evidence.branch, base_sha=evidence.base_sha, head_sha=evidence.new_sha,
        changed_files=evidence.changed_files, test_evidence=evidence, examiner_verdict=examiner_verdict,
    )
