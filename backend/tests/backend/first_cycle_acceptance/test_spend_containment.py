"""Spend containment against production billed_execution_chat / provider_spend."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text as sa_text

from first_cycle_acceptance._adapters import PRODUCTION
from first_cycle_acceptance._matrix import BLOCKED, FAIL, PASS
from first_cycle_acceptance.conftest import conclude

pytestmark = pytest.mark.first_cycle_acceptance


@pytest.fixture(autouse=True, scope="module")
def _privileges():
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges, apply_mainai_job_runtime_privileges

    apply_mainai_job_runtime_privileges(migration_engine)
    apply_mainai_execution_privileges(migration_engine)


@pytest.fixture
def owner_id(db_session, make_verified_user):
    from app.request_context import current_user_id as current_user_id_var

    user, _ = make_verified_user()
    current_user_id_var.set(str(user.id))
    db_session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(user.id)})
    return user.id


def _goal_and_envelope(db, owner_id, *, providers, models, max_requests=2, max_cost="1.00"):
    from app.config import get_settings
    from app.execution_envelopes import authorize_execution_scope, propose_execution_scope
    from app.mainai_execution import planner
    from app.provider_spend import authorize_provider_spend

    goal = planner.create_goal(
        db,
        owner_id=owner_id,
        title="spend",
        original_instruction="bounded spend",
        created_by="founder",
    )
    db.commit()
    proposal = propose_execution_scope(db, owner_id=owner_id, goal_id=goal.id, idempotency_key=f"p-{uuid.uuid4()}")
    _, envelope = authorize_execution_scope(
        db,
        owner_id=owner_id,
        proposal_id=proposal.id,
        authorized_by="founder",
        authorized_paths=["backend/app/greeting.py"],
        authorized_capabilities=["path_a", "read_file", "repo_edit"],
        authorized_risk="low",
        envelope_idempotency_key=f"e-{uuid.uuid4()}",
    )
    authorize_provider_spend(
        db,
        owner_id=owner_id,
        goal_id=goal.id,
        execution_envelope_id=envelope.id,
        authorized_by="founder",
        max_cost_usd=Decimal(max_cost),
        max_requests=max_requests,
        max_cost_per_request_usd=Decimal("0.50"),
        idempotency_key=f"s-{uuid.uuid4()}",
        allowed_providers=providers,
        allowed_models=models or [get_settings().default_llm_model],
    )
    db.commit()
    return goal, envelope


@pytest.mark.asyncio
async def test_one_approved_provider_model_only(db_session, owner_id, monkeypatch):
    from app.config import get_settings
    from app.mainai_execution.billed_chat import ExecutionSpendError, billed_execution_chat
    from app.providers.base import ChatResult, Message
    from app.providers.openai_provider import OpenAIProvider

    settings = get_settings()
    monkeypatch.setattr(settings, "mainai_execution_require_provider_spend", True)
    calls = []

    async def chat(self, messages, model, **kwargs):
        calls.append((self.name if hasattr(self, "name") else "openai", model))
        return ChatResult(
            content="ok",
            provider="openai",
            model=model,
            raw_usage={"prompt_tokens": 10, "completion_tokens": 10},
        )

    monkeypatch.setattr(OpenAIProvider, "chat", chat)
    goal, _ = _goal_and_envelope(
        db_session, owner_id, providers=["openai"], models=[settings.default_llm_model]
    )
    result, _ = await billed_execution_chat(
        db_session,
        [Message(role="user", content="hi")],
        owner_id=owner_id,
        goal_id=goal.id,
        purpose="first_cycle.spend",
        requested_by="first_cycle_acceptance",
    )
    conclude(
        "SPEND-1 one approved provider/model",
        PASS if result.provider == "openai" and len(calls) == 1 else FAIL,
        boundary="production.billed_execution_chat",
        detail=f"provider={result.provider}; model={result.model}; calls={calls}",
        production_module="app.mainai_execution.billed_chat",
    )


@pytest.mark.asyncio
async def test_no_unauthorized_fallback(db_session, owner_id, monkeypatch):
    from app.config import get_settings
    from app.mainai_execution.billed_chat import ExecutionSpendError, billed_execution_chat
    from app.providers.base import ChatResult, Message
    from app.providers.openai_provider import OpenAIProvider

    settings = get_settings()
    monkeypatch.setattr(settings, "mainai_execution_require_provider_spend", True)
    paid = {"n": 0}

    async def boom(self, messages, model, **kwargs):
        paid["n"] += 1
        raise RuntimeError("provider down")

    monkeypatch.setattr(OpenAIProvider, "chat", boom)
    goal, _ = _goal_and_envelope(
        db_session, owner_id, providers=["openai"], models=[settings.default_llm_model], max_requests=1
    )
    with pytest.raises(ExecutionSpendError):
        await billed_execution_chat(
            db_session,
            [Message(role="user", content="hi")],
            owner_id=owner_id,
            goal_id=goal.id,
            purpose="first_cycle.spend",
            requested_by="first_cycle_acceptance",
        )
    conclude(
        "SPEND-2 no unauthorized fallback after the allowlisted attempt",
        PASS if paid["n"] <= 1 else FAIL,
        boundary="production.billed_execution_chat.fallback",
        detail=f"paid_attempts={paid['n']}",
        production_module="app.mainai_execution.billed_chat",
    )


@pytest.mark.asyncio
async def test_bounded_requests_tokens_usd(db_session, owner_id, monkeypatch):
    from app.config import get_settings
    from app.mainai_execution.billed_chat import ExecutionSpendError, billed_execution_chat
    from app.providers.base import ChatResult, Message
    from app.providers.openai_provider import OpenAIProvider

    settings = get_settings()
    monkeypatch.setattr(settings, "mainai_execution_require_provider_spend", True)

    async def chat(self, messages, model, **kwargs):
        return ChatResult(
            content="ok",
            provider="openai",
            model=model,
            raw_usage={"prompt_tokens": 10, "completion_tokens": 10},
        )

    monkeypatch.setattr(OpenAIProvider, "chat", chat)
    goal, _ = _goal_and_envelope(
        db_session,
        owner_id,
        providers=["openai"],
        models=[settings.default_llm_model],
        max_requests=1,
        max_cost="0.50",
    )
    await billed_execution_chat(
        db_session,
        [Message(role="user", content="one")],
        owner_id=owner_id,
        goal_id=goal.id,
        purpose="first_cycle.spend",
        requested_by="first_cycle_acceptance",
    )
    with pytest.raises(ExecutionSpendError):
        await billed_execution_chat(
            db_session,
            [Message(role="user", content="two")],
            owner_id=owner_id,
            goal_id=goal.id,
            purpose="first_cycle.spend",
            requested_by="first_cycle_acceptance",
        )
    conclude(
        "SPEND-3 bounded requests/tokens/USD",
        PASS,
        boundary="production.provider_spend",
        detail="second billed call refused after max_requests=1",
        production_module="app.provider_spend",
    )


def test_pricing_freshness_unknown_model_refused():
    estimate = PRODUCTION["pricing"].symbols.get("estimate_cost")
    if estimate is None:
        conclude(
            "SPEND-4 unknown pricing fails closed",
            BLOCKED,
            boundary="production.pricing",
            detail="estimate_cost missing",
            production_module="app.providers.pricing",
        )
        return
    unknown = estimate("openai", "not-a-real-model-xyz", 100, 100)
    conclude(
        "SPEND-4 unknown pricing fails closed",
        PASS if unknown is None else FAIL,
        boundary="production.pricing",
        detail=f"estimate_cost(openai/not-a-real-model-xyz)={unknown}",
        production_module="app.providers.pricing",
    )


@pytest.mark.asyncio
async def test_invalid_authority_fails_before_paid_call(db_session, owner_id, monkeypatch):
    from app.config import get_settings
    from app.mainai_execution.billed_chat import ExecutionSpendError, billed_execution_chat
    from app.providers.base import ChatResult, Message
    from app.providers.openai_provider import OpenAIProvider

    settings = get_settings()
    monkeypatch.setattr(settings, "mainai_execution_require_provider_spend", True)
    paid = {"n": 0}

    async def chat(self, messages, model, **kwargs):
        paid["n"] += 1
        return ChatResult(content="should-not-run", provider="openai", model=model, raw_usage={"prompt_tokens": 1, "completion_tokens": 1})

    monkeypatch.setattr(OpenAIProvider, "chat", chat)
    goal, _ = _goal_and_envelope(db_session, owner_id, providers=["openai"], models=["gpt-4o-mini"])
    from app.mainai_execution import planner

    other = planner.create_goal(
        db_session, owner_id=owner_id, title="no-auth", original_instruction="x", created_by="founder"
    )
    db_session.commit()
    with pytest.raises(ExecutionSpendError):
        await billed_execution_chat(
            db_session,
            [Message(role="user", content="hi")],
            owner_id=owner_id,
            goal_id=other.id,
            purpose="first_cycle.spend",
            requested_by="first_cycle_acceptance",
        )
    conclude(
        "SPEND-5 invalid authority fails before any paid call",
        PASS if paid["n"] == 0 else FAIL,
        boundary="production.billed_execution_chat.reserve",
        detail=f"paid_calls={paid['n']}",
        production_module="app.mainai_execution.billed_chat",
    )


def test_no_paid_replan_after_inherited_ci_failure(db_session, owner_id):
    from app.mainai_execution import planner
    from app.mainai_execution.planner import PlannedTaskSpec
    from app.mainai_execution.replan import find_replan_trigger
    from app.models.mainai_execution import MainAITask, MainAITaskStatus

    goal = planner.create_goal(
        db_session, owner_id=owner_id, title="ci-fail", original_instruction="x", created_by="founder"
    )
    planner.create_plan(
        db_session,
        goal=goal,
        rationale="ci",
        tasks=[PlannedTaskSpec(description="open pr", task_type="open_pr", target_files=["backend/app/greeting.py"])],
        created_by="founder",
    )
    db_session.commit()
    task = db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()
    task.status = MainAITaskStatus.failed
    db_session.commit()
    trigger = find_replan_trigger(db_session, goal=goal)
    # First cycle must not spend on replanning inherited CI failures. A trigger here is FAIL.
    conclude(
        "SPEND-6 no paid replanning after inherited CI failures",
        FAIL if trigger is not None else PASS,
        boundary="production.replan",
        detail=f"find_replan_trigger={trigger!r} after a failed open_pr (CI-failure stand-in)",
        production_module="app.mainai_execution.replan",
    )
