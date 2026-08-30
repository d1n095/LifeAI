"""Stage 3 — long autonomy soak: 8 tasks + gap/repair + restart + lease takeover.

Composes proofs from Stages 0E / 1 / 2 into one Worker-tick-only soak:

* 8 sequential calculator helpers on a shared goal worktree
* transient provider rate-limit + wake
* denied out-of-scope plan
* broken multiply → LifeProblem → repair child → founder grant → re-verify
* crash-hold lease (Worker A never releases) → Worker B ZERO progress
* real supervisor_goal_leases expiry → B reclaim → remaining helpers → goal complete
* 3 idle ticks: ZERO further filesystem mutation (bytes + mtime)

Forbidden harness bridges: status mutation, hand PlanCandidate after start, hand
repair task, record_final_report, run_driver as orchestration, shared Session across
restart.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import sessionmaker

from app.development_supervisor.lease import claim_supervisor_goal_lease
from app.development_supervisor.production_entry import eligible_authorized_goals
from app.execution_envelopes import authorize_execution_scope, propose_execution_scope
from app.mainai_execution import planner
from app.mainai_execution.approval import grant_task_approval
from app.mainai_execution.planner import PlannedTaskSpec
from app.mainai_execution.provider_wait_wake import WAITING_PROVIDER_BACKOFF_BASE_SECONDS
from app.models.document import ActiveTruthStatus, Document, DocumentSource
from app.models.knowledge_claim import ClaimType, KnowledgeClaim
from app.models.mainai_execution import MainAIGoal, MainAIGoalStatus, MainAITask, MainAITaskStatus
from app.models.mainai_job import MainAIJob
from app.models.problem_learning import LifeProblem
from app.models.provider_disclosure import ProviderDisclosureEvent
from app.models.provider_spend import ProviderSpendAuthorization
from app.models.user import User
from app.project_entities import promote_interpretation_proposal, record_interpretation_proposal
from app.provider_spend import authorize_provider_spend
from app.providers.base import ProviderError
from app.work_candidates import authorize_work_candidate, list_unreviewed_work_candidates
from app.worker import Worker
from tests.backend.mainai.test_autonomous_gap_live_integration import _repair_child
from tests.backend.mainai.test_autonomous_gap_worker_live_loop import (
    _broken_multiply_candidate_payload,
)
from tests.backend.mainai.test_composed_autonomy_milestone import (
    _OPERATOR_CAPABILITIES,
    _divide_candidate_payload,
)
from tests.backend.mainai.test_composed_autonomy_soak import (
    _SoakPlanningAdapter,
    _out_of_scope_candidate_payload,
)
from tests.backend.mainai.test_composed_autonomy_soak_v2 import (
    _append_fn_payload,
    _provider_response,
    _worktree_sources,
)

pytest_plugins = ["tests.backend.mainai.test_composed_autonomy_milestone"]

_HELPER_NAMES = (
    "multiply",
    "divide",
    "subtract",
    "power",
    "modulo",
    "absolute",
    "negate",
    "minimum",
)


async def _bootstrap_n_task_goal(db, n: int = 8):
    """Founder bootstrap for N sequential calculator helpers (does not mutate soak v2)."""
    if n != len(_HELPER_NAMES):
        raise ValueError(f"expected {len(_HELPER_NAMES)} helpers, got n={n}")
    user = User(
        email=f"soak-v4-{uuid.uuid4()}@example.com",
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
            "Lägg till multiply, divide, subtract, power, modulo, absolute, negate "
            "och minimum i calculator.py som åtta sekventiella tasks."
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
        idempotency_key=f"soak-v4-proposal-{uuid.uuid4()}",
        classifier_strategy="claim_type_extraction_v1",
        classifier_confidence="certain",
    )
    db.commit()
    promote_interpretation_proposal(
        db,
        owner_id=user.id,
        proposal_id=proposal.id,
        entity_type="decision",
        title="Åtta sekventiella calculator-helpers inklusive multiply",
        authority="founder",
        basis="manual",
        entity_idempotency_key=f"soak-v4-entity-{uuid.uuid4()}",
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

    # Spend-park + rate-limit + deny + broken multiply + repair re-verify need headroom
    # beyond the default max_attempts=3 (mark_completed commits mid-tick; exhausted
    # attempts leave source parked after repair without a durable resume).
    specs = [
        PlannedTaskSpec(
            description="add multiply helper and focused test",
            task_type="repo_edit",
            risk_level="low",
            max_attempts=12,
        )
    ]
    for index in range(1, n):
        specs.append(
            PlannedTaskSpec(
                description=(
                    f"add {_HELPER_NAMES[index]} helper and update focused test"
                ),
                task_type="repo_edit",
                risk_level="low",
                depends_on=[index - 1],
                max_attempts=12,
            )
        )

    planner.create_plan(
        db,
        goal=goal,
        rationale="eight sequential local edits on shared goal worktree",
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
    assert len(tasks) == n
    for task in tasks:
        grant_task_approval(db, task=task, approved_by="founder")
    proposal_scope = propose_execution_scope(
        db,
        owner_id=user.id,
        goal_id=goal.id,
        idempotency_key=f"soak-v4-scope-{uuid.uuid4()}",
    )
    _, envelope = authorize_execution_scope(
        db,
        owner_id=user.id,
        proposal_id=proposal_scope.id,
        authorized_by="founder",
        authorized_paths=["calculator.py", "test_calculator.py"],
        authorized_capabilities=_OPERATOR_CAPABILITIES,
        authorized_risk="low",
        envelope_idempotency_key=f"soak-v4-env-{uuid.uuid4()}",
    )
    db.commit()
    return user, goal, envelope, tasks


def _append_builder(fn_name: str, fn_body: str, test_fn: str, imports: list[str], version: str):
    def _builder():
        _, current, test = _worktree_sources()
        return _provider_response(
            _append_fn_payload(
                current,
                test,
                fn_name=fn_name,
                fn_body=fn_body,
                test_fn=test_fn,
                imports=imports,
                commit_message=f"Add {fn_name} helper",
            ),
            version,
        )

    return _builder


def _lease_row(db, goal_id):
    return (
        db.execute(
            text(
                "SELECT worker_id, lease_generation, status, expires_at < now() AS expired "
                "FROM supervisor_goal_leases WHERE goal_id = :gid "
                "ORDER BY acquired_at DESC NULLS LAST LIMIT 1"
            ),
            {"gid": str(goal_id)},
        )
        .mappings()
        .first()
    )


def _worktree_fs_snapshot():
    import app.development_supervisor.production_worktree as wt_module

    root = Path(wt_module.WORKTREE_ROOT)
    snapshot = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            snapshot[str(path.relative_to(root))] = (
                path.read_bytes(),
                path.stat().st_mtime_ns,
            )
    return snapshot


@pytest.mark.asyncio
async def test_composed_autonomy_soak_v4_long_autonomy_gap_restart_lease(
    superuser_db, source_repo, monkeypatch
):
    import app.development_supervisor.production_worktree as wt_module
    import app.mainai_execution.provider_wait_wake as wake_module

    bind = superuser_db.get_bind()
    SessionFactory = sessionmaker(bind=bind)
    session_a = SessionFactory()

    user, goal, envelope, tasks = await _bootstrap_n_task_goal(session_a, n=8)
    ids = {
        "user_id": user.id,
        "goal_id": goal.id,
        "envelope_id": envelope.id,
        "task_ids": [t.id for t in tasks],
    }
    calculator = (source_repo / "calculator.py").read_text(encoding="utf-8")
    monkeypatch.setattr(wake_module, "WAITING_PROVIDER_BACKOFF_BASE_SECONDS", 0.0)
    assert WAITING_PROVIDER_BACKOFF_BASE_SECONDS == 30.0

    def _divide_builder():
        _, current, test = _worktree_sources()
        assert "return left * right" in current
        return _provider_response(_divide_candidate_payload(current, test), "soak-v4-b")

    adapter = _SoakPlanningAdapter(
        [
            ProviderError(
                "transient soak-v4 outage",
                category="rate_limited",
                provider_request_may_have_left=False,
            ),
            _provider_response(_out_of_scope_candidate_payload(), "soak-v4-deny"),
            _provider_response(
                _broken_multiply_candidate_payload(calculator), "soak-v4-broken"
            ),
            _divide_builder,
            _append_builder(
                "subtract",
                "\ndef subtract(left, right):\n    return left - right\n",
                "def test_subtract():\n    assert subtract(9, 4) == 5",
                ["multiply", "divide", "subtract"],
                "soak-v4-c",
            ),
            _append_builder(
                "power",
                "\ndef power(left, right):\n    return left ** right\n",
                "def test_power():\n    assert power(2, 3) == 8",
                ["multiply", "divide", "subtract", "power"],
                "soak-v4-d",
            ),
            _append_builder(
                "modulo",
                "\ndef modulo(left, right):\n    return left % right\n",
                "def test_modulo():\n    assert modulo(10, 3) == 1",
                ["multiply", "divide", "subtract", "power", "modulo"],
                "soak-v4-e",
            ),
            _append_builder(
                "absolute",
                "\ndef absolute(value):\n    return abs(value)\n",
                "def test_absolute():\n    assert absolute(-5) == 5",
                ["multiply", "divide", "subtract", "power", "modulo", "absolute"],
                "soak-v4-f",
            ),
            _append_builder(
                "negate",
                "\ndef negate(value):\n    return -value\n",
                "def test_negate():\n    assert negate(4) == -4",
                [
                    "multiply",
                    "divide",
                    "subtract",
                    "power",
                    "modulo",
                    "absolute",
                    "negate",
                ],
                "soak-v4-g",
            ),
            _append_builder(
                "minimum",
                "\ndef minimum(left, right):\n    return left if left < right else right\n",
                "def test_minimum():\n    assert minimum(3, 8) == 3",
                list(_HELPER_NAMES),
                "soak-v4-h",
            ),
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
    worker_a.worker_id = "worker-a"

    t0 = tasks[0]
    await worker_a._advance_authorized_supervisor_goals(session_a)
    session_a.commit()
    session_a.refresh(t0)
    assert t0.status == MainAITaskStatus.blocked
    assert len(adapter.calls) == 0

    authorize_provider_spend(
        session_a,
        owner_id=ids["user_id"],
        goal_id=ids["goal_id"],
        execution_envelope_id=ids["envelope_id"],
        authorized_by="founder",
        max_cost_usd=Decimal("20.00"),
        max_requests=24,
        max_cost_per_request_usd=Decimal("0.50"),
        idempotency_key=f"soak-v4-spend-{uuid.uuid4()}",
        allowed_providers=["fake-local"],
        allowed_models=["planner-v2"],
    )
    session_a.commit()

    await worker_a._advance_authorized_supervisor_goals(session_a)
    session_a.commit()
    session_a.refresh(t0)
    assert t0.status == MainAITaskStatus.blocked
    assert t0.next_retry_at is not None
    assert len(adapter.calls) == 1

    worker_a._advance_waiting_provider_backoff(session_a)
    session_a.commit()

    # Transient → deny out-of-scope → broken multiply (gap + repair child).
    for _ in range(12):
        session_a.refresh(t0)
        problem = session_a.execute(
            select(LifeProblem).where(LifeProblem.mainai_task_id == ids["task_ids"][0])
        ).scalar_one_or_none()
        if problem is not None:
            break
        if t0.status == MainAITaskStatus.blocked and t0.next_retry_at is not None:
            worker_a._advance_waiting_provider_backoff(session_a)
            session_a.commit()
        await worker_a._advance_authorized_supervisor_goals(session_a)
        worker_a._finalize_mainai_execution_goals(session_a)
        session_a.commit()

    session_a.refresh(t0)
    goal_row = session_a.get(MainAIGoal, ids["goal_id"])
    problem = session_a.execute(
        select(LifeProblem).where(LifeProblem.mainai_task_id == ids["task_ids"][0])
    ).scalar_one_or_none()
    assert problem is not None, (
        f"expected LifeProblem after broken multiply; "
        f"t0={t0.status} calls={len(adapter.calls)} instruction={goal_row.original_instruction!r}"
    )
    child = _repair_child(session_a, goal_row, t0)
    assert t0.status == MainAITaskStatus.blocked
    assert child.status == MainAITaskStatus.ready

    # Founder edge: repair child's repo_edit needs explicit approval (not a harness cheat).
    await worker_a._advance_authorized_supervisor_goals(session_a)
    session_a.commit()
    grant_task_approval(session_a, task=child, approved_by="founder")
    session_a.commit()

    for _ in range(24):
        session_a.refresh(t0)
        session_a.refresh(child)
        if (
            t0.status == MainAITaskStatus.completed
            and child.status == MainAITaskStatus.completed
        ):
            break
        await worker_a._advance_authorized_supervisor_goals(session_a)
        worker_a._finalize_mainai_execution_goals(session_a)
        session_a.commit()

    session_a.refresh(t0)
    session_a.refresh(child)
    assert child.status == MainAITaskStatus.completed, (
        f"repair child stuck at {child.status}; t0={t0.status}; calls={len(adapter.calls)}"
    )
    assert t0.status == MainAITaskStatus.completed, (
        f"t0 stuck at {t0.status}; child={child.status}; calls={len(adapter.calls)}"
    )
    assert "return left * right" in _worktree_sources()[1]
    assert not list(Path(wt_module.WORKTREE_ROOT).rglob("outside_envelope.py"))
    ids["repair_child_id"] = child.id

    claimed = claim_supervisor_goal_lease(
        session_a,
        owner_id=ids["user_id"],
        goal_id=ids["goal_id"],
        envelope_id=ids["envelope_id"],
        worker_id=worker_a.worker_id,
        lease_seconds=300,
    )
    assert claimed is not None
    lease_id, held_generation = claimed
    session_a.commit()
    held = _lease_row(session_a, ids["goal_id"])
    assert held["worker_id"] == "worker-a"
    assert held["status"] == "active"
    assert held["expired"] is False
    assert int(held["lease_generation"]) == held_generation

    _, mid_fs, _ = _worktree_sources()
    assert "def divide" not in mid_fs
    calls_at_hold = len(adapter.calls)

    # Drop ALL Session A / Worker A / ORM authority objects. Only durable IDs + adapter survive.
    session_a.commit()
    session_a.close()
    del worker_a, session_a, user, goal, envelope, tasks, t0, child

    session_b = SessionFactory()
    worker_b = Worker()
    worker_b.worker_id = "worker-b"
    assert worker_b.worker_id != "worker-a"

    goal_b = session_b.get(MainAIGoal, ids["goal_id"])
    tasks_b = [session_b.get(MainAITask, tid) for tid in ids["task_ids"]]
    assert goal_b is not None and all(t is not None for t in tasks_b)
    assert tasks_b[0].status == MainAITaskStatus.completed

    # Worker B blocked while lease valid — ZERO progress.
    statuses_before = [t.status for t in tasks_b]
    await worker_b._advance_authorized_supervisor_goals(session_b)
    worker_b._finalize_mainai_execution_goals(session_b)
    session_b.commit()
    for task, before in zip(tasks_b, statuses_before, strict=True):
        session_b.refresh(task)
        assert task.status == before
    _, still_mid, _ = _worktree_sources()
    assert still_mid == mid_fs
    assert "def divide" not in still_mid
    assert len(adapter.calls) == calls_at_hold

    session_b.execute(
        text(
            "UPDATE supervisor_goal_leases SET expires_at = now() - interval '1 second' "
            "WHERE id = :lid"
        ),
        {"lid": str(lease_id)},
    )
    session_b.commit()
    expired = _lease_row(session_b, ids["goal_id"])
    assert expired["expired"] is True

    for _ in range(40):
        session_b.refresh(goal_b)
        if goal_b.status == MainAIGoalStatus.completed:
            break
        await worker_b._advance_authorized_supervisor_goals(session_b)
        worker_b._finalize_mainai_execution_goals(session_b)
        session_b.commit()

    for task in tasks_b:
        session_b.refresh(task)
        assert task.status == MainAITaskStatus.completed
    repair_b = session_b.get(MainAITask, ids["repair_child_id"])
    session_b.refresh(repair_b)
    assert repair_b.status == MainAITaskStatus.completed
    session_b.refresh(goal_b)
    assert goal_b.status == MainAIGoalStatus.completed
    assert goal_b.final_outcome is not None

    calc_path, final, _ = _worktree_sources()
    assert all(name in final for name in _HELPER_NAMES)
    assert "return left * right" in final
    assert not (calc_path.parent / "outside_envelope.py").exists()

    auth = session_b.execute(
        select(ProviderSpendAuthorization).where(
            ProviderSpendAuthorization.owner_id == ids["user_id"],
            ProviderSpendAuthorization.status == "active",
        )
    ).scalar_one()
    assert auth.spent_requests <= auth.max_requests
    assert auth.reserved_requests == 0
    assert (
        session_b.execute(
            select(ProviderDisclosureEvent).where(
                ProviderDisclosureEvent.owner_id == ids["user_id"]
            )
        )
        .scalars()
        .all()
    )

    fs_before = _worktree_fs_snapshot()
    calls_before = len(adapter.calls)
    jobs_before = len(
        session_b.execute(select(MainAIJob).where(MainAIJob.owner_id == ids["user_id"]))
        .scalars()
        .all()
    )
    for _ in range(3):
        await worker_b._advance_authorized_supervisor_goals(session_b)
        worker_b._finalize_mainai_execution_goals(session_b)
        session_b.commit()
    assert _worktree_fs_snapshot() == fs_before
    assert len(adapter.calls) == calls_before
    assert (
        len(
            session_b.execute(select(MainAIJob).where(MainAIJob.owner_id == ids["user_id"]))
            .scalars()
            .all()
        )
        == jobs_before
    )
    assert [
        g.id
        for g, _ in eligible_authorized_goals(session_b, limit=50)
        if g.owner_id == ids["user_id"]
    ] == []

    session_b.close()
