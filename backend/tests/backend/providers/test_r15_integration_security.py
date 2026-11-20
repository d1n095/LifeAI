"""Exact R15 + #463 boundaries; all adapters mocked, real PostgreSQL/Redis fixtures."""
from contextvars import Context
from dataclasses import replace
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from app.config import get_settings
from app.providers import registry, verification
from app.providers.base import ChatResult, Message, ProviderError
from app.providers.zai_eval_provider import ZAIEvaluationProvider
from app.providers.zai_provider import ZAIProvider
from app.models.provider_config import ProviderConfig
from app.models.usage import UsageLog
from app.models.conversation import Message as StoredMessage
from app.billing.enforcement import ComputeBudgetStore, BillingEnforcementUnavailableError


class FakeEvaluation(ZAIEvaluationProvider):
    def is_configured(self):
        return True


@pytest.mark.parametrize("environment", ["production", "prod", "unknown"])
def test_evaluation_refused_in_untrusted_environments(monkeypatch, environment):
    monkeypatch.setattr(get_settings(), "environment", environment)
    with pytest.raises(ProviderError):
        with registry.evaluation_provider_scope(FakeEvaluation, {"chat":"glm", "embedding":"bow"}, authorized=True):
            pytest.fail("evaluation authorized")


def test_evaluation_identity_is_task_local_and_never_mutates_config(db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "environment", "test")
    db_session.add(ProviderConfig(role="chat", provider="zai", model="glm-5.3", is_active=True))
    db_session.commit()
    original = registry._PROVIDERS["zai"]
    with pytest.raises(ProviderError):
        with registry.evaluation_provider_scope(FakeEvaluation, {"chat":"glm", "embedding":"bow"}):
            pass
    with pytest.raises(RuntimeError, match="exit test"):
        with registry.evaluation_provider_scope(FakeEvaluation, {"chat":"glm", "embedding":"bow"}, authorized=True):
            provider, model = registry.resolve_active(db_session, "chat")
            assert provider.name == "zai_eval" and model == "glm"
            assert isinstance(registry.get_provider("zai"), ZAIProvider)
            independent = Context().run(registry.resolve_active, db_session, "chat")
            assert independent[0].name == "zai" and independent[1] == "glm-5.3"
            assert [p.name for p, _ in registry.resolve_chat_chain(db_session)] == ["zai_eval"]
            assert registry._PROVIDERS["zai"] is original
            assert db_session.query(ProviderConfig).one().model == "glm-5.3"
            raise RuntimeError("exit test")
    assert registry.resolve_active(db_session, "chat")[0].name == "zai"
    assert "zai_eval" not in registry.provider_names()
    with pytest.raises(ProviderError):
        registry.get_provider("zai_eval")


@pytest.mark.asyncio
async def test_verification_composes_exact_model_and_local_timeout(db_session, monkeypatch):
    from app.providers.ollama_provider import OllamaProvider
    monkeypatch.setattr(get_settings(), "local_verification_timeout_seconds", 123)
    captured = []
    async def chat(self, messages, model, **kwargs):
        captured.append((model, kwargs["timeout"]))
        return ChatResult("ok", "ollama", model)
    monkeypatch.setattr(OllamaProvider, "chat", chat)
    result = await verification.verify_now(db_session, "ollama", "chat", model="gemma3:4b", checked_by="founder")
    assert result.result.value == "ok" and result.model == "gemma3:4b"
    assert captured == [("gemma3:4b", 123)]


@pytest.mark.parametrize("redis_backed", [False, True])
def test_budget_settlement_is_atomic_and_idempotent(redis_backed):
    store = ComputeBudgetStore(redis_url=get_settings().redis_url if redis_backed else None)
    reservation = store.reserve(subject_type="user", subject_id="integration-idempotency", estimated_tokens=100,
                                estimated_cost_usd=Decimal("1"))
    def settle(_):
        store.settle(reservation, actual_tokens=40, actual_cost_usd=Decimal("0.4"))
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(settle, range(12)))
    day = store.remaining(subject_type="user", subject_id="integration-idempotency",
                          token_caps=(1000,1000), cost_caps_micros=(10000000,10000000))["day"]
    assert day[0] == 960
    assert day[1] == 9600000
    with pytest.raises(BillingEnforcementUnavailableError):
        store.release(reservation)


@pytest.mark.parametrize("redis_backed", [False, True])
def test_budget_release_cannot_release_another_subject(redis_backed):
    store = ComputeBudgetStore(redis_url=get_settings().redis_url if redis_backed else None)
    a = store.reserve(subject_type="user", subject_id="subject-a", estimated_tokens=100, estimated_cost_usd=1)
    b = store.reserve(subject_type="tenant", subject_id="subject-b", estimated_tokens=100, estimated_cost_usd=1)
    with pytest.raises(BillingEnforcementUnavailableError):
        store.release(replace(a, period_keys=b.period_keys))
    store.release(a)
    store.release(a)
    assert store.remaining(subject_type="tenant", subject_id="subject-b", token_caps=(1000,1000),
                           cost_caps_micros=(10000000,10000000))["day"][0] == 900


@pytest.mark.asyncio
async def test_ambiguous_failure_never_falls_back_or_releases_hold(db_session, monkeypatch):
    calls, events = [], []
    async def first(*args, **kwargs):
        calls.append("first")
        raise ProviderError("private upstream response", provider_request_may_have_left=True)
    async def second(*args, **kwargs):
        calls.append("second")
        return ChatResult("unexpected", "second", "model")
    monkeypatch.setattr(registry, "resolve_chat_chain", lambda *a, **k: [
        (SimpleNamespace(name="first", chat=first, is_configured=lambda:True), "model"),
        (SimpleNamespace(name="second", chat=second, is_configured=lambda:True), "model")])
    observer = SimpleNamespace(before=lambda *a: events.append("reserved"),
                               failed=lambda e: events.append("held"), succeeded=lambda r: events.append("settled"))
    with pytest.raises(ProviderError) as error:
        await registry.chat_with_fallback(db_session, [Message("user","test")], effect_guard=True, attempt_observer=observer)
    assert error.value.category == "external_effect_unknown"
    assert "private" not in str(error.value)
    assert calls == ["first"] and events == ["reserved", "held"]


@pytest.mark.asyncio
async def test_proven_pre_effect_failure_can_fall_back(db_session, monkeypatch):
    async def first(*args, **kwargs):
        raise ProviderError("not sent", provider_request_may_have_left=False)
    async def second(*args, **kwargs):
        return ChatResult("ok", "second", "m")
    monkeypatch.setattr(registry, "resolve_chat_chain", lambda *a, **k: [
        (SimpleNamespace(name="first", chat=first, is_configured=lambda:True), "m"), (SimpleNamespace(name="second", chat=second, is_configured=lambda:True), "m")])
    result, attempts = await registry.chat_with_fallback(db_session, [], effect_guard=True)
    assert result.content == "ok" and attempts == ["first", "second"]


def login(client):
    r = client.post("/api/auth/login", json={"email":"founder@lifeos.local", "password":"TestFounderPassword123!"})
    assert r.status_code == 200
    return {"X-CSRF-Token":r.json()["csrf_token"]}


def test_uncertain_chat_outcome_is_durable_and_retry_is_blocked(client, db_session, monkeypatch):
    from app.routers import chat
    from app.providers.openai_provider import OpenAIProvider
    async def empty(*a, **k): return []
    calls = []
    async def timeout(*a, **k):
        calls.append(True)
        raise ProviderError("timeout", provider_request_may_have_left=True)
    monkeypatch.setattr(chat, "retrieve_context", empty)
    monkeypatch.setattr(OpenAIProvider, "chat", timeout)
    headers = login(client)
    r = client.post("/api/chat", json={"message":"durable-effect"}, headers=headers)
    assert r.status_code == 200
    assert r.json()["error_category"] == "external_effect_unknown" and not r.json()["retryable"]
    db_session.rollback()
    retry = client.post(f"/api/chat/messages/{r.json()['user_message_id']}/retry", headers=headers)
    assert retry.status_code == 409 and calls == [True]
    assert db_session.query(UsageLog).filter_by(role="chat").count() == 0
    from app.request_context import current_user_id
    from app.founder import FOUNDER_USER_ID
    from sqlalchemy import text
    current_user_id.set(str(FOUNDER_USER_ID))
    db_session.execute(text("SET LOCAL app.current_user_id = :uid"), {"uid":str(FOUNDER_USER_ID)})
    assert db_session.query(StoredMessage).filter_by(error_category="external_effect_unknown").count() == 1


def test_external_unknown_price_blocks_before_provider_when_enforcement_on(client, monkeypatch):
    from app.routers import chat
    from app.providers.openai_provider import OpenAIProvider
    async def empty(*a, **k): return []
    calls = []
    async def forbidden(*a, **k):
        calls.append(True)
        raise AssertionError("provider invoked")
    monkeypatch.setattr(chat, "retrieve_context", empty)
    monkeypatch.setattr(OpenAIProvider, "chat", forbidden)
    monkeypatch.setattr(get_settings(), "billing_enforcement_enabled", True)
    r = client.post("/api/chat", json={"message":"price-bound"}, headers=login(client))
    assert r.status_code == 503 and calls == []


def test_concurrent_reply_claim_refreshes_cached_failed_reply(db_session, make_verified_user):
    import threading
    from fastapi import HTTPException
    from sqlalchemy import text
    from app.db import SessionLocal
    from app.request_context import current_user_id
    from app.models.conversation import Conversation, MessageRole, MessageStatus
    from app.models.user import User
    from app.routers.chat import _claim_reply_attempt, _get_or_create_reply_slot
    user, _ = make_verified_user()
    current_user_id.set(str(user.id))
    db_session.execute(text("SET LOCAL app.current_user_id = :uid"), {"uid":str(user.id)})
    conversation = Conversation(user_id=user.id, title="claim race")
    db_session.add(conversation)
    db_session.flush()
    message = StoredMessage(conversation_id=conversation.id, role=MessageRole.user, content="request")
    db_session.add(message)
    db_session.flush()
    reply = StoredMessage(conversation_id=conversation.id, role=MessageRole.assistant, in_reply_to_id=message.id,
                          content="", status=MessageStatus.failed, error_category="unreachable")
    db_session.add(reply)
    db_session.commit()
    uid, cid, mid = user.id, conversation.id, message.id
    barrier = threading.Barrier(2)
    def claim(_):
        token = current_user_id.set(str(uid))
        try:
            with SessionLocal() as session:
                u, c, m = session.get(User,uid), session.get(Conversation,cid), session.get(StoredMessage,mid)
                cached = _get_or_create_reply_slot(session,mid)
                assert cached.error_category == "unreachable"
                barrier.wait(timeout=10)
                try:
                    _claim_reply_attempt(session, conversation=c, user_message=m, user=u)
                    return "claimed"
                except HTTPException as error:
                    assert error.status_code == 409
                    return "blocked"
        finally:
            current_user_id.reset(token)
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(claim, range(2))) == ["blocked", "claimed"]


def test_copied_evaluation_context_expires_on_scope_exit(db_session, monkeypatch):
    from contextvars import copy_context
    monkeypatch.setattr(get_settings(), "environment", "test")
    with registry.evaluation_provider_scope(FakeEvaluation, {"chat":"glm", "embedding":"bow"}, authorized=True):
        copied = copy_context()
    with pytest.raises(ProviderError, match="expired"):
        copied.run(registry.resolve_active, db_session, "chat")


@pytest.mark.asyncio
async def test_direct_eval_adapter_requires_context_before_subprocess(monkeypatch):
    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("CLI invoked without authority"))
    with pytest.raises(ProviderError, match="active authorized scope"):
        await ZAIEvaluationProvider().chat([], model="glm")
    with pytest.raises(ProviderError, match="active authorized scope"):
        await ZAIEvaluationProvider().embed(["private"], model="bow")


def test_usage_idempotency_cannot_change_owner_or_provider(db_session, make_verified_user):
    from app.billing import record_usage
    first, _ = make_verified_user()
    other, _ = make_verified_user()
    kwargs = dict(user_id=first.id, tenant_id=None, role="chat", provider="ollama", model="gemma3:4b",
                  prompt_tokens=5, completion_tokens=2, idempotency_key="owner-bound-usage")
    row = record_usage(db_session, **kwargs)
    db_session.commit()
    assert record_usage(db_session, **kwargs).id == row.id
    with pytest.raises(ValueError, match="identity mismatch"):
        record_usage(db_session, **{**kwargs,"user_id":other.id})
    with pytest.raises(ValueError, match="identity mismatch"):
        record_usage(db_session, **{**kwargs,"provider":"zai"})


@pytest.mark.parametrize("redis_backed", [False,True])
def test_reserve_replay_does_not_double_hold(redis_backed):
    store = ComputeBudgetStore(redis_url=get_settings().redis_url if redis_backed else None)
    kwargs = dict(subject_type="user",subject_id="reserve-replay",estimated_tokens=100,
                  estimated_cost_usd=1,idempotency_key="one-external-attempt",cost_caps_micros=(1000000,1000000))
    first = store.reserve(**kwargs)
    second = store.reserve(**kwargs)
    assert first.reservation_id == second.reservation_id
    assert store.remaining(subject_type="user",subject_id="reserve-replay",token_caps=(1000,1000),
                           cost_caps_micros=(1000000,1000000))["day"] == (900,0)
    with pytest.raises(BillingEnforcementUnavailableError):
        store.reserve(**{**kwargs,"estimated_tokens":101})


@pytest.mark.parametrize("provider_name", ["openai","anthropic","gemini","deepseek","openrouter","ollama","zai"])
@pytest.mark.asyncio
async def test_output_limit_reaches_every_transport(monkeypatch, provider_name):
    import httpx
    settings = get_settings()
    for key in ("openai_api_key","anthropic_api_key","google_api_key","deepseek_api_key","openrouter_api_key","zai_api_key"):
        monkeypatch.setattr(settings,key,"mock-test-key")
    payloads = []
    async def post(self,url,**kwargs):
        payloads.append(kwargs["json"])
        return httpx.Response(200,request=httpx.Request("POST",url),json={
            "choices":[{"message":{"content":"ok"}}], "message":{"content":"ok"},
            "content":[{"text":"ok"}], "candidates":[{"content":{"parts":[{"text":"ok"}]}}],
            "usage":{"prompt_tokens":1,"completion_tokens":1,"input_tokens":1,"output_tokens":1}})
    monkeypatch.setattr(httpx.AsyncClient,"post",post)
    await registry.get_provider(provider_name).chat([Message("user","safe")],model="test-model",max_tokens=7)
    payload = payloads[0]
    if provider_name == "gemini":
        assert payload["generationConfig"]["maxOutputTokens"] == 7
    elif provider_name == "ollama":
        assert payload["options"]["num_predict"] == 7
    else:
        assert payload["max_tokens"] == 7
