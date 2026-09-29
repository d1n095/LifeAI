"""Agent claims are evidence. They never authoritatively mutate GitHub-backed task fields.

Codex saying "branch pushed" is stored. `remote_pushed` stays false until GitHub confirms.
Claude saying "PASS" is examiner evidence; certification still binds to the reviewed SHA.
Cursor saying "2883 tests passed" is a claim until pytest execution evidence names the SHA.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any

from app.mainai_orchestration_ledger.types import (
    AGENT_CLAIM_SOURCE,
    GITHUB_BACKED_FIELDS,
    AgentClaim,
    ClaimKind,
    GitHubTruthSnapshot,
    TaskRecord,
    PytestRunEvidence,
)


@dataclass(frozen=True)
class ClaimIngestResult:
    claim: AgentClaim
    task: TaskRecord
    github_fields_unchanged: bool
    note: str


def ingest_agent_claim(task: TaskRecord, claim: AgentClaim) -> ClaimIngestResult:
    if claim.authoritative:
        raise ValueError("agent claims cannot be marked authoritative")
    stored = AgentClaim(
        agent_key=claim.agent_key,
        kind=claim.kind,
        raw_text=claim.raw_text,
        claimed_value=dict(claim.claimed_value),
        task_id=claim.task_id or task.task_id,
        bound_sha=claim.bound_sha,
        recorded_at=claim.recorded_at or datetime.now(timezone.utc),
        authoritative=False,
    )
    return ClaimIngestResult(
        claim=stored,
        task=task,
        github_fields_unchanged=True,
        note=(
            "stored as agent_claim evidence only; GitHub-backed fields "
            f"{sorted(GITHUB_BACKED_FIELDS)} were not mutated"
        ),
    )


def apply_github_snapshot(task: TaskRecord, snapshot: GitHubTruthSnapshot) -> TaskRecord:
    if snapshot.source != "github":
        raise ValueError("only a GitHub snapshot may set GitHub-backed task fields")
    remote_sha = snapshot.commit_sha if snapshot.exists_remotely else None
    return replace(
        task,
        remote_sha=remote_sha,
        remote_pushed=bool(snapshot.exists_remotely and remote_sha),
        last_verified_source="github",
        last_verified_at=snapshot.captured_at or datetime.now(timezone.utc),
    )


def bind_test_run(task: TaskRecord, evidence: PytestRunEvidence) -> TaskRecord:
    payload: dict[str, Any] = {
        "sha": evidence.sha,
        "tree_sha": evidence.tree_sha,
        "passed": evidence.passed,
        "failed": evidence.failed,
        "skipped": evidence.skipped,
        "command": evidence.command,
        "source": evidence.source,
        "executed_at": evidence.executed_at.isoformat() if evidence.executed_at else None,
    }
    return replace(
        task,
        test_runs=[*task.test_runs, payload],
        last_verified_source="pytest_execution",
        last_verified_at=evidence.executed_at or datetime.now(timezone.utc),
    )


def examiner_evidence_not_certification(claim: AgentClaim, reviewed_sha: str) -> dict[str, Any]:
    """Claude/examiner PASS is evidence. Certification still names the exact SHA."""

    if claim.kind is not ClaimKind.EXAMINER_RESULT:
        raise ValueError("examiner evidence requires ClaimKind.EXAMINER_RESULT")
    result = str(claim.claimed_value.get("result") or claim.raw_text)
    return {
        "source": AGENT_CLAIM_SOURCE,
        "examiner_agent": claim.agent_key,
        "claimed_result": result,
        "reviewed_sha": reviewed_sha,
        "certified": False,
        "reason": "examiner text is evidence; certification binds to the exact reviewed SHA via the verification registry",
    }


def claim_cannot_set_github_field(field_name: str) -> bool:
    return field_name in GITHUB_BACKED_FIELDS
