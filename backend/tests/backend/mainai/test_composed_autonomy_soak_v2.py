"""Composed autonomy soak v2 — longer multi-tick duration on the #187 baseline.

Extends #187 without redesigning it:

* 4 dependent provider-assisted tasks on the shared goal worktree
* transient provider rate-limit + Worker wake
* one denied out-of-scope plan
* fresh Worker instance mid-run (PROCESS MEMORY != AUTHORITY)
* truthful goal finalize + 3 later idle ticks

Local Operator effects after ACCEPTED plans are the local execution surface
(production_entry still plans via provider when spend is live). No hand
PlanCandidate, no task-status cheats, no test-side record_final_report.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select

from app.development_supervisor.production_entry import eligible_authorized_goals
from app.execution_envelopes import authorize_execution_scope, propose_execution_scope
from app.mainai_execution import planner
from app.mainai_execution.approval import grant_task_approval
from app.mainai_execution.planner import PlannedTaskSpec
from app.mainai_execution.provider_wait_wake import WAITING_PROVIDER_BACKOFF_BASE_SECONDS
from app.models.document import ActiveTruthStatus, Document, DocumentSource
from app.models.knowledge_claim import ClaimType, KnowledgeClaim
from app.models.mainai_execution import MainAICheckpoint, MainAIGoalStatus, MainAITask, MainAITaskStatus
from app.models.mainai_job import MainAIJob
from app.models.provider_disclosure import ProviderDisclosureEvent
from app.models.provider_spend import ProviderSpendAuthorization, ProviderSpendUsageEvent
from app.models.user import User
from app.project_entities import promote_interpretation_proposal, record_interpretation_proposal
from app.provider_planning.service import ProviderResponse
from app.provider_spend import authorize_provider_spend
from app.providers.base import ProviderError
from app.work_candidates import authorize_work_candidate, list_unreviewed_work_candidates
from app.worker import Worker
from tests.backend.mainai.test_composed_autonomy_milestone import (
    _OPERATOR_CAPABILITIES,
    _committed_multiply_candidate_payload,
    _divide_candidate_payload,
)
from tests.backend.mainai.test_composed_autonomy_soak import (
    _out_of_scope_candidate_payload,
    _SoakPlanningAdapter,
)

pytest_plugins = ["tests.backend.mainai.test_composed_autonomy_milestone"]


def _append_fn_payload(
    calculator: str,
    existing_test: str,
    *,
    fn_name: str,
    fn_body: str,
    test_fn: str,
    imports: list[str],
    commit_message: str,
) -> dict:
    updated = calculator + fn_body
    import_line = "from calculator import " + ", ".join(imports)
    # Preserve prior tests; append the new one.
    if existing_test.strip():
        test = existing_test.rstrip() + "\n\n" + test_fn + "\n"
        # Fix import line if present.
        lines = test.splitlines()
        if lines and lines[0].startswith("from calculator import"):
            lines[0] = import_line
            test = "\n".join(lines) + "\n"
    else:
        test = import_line + "\n\n" + test_fn + "\n"
    return {
        "interpretation": f"Add {fn_name} helper and verify.",
        "requested_outcome": f"Verified {fn_name} helper.",
        "rationale": "Patch exact hash, update focused test, commit.",
        "facts": [],
        "assumptions": [],
        "unknowns": [],
        "exclusions": ["No unrelated edits or remote operations."],
        "steps": [
            {
                "step_id": "inspect",
                "purpose": "inspect current calculator",
                "expected_result": "bounded source text",
                "capability": "read_file",
                "arguments": {"path": "calculator.py"},
            },
            {
                "step_id": "patch",
                "purpose": f"add {fn_name} helper",
                "expected_result": "new source hash",
                "capability": "patch_file",
                "arguments": {
                    "path": "calculator.py",
                    "content": updated,
                    "expected_sha256": hashlib.sha256(calculator.encode()).hexdigest(),
                },
                "depends_on": ["inspect"],
                "required_risk": "LOCAL_WRITE",
            },
            {
                "step_id": "test-file",
                "purpose": f"update focused test for {fn_name}",
                "expected_result": "test file hash",
                "capability": "patch_file",
                "arguments": {
                    "path": "test_calculator.py",
                    "content": test,
                    "expected_sha256": hashlib.sha256(existing_test.encode()).hexdigest(),
                },
                "depends_on": ["patch"],
                "required_risk": "LOCAL_WRITE",
            },
            {
                "step_id": "test",
                "purpose": "verify helpers",
                "expected_result": "pytest exit zero",
                "capability": "run_focused_test",
                "arguments": {
                    "profile_name": "focused_pytest",
                    "arguments": ["test_calculator.py"],
                },
                "depends_on": ["test-file"],
                "required_risk": "LOCAL_EXECUTION",
                "verification_required": True,
            },
            {
                "step_id": "gate",
                "purpose": "evaluate deterministic evidence",
                "expected_result": "verification checkpoint",
                "capability": "verification_evaluate",
                "arguments": {},
                "depends_on": ["test"],
            },
            {
                "step_id": "stage",
                "purpose": "stage helper and test",
                "expected_result": "staged diff",
                "capability": "stage_scoped_changes",
                "arguments": {"paths": ["calculator.py", "test_calculator.py"]},
                "depends_on": ["gate"],
                "required_risk": "LOCAL_WRITE",
            },
            {
                "step_id": "commit",
                "purpose": f"commit {fn_name} helper",
                "expected_result": "commit sha",
                "capability": "commit_scoped_changes",
                "arguments": {"message": commit_message},
                "depends_on": ["stage"],
                "required_risk": "LOCAL_WRITE",
            },
        ],
    }


async def _bootstrap_four_task_goal(db):
    user = User(
        email=f"soak-v2-{uuid.uuid4()}@example.com",
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
        claim_text=(
            "Lägg till multiply, divide, subtract och power i calculator.py "
            "som fyra sekventiella tasks."
        ),
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
        idempotency_key=f"soak-v2-proposal-{uuid.uuid4()}",
        classifier_strategy="claim_type_extraction_v1",
        classifier_confidence="certain",
    )
    db.commit()
    promote_interpretation_proposal(
        db,
        owner_id=user.id,
        proposal_id=proposal.id,
        entity_type="decision",
        title="Fyra sekventiella calculator-helpers",
        authority="founder",
        basis="manual",
        entity_idempotency_key=f"soak-v2-entity-{uuid.uuid4()}",
    )
    db.commit()

    candidates = list_unreviewed_work_candidates(db, owner_id=user.id)
    assert len(candidates) == 1
    _, goal = authorize_work_candidate(
        db,
        owner_id=user.id,
        candidate_id=candidates[0].id,
        authorized_by="founder",
        approval_policy="autonomous_development_work",
    )
    db.commit()

    specs = [
        PlannedTaskSpec(
            description="add multiply helper and focused test",
            task_type="repo_edit",
            risk_level="low",
        ),
        PlannedTaskSpec(
            description="add divide helper and update focused test",
            task_type="repo_edit",
            risk_level="low",
            depends_on=[0],
        ),
        PlannedTaskSpec(
            description="add subtract helper and update focused test",
            task_type="repo_edit",
            risk_level="low",
            depends_on=[1],
        ),
        PlannedTaskSpec(
            description="add power helper and update focused test",
            task_type="repo_edit",
            risk_level="low",
            depends_on=[2],
        ),
    ]
    planner.create_plan(
        db,
        goal=goal,
        rationale="four sequential local edits on shared goal worktree",
        tasks=specs,
        created_by="founder",
    )
    tasks = (
        db.execute(
            select(MainAITask)
            .where(MainAITask.goal_id == goal.id)
            .order_by(MainAITask.created_at.asc(), MainAITask.id.asc())
        )
        .scalars()
        .all()
    )
    assert len(tasks) == 4
    for task in tasks:
        grant_task_approval(db, task=task, approved_by="founder")
    proposal_scope = propose_execution_scope(
        db,
        owner_id=user.id,
        goal_id=goal.id,
        idempotency_key=f"soak-v2-scope-{uuid.uuid4()}",
    )
    _, envelope = authorize_execution_scope(
        db,
        owner_id=user.id,
        proposal_id=proposal_scope.id,
        authorized_by="founder",
        authorized_paths=["calculator.py", "test_calculator.py"],
        authorized_capabilities=_OPERATOR_CAPABILITIES,
        authorized_risk="low",
        envelope_idempotency_key=f"soak-v2-env-{uuid.uuid4()}",
    )
    db.commit()
    return user, goal, envelope, tasks


def _worktree_sources():
    import app.development_supervisor.production_worktree as wt_module

    calc_path = list(Path(wt_module.WORKTREE_ROOT).rglob("calculator.py"))[0]
    test_path = calc_path.parent / "test_calculator.py"
    return (
        calc_path,
        calc_path.read_text(encoding="utf-8"),
        test_path.read_text(encoding="utf-8") if test_path.exists() else "",
    )


def _provider_response(payload: dict, version: str) -> ProviderResponse:
    return ProviderResponse(
        content=json.dumps(
            {
                "candidate": payload,
                "clarification_required": False,
                "clarification_question": None,
                "capability_gaps": [],
                "useful_components": [],
                "confidence": 0.9,
            }
        ),
        provider="fake-local",
        model="planner-v2",
        model_version=version,
        raw_usage={"prompt_tokens": 40, "completion_tokens": 30},
    )


@pytest.mark.asyncio
async def test_composed_autonomy_soak_v2_multi_tick_and_fresh_worker(
    superuser_db, source_repo, monkeypatch
):
    import app.mainai_execution.provider_wait_wake as wake_module

    user, goal, envelope, tasks = await _bootstrap_four_task_goal(superuser_db)
    t0, t1, t2, t3 = tasks
    calculator = (source_repo / "calculator.py").read_text(encoding="utf-8")
    monkeypatch.setattr(wake_module, "WAITING_PROVIDER_BACKOFF_BASE_SECONDS", 0.0)
    assert WAITING_PROVIDER_BACKOFF_BASE_SECONDS == 30.0

    def _divide_builder():
        _, current, test = _worktree_sources()
        assert "multiply" in current
        return _provider_response(_divide_candidate_payload(current, test), "soak-v2-b")

    def _subtract_builder():
        _, current, test = _worktree_sources()
        assert "divide" in current
        return _provider_response(
            _append_fn_payload(
                current,
                test,
                fn_name="subtract",
                fn_body="\ndef subtract(left, right):\n    return left - right\n",
                test_fn="def test_subtract():\n    assert subtract(9, 4) == 5",
                imports=["multiply", "divide", "subtract"],
                commit_message="Add subtract helper",
            ),
            "soak-v2-c",
        )

    def _power_builder():
        _, current, test = _worktree_sources()
        assert "subtract" in current
        return _provider_response(
            _append_fn_payload(
                current,
                test,
                fn_name="power",
                fn_body="\ndef power(left, right):\n    return left ** right\n",
                test_fn="def test_power():\n    assert power(2, 3) == 8",
                imports=["multiply", "divide", "subtract", "power"],
                commit_message="Add power helper",
            ),
            "soak-v2-d",
        )

    adapter = _SoakPlanningAdapter(
        [
            ProviderError(
                "transient soak-v2 outage",
                category="rate_limited",
                provider_request_may_have_left=False,
            ),
            _provider_response(_out_of_scope_candidate_payload(), "soak-v2-deny"),
            _provider_response(
                _committed_multiply_candidate_payload(calculator), "soak-v2-a"
            ),
            _divide_builder,
            _subtract_builder,
            _power_builder,
        ]
    )

    class _Registry:
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
        _Registry,
    )

    worker_a = Worker()
    worker_a.worker_id = "soak-v2-worker-a"

    await worker_a._advance_authorized_supervisor_goals(superuser_db)
    superuser_db.commit()
    superuser_db.refresh(t0)
    assert t0.status == MainAITaskStatus.blocked
    assert len(adapter.calls) == 0

    authorize_provider_spend(
        superuser_db,
        owner_id=user.id,
        goal_id=goal.id,
        execution_envelope_id=envelope.id,
        authorized_by="founder",
        max_cost_usd=Decimal("10.00"),
        max_requests=12,
        max_cost_per_request_usd=Decimal("0.50"),
        idempotency_key=f"soak-v2-spend-{uuid.uuid4()}",
        allowed_providers=["fake-local"],
        allowed_models=["planner-v2"],
    )
    superuser_db.commit()

    await worker_a._advance_authorized_supervisor_goals(superuser_db)
    superuser_db.commit()
    superuser_db.refresh(t0)
    assert t0.status == MainAITaskStatus.blocked
    assert t0.next_retry_at is not None
    assert len(adapter.calls) == 1

    worker_a._advance_waiting_provider_backoff(superuser_db)
    superuser_db.commit()

    # Deny out-of-scope, then recover through remaining ticks.
    for _ in range(12):
        superuser_db.refresh(t0)
        if t0.status == MainAITaskStatus.completed:
            break
        if t0.status == MainAITaskStatus.blocked and t0.next_retry_at is not None:
            worker_a._advance_waiting_provider_backoff(superuser_db)
            superuser_db.commit()
        await worker_a._advance_authorized_supervisor_goals(superuser_db)
        worker_a._finalize_mainai_execution_goals(superuser_db)
        superuser_db.commit()

    superuser_db.refresh(t0)
    superuser_db.refresh(t1)
    assert t0.status == MainAITaskStatus.completed
    assert t1.status == MainAITaskStatus.ready
    assert not list(
        Path(
            __import__(
                "app.development_supervisor.production_worktree", fromlist=["WORKTREE_ROOT"]
            ).WORKTREE_ROOT
        ).rglob("outside_envelope.py")
    )

    # PROCESS MEMORY != AUTHORITY: destroy worker_a; continue with a fresh instance.
    del worker_a
    worker_b = Worker()
    worker_b.worker_id = "soak-v2-worker-b"

    for _ in range(10):
        superuser_db.refresh(goal)
        if goal.status == MainAIGoalStatus.completed:
            break
        await worker_b._advance_authorized_supervisor_goals(superuser_db)
        worker_b._finalize_mainai_execution_goals(superuser_db)
        superuser_db.commit()

    for task in tasks:
        superuser_db.refresh(task)
        assert task.status == MainAITaskStatus.completed
    superuser_db.refresh(goal)
    assert goal.status == MainAIGoalStatus.completed
    assert goal.final_outcome is not None

    calc_path, final, _ = _worktree_sources()
    assert all(name in final for name in ("multiply", "divide", "subtract", "power"))
    assert not (calc_path.parent / "outside_envelope.py").exists()

    # rate_limit + deny + 4 successful plans
    assert len(adapter.calls) == 6

    auth = superuser_db.execute(
        select(ProviderSpendAuthorization).where(
            ProviderSpendAuthorization.owner_id == user.id,
            ProviderSpendAuthorization.status == "active",
        )
    ).scalar_one()
    assert auth.spent_requests <= auth.max_requests
    assert auth.reserved_requests == 0
    assert (
        superuser_db.execute(
            select(ProviderDisclosureEvent).where(
                ProviderDisclosureEvent.owner_id == user.id
            )
        )
        .scalars()
        .all()
    )

    calls_before = len(adapter.calls)
    jobs_before = len(
        superuser_db.execute(select(MainAIJob).where(MainAIJob.owner_id == user.id))
        .scalars()
        .all()
    )
    for _ in range(3):
        await worker_b._advance_authorized_supervisor_goals(superuser_db)
        worker_b._finalize_mainai_execution_goals(superuser_db)
        superuser_db.commit()
    assert len(adapter.calls) == calls_before
    assert (
        len(
            superuser_db.execute(select(MainAIJob).where(MainAIJob.owner_id == user.id))
            .scalars()
            .all()
        )
        == jobs_before
    )
    assert [
        g.id
        for g, _ in eligible_authorized_goals(superuser_db, limit=50)
        if g.owner_id == user.id
    ] == []
