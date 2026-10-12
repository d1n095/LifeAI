"""Regression tests for Claude's independent Path A exam of #264 @ 21ff3cc
(examiner SHA e5d2784e48c40ffe3667c26cc52ad3eeecaee877).

Each test reproduces one published probe (CTX-1, CTX-2, TEST-1, SPEND-1..5) as a
fail-closed assertion. No live provider, GitHub write, or agent call."""

from __future__ import annotations

import json
import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import text as sa_text

from app.config import get_settings
from app.execution_envelopes import authorize_execution_scope, propose_execution_scope
from app.mainai_execution import billed_chat, execution_job, planner
from app.mainai_execution.billed_chat import ExecutionSpendError, billed_execution_chat
from app.mainai_execution.planner import PlannedTaskSpec
from app.models.mainai_execution import MainAITask
from app.models.provider_spend import ProviderSpendUsageEvent
from app.provider_spend import authorize_provider_spend
from app.providers.base import ChatResult, Message
from app.providers.openai_provider import OpenAIProvider
from app.request_context import current_user_id as current_user_id_var


@pytest.fixture(autouse=True, scope="module")
def _privileges():
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges, apply_mainai_job_runtime_privileges

    apply_mainai_job_runtime_privileges(migration_engine)
    apply_mainai_execution_privileges(migration_engine)


@pytest.fixture
def owner_id(db_session, make_verified_user):
    user, _ = make_verified_user()
    current_user_id_var.set(str(user.id))
    db_session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(user.id)})
    return user.id


def _goal(db, owner_id):
    return planner.create_goal(
        db,
        owner_id=owner_id,
        title="exam",
        original_instruction="examiner probe goal",
        created_by="examiner",
    )


def _task(db, goal, description, plan=None):
    planner.create_plan(
        db,
        goal=goal,
        rationale="exam",
        created_by="examiner",
        tasks=[
            PlannedTaskSpec(
                description=description,
                task_type="repo_edit",
                verification_plan=plan
                if plan is not None
                else [{"kind": "targeted_tests", "target": "tests/backend/test_x.py"}],
            )
        ],
    )
    db.commit()
    return db.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()


def _fake_model(captured, files):
    async def _chat(self, messages, model, **kwargs):
        captured["prompt"] = messages[1].content
        return ChatResult(
            content=json.dumps({"files": files, "commit_message": "exam"}),
            provider="openai",
            model=model,
            raw_usage={},
        )

    return _chat


def _grant(db, owner_id, goal):
    proposal = propose_execution_scope(db, owner_id=owner_id, goal_id=goal.id, idempotency_key=f"prop-{uuid.uuid4()}")
    _, envelope = authorize_execution_scope(
        db,
        owner_id=owner_id,
        proposal_id=proposal.id,
        authorized_by="founder",
        authorized_paths=["backend/app/x.py"],
        authorized_capabilities=["read_file", "patch_file"],
        authorized_risk="low",
        envelope_idempotency_key=f"env-{uuid.uuid4()}",
    )
    authorize_provider_spend(
        db,
        owner_id=owner_id,
        goal_id=goal.id,
        execution_envelope_id=envelope.id,
        authorized_by="founder",
        max_cost_usd=Decimal("1.00"),
        max_requests=5,
        max_cost_per_request_usd=Decimal("0.50"),
        idempotency_key=f"spend-{uuid.uuid4()}",
        allowed_providers=["openai"],
        allowed_models=["gpt-4o-mini"],
    )
    db.commit()


def _patch_fallback(monkeypatch, *, prompt_tokens, completion_tokens):
    monkeypatch.setattr(billed_chat, "resolve_chat_chain", lambda db: [(SimpleNamespace(name="openai"), "gpt-4o-mini")])

    async def _fallback(db, messages, **kwargs):
        return (
            ChatResult(
                content="ok",
                provider="openrouter",
                model="vendor/unlisted-model",
                raw_usage={"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
            ),
            ["openai", "openrouter"],
        )

    monkeypatch.setattr(billed_chat, "chat_with_fallback", _fallback)


@pytest.mark.asyncio
async def test_ctx1_unseen_existing_file_cannot_be_overwritten(db_session, owner_id, monkeypatch, tmp_path):
    monkeypatch.setattr(get_settings(), "mainai_execution_require_provider_spend", False)
    target = tmp_path / "backend" / "app" / "core.py"
    target.parent.mkdir(parents=True)
    target.write_text("IMPORTANT = 'original logic'\n")
    monkeypatch.setattr(execution_job, "_repo_root", lambda: tmp_path)
    captured = {}
    monkeypatch.setattr(
        OpenAIProvider,
        "chat",
        _fake_model(captured, [{"path": "backend/app/core.py", "content": "IMPORTANT = 'rewritten blind'\n"}]),
    )
    task = _task(db_session, _goal(db_session, owner_id), "Improve error handling in the core module.")
    with pytest.raises(Exception, match="explicit target|complete, untruncated|Refusing"):
        await execution_job._propose_repo_edit(db_session, task)
    assert target.read_text() == "IMPORTANT = 'original logic'\n"
    assert "original logic" not in captured.get("prompt", "")


@pytest.mark.asyncio
async def test_ctx2_large_file_is_not_silently_truncated_by_an_edit(db_session, owner_id, monkeypatch, tmp_path):
    monkeypatch.setattr(get_settings(), "mainai_execution_require_provider_spend", False)
    target = tmp_path / "backend" / "app" / "big.py"
    target.parent.mkdir(parents=True)
    body = "".join(f"LINE_{i:05d} = {i}\n" for i in range(2000))
    target.write_text(body)
    monkeypatch.setattr(execution_job, "_repo_root", lambda: tmp_path)
    captured = {}

    async def _faithful_model(self, messages, model, **kwargs):
        prompt = messages[1].content
        captured["prompt"] = prompt
        if "### backend/app/big.py\n```\n" in prompt:
            shown = prompt.split("### backend/app/big.py\n```\n", 1)[1].split("\n```", 1)[0].replace(
                "\n...[truncated]...\n", ""
            )
        else:
            shown = ""
        captured["shown_chars"] = len(shown)
        content = "# edited\n" + shown
        return ChatResult(
            content=json.dumps({"files": [{"path": "backend/app/big.py", "content": content}]}),
            provider="openai",
            model=model,
            raw_usage={},
        )

    monkeypatch.setattr(OpenAIProvider, "chat", _faithful_model)
    task = _task(db_session, _goal(db_session, owner_id), "Add a header comment to backend/app/big.py")
    with pytest.raises(Exception, match="complete, untruncated|Refusing"):
        await execution_job._propose_repo_edit(db_session, task)
    after = target.read_text()
    assert after == body
    assert "...[truncated]..." not in captured.get("prompt", "")
    lost = sum(1 for i in range(2000) if f"LINE_{i:05d} = {i}\n" not in after)
    assert lost == 0


def test_test1_repo_edit_without_targeted_tests_is_rejected_at_plan_time(db_session, owner_id):
    goal = _goal(db_session, owner_id)
    with pytest.raises(Exception, match="targeted|verification"):
        planner.create_plan(
            db_session,
            goal=goal,
            rationale="exam",
            created_by="examiner",
            tasks=[
                PlannedTaskSpec(
                    description="edit backend/app/x.py",
                    task_type="repo_edit",
                    verification_plan=[],
                )
            ],
        )
        db_session.commit()


@pytest.mark.asyncio
async def test_spend1_default_configuration_does_not_make_unbilled_ai_calls(db_session, owner_id, monkeypatch):
    settings = get_settings()
    assert settings.mainai_execution_require_provider_spend is True
    calls = []

    async def _chat(self, messages, model, **kwargs):
        calls.append(model)
        return ChatResult(
            content="ok",
            provider="openai",
            model=model,
            raw_usage={"prompt_tokens": 1, "completion_tokens": 1},
        )

    monkeypatch.setattr(OpenAIProvider, "chat", _chat)
    goal = _goal(db_session, owner_id)
    with pytest.raises(ExecutionSpendError):
        await billed_execution_chat(
            db_session,
            [Message(role="user", content="x")],
            owner_id=owner_id,
            goal_id=goal.id,
            purpose="exam",
            requested_by="exam",
        )
    events = db_session.query(ProviderSpendUsageEvent).filter(ProviderSpendUsageEvent.goal_id == goal.id).count()
    assert calls == []
    assert events == 0


@pytest.mark.asyncio
async def test_spend2_fallback_provider_outside_the_grant_allowlist_is_refused(db_session, owner_id, monkeypatch):
    goal = _goal(db_session, owner_id)
    _grant(db_session, owner_id, goal)
    _patch_fallback(monkeypatch, prompt_tokens=10, completion_tokens=5)
    with pytest.raises(ExecutionSpendError, match="allowlist|outside"):
        await billed_execution_chat(
            db_session,
            [Message(role="user", content="x")],
            owner_id=owner_id,
            goal_id=goal.id,
            purpose="exam",
            requested_by="exam",
        )
    events = db_session.query(ProviderSpendUsageEvent).filter(ProviderSpendUsageEvent.goal_id == goal.id).all()
    assert events == []


@pytest.mark.asyncio
async def test_spend3_unpriced_answer_is_not_settled_as_zero(db_session, owner_id, monkeypatch):
    goal = _goal(db_session, owner_id)
    _grant(db_session, owner_id, goal)
    _patch_fallback(monkeypatch, prompt_tokens=10, completion_tokens=5)
    with pytest.raises(ExecutionSpendError):
        await billed_execution_chat(
            db_session,
            [Message(role="user", content="x")],
            owner_id=owner_id,
            goal_id=goal.id,
            purpose="exam",
            requested_by="exam",
        )
    events = db_session.query(ProviderSpendUsageEvent).filter(ProviderSpendUsageEvent.goal_id == goal.id).all()
    assert not any(Decimal(str(e.cost_usd or 0)) == 0 for e in events)


@pytest.mark.asyncio
async def test_spend4_answer_exceeding_the_reservation_is_a_handled_spend_failure(db_session, owner_id, monkeypatch):
    goal = _goal(db_session, owner_id)
    _grant(db_session, owner_id, goal)
    _patch_fallback(monkeypatch, prompt_tokens=50_000, completion_tokens=50_000)
    with pytest.raises(ExecutionSpendError):
        await billed_execution_chat(
            db_session,
            [Message(role="user", content="x")],
            owner_id=owner_id,
            goal_id=goal.id,
            purpose="exam",
            requested_by="exam",
        )


@pytest.mark.asyncio
async def test_spend5_over_reservation_answer_does_not_strand_the_task(db_session, superuser_db, owner_id, monkeypatch):
    from app.jobs.mainai_job_lease import claim_next_mainai_job
    from app.mainai_execution import executor
    from app.mainai_execution.execution_job import run_task_execution_job
    from app.models.mainai_job import MainAIJob

    goal = _goal(db_session, owner_id)
    planner.create_plan(
        db_session,
        goal=goal,
        rationale="exam",
        created_by="examiner",
        tasks=[PlannedTaskSpec(description="audit", task_type="read_only_audit", verification_plan=[])],
    )
    db_session.commit()
    task = db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()
    _grant(db_session, owner_id, goal)
    _patch_fallback(monkeypatch, prompt_tokens=50_000, completion_tokens=50_000)
    job = executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="examiner")
    db_session.commit()
    _, _, generation = claim_next_mainai_job(superuser_db, "exam-worker", 120)
    current_user_id_var.set(str(owner_id))
    db_session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})
    await run_task_execution_job(
        db_session,
        job.id,
        owner_id,
        worker_id="exam-worker",
        lease_generation=generation,
        lease_seconds=120,
    )
    superuser_db.expire_all()
    status = superuser_db.get(MainAITask, task.id).status.value
    job_status = superuser_db.get(MainAIJob, job.id).status.value
    assert status != "running"
    assert job_status != "running"
    assert status in {"retryable_failed", "failed"}


def test_planner_target_files_are_required_and_persisted_into_the_task(db_session, owner_id):
    goal = _goal(db_session, owner_id)
    planner.create_plan(
        db_session,
        goal=goal,
        rationale="exam",
        created_by="examiner",
        tasks=[
            PlannedTaskSpec(
                description="Improve error handling.",
                task_type="repo_edit",
                target_files=["backend/app/core.py"],
                verification_plan=[{"kind": "targeted_tests", "target": "tests/backend/test_x.py"}],
            )
        ],
    )
    db_session.commit()
    task = db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()
    assert "backend/app/core.py" in task.description
