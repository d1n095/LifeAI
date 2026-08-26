"""Composed autonomy runtime probe — spend/remote_write remain false.

Proves the production chain is RUNTIME REACHABLE through:

    source claim → interpretation → project entity → WorkCandidate → founder authorize
    → MainAIGoal → plan/task → ExecutionAuthorizationEnvelope → eligible_authorized_goals
    → run_authorized_goal_supervisor_tick

and that the honest production stop under night-shift boundaries is
`PROVIDER_SPEND_NOT_AUTHORIZED` (not silent V0.1 fallback, not invented spend, not remote write).

Safe Planner ACCEPTED → operator write → verification → rollup remain out of reach on
production plain bindings until spend is founder-authorized or a durable deterministic
binding path is production-wired (B6 gap-child recipes are a separate merge).
"""

from __future__ import annotations

import subprocess
import uuid

import pytest
from sqlalchemy import select

from app.development_supervisor.production_entry import (
    eligible_authorized_goals,
    run_authorized_goal_supervisor_tick,
)
from app.execution_envelopes import authorize_execution_scope, propose_execution_scope
from app.mainai_execution import planner
from app.mainai_execution.planner import PlannedTaskSpec, get_goal
from app.models.document import ActiveTruthStatus, Document, DocumentSource
from app.models.knowledge_claim import ClaimType, KnowledgeClaim
from app.models.mainai_execution import MainAICheckpoint, MainAIGoalStatus, MainAITask, MainAITaskStatus
from app.models.user import User
from app.project_entities import promote_interpretation_proposal, record_interpretation_proposal
from app.work_candidates import authorize_work_candidate, list_unreviewed_work_candidates


def _git(cwd, *args):
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture(autouse=True)
def _isolate_worktree_root(tmp_path, monkeypatch):
    import app.development_supervisor.production_worktree as module

    monkeypatch.setattr(module, "WORKTREE_ROOT", tmp_path / "supervisor-goal-worktrees")


@pytest.fixture
def source_repo(tmp_path, monkeypatch):
    import app.development_supervisor.production_entry as entry_module

    repo = tmp_path / "worker-source-repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "worker@test.local")
    _git(repo, "config", "user.name", "Worker")
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "seed")
    monkeypatch.setattr(entry_module, "worker_source_repo_root", lambda: repo)
    return repo


_OPERATOR_CAPABILITIES = [
    "read_file",
    "patch_file",
    "run_focused_test",
    "stage_scoped_changes",
    "commit_scoped_changes",
]


@pytest.mark.asyncio
async def test_composed_claim_to_supervisor_tick_stops_honestly_without_spend(
    superuser_db, source_repo
):
    # --- cognition chain (Claude-owned surface; Cursor only composes through it) ---
    user = User(
        email=f"composed-{uuid.uuid4()}@example.com",
        password_hash="x",
        email_verified=True,
    )
    superuser_db.add(user)
    superuser_db.flush()
    document = Document(
        title="Källa",
        source=DocumentSource.upload,
        uploaded_by=user.id,
        active_truth_status=ActiveTruthStatus.active,
    )
    superuser_db.add(document)
    superuser_db.flush()
    claim = KnowledgeClaim(
        owner_id=user.id,
        source_id=document.id,
        claim_text="Dokumentera README med en tydlig lokal fix innan Q1.",
        extraction_version="v1",
        claim_type=ClaimType.decision,
    )
    superuser_db.add(claim)
    superuser_db.flush()
    superuser_db.commit()

    proposal = record_interpretation_proposal(
        superuser_db,
        owner_id=user.id,
        source_claim_id=claim.id,
        proposed_entity_type="decision",
        idempotency_key=f"composed-proposal-{uuid.uuid4()}",
        classifier_strategy="claim_type_extraction_v1",
        classifier_confidence="certain",
    )
    superuser_db.commit()

    _, entity = promote_interpretation_proposal(
        superuser_db,
        owner_id=user.id,
        proposal_id=proposal.id,
        entity_type="decision",
        title="Dokumentera README med en tydlig lokal fix innan Q1.",
        authority="founder",
        basis="manual",
        entity_idempotency_key=f"composed-entity-{uuid.uuid4()}",
    )
    superuser_db.commit()
    assert entity.derived_from_claim_id == claim.id

    candidates = list_unreviewed_work_candidates(superuser_db, owner_id=user.id)
    assert len(candidates) == 1
    wc = candidates[0]

    authorized_wc, goal = authorize_work_candidate(
        superuser_db,
        owner_id=user.id,
        candidate_id=wc.id,
        authorized_by="founder",
    )
    superuser_db.commit()
    assert authorized_wc.status == "authorized"
    assert authorized_wc.authorized_goal_id == goal.id
    assert get_goal(superuser_db, goal.id).owner_id == user.id

    # --- plan + envelope (founder execution authority) ---
    planner.create_plan(
        superuser_db,
        goal=goal,
        rationale="single bounded local edit",
        tasks=[
            PlannedTaskSpec(
                description="edit README.md",
                task_type="repo_edit",
                risk_level="low",
            )
        ],
        created_by="founder",
    )
    proposal_scope = propose_execution_scope(
        superuser_db,
        owner_id=user.id,
        goal_id=goal.id,
        idempotency_key=f"composed-scope-{uuid.uuid4()}",
    )
    _, envelope = authorize_execution_scope(
        superuser_db,
        owner_id=user.id,
        proposal_id=proposal_scope.id,
        authorized_by="founder",
        authorized_paths=["README.md"],
        authorized_capabilities=_OPERATOR_CAPABILITIES,
        authorized_risk="low",
        envelope_idempotency_key=f"composed-env-{uuid.uuid4()}",
    )
    superuser_db.commit()

    eligible = eligible_authorized_goals(superuser_db, limit=50)
    assert [g.id for g, e in eligible] == [goal.id]
    assert eligible[0][1].id == envelope.id
    assert goal.status == MainAIGoalStatus.running

    # --- production Supervisor tick under night-shift spend=false ---
    result = await run_authorized_goal_supervisor_tick(
        superuser_db,
        goal=goal,
        envelope=envelope,
        worker_id="composed-probe-worker",
    )
    superuser_db.commit()

    assert result is not None
    assert result.classification == "PROVIDER_SPEND_NOT_AUTHORIZED"

    spend_cps = [
        row
        for row in superuser_db.execute(
            select(MainAICheckpoint).where(MainAICheckpoint.goal_id == goal.id)
        ).scalars()
        if row.executor_state.get("phase") == "PROVIDER_SPEND_NOT_AUTHORIZED"
    ]
    assert spend_cps

    task = superuser_db.execute(
        select(MainAITask).where(MainAITask.goal_id == goal.id)
    ).scalar_one()
    # Spend denial parks durable blocked (wakeable after founder grant) — not dead-running.
    assert task.status == MainAITaskStatus.blocked
    assert task.blocker_reason == (
        "provider-assisted planning is not authorized for this scope"
    )
    assert goal.final_outcome is None

    # Boundaries: no invented spend, no remote write side effects on the seed repo.
    assert (source_repo / "README.md").read_text(encoding="utf-8") == "seed\n"
