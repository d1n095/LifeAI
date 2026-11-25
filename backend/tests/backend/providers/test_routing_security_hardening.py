"""Adversarial routing tests; real PostgreSQL fixtures, no paid provider calls."""
import asyncio
import uuid
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import text

from app.config import Settings, get_settings
from app.models.provider_verification import ProviderVerificationCheck as Check, VerificationResult as Result
from app.providers import registry, verification
from app.providers.base import ChatResult, Message, ProviderError
from app.providers.catalog import ModelSelectionError, ModelSelectionPolicy, evaluate_model, get_model_spec
from app.providers.openai_provider import OpenAIProvider
from app.providers.zai_provider import ZAIProvider
from app.schemas import ModelSelectionPreviewIn


def check(db, provider="openai", model="gpt-4o-mini", role="chat", result=Result.ok, age=0, at=None):
    row = Check(provider_name=provider, model=model, role=role, result=result,
                message="classified", checked_by="founder",
                checked_at=at or datetime.utcnow()-timedelta(seconds=age))
    db.add(row)
    db.commit()
    return row


@pytest.mark.parametrize("failure", [Result.invalid_key, Result.unreachable, Result.unsupported, Result.rate_limited])
def test_newer_failure_revokes_older_pass(db_session, failure):
    check(db_session, age=30)
    check(db_session, result=failure)
    with pytest.raises(ModelSelectionError, match="fresh successful"):
        registry.select_model(db_session, ModelSelectionPolicy(allow_external=True),
                              requested_provider="openai", requested_model="gpt-4o-mini")


def test_equal_timestamp_failure_wins_and_future_pass_is_not_fresh(db_session):
    now = datetime.utcnow()
    check(db_session, at=now, result=Result.invalid_key)
    check(db_session, at=now)
    assert not registry._model_is_freshly_verified(db_session, get_model_spec("openai", "gpt-4o-mini"), "chat")
    check(db_session, at=now+timedelta(hours=1))
    assert not registry._model_is_freshly_verified(db_session, get_model_spec("openai", "gpt-4o-mini"), "chat")


@pytest.mark.asyncio
async def test_ingest_cache_is_exact_model(db_session, monkeypatch):
    check(db_session, model="different-model", role="embedding")
    calls = []
    async def probe(provider, model, role, **kwargs):
        calls.append(model)
        return verification.Outcome(Result.invalid_key, "classified")
    monkeypatch.setattr(verification, "verify_provider", probe)
    result = await verification.ensure_verified(db_session, "embedding")
    assert calls == [get_settings().default_embedding_model]
    assert result.result == Result.invalid_key


@pytest.mark.asyncio
async def test_slow_older_success_cannot_override_newer_failure(db_session, monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()
    async def probe(provider, model, role, **kwargs):
        started.set()
        await release.wait()
        return verification.Outcome(Result.ok, "classified")
    monkeypatch.setattr(verification, "verify_provider", probe)
    older = asyncio.create_task(verification.verify_now(db_session, "openai", "chat", checked_by="founder", model="gpt-4o-mini"))
    await started.wait()
    newer = check(db_session, result=Result.invalid_key)
    release.set()
    await older
    assert verification.latest_check(db_session, "openai", "chat", model="gpt-4o-mini").id == newer.id


@pytest.mark.parametrize("role,model", [("chat", "qwen2.5:0.5b"), ("embedding", "all-minilm"), ("chat", "gemma3:4b")])
def test_alpha_models_can_be_selected_without_external_permission(db_session, role, model):
    check(db_session, provider="ollama", model=model, role=role)
    provider, chosen, _ = registry.select_model(db_session, ModelSelectionPolicy(role=role),
                                               requested_provider="ollama", requested_model=model)
    assert (provider.name, chosen) == ("ollama", model)


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity", "-0.01"])
def test_nonfinite_or_negative_price_ceiling_is_rejected(bad):
    with pytest.raises(ValueError):
        ModelSelectionPreviewIn(max_input_price_per_million_usd=bad)
    eligible, _ = evaluate_model(get_model_spec("openai", "gpt-4o-mini"),
        ModelSelectionPolicy(allow_external=True, max_input_price_per_million_usd=Decimal(bad)))
    assert not eligible


@pytest.mark.parametrize("offset", [-31, 1])
def test_stale_or_future_price_snapshot_cannot_satisfy_ceiling(offset):
    spec = get_model_spec("openai", "gpt-4o-mini")
    spec = replace(spec, pricing=replace(spec.pricing, as_of=(date.today()+timedelta(days=offset)).isoformat()))
    assert evaluate_model(spec, ModelSelectionPolicy(allow_external=True,
        max_input_price_per_million_usd=Decimal("1"))) == (False, "price snapshot is stale")


def test_unknown_snapshot_date_context_output_and_capabilities_fail_closed():
    spec = get_model_spec("openai", "gpt-4o-mini")
    assert evaluate_model(spec, ModelSelectionPolicy(allow_external=True,
        max_input_price_per_million_usd=Decimal("1")))[0] is False
    assert evaluate_model(spec, ModelSelectionPolicy(allow_external=True,
        estimated_input_tokens=127999, estimated_output_tokens=2))[0] is False
    for capability in ("tools", "json", "vision"):
        assert not evaluate_model(spec, ModelSelectionPolicy(allow_external=True,
            required_capabilities=frozenset({capability})))[0]
    assert not evaluate_model(get_model_spec("ollama", "llama3.1"),
        ModelSelectionPolicy(minimum_context_tokens=100))[0]


@pytest.mark.parametrize("url", ["http://api.z.ai/api/paas/v4", "https://127.0.0.1/api/paas/v4",
    "https://169.254.169.254/api/paas/v4", "https://api.z.ai.evil.test/api/paas/v4",
    "https://secret@api.z.ai/api/paas/v4", "https://api.z.ai/api/paas/v4?key=secret",
    "https://api.z.ai:444/api/paas/v4", "https://api.z.ai/api/paas/v4/../admin"])
@pytest.mark.asyncio
async def test_zai_ssrf_and_credential_destination_fail_before_http(monkeypatch, url):
    with pytest.raises(ValueError):
        Settings(zai_base_url=url)
    monkeypatch.setattr(get_settings(), "zai_api_key", "test-secret")
    monkeypatch.setattr(get_settings(), "zai_base_url", url)
    async def no_http(*args, **kwargs):
        raise AssertionError("HTTP must not be attempted")
    monkeypatch.setattr(httpx.AsyncClient, "post", no_http)
    with pytest.raises(ProviderError) as error:
        await ZAIProvider().chat([], "glm-5.3")
    assert error.value.provider_request_may_have_left is False
    assert "secret" not in str(error.value)


@pytest.mark.parametrize("response", [None, {}, {"choices": []},
    {"choices": [{"message": {"content": None}}]},
    {"choices": [{"message": {"content": ""}}]},
    {"choices": [{"message": {"content": "ok"}}], "usage": {"prompt_tokens": -1, "completion_tokens": 1}},
    {"choices": [{"message": {"content": "ok"}}], "usage": {"prompt_tokens": True, "completion_tokens": 1}}])
@pytest.mark.asyncio
async def test_zai_malformed_success_never_becomes_pass(monkeypatch, response):
    monkeypatch.setattr(get_settings(), "zai_api_key", "test-secret")
    async def post(self, url, **kwargs):
        return httpx.Response(200, request=httpx.Request("POST", url), json=response)
    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    result = await verification.verify_provider(ZAIProvider(), "glm-5.3", "chat", timeout=1)
    assert result.result != Result.ok
    assert "test-secret" not in result.message


@pytest.mark.parametrize("status,category", [(302,"unreachable"), (401,"invalid_key"), (403,"invalid_key"),
    (429,"rate_limited"), (500,"unreachable")])
@pytest.mark.asyncio
async def test_zai_provider_errors_and_redirects_never_leak_body(monkeypatch, status, category):
    monkeypatch.setattr(get_settings(), "zai_api_key", "test-secret")
    async def post(self, url, **kwargs):
        assert "test-secret" not in url
        return httpx.Response(status, request=httpx.Request("POST", url), text="test-secret private response",
                              headers={"location": "http://169.254.169.254/"})
    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    with pytest.raises(ProviderError) as error:
        await ZAIProvider().chat([], "glm-5.3")
    assert error.value.category == category
    assert error.value.provider_request_may_have_left is True
    assert "test-secret" not in str(error.value)


@pytest.mark.asyncio
async def test_unsupported_zai_options_are_not_silently_dropped(monkeypatch):
    monkeypatch.setattr(get_settings(), "zai_api_key", "test-secret")
    with pytest.raises(ProviderError) as error:
        await ZAIProvider().chat([], "glm-5.3", tools=[{}])
    assert error.value.category == "unsupported"
    assert error.value.provider_request_may_have_left is False


@pytest.mark.asyncio
async def test_policy_rechecks_verification_after_chain_snapshot(db_session, make_verified_user, monkeypatch):
    from app.request_context import current_user_id

    user, _ = make_verified_user()
    current_user_id.set(str(user.id))
    db_session.execute(text("SET LOCAL app.current_user_id = :uid"), {"uid": str(user.id)})
    check(db_session)
    original = registry.resolve_chat_chain
    def snapshot(*args, **kwargs):
        chain = original(*args, **kwargs)
        check(db_session, result=Result.invalid_key)
        return chain
    monkeypatch.setattr(get_settings(), "chat_fallback_order", "")
    monkeypatch.setattr(registry, "resolve_chat_chain", snapshot)
    called = []
    async def no_call(*args, **kwargs):
        called.append(True)
        return ChatResult("bad", "openai", "gpt-4o-mini")
    monkeypatch.setattr(OpenAIProvider, "chat", no_call)
    with pytest.raises(ProviderError):
        await registry.chat_with_fallback(db_session, [Message("user", "safe")],
            selection_policy=ModelSelectionPolicy(allow_external=True), owner_id=user.id)
    assert called == []


def test_preview_and_verification_do_not_grant_authority(client):
    # Non-founder requests must fail before provider invocation or database mutation.
    for path in ("/api/admin/providers/select/preview", "/api/admin/providers/verify"):
        response = client.post(path, json={"provider":"zai", "model":"glm-5.3", "role":"chat"})
        assert response.status_code in (401, 403)
    assert client.get("/api/admin/providers/models").status_code in (401, 403)


@pytest.mark.asyncio
async def test_egress_hard_denial_aborts_two_configured_providers(db_session, make_verified_user, monkeypatch):
    from app.request_context import current_user_id
    from app.models.provider_disclosure import ProviderDisclosureEvent
    user, _ = make_verified_user()
    current_user_id.set(str(user.id))
    db_session.execute(text("SET LOCAL app.current_user_id = :uid"), {"uid": str(user.id)})
    monkeypatch.setattr(get_settings(), "chat_fallback_order", "zai")
    monkeypatch.setattr(get_settings(), "zai_api_key", "test-key")
    calls = []
    async def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError("content escaped")
    monkeypatch.setattr(OpenAIProvider, "chat", forbidden)
    monkeypatch.setattr(ZAIProvider, "chat", forbidden)
    with pytest.raises(RuntimeError, match="egress denied"):
        await registry.chat_with_fallback(db_session, [Message("user", "NEVER_EGRESS: private")], owner_id=user.id)
    assert calls == []
    events = db_session.query(ProviderDisclosureEvent).all()
    assert len(events) == 1 and events[0].decision == "denied" and events[0].owner_id == user.id


@pytest.mark.asyncio
async def test_each_fallback_attempt_keeps_tenant_and_audit(db_session, make_verified_user, monkeypatch):
    from app.request_context import current_user_id
    from app.models.provider_disclosure import ProviderDisclosureEvent
    user, _ = make_verified_user()
    current_user_id.set(str(user.id))
    db_session.execute(text("SET LOCAL app.current_user_id = :uid"), {"uid": str(user.id)})
    monkeypatch.setattr(get_settings(), "chat_fallback_order", "zai")
    monkeypatch.setattr(get_settings(), "zai_api_key", "test-key")
    async def unavailable(*args, **kwargs):
        raise ProviderError("unavailable", category="unreachable")
    async def success(*args, **kwargs):
        return ChatResult("ok", "zai", "glm-5.3")
    monkeypatch.setattr(OpenAIProvider, "chat", unavailable)
    monkeypatch.setattr(ZAIProvider, "chat", success)
    result, attempted = await registry.chat_with_fallback(db_session, [Message("user", "safe")], owner_id=user.id)
    assert result.content == "ok" and attempted == ["openai", "zai"]
    events = db_session.query(ProviderDisclosureEvent).all()
    assert {e.provider for e in events} == {"openai", "zai"}
    assert len(events) == 2 and all(e.owner_id == user.id and e.decision == "allowed" for e in events)


def test_primary_preference_cannot_waive_policy_but_local_fallback_remains_available(db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "chat_fallback_order", "ollama,zai")
    monkeypatch.setattr(get_settings(), "zai_api_key", "test-key")
    check(db_session, provider="ollama", model="llama3.1")
    # External primary is disallowed; an unrelated external fallback cannot gain authority.
    chain = registry.resolve_chat_chain(db_session, ModelSelectionPolicy(allow_external=False))
    assert [(p.name, m) for p, m in chain] == [("ollama", "llama3.1")]


def test_configuration_rolls_back_when_audit_cannot_be_written(client, db_session, monkeypatch):
    from app.routers import admin
    from app.models.provider_config import ProviderConfig
    login = client.post("/api/auth/login", json={"email":"founder@lifeos.local", "password":"TestFounderPassword123!"})
    csrf = login.json()["csrf_token"]
    check(db_session)
    def fail_audit(*args, **kwargs):
        raise RuntimeError("audit unavailable")
    monkeypatch.setattr(admin, "record_audit", fail_audit)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        client.put("/api/admin/providers/config", headers={"X-CSRF-Token":csrf},
                   json={"role":"chat", "provider":"openai", "model":"gpt-4o-mini"})
    assert db_session.query(ProviderConfig).count() == 0


@pytest.mark.asyncio
async def test_controlled_routing_cannot_skip_egress_by_omitting_owner(db_session):
    with pytest.raises(ModelSelectionError, match="requires owner_id"):
        await registry.chat_with_fallback(db_session, [Message("user", "private")],
            selection_policy=ModelSelectionPolicy(allow_external=True, require_verified=False))


@pytest.mark.parametrize("role", ["member", "founder"])
def test_other_account_cannot_route_or_verify_even_with_founder_role(client, make_verified_user, role):
    user, password = make_verified_user(role=role)
    login = client.post("/api/auth/login", json={"email":user.email, "password":password})
    assert login.status_code == 200
    headers = {"X-CSRF-Token":login.json()["csrf_token"]}
    assert client.get("/api/admin/providers/models").status_code == 403
    for path in ("/api/admin/providers/select/preview", "/api/admin/providers/verify", "/api/admin/providers/config"):
        response = client.request("PUT" if path.endswith("/config") else "POST", path,
            headers=headers, json={"provider":"zai", "role":"chat", "model":"glm-5.3"})
        assert response.status_code == 403
