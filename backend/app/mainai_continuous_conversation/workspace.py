"""Workspace ownership: SHA sharing allowed, workspace sharing forbidden.

CURSOR builder worktree != CLAUDE examiner worktree. A branch under examination
must not be a mutable builder workspace. Occupancy of a worktree is exclusive.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.mainai_continuous_conversation.types import WorkspaceMutability
from app.models.continuous_conversation import FounderWorkspaceLease

CURSOR_BUILDER_WORKTREE = "/home/ubuntu/cursor-builder-worktrees/mainai-continuous-conversation-p1-fix"
CLAUDE_EXAMINER_WORKTREE = "/home/ubuntu/worktrees/mainai-continuous-conversation-foundation"


class WorkspaceOwnershipError(ValueError):
    pass


@dataclass(frozen=True)
class WorkspaceClaim:
    owner_id: UUID
    agent_key: str
    branch: str
    worktree_path: str
    mutability: WorkspaceMutability
    task_id: UUID | None = None
    execution_id: UUID | None = None
    shared_sha: str | None = None


def sha_sharing_allowed(left_sha: str | None, right_sha: str | None) -> bool:
    return bool(left_sha and right_sha and left_sha == right_sha)


def workspaces_are_isolated(builder_path: str, examiner_path: str) -> bool:
    return builder_path.rstrip("/") != examiner_path.rstrip("/")


def validate_claim(existing: list[FounderWorkspaceLease], claim: WorkspaceClaim) -> None:
    for row in existing:
        if row.worktree_path == claim.worktree_path and (
            row.agent_key != claim.agent_key or row.mutability != claim.mutability.value
        ):
            raise WorkspaceOwnershipError("workspace sharing forbidden: worktree already owned")
        same_branch = row.branch == claim.branch
        if not same_branch:
            continue
        row_mut = WorkspaceMutability(row.mutability)
        if row_mut is WorkspaceMutability.READ_ONLY_EXAMINER and claim.mutability is WorkspaceMutability.MUTABLE_BUILDER:
            raise WorkspaceOwnershipError(
                f"workspace sharing forbidden: branch {claim.branch} is under examination"
            )
        if row_mut is WorkspaceMutability.MUTABLE_BUILDER and claim.mutability is WorkspaceMutability.MUTABLE_BUILDER:
            if row.worktree_path != claim.worktree_path:
                raise WorkspaceOwnershipError(
                    f"workspace sharing forbidden: branch {claim.branch} already has a mutable builder"
                )
        if row_mut is WorkspaceMutability.MUTABLE_BUILDER and claim.mutability is WorkspaceMutability.READ_ONLY_EXAMINER:
            raise WorkspaceOwnershipError(
                f"workspace sharing forbidden: branch {claim.branch} is a mutable builder workspace"
            )


def claim_workspace(db: Session, claim: WorkspaceClaim) -> FounderWorkspaceLease:
    existing = (
        db.query(FounderWorkspaceLease)
        .filter(FounderWorkspaceLease.owner_id == claim.owner_id)
        .all()
    )
    validate_claim(existing, claim)
    row = FounderWorkspaceLease(
        id=uuid4(),
        owner_id=claim.owner_id,
        agent_key=claim.agent_key,
        branch=claim.branch,
        task_id=claim.task_id,
        execution_id=claim.execution_id,
        worktree_path=claim.worktree_path,
        mutability=claim.mutability.value,
        shared_sha=claim.shared_sha,
        created_at=datetime.now(timezone.utc),
    )
    db.add(row)
    db.flush()
    return row
