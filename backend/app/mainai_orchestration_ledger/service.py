"""Persistence for the orchestration truth ledger.

Writes occupancy and GitHub snapshots. Does not grant merge, deploy, Recall, or RLS
authority. Agent claims are inserted as non-authoritative rows.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy.orm import Session

from app.mainai_orchestration_ledger.claims import ingest_agent_claim
from app.mainai_orchestration_ledger.types import AgentClaim, GitHubTruthSnapshot, TaskRecord
from app.models.orchestration_ledger import (
    OrchestrationAgent,
    OrchestrationClaim,
    OrchestrationGitHubSnapshot,
    OrchestrationSlot,
    OrchestrationTask,
    OrchestrationTaskDependency,
)


class OrchestrationLedgerError(ValueError):
    pass


def persist_github_snapshot(
    db: Session,
    *,
    owner_id: UUID,
    snapshot: GitHubTruthSnapshot,
    task_id: UUID | None = None,
) -> OrchestrationGitHubSnapshot:
    if snapshot.source != "github":
        raise OrchestrationLedgerError("snapshots must come from GitHub, not agent text")
    row = OrchestrationGitHubSnapshot(
        owner_id=owner_id,
        task_id=task_id,
        branch=snapshot.branch,
        exists_remotely=snapshot.exists_remotely,
        commit_sha=snapshot.commit_sha,
        tree_sha=snapshot.tree_sha,
        local_sha=snapshot.local_sha,
        local_matches_remote=snapshot.local_matches_remote,
        ci_payload={
            "check_runs": [
                {
                    "name": run.name,
                    "status": run.status,
                    "conclusion": run.conclusion,
                    "head_sha": run.head_sha,
                }
                for run in snapshot.check_runs
            ]
        },
        pr_payload={
            "pull_requests": [
                {
                    "number": pr.number,
                    "state": pr.state,
                    "merged": pr.merged,
                    "head_sha": pr.head_sha,
                    "head_ref": pr.head_ref,
                    "base_ref": pr.base_ref,
                }
                for pr in snapshot.pull_requests
            ]
        },
        default_branch=snapshot.default_branch,
        default_branch_sha=snapshot.default_branch_sha,
        deployments_payload=[
            {
                "id": item.id,
                "sha": item.sha,
                "environment": item.environment,
                "state": item.state,
            }
            for item in snapshot.deployments
        ],
        captured_at=snapshot.captured_at or datetime.now(timezone.utc),
        source="github",
    )
    db.add(row)
    db.flush()
    return row


def persist_claim(db: Session, *, owner_id: UUID, task: TaskRecord, claim: AgentClaim) -> OrchestrationClaim:
    ingested = ingest_agent_claim(task, claim)
    task_uuid = _as_uuid(ingested.claim.task_id)
    row = OrchestrationClaim(
        owner_id=owner_id,
        task_id=task_uuid,
        agent_key=ingested.claim.agent_key,
        claim_kind=ingested.claim.kind.value,
        claimed_value=ingested.claim.claimed_value,
        raw_text=ingested.claim.raw_text,
        bound_sha=ingested.claim.bound_sha,
        authoritative=False,
        recorded_at=ingested.claim.recorded_at or datetime.now(timezone.utc),
    )
    db.add(row)
    db.flush()
    return row


def apply_snapshot_to_task_row(task: OrchestrationTask, snapshot: GitHubTruthSnapshot) -> None:
    if snapshot.source != "github":
        raise OrchestrationLedgerError("only GitHub may set remote_pushed")
    task.remote_sha = snapshot.commit_sha if snapshot.exists_remotely else None
    task.remote_pushed = bool(snapshot.exists_remotely and snapshot.commit_sha)
    task.last_verified_source = "github"
    task.last_verified_at = snapshot.captured_at or datetime.now(timezone.utc)


def _as_uuid(value: object) -> UUID | None:
    if isinstance(value, UUID):
        return value
    if not isinstance(value, str):
        return None
    try:
        return UUID(value)
    except ValueError:
        return None


# Imported by models package consumers; keep names discoverable.
__all__ = [
    "OrchestrationAgent",
    "OrchestrationClaim",
    "OrchestrationGitHubSnapshot",
    "OrchestrationLedgerError",
    "OrchestrationSlot",
    "OrchestrationTask",
    "OrchestrationTaskDependency",
    "apply_snapshot_to_task_row",
    "persist_claim",
    "persist_github_snapshot",
]
