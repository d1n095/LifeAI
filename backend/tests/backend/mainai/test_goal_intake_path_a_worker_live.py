"""Stage 5 — Path A goal intake / bootstrap production path (Worker → Supervisor).

Proves the real document-derived spine end-to-end:

    Document / KnowledgeClaim
    → interpretation proposal → promote
    → WorkCandidate → authorize_work_candidate (creates MainAIGoal)
    → Path A auto execution-scope proposal (work_candidate_authorization_v1)
    → create_plan → grant_task_approval
    → founder authorize_execution_scope (paths supplied; capabilities from proposal)
    → authorize_provider_spend
    → Worker ticks → task + goal completed

Path B (direct create_goal + founder propose-scope bridge) is owned by Claude PR #197
and is intentionally NOT duplicated here.

Fake provider adapter is the sole fake boundary. No harness PlanCandidate / task.status
mutation / record_final_report bridges. Reuses calculator helpers from the composed
autonomy milestone soak path.
"""

from __future__ import annotations

import json
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select

from app.development_supervisor.production_entry import eligible_authorized_goals
from app.execution_envelopes import (
    authorize_execution_scope,
    list_unreviewed_execution_scope_proposals,
)
from app.mainai_execution import planner
from app.mainai_execution.approval import grant_task_approval
from app.mainai_execution.planner import PlannedTaskSpec, get_goal
from app.models.document import ActiveTruthStatus, Document, DocumentSource
from app.models.knowledge_claim import ClaimType, KnowledgeClaim
from app.models.mainai_execution import (
    MainAICheckpoint,
    MainAIGoalStatus,
    MainAITask,
    MainAITaskStatus,
)
from app.models.user import User
from app.models.work_candidate import WorkCandidate
from app.project_entities import promote_interpretation_proposal, record_interpretation_proposal
from app.provider_planning.service import ProviderResponse
from app.provider_spend import authorize_provider_spend, provider_spend_is_live
from app.work_candidates import authorize_work_candidate, list_unreviewed_work_candidates
from app.worker import Worker
from tests.backend.mainai.test_composed_autonomy_milestone import _OPERATOR_CAPABILITIES
from tests.backend.mainai.test_provider_assisted_planning import (
    FakePlanningAdapter,
    _candidate_payload,
)

# Reuse production-shaped worktree / source_repo fixtures from the milestone module.
pytest_plugins = ["tests.backend.mainai.test_composed_autonomy_milestone"]


async def _bootstrap_path_a_goal(db):
    """Founder bootstrap only — real Path A spine, no hand create_goal."""
    user = User(
        email=f"path-a-intake-{uuid.uuid4()}@example.com",
        password_hash="x",
        email_verified=True,
    )
    db.add(user)
    db.flush()
    document = Document(
        title="Källa",
        source=DocumentSource.upload,
        uploaded_by=user.id,
        active_truth_status=ActiveTruthStatus.active,
    )
    db.add(document)
    db.flush()
    claim = KnowledgeClaim(
        owner_id=user.id,
        source_id=document.id,
        claim_text="Lägg till en lokal multiply-hjälpare i calculator.py och verifiera den.",
        extraction_version="v1",
        claim_type=ClaimType.decision,
    )
    db.add(claim)
    db.flush()
    db.commit()

    proposal = record_interpretation_proposal(
        db,
        owner_id=user.id,
        source_claim_id=claim.id,
        proposed_entity_type="decision",
        idempotency_key=f"path-a-proposal-{uuid.uuid4()}",
        classifier_strategy="claim_type_extraction_v1",
        classifier_confidence="certain",
    )
    db.commit()
    promote_interpretation_proposal(
        db,
        owner_id=user.id,
        proposal_id=proposal.id,
        entity_type="decision",
        title="Lokal multiply-hjälpare i calculator.py",
        authority="founder",
        basis="manual",
        entity_idempotency_key=f"path-a-entity-{uuid.uuid4()}",
    )
    db.commit()

    candidates = list_unreviewed_work_candidates(db, owner_id=user.id)
    assert len(candidates) == 1
    authorized_wc, goal = authorize_work_candidate(
        db,
        owner_id=user.id,
        candidate_id=candidates[0].id,
        authorized_by="founder",
        approval_policy="autonomous_development_work",
    )
    db.commit()

    # Path A invariant: WorkCandidate authorization — not bare create_goal alone.
    assert authorized_wc.status == "authorized"
    assert authorized_wc.authorized_goal_id == goal.id
    wc_row = db.execute(
        select(WorkCandidate).where(WorkCandidate.id == authorized_wc.id)
    ).scalar_one()
    assert wc_row.status == "authorized"
    assert wc_row.authorized_goal_id == goal.id
    assert get_goal(db, goal.id).approval_policy == "autonomous_development_work"

    # Path A auto-proposal from authorize_work_candidate (not a hand propose_execution_scope).
    path_a_proposals = [
        p
        for p in list_unreviewed_execution_scope_proposals(db, owner_id=user.id)
        if p.goal_id == goal.id
    ]
    assert len(path_a_proposals) == 1
    path_a_proposal = path_a_proposals[0]
    assert path_a_proposal.proposal_strategy == "work_candidate_authorization_v1"
    assert path_a_proposal.idempotency_key == (
        f"work-candidate-authorization:{authorized_wc.id}"
    )
    assert path_a_proposal.proposed_paths == []  # honest: no file signal yet

    planner.create_plan(
        db,
        goal=goal,
        rationale="single bounded local edit via Path A intake",
        tasks=[
            PlannedTaskSpec(
                description="add multiply helper and focused test",
                task_type="repo_edit",
                risk_level="low",
            )
        ],
        created_by="founder",
    )
    task = db.execute(select(MainAITask).where(MainAITask.goal_id == goal.id)).scalar_one()
    grant_task_approval(db, task=task, approved_by="founder")

    # Founder authorizes the Path A proposal; supplies paths (never proposed by Path A).
    _, envelope = authorize_execution_scope(
        db,
        owner_id=user.id,
        proposal_id=path_a_proposal.id,
        authorized_by="founder",
        authorized_paths=["calculator.py", "test_calculator.py"],
        authorized_capabilities=_OPERATOR_CAPABILITIES,
        authorized_risk="low",
        envelope_idempotency_key=f"path-a-env-{uuid.uuid4()}",
    )
    db.commit()
    return user, goal, envelope, task, authorized_wc


def _install_fake_registry_adapter(monkeypatch, adapter: FakePlanningAdapter):
    class _FakeRegistry:
        provider_name = "fake-local"
        model = "planner-v2"
        default_provider = "fake-local"
        default_model = "planner-v2"

        def __init__(self, db, *, provider_name=None, model=None):
            self.db = db

        async def propose(self, *args, **kwargs):
            return await adapter.propose(*args, **kwargs)

    monkeypatch.setattr(
        "app.provider_planning.service.RegistryPlanningAdapter",
        _FakeRegistry,
    )


@pytest.mark.asyncio
async def test_goal_intake_path_a_worker_live_to_goal_complete(
    superuser_db, source_repo, monkeypatch
):
    user, goal, envelope, task, authorized_wc = await _bootstrap_path_a_goal(superuser_db)

    # Envelope makes the goal eligible; spend is still absent → Worker must not execute.
    assert eligible_authorized_goals(superuser_db, limit=50)
    assert (
        provider_spend_is_live(
            superuser_db,
            owner_id=user.id,
            goal_id=goal.id,
            execution_envelope_id=envelope.id,
        )
        is False
    )

    calculator = (source_repo / "calculator.py").read_text(encoding="utf-8")
    adapter = FakePlanningAdapter(
        ProviderResponse(
            content=json.dumps(
                {
                    "candidate": _candidate_payload(calculator),
                    "clarification_required": False,
                    "clarification_question": None,
                    "capability_gaps": [],
                    "useful_components": [],
                    "confidence": 0.9,
                }
            ),
            provider="fake-local",
            model="planner-v2",
            model_version="path-a-intake",
            raw_usage={"prompt_tokens": 40, "completion_tokens": 30},
        )
    )
    _install_fake_registry_adapter(monkeypatch, adapter)

    worker = Worker()
    worker.worker_id = "goal-intake-path-a-worker"

    # Worker→Supervisor after envelope but BEFORE spend: park only, no provider, no edit.
    await worker._advance_authorized_supervisor_goals(superuser_db)
    superuser_db.commit()
    superuser_db.refresh(task)
    superuser_db.refresh(goal)
    assert task.status == MainAITaskStatus.blocked
    assert goal.status != MainAIGoalStatus.completed
    assert len(adapter.calls) == 0
    assert "multiply" not in (source_repo / "calculator.py").read_text(encoding="utf-8")

    authorize_provider_spend(
        superuser_db,
        owner_id=user.id,
        goal_id=goal.id,
        execution_envelope_id=envelope.id,
        authorized_by="founder",
        max_cost_usd=Decimal("2.00"),
        max_requests=5,
        max_cost_per_request_usd=Decimal("0.50"),
        idempotency_key=f"path-a-spend-{uuid.uuid4()}",
        allowed_providers=["fake-local"],
        allowed_models=["planner-v2"],
    )
    superuser_db.commit()
    superuser_db.refresh(task)
    assert task.status == MainAITaskStatus.ready
    assert provider_spend_is_live(
        superuser_db,
        owner_id=user.id,
        goal_id=goal.id,
        execution_envelope_id=envelope.id,
    )

    for _ in range(8):
        superuser_db.refresh(goal)
        superuser_db.refresh(task)
        if (
            task.status == MainAITaskStatus.completed
            and goal.status == MainAIGoalStatus.completed
        ):
            break
        await worker._advance_authorized_supervisor_goals(superuser_db)
        worker._finalize_mainai_execution_goals(superuser_db)
        superuser_db.commit()

    superuser_db.refresh(task)
    superuser_db.refresh(goal)
    superuser_db.refresh(authorized_wc)
    assert task.status == MainAITaskStatus.completed, [
        (
            row.executor_state.get("phase"),
            row.executor_state.get("classification"),
            row.executor_state,
        )
        for row in superuser_db.execute(
            select(MainAICheckpoint).where(MainAICheckpoint.goal_id == goal.id)
        ).scalars()
    ]
    assert goal.status == MainAIGoalStatus.completed
    assert goal.final_outcome is not None
    assert authorized_wc.status == "authorized"
    assert authorized_wc.authorized_goal_id == goal.id
    assert len(adapter.calls) == 1

    import app.development_supervisor.production_worktree as wt_module

    edited = list(Path(wt_module.WORKTREE_ROOT).rglob("calculator.py"))
    assert edited and "multiply" in edited[0].read_text(encoding="utf-8")

    phases = [
        row.executor_state.get("phase")
        for row in superuser_db.execute(
            select(MainAICheckpoint).where(MainAICheckpoint.goal_id == goal.id)
        ).scalars()
    ]
    assert "PROVIDER_SPEND_NOT_AUTHORIZED" in phases

    # Later tick: idle — no re-execution, no extra provider calls.
    calls_before = len(adapter.calls)
    await worker._advance_authorized_supervisor_goals(superuser_db)
    worker._finalize_mainai_execution_goals(superuser_db)
    superuser_db.commit()
    assert len(adapter.calls) == calls_before
    superuser_db.refresh(goal)
    assert goal.status == MainAIGoalStatus.completed
    assert [
        g.id
        for g, _ in eligible_authorized_goals(superuser_db, limit=50)
        if g.owner_id == user.id
    ] == []
