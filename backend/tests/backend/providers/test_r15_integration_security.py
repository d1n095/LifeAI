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


def test_uncertain_chat_outcome_is_durable_and_retry_is_blocked(client, db_session, superuser_db, monkeypatch):
    from app.routers import chat
    from app.providers.openai_provider import OpenAIProvider
    async def empty(*a, **k):
        assert k["effect_guard"] is True
        return []
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
    import json
    from app.models.audit import AuditLog
    event = superuser_db.query(AuditLog).filter_by(action="chat_provider_intent").one()
    detail = json.loads(event.detail)
    assert detail["provider"] == "openai" and detail["model"] == get_settings().default_llm_model
    assert detail["source_ref"].startswith(f"chat:{r.json()['user_message_id']}:inflight:")
    assert event.entity_id == r.json()["assistant_message_id"]
    saved = superuser_db.get(StoredMessage,r.json()["assistant_message_id"])
    assert saved.provider == detail["provider"] and saved.model == detail["model"]


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


def test_evaluation_observations_are_scoped_and_erased(monkeypatch):
    from app.providers.zai_eval_provider import evaluation_observation
    monkeypatch.setattr(get_settings(),"environment","test")
    with registry.evaluation_provider_scope(FakeEvaluation,{"chat":"glm","embedding":"bow"},authorized=True):
        lease = registry._current_evaluation()
        lease.observation["system"] = "owner-private"
        assert Context().run(evaluation_observation) == {}
        with registry.evaluation_provider_scope(FakeEvaluation,{"chat":"other","embedding":"bow"},authorized=True):
            assert evaluation_observation() == {}
        assert evaluation_observation()["system"] == "owner-private"
    assert lease.observation == {} and evaluation_observation() == {}


@pytest.mark.asyncio
async def test_eval_cli_never_inherits_http_credentials(monkeypatch):
    import json
    import subprocess
    from pathlib import Path
    monkeypatch.setattr(get_settings(),"environment","test")
    for key in ("OPENAI_API_KEY","ZAI_API_KEY","DATABASE_URL","SECRET_KEY"):
        monkeypatch.setenv(key,"must-not-reach-cli")
    captured = {}
    def run(cmd,**kwargs):
        captured.update(kwargs)
        Path(cmd[cmd.index("-o")+1]).write_text(json.dumps({"model":"glm", "choices":[{"message":{"content":"ok"}}],
            "usage":{"prompt_tokens":1,"completion_tokens":1}}))
        return SimpleNamespace(returncode=0,stdout="",stderr="")
    monkeypatch.setattr(subprocess,"run",run)
    with registry.evaluation_provider_scope(ZAIEvaluationProvider,{"chat":"glm","embedding":"bow"},authorized=True):
        result = await ZAIEvaluationProvider().chat([Message("user","safe")],model="glm")
    assert result.provider == "zai_eval" and result.model == "glm"
    assert all(key not in captured["env"] for key in ("OPENAI_API_KEY","ZAI_API_KEY","DATABASE_URL","SECRET_KEY"))


@pytest.mark.asyncio
async def test_malformed_usage_cannot_settle_a_billed_hold_as_free(db_session,monkeypatch):
    events = []
    async def reply(*a,**k):
        return ChatResult("nonempty", "transport", "model", raw_usage={})
    monkeypatch.setattr(registry,"resolve_chat_chain",lambda *a,**k:[
        (SimpleNamespace(name="transport",chat=reply,is_configured=lambda:True),"model")])
    observer = SimpleNamespace(before=lambda *a:events.append("reserved"),
                               failed=lambda e:events.append("held"),succeeded=lambda r:pytest.fail("settled unknown usage"))
    with pytest.raises(ProviderError) as error:
        await registry.chat_with_fallback(db_session,[],effect_guard=True,attempt_observer=observer)
    assert error.value.category == "external_effect_unknown" and events == ["reserved","held"]


def test_proven_pre_effect_retry_uses_new_budget_attempt(client,db_session,monkeypatch):
    from app.routers import chat
    from app.providers.ollama_provider import OllamaProvider
    from app.billing.enforcement import ComputeBudgetStore
    from app.billing import reservation
    async def empty(*a,**k): return []
    calls = []
    async def invoke(self,messages,model,**kwargs):
        calls.append(True)
        if len(calls) == 1:
            raise ProviderError("not sent",provider_request_may_have_left=False)
        return ChatResult("ok","ollama",model,raw_usage={"prompt_tokens":1,"completion_tokens":2})
    db_session.add(ProviderConfig(role="chat",provider="ollama",model="gemma3:4b",is_active=True))
    db_session.commit()
    monkeypatch.setattr(get_settings(),"chat_fallback_order","")
    monkeypatch.setattr(get_settings(),"billing_enforcement_enabled",True)
    monkeypatch.setattr(chat,"retrieve_context",empty)
    monkeypatch.setattr(OllamaProvider,"chat",invoke)
    store = ComputeBudgetStore(redis_url=None)
    monkeypatch.setattr(reservation,"_store",lambda *a:store)
    limits = SimpleNamespace(tier="free",token_caps=lambda:(None,None),cost_caps_micros=lambda:(None,None))
    monkeypatch.setattr(chat,"effective_limits",lambda *a:limits)
    headers = login(client)
    first = client.post("/api/chat",json={"message":"safe pre-effect retry"},headers=headers)
    assert first.status_code == 200 and first.json()["retryable"]
    retried = client.post(f"/api/chat/messages/{first.json()['user_message_id']}/retry",headers=headers)
    assert retried.status_code == 200 and retried.json()["assistant_status"] == "succeeded"
    assert len(calls) == 2 and len(store._reservations) == 2
    assert {r[0] for r in store._reservations.values()} == {"released","settled"}
    assert all(value == (3,0) for value in store._local.values())

@pytest.mark.asyncio
@pytest.mark.parametrize("provider_name", ["openai", "zai", "gemini", "unknown", "zai_eval"])
async def test_guarded_embedding_refuses_unmetered_external_effects(db_session, provider_name):
    calls = []
    class External:
        name = provider_name
        async def embed(self, *args, **kwargs):
            calls.append(True)
            return [[1.0]]
    with pytest.raises(ProviderError, match="metered effect contract") as error:
        await registry.embed_with_policy(db_session, External(), ["query"], model="embedding", effect_guard=True)
    assert error.value.provider_request_may_have_left is False
    assert calls == []


@pytest.mark.asyncio
async def test_guarded_retrieval_propagates_guard_and_preserves_owner(db_session, monkeypatch):
    from app.rag import retrieve
    import uuid
    owner = uuid.uuid4()
    captured = []
    monkeypatch.setattr(retrieve, "resolve_active", lambda *a, **kw: (SimpleNamespace(name="openai"), "embedding"))
    async def embed(*args, **kwargs):
        captured.append(kwargs)
        raise ProviderError("metered effect contract", category="unsupported", provider_request_may_have_left=False)
    monkeypatch.setattr(retrieve, "embed_with_policy", embed)
    with pytest.raises(ProviderError):
        await retrieve.retrieve_context(db_session, owner, "query", effect_guard=True)
    assert captured[0]["effect_guard"] is True
    assert captured[0]["owner_id"] == owner


@pytest.mark.asyncio
async def test_guarded_embedding_keeps_local_transport_and_egress_gate(db_session, monkeypatch):
    import app.egress_policy as egress
    import uuid
    calls = []
    class Local:
        name = "ollama"
        async def embed(self, texts, model):
            calls.append((texts, model))
            return [[1.0]]
    owner = uuid.uuid4()
    def gate(db, **kwargs):
        assert kwargs["owner_id"] == owner
        return ["redacted"]
    monkeypatch.setattr(egress, "enforce_egress_policy", gate)
    assert await registry.embed_with_policy(db_session, Local(), ["query"], model="nomic", owner_id=owner, effect_guard=True) == [[1.0]]
    assert calls == [(["redacted"], "nomic")]

@pytest.mark.asyncio
async def test_guarded_embedding_only_allows_evaluation_with_active_lease(db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "environment", "test")
    async def embed(self, texts, model, **kwargs):
        return [[1.0]]
    monkeypatch.setattr(FakeEvaluation, "embed", embed)
    with registry.evaluation_provider_scope(FakeEvaluation, {"chat":"glm", "embedding":"bow"}, authorized=True):
        assert await registry.embed_with_policy(db_session, FakeEvaluation(), ["query"], model="bow", effect_guard=True) == [[1.0]]
    with pytest.raises(ProviderError):
        await registry.embed_with_policy(db_session, FakeEvaluation(), ["query"], model="bow", effect_guard=True)


@pytest.mark.parametrize("question", ["Var är vi?", "Vad är nästa steg?", "Review PR 464", "Fortsätt integrationen", "What is the project status?", "Koda nästa uppgift"])
def test_project_context_keeps_status_questions(question):
    from app.project_status.service import project_status_relevant
    assert project_status_relevant(question)


@pytest.mark.parametrize("question", ["Hej, vad heter jag?", "Vad vet vi om kaffe och sömn?"])
def test_unrelated_question_cannot_read_or_invent_project_status(monkeypatch, question):
    from app.project_status import service
    import uuid
    def forbidden(*args, **kwargs):
        pytest.fail("unrelated question loaded project snapshot")
    monkeypatch.setattr(service, "build_project_status", forbidden)
    block = service.format_project_status_instructions(None, owner_id=uuid.uuid4(), question=question)
    assert "inte hämtad" in block and "utan registerfakta" in block


def test_relevant_question_preserves_owner_for_existing_status_composer(monkeypatch):
    from app.project_status import service
    import uuid
    owner = uuid.uuid4()
    observed = []
    def stop(db, *, owner_id):
        observed.append(owner_id)
        raise RuntimeError("snapshot reached")
    monkeypatch.setattr(service, "build_project_status", stop)
    with pytest.raises(RuntimeError, match="snapshot reached"):
        service.format_project_status_instructions(None, owner_id=owner, question="Projektstatus?")
    assert observed == [owner]


def test_project_status_followup_keeps_recent_user_context(client, monkeypatch):
    from app.routers import chat
    from app.providers.openai_provider import OpenAIProvider
    captured = []
    async def empty(*args, **kwargs):
        return []
    async def reply(self, messages, model, **kwargs):
        return ChatResult("ok", "openai", model, {"prompt_tokens": 10, "completion_tokens": 1})
    def status(db, *, owner_id, question):
        captured.append((owner_id, question))
        return "REGISTERFAKTA"
    monkeypatch.setattr(chat, "retrieve_context", empty)
    monkeypatch.setattr(chat, "format_project_status_instructions", status)
    monkeypatch.setattr(OpenAIProvider, "chat", reply)
    headers = login(client)
    first = client.post("/api/chat", json={"message":"Projektstatus?"}, headers=headers)
    assert first.status_code == 200 and first.json()["assistant_status"] == "succeeded"
    second = client.post("/api/chat", json={"message":"Och sedan?", "conversation_id":first.json()["conversation_id"]}, headers=headers)
    assert second.status_code == 200 and second.json()["assistant_status"] == "succeeded"
    assert len(captured) == 2 and captured[0][0] == captured[1][0]
    assert "Projektstatus?" in captured[1][1] and "Och sedan?" in captured[1][1]


def test_knowledge_search_surfaces_embedding_transport_failure(client, monkeypatch):
    """An unwrapped httpx failure from embed() must be a 503, never an unhandled 500."""
    import httpx

    from app.rag import retrieve

    async def fail(*args, **kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(
        retrieve,
        "resolve_active",
        lambda *a, **k: (SimpleNamespace(name="ollama", is_local=True), "nomic-embed-text"),
    )
    monkeypatch.setattr(retrieve, "embed_with_policy", fail)

    response = client.post(
        "/api/knowledge/search", json={"query": "fraga"}, headers=login(client)
    )

    assert response.status_code == 503, response.text
    assert "Kunskapssökning" in response.json()["detail"]


def test_chat_surfaces_degraded_grounding_on_embedding_transport_failure(client, monkeypatch):
    """Chat still answers, but explicitly reports degraded grounding instead of silence."""
    import httpx

    from app.providers.openai_provider import OpenAIProvider
    from app.rag import retrieve

    async def fail(*args, **kwargs):
        raise httpx.ConnectError("connection refused")

    async def reply(self, messages, model, **kwargs):
        return ChatResult(
            "Svar.",
            "openai",
            model,
            raw_usage={"prompt_tokens": 5, "completion_tokens": 2},
        )

    monkeypatch.setattr(
        retrieve,
        "resolve_active",
        lambda *a, **k: (SimpleNamespace(name="ollama", is_local=True), "nomic-embed-text"),
    )
    monkeypatch.setattr(retrieve, "embed_with_policy", fail)
    monkeypatch.setattr(OpenAIProvider, "chat", reply)
    monkeypatch.setattr(get_settings(), "chat_fallback_order", "")

    response = client.post("/api/chat", json={"message": "fraga"}, headers=login(client))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["assistant_status"] == "succeeded"
    assert body["grounding_status"] == "degraded"
    assert "Kunskapssökning" in body["grounding_reason"]
    assert body["sources"] == []
    assert body["context_status"]["reason"] == "search_provider_unavailable"
