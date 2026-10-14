"""Regression tests for Claude's independent Path A exam of #264 @ 21ff3cc
(examiner SHA e5d2784e48c40ffe3667c26cc52ad3eeecaee877).

Each test reproduces one published probe (CTX-1, CTX-2, TEST-1, SPEND-1..5) as a
fail-closed assertion. No live provider, GitHub write, or agent call."""

from __future__ import annotations

import asyncio
import json
import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import text as sa_text

from app.config import get_settings
from app.execution_envelopes import authorize_execution_scope, propose_execution_scope
from app.mainai_execution import billed_chat, execution_job, planner
from app.mainai_execution.billed_chat import (
    ExecutionSpendError,
    _estimate_prompt_tokens,
    billed_execution_chat,
)
from app.mainai_execution.planner import PlannedTaskSpec
from app.models.mainai_execution import MainAITask
from app.models.provider_spend import ProviderSpendAuthorization, ProviderSpendUsageEvent
from app.provider_spend import authorize_provider_spend
from app.providers import registry as provider_registry
from app.providers.base import ChatResult, Message
from app.providers.gemini_provider import GeminiProvider
from app.providers.openai_provider import OpenAIProvider
from app.providers.pricing import estimate_cost
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
        authorized_capabilities=["path_a", "read_file", "patch_file"],
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
    db_session.expire_all()
    events = db_session.query(ProviderSpendUsageEvent).filter(ProviderSpendUsageEvent.goal_id == goal.id).all()
    assert events, "a paid fallback that is then refused must still be counted against the grant"
    assert all(str(e.status) not in ("released", "ProviderSpendUsageStatus.released") for e in events)


def _counted_events(db, goal_id):
    db.expire_all()
    events = db.query(ProviderSpendUsageEvent).filter(ProviderSpendUsageEvent.goal_id == goal_id).all()
    return [e for e in events if str(e.status) not in ("released", "ProviderSpendUsageStatus.released")]


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
    events = _counted_events(db_session, goal.id)
    assert events
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
    assert _counted_events(db_session, goal.id)


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
    assert _counted_events(db_session, goal.id)


def _counting_fallback(
    monkeypatch,
    *,
    provider="openai",
    model="gpt-4o-mini",
    prompt_tokens=10,
    completion_tokens=5,
    chain_model="gpt-4o-mini",
):
    calls = []
    monkeypatch.setattr(
        billed_chat,
        "resolve_chat_chain",
        lambda db, _m=chain_model: [(SimpleNamespace(name="openai"), _m)],
    )

    async def _paid(db, messages, **kwargs):
        calls.append((provider, model))
        return (
            ChatResult(
                content="ok",
                provider=provider,
                model=model,
                raw_usage={"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
            ),
            [provider],
        )

    monkeypatch.setattr(billed_chat, "chat_with_fallback", _paid)
    return calls


def _grant_n(db, owner_id, goal, *, max_requests, models=("gpt-4o-mini",)):
    proposal = propose_execution_scope(
        db, owner_id=owner_id, goal_id=goal.id, idempotency_key=f"prop-{uuid.uuid4()}"
    )
    _, envelope = authorize_execution_scope(
        db,
        owner_id=owner_id,
        proposal_id=proposal.id,
        authorized_by="founder",
        authorized_paths=["backend/app/x.py"],
        authorized_capabilities=["path_a", "read_file", "patch_file"],
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
        max_requests=max_requests,
        max_cost_per_request_usd=Decimal("0.50"),
        idempotency_key=f"spend-{uuid.uuid4()}",
        allowed_providers=["openai"],
        allowed_models=list(models),
    )
    db.commit()


async def _call_and_rollback_on_spend_error(db, owner_id, goal):
    try:
        await billed_execution_chat(
            db,
            [Message(role="user", content="x")],
            owner_id=owner_id,
            goal_id=goal.id,
            purpose="exam",
            requested_by="exam",
        )
        db.commit()
        return "accepted"
    except ExecutionSpendError:
        db.rollback()
        return "ExecutionSpendError"
    except Exception:
        db.rollback()
        return "other"


@pytest.mark.asyncio
async def test_spend6_exhausted_grant_makes_no_further_paid_provider_call(db_session, owner_id, monkeypatch):
    goal = _goal(db_session, owner_id)
    _grant_n(db_session, owner_id, goal, max_requests=1)
    calls = _counting_fallback(monkeypatch)
    first = await _call_and_rollback_on_spend_error(db_session, owner_id, goal)
    second = await _call_and_rollback_on_spend_error(db_session, owner_id, goal)
    third = await _call_and_rollback_on_spend_error(db_session, owner_id, goal)
    assert first == "accepted"
    assert second == "ExecutionSpendError"
    assert third == "ExecutionSpendError"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_spend7_refused_paid_answers_are_still_recorded_against_the_grant(db_session, owner_id, monkeypatch):
    results = {}
    cases = (
        ("outside-allowlist", dict(provider="openrouter", model="vendor/x"), ("gpt-4o-mini",)),
        ("unpriced-allowlisted", dict(model="gpt-unpriced-exam"), ("gpt-unpriced-exam",)),
        ("over-hold", dict(prompt_tokens=50_000, completion_tokens=50_000), ("gpt-4o-mini",)),
    )
    for label, kw, models in cases:
        goal = _goal(db_session, owner_id)
        _grant_n(db_session, owner_id, goal, max_requests=5, models=models)
        calls = _counting_fallback(monkeypatch, chain_model=models[0], **kw)
        outcomes = [await _call_and_rollback_on_spend_error(db_session, owner_id, goal) for _ in range(3)]
        counted = _counted_events(db_session, goal.id)
        results[label] = (len(calls), len(counted), outcomes[0])
        assert len(calls) >= 1, results
        assert len(calls) <= len(counted), results
        assert all(outcome == "ExecutionSpendError" for outcome in outcomes)
    assert all(paid <= counted for paid, counted, _ in results.values()), results


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


def _grant_custom(
    db,
    owner_id,
    goal,
    *,
    max_cost,
    max_requests,
    per_request=None,
    models=("gpt-4o-mini",),
    providers=("openai",),
):
    proposal = propose_execution_scope(
        db, owner_id=owner_id, goal_id=goal.id, idempotency_key=f"prop-{uuid.uuid4()}"
    )
    _, envelope = authorize_execution_scope(
        db,
        owner_id=owner_id,
        proposal_id=proposal.id,
        authorized_by="founder",
        authorized_paths=["backend/app/x.py"],
        authorized_capabilities=["path_a", "read_file", "patch_file"],
        authorized_risk="low",
        envelope_idempotency_key=f"env-{uuid.uuid4()}",
    )
    authorize_provider_spend(
        db,
        owner_id=owner_id,
        goal_id=goal.id,
        execution_envelope_id=envelope.id,
        authorized_by="founder",
        max_cost_usd=Decimal(max_cost),
        max_requests=max_requests,
        max_cost_per_request_usd=None if per_request is None else Decimal(per_request),
        idempotency_key=f"spend-{uuid.uuid4()}",
        allowed_providers=list(providers),
        allowed_models=list(models),
    )
    db.commit()


def _real_chain(monkeypatch, models, behaviours, seen):
    """Exercise the real chat_with_fallback path; only provider.chat is faked."""
    provider = OpenAIProvider()
    monkeypatch.setattr(billed_chat, "resolve_chat_chain", lambda db: [(provider, m) for m in models])
    monkeypatch.setattr(
        "app.egress_policy.enforce_egress_policy",
        lambda db, *, payload, **kw: payload,
        raising=True,
    )

    async def _chat(self, messages, model, **kwargs):
        seen.append(
            {
                "model": model,
                "kwargs": sorted(kwargs),
                "max_tokens": kwargs.get("max_tokens"),
                "chars": sum(len(m.content) for m in messages),
            }
        )
        behaviour = behaviours[model]
        return behaviour(messages)

    monkeypatch.setattr(OpenAIProvider, "chat", _chat)


@pytest.mark.asyncio
async def test_spend7b_settlement_failure_keeps_the_paid_request_counted(db_session, owner_id, monkeypatch):
    goal = _goal(db_session, owner_id)
    _grant_n(db_session, owner_id, goal, max_requests=2)
    calls = _counting_fallback(monkeypatch)

    def _boom(*a, **k):
        raise RuntimeError("settle exploded")

    monkeypatch.setattr(billed_chat, "settle_provider_spend_call", _boom)
    outcomes = [await _call_and_rollback_on_spend_error(db_session, owner_id, goal) for _ in range(4)]
    counted = _counted_events(db_session, goal.id)
    statuses = [str(getattr(row.status, "value", row.status)) for row in counted]
    assert len(calls) <= 2
    assert len(counted) == len(calls)
    assert all(status == "reserved" for status in statuses)
    assert outcomes[0] == "ExecutionSpendError"


@pytest.mark.asyncio
async def test_spend7c_provider_exception_after_reservation_is_counted(db_session, owner_id, monkeypatch):
    goal = _goal(db_session, owner_id)
    _grant_n(db_session, owner_id, goal, max_requests=2)
    seen = []

    def _timeout(messages):
        raise TimeoutError("read timeout after request was sent")

    _real_chain(monkeypatch, ["gpt-4o-mini"], {"gpt-4o-mini": _timeout}, seen)
    outcomes = [await _call_and_rollback_on_spend_error(db_session, owner_id, goal) for _ in range(3)]
    counted = _counted_events(db_session, goal.id)
    assert len(seen) == 2
    assert len(counted) == 2
    assert all(str(getattr(row.status, "value", row.status)) == "settled" for row in counted)
    assert all(outcome == "ExecutionSpendError" for outcome in outcomes)


@pytest.mark.asyncio
async def test_spend8_oversized_prompt_is_refused_before_any_paid_request(db_session, owner_id, monkeypatch):
    goal = _goal(db_session, owner_id)
    _grant_custom(db_session, owner_id, goal, max_cost="0.05", max_requests=100)
    seen = []

    def _big(messages):
        return ChatResult(
            content="ok",
            provider="openai",
            model="gpt-4o-mini",
            raw_usage={"prompt_tokens": 25_000, "completion_tokens": 2_048},
        )

    _real_chain(monkeypatch, ["gpt-4o-mini"], {"gpt-4o-mini": _big}, seen)
    prompt = [Message(role="system", content="s"), Message(role="user", content="x" * 80_000)]
    outcomes = []
    for _ in range(40):
        try:
            await billed_execution_chat(
                db_session,
                prompt,
                owner_id=owner_id,
                goal_id=goal.id,
                purpose="exam",
                requested_by="exam",
            )
            db_session.commit()
            outcomes.append("accepted")
        except ExecutionSpendError as exc:
            db_session.rollback()
            outcomes.append(str(exc)[:80])
    counted = _counted_events(db_session, goal.id)
    actual = estimate_cost("openai", "gpt-4o-mini", 25_000, 2_048) * len(seen)
    assert seen == []
    assert counted == []
    assert outcomes.count("accepted") == 0
    assert actual == 0 or actual <= Decimal("0.05")
    assert "estimated prompt" in outcomes[0]


@pytest.mark.asyncio
async def test_spend8_in_budget_call_passes_completion_token_limit(db_session, owner_id, monkeypatch):
    goal = _goal(db_session, owner_id)
    _grant_custom(db_session, owner_id, goal, max_cost="1.00", max_requests=1, per_request="0.50")
    seen = []

    def _ok(messages):
        return ChatResult(
            content="ok",
            provider="openai",
            model="gpt-4o-mini",
            raw_usage={"prompt_tokens": 10, "completion_tokens": 5},
        )

    _real_chain(monkeypatch, ["gpt-4o-mini"], {"gpt-4o-mini": _ok}, seen)
    result, attempted = await billed_execution_chat(
        db_session,
        [Message(role="user", content="x")],
        owner_id=owner_id,
        goal_id=goal.id,
        purpose="exam",
        requested_by="exam",
    )
    db_session.commit()
    counted = _counted_events(db_session, goal.id)
    assert result.content == "ok"
    assert attempted == ["openai"]
    assert len(seen) == 1
    assert seen[0]["max_tokens"] == counted[0].reserved_completion_tokens
    assert seen[0]["max_tokens"] > 0


@pytest.mark.asyncio
async def test_spend9_max_requests_one_is_one_paid_provider_request(db_session, owner_id, monkeypatch):
    goal = _goal(db_session, owner_id)
    _grant_custom(
        db_session,
        owner_id,
        goal,
        max_cost="1.00",
        max_requests=1,
        per_request="0.50",
        models=("gpt-4o-mini", "gpt-4o"),
    )
    seen = []

    def _timeout(messages):
        raise TimeoutError("read timeout after request was sent")

    def _ok(messages):
        return ChatResult(
            content="ok",
            provider="openai",
            model="gpt-4o",
            raw_usage={"prompt_tokens": 10, "completion_tokens": 5},
        )

    _real_chain(monkeypatch, ["gpt-4o-mini", "gpt-4o"], {"gpt-4o-mini": _timeout, "gpt-4o": _ok}, seen)
    outcome = await _call_and_rollback_on_spend_error(db_session, owner_id, goal)
    counted = _counted_events(db_session, goal.id)
    assert len(seen) == 1
    assert [entry["model"] for entry in seen] == ["gpt-4o-mini"]
    assert len(counted) == 1
    assert counted[0].model == "gpt-4o-mini"
    assert outcome == "ExecutionSpendError"


@pytest.mark.asyncio
async def test_spend10_call_scoped_chain_does_not_mutate_process_registry(db_session, owner_id, monkeypatch):
    original = provider_registry.resolve_chat_chain
    goal_a, goal_b = _goal(db_session, owner_id), _goal(db_session, owner_id)
    _grant_custom(db_session, owner_id, goal_a, max_cost="1.00", max_requests=5, per_request="0.10", models=("gpt-4o-mini",))
    _grant_custom(db_session, owner_id, goal_b, max_cost="1.00", max_requests=5, per_request="0.10", models=("gpt-4o",))
    provider = OpenAIProvider()
    monkeypatch.setattr(
        billed_chat,
        "resolve_chat_chain",
        lambda db: [(provider, "gpt-4o-mini"), (provider, "gpt-4o")],
    )
    monkeypatch.setattr(
        "app.egress_policy.enforce_egress_policy",
        lambda db, *, payload, **kw: payload,
        raising=True,
    )
    gate_a, gate_b = asyncio.Event(), asyncio.Event()
    seen = []

    async def _chat(self, messages, model, **kwargs):
        seen.append(model)
        await (gate_a if model == "gpt-4o-mini" else gate_b).wait()
        return ChatResult(
            content="ok",
            provider="openai",
            model=model,
            raw_usage={"prompt_tokens": 10, "completion_tokens": 5},
        )

    monkeypatch.setattr(OpenAIProvider, "chat", _chat)
    from app.db import SessionLocal

    db_b = SessionLocal()
    db_b.execute(sa_text("SET app.current_user_id = :uid"), {"uid": str(owner_id)})

    async def run(db, goal):
        try:
            await billed_execution_chat(
                db,
                [Message(role="user", content="x")],
                owner_id=owner_id,
                goal_id=goal.id,
                purpose="exam",
                requested_by="exam",
            )
            return "accepted"
        except Exception as exc:  # noqa: BLE001
            return type(exc).__name__

    try:
        ta = asyncio.create_task(run(db_session, goal_a))
        await asyncio.sleep(0.2)
        tb = asyncio.create_task(run(db_b, goal_b))
        await asyncio.sleep(0.2)
        gate_a.set()
        ra = await ta
        gate_b.set()
        rb = await tb
        assert provider_registry.resolve_chat_chain is original
        assert ra == "accepted"
        assert rb == "accepted"
        assert seen == ["gpt-4o-mini", "gpt-4o"]
    finally:
        db_b.rollback()
        db_b.close()


def test_spend8b_utf8_bytes_are_the_prompt_upper_bound():
    ascii_msg = [Message(role="user", content="x" * 80_000)]
    emoji_msg = [Message(role="user", content="\U0001F600" * 20_000)]
    assert _estimate_prompt_tokens(ascii_msg) >= 80_000
    assert _estimate_prompt_tokens(emoji_msg) >= 80_000
    assert _estimate_prompt_tokens(emoji_msg) > _estimate_prompt_tokens(
        [Message(role="user", content="x" * 20_000)]
    )


@pytest.mark.asyncio
async def test_spend8a_per_request_usd_cap_shrinks_max_tokens(db_session, owner_id, monkeypatch):
    goal = _goal(db_session, owner_id)
    _grant_custom(db_session, owner_id, goal, max_cost="0.01", max_requests=100, per_request="0.001")
    seen = []
    monkeypatch.setattr(get_settings(), "mainai_execution_require_provider_spend", True)
    provider = OpenAIProvider()
    monkeypatch.setattr(billed_chat, "resolve_chat_chain", lambda db: [(provider, "gpt-4o-mini")])
    monkeypatch.setattr(
        "app.egress_policy.enforce_egress_policy",
        lambda db, *, payload, **kw: payload,
        raising=True,
    )

    async def _chat(self, messages, model, **kwargs):
        cap = int(kwargs["max_tokens"])
        prompt_tokens = _estimate_prompt_tokens(messages)
        seen.append((prompt_tokens, cap))
        return ChatResult(
            content="ok",
            provider="openai",
            model=model,
            raw_usage={"prompt_tokens": prompt_tokens, "completion_tokens": cap},
        )

    monkeypatch.setattr(OpenAIProvider, "chat", _chat)
    accepted = 0
    for _ in range(30):
        try:
            await billed_execution_chat(
                db_session,
                [Message(role="user", content="x")],
                owner_id=owner_id,
                goal_id=goal.id,
                purpose="exam",
                requested_by="exam",
            )
            db_session.commit()
            accepted += 1
        except ExecutionSpendError:
            db_session.rollback()
    row = (
        db_session.query(ProviderSpendAuthorization)
        .filter(ProviderSpendAuthorization.goal_id == goal.id)
        .one()
    )
    real = sum(
        estimate_cost("openai", "gpt-4o-mini", prompt_tokens, cap) or Decimal("0")
        for prompt_tokens, cap in seen
    )
    assert seen
    assert all(0 < cap < 4_000 for _, cap in seen)
    assert real <= Decimal("0.01")
    assert Decimal(str(row.spent_cost_usd or 0)) <= Decimal("0.01")
    assert accepted >= 1


@pytest.mark.asyncio
async def test_spend8b_unicode_prompt_is_refused_before_any_paid_request(db_session, owner_id, monkeypatch):
    goal = _goal(db_session, owner_id)
    _grant_custom(db_session, owner_id, goal, max_cost="0.05", max_requests=100)
    seen = []

    def _ok(messages):
        return ChatResult(
            content="ok",
            provider="openai",
            model="gpt-4o-mini",
            raw_usage={"prompt_tokens": 20_000, "completion_tokens": 100},
        )

    _real_chain(monkeypatch, ["gpt-4o-mini"], {"gpt-4o-mini": _ok}, seen)
    prompt = [Message(role="user", content="\U0001F600" * 20_000)]
    outcomes = []
    for _ in range(3):
        try:
            await billed_execution_chat(
                db_session,
                prompt,
                owner_id=owner_id,
                goal_id=goal.id,
                purpose="exam",
                requested_by="exam",
            )
            db_session.commit()
            outcomes.append("accepted")
        except ExecutionSpendError as exc:
            db_session.rollback()
            outcomes.append(str(exc)[:80])
    assert seen == []
    assert _counted_events(db_session, goal.id) == []
    assert outcomes.count("accepted") == 0
    assert "estimated prompt" in outcomes[0]


@pytest.mark.asyncio
async def test_spend8c_gemini_receives_max_output_tokens_and_counts_thoughts(
    db_session, owner_id, monkeypatch
):
    goal = _goal(db_session, owner_id)
    _grant_custom(
        db_session,
        owner_id,
        goal,
        max_cost="1.00",
        max_requests=5,
        per_request="0.50",
        providers=("gemini",),
        models=("gemini-2.5-flash",),
    )
    provider = GeminiProvider()
    monkeypatch.setattr(GeminiProvider, "is_configured", lambda self: True)
    monkeypatch.setattr(billed_chat, "resolve_chat_chain", lambda db: [(provider, "gemini-2.5-flash")])
    monkeypatch.setattr(
        "app.egress_policy.enforce_egress_policy",
        lambda db, *, payload, **kw: payload,
        raising=True,
    )
    sent = []

    async def _post(self, action, model, payload, timeout):
        sent.append(payload)
        return {
            "candidates": [{"content": {"parts": [{"text": "ok"}]}}],
            "usageMetadata": {
                "promptTokenCount": 10,
                "candidatesTokenCount": 100,
                "thoughtsTokenCount": 20,
            },
        }

    monkeypatch.setattr(GeminiProvider, "_post", _post)
    result, attempted = await billed_execution_chat(
        db_session,
        [Message(role="user", content="x")],
        owner_id=owner_id,
        goal_id=goal.id,
        purpose="exam",
        requested_by="exam",
    )
    db_session.commit()
    cap = (sent[0].get("generationConfig") or {}).get("maxOutputTokens")
    assert sent
    assert cap is not None and cap > 0
    assert result.raw_usage["completion_tokens"] == 120
    assert result.raw_usage["thoughts_tokens"] == 20
    assert attempted == ["gemini"]
