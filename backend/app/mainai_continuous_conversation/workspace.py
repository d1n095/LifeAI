"""Workspace ownership: SHA sharing allowed, workspace sharing forbidden.

Workspace paths come from coordination/lease state, never from hard-coded builder or
examiner worktree literals. One mutable worktree = one agent owner.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.mainai_continuous_conversation.types import WorkspaceMutability
from app.models.continuous_conversation import FounderWorkspaceLease


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


def leases_for_owner(db: Session, *, owner_id: UUID) -> list[FounderWorkspaceLease]:
    return db.query(FounderWorkspaceLease).filter(FounderWorkspaceLease.owner_id == owner_id).all()


def workspace_for_agent(db: Session, *, owner_id: UUID, agent_key: str) -> FounderWorkspaceLease | None:
    return (
        db.query(FounderWorkspaceLease)
        .filter(FounderWorkspaceLease.owner_id == owner_id, FounderWorkspaceLease.agent_key == agent_key)
        .order_by(FounderWorkspaceLease.created_at.desc())
        .first()
    )


def mutable_builder_lease(db: Session, *, owner_id: UUID, branch: str | None = None) -> FounderWorkspaceLease | None:
    query = db.query(FounderWorkspaceLease).filter(
        FounderWorkspaceLease.owner_id == owner_id,
        FounderWorkspaceLease.mutability == WorkspaceMutability.MUTABLE_BUILDER.value,
    )
    if branch is not None:
        query = query.filter(FounderWorkspaceLease.branch == branch)
    return query.order_by(FounderWorkspaceLease.created_at.desc()).first()


def validate_claim(existing: list[FounderWorkspaceLease], claim: WorkspaceClaim) -> None:
    if not claim.worktree_path:
        raise WorkspaceOwnershipError("workspace path must come from coordination state")
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
    existing = leases_for_owner(db, owner_id=claim.owner_id)
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
