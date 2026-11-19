import uuid
from datetime import datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import text

from app.config import get_settings
from app.models.provider_disclosure import ProviderDisclosureEvent
from app.models.provider_verification import ProviderVerificationCheck, VerificationResult
from app.providers.base import ChatResult, Message
from app.providers.catalog import (
    DeploymentLocality,
    ModelSelectionError,
    ModelSelectionPolicy,
    evaluate_model,
    get_model_spec,
    model_specs,
)
from app.providers.openai_provider import OpenAIProvider
from app.providers.registry import (
    chat_with_fallback,
    provider_names,
    resolve_chat_chain,
    select_model,
)
from app.providers.zai_provider import ZAIProvider

FOUNDER_EMAIL = "founder@lifeos.local"
FOUNDER_PASSWORD = "TestFounderPassword123!"


def _verified(db, provider: str, model: str, role: str = "chat", *, age_seconds: int = 0):
    db.add(
        ProviderVerificationCheck(
            id=uuid.uuid4(),
            provider_name=provider,
            role=role,
            model=model,
            result=VerificationResult.ok,
            message="Verifierad.",
            checked_by="founder",
            checked_at=datetime.utcnow() - timedelta(seconds=age_seconds),
        )
    )
    db.commit()


def _login(client) -> str:
    response = client.post(
        "/api/auth/login",
        json={"email": FOUNDER_EMAIL, "password": FOUNDER_PASSWORD},
    )
    assert response.status_code == 200
    return response.json()["csrf_token"]


def test_catalog_covers_every_registered_provider_with_truthful_metadata():
    assert set(provider_names()) == {
        "openai",
        "anthropic",
        "gemini",
        "deepseek",
        "openrouter",
        "ollama",
        "zai",
    }
    assert {spec.provider for spec in model_specs()} == set(provider_names())
    for spec in model_specs():
        assert spec.model_id
        assert spec.version
        assert spec.capabilities
        assert spec.license_name
        assert spec.pricing.source
        # Inherited snapshots have no independently captured date; never label them fresh.
        assert spec.pricing.as_of is None
        assert spec.quota.note
        assert spec.locality in (DeploymentLocality.local, DeploymentLocality.external)


def test_unknown_model_and_partial_manual_request_fail_closed(db_session):
    policy = ModelSelectionPolicy(role="chat", allow_external=True, require_verified=False)
    with pytest.raises(ModelSelectionError, match="unregistered"):
        select_model(
            db_session,
            policy,
            requested_provider="openai",
            requested_model="invented-model",
        )
    with pytest.raises(ModelSelectionError, match="supplied together"):
        select_model(db_session, policy, requested_provider="openai")


def test_external_and_capability_policy_is_deterministic():
    spec = get_model_spec("openai", "gpt-4o-mini")
    allowed, reason = evaluate_model(spec, ModelSelectionPolicy(role="chat"))
    assert allowed is False
    assert reason == "external provider is not allowed"

    allowed, reason = evaluate_model(
        spec,
        ModelSelectionPolicy(
            role="embedding",
            allow_external=True,
            require_verified=False,
        ),
    )
    assert allowed is False
    assert reason == "required capability is not declared"


def test_context_and_unknown_price_never_silently_pass_a_bound():
    ollama = get_model_spec("ollama", "llama3.1")
    allowed, reason = evaluate_model(
        ollama,
        ModelSelectionPolicy(
            role="chat",
            estimated_input_tokens=ollama.context_window_tokens + 1,
            require_verified=False,
        ),
    )
    assert allowed is False
    assert "context" in reason

    auto_router = get_model_spec("openrouter", "openrouter/auto")
    allowed, reason = evaluate_model(
        auto_router,
        ModelSelectionPolicy(
            role="chat",
            allow_external=True,
            max_input_price_per_million_usd=Decimal("1"),
            require_verified=False,
        ),
    )
    assert allowed is False
    assert reason == "input price is unknown"


def test_automatic_selection_requires_exact_fresh_verification(
    db_session,
    monkeypatch,
):
    monkeypatch.setattr(get_settings(), "openai_api_key", "configured")
    policy = ModelSelectionPolicy(
        role="chat",
        allow_external=True,
        prefer_local=False,
        allowed_providers=frozenset({"openai"}),
    )

    with pytest.raises(ModelSelectionError, match="no configured model"):
        select_model(db_session, policy)

    _verified(db_session, "openai", "text-embedding-3-small", role="chat")
    with pytest.raises(ModelSelectionError, match="no configured model"):
        select_model(db_session, policy)

    _verified(db_session, "openai", "gpt-4o-mini", age_seconds=301)
    with pytest.raises(ModelSelectionError, match="no configured model"):
        select_model(db_session, policy)

    _verified(db_session, "openai", "gpt-4o-mini")
    provider, model, spec = select_model(db_session, policy)
    assert (provider.name, model, spec.model_id) == (
        "openai",
        "gpt-4o-mini",
        "gpt-4o-mini",
    )


def test_automatic_selection_prefers_verified_local_model(db_session):
    _verified(db_session, "ollama", "llama3.1")
    policy = ModelSelectionPolicy(
        role="chat",
        allow_external=True,
        prefer_local=True,
    )
    provider, model, spec = select_model(db_session, policy)
    assert provider.name == "ollama"
    assert model == "llama3.1"
    assert spec.locality == DeploymentLocality.local


def test_fallback_policy_cannot_add_external_provider(
    db_session,
    monkeypatch,
):
    settings = get_settings()
    monkeypatch.setattr(settings, "default_llm_provider", "ollama")
    monkeypatch.setattr(settings, "default_llm_model", "llama3.1")
    monkeypatch.setattr(settings, "chat_fallback_order", "openai")
    monkeypatch.setattr(settings, "openai_api_key", "configured")
    _verified(db_session, "ollama", "llama3.1")
    _verified(db_session, "openai", "gpt-4o-mini")

    chain = resolve_chat_chain(
        db_session,
        selection_policy=ModelSelectionPolicy(
            role="chat",
            allow_external=False,
            require_verified=True,
        ),
    )
    assert [(provider.name, model) for provider, model in chain] == [
        ("ollama", "llama3.1")
    ]


@pytest.mark.asyncio
async def test_never_egress_content_aborts_fallback_before_external_call(
    db_session,
    make_verified_user,
    monkeypatch,
):
    user, _ = make_verified_user()
    # The disclosure ledger is owner-scoped under RLS. Exercise the production boundary
    # with the same authoritative tenant context used by the existing egress-policy tests.
    db_session.execute(
        text("SET LOCAL app.current_user_id = :owner_id"),
        {"owner_id": str(user.id)},
    )
    settings = get_settings()
    monkeypatch.setattr(settings, "default_llm_provider", "openai")
    monkeypatch.setattr(settings, "default_llm_model", "gpt-4o-mini")
    monkeypatch.setattr(settings, "openai_api_key", "configured")
    monkeypatch.setattr(settings, "chat_fallback_order", "anthropic")

    called = {"value": False}

    async def _must_not_call(self, messages, model, **kwargs):
        called["value"] = True
        raise AssertionError("protected content reached a provider")

    monkeypatch.setattr(OpenAIProvider, "chat", _must_not_call)

    # RuntimeError is the stable public base across test/dev module reloads; the exact
    # denial message and ledger row prove this is the egress boundary, not a provider error.
    with pytest.raises(RuntimeError, match="egress denied"):
        await chat_with_fallback(
            db_session,
            [Message(role="user", content="NEVER_EGRESS: tenant-private")],
            owner_id=user.id,
            purpose="model_selection_security_test",
        )

    assert called["value"] is False
    event = (
        db_session.query(ProviderDisclosureEvent)
        .filter(ProviderDisclosureEvent.owner_id == user.id)
        .one()
    )
    assert event.decision == "denied"
    assert event.provider == "openai"


@pytest.mark.asyncio
async def test_zai_adapter_uses_header_auth_and_normalized_usage(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "zai_api_key", "zai-test-secret")
    monkeypatch.setattr(settings, "zai_base_url", "https://api.z.ai/api/paas/v4/")
    captured = {}

    async def _post(self, url, *, headers, json):
        captured.update(url=url, headers=headers, json=json)
        request = httpx.Request("POST", url)
        return httpx.Response(
            200,
            request=request,
            json={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 3},
            },
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", _post)
    result = await ZAIProvider().chat(
        [Message(role="user", content="hej")],
        model="glm-5.3",
    )

    assert captured["url"] == "https://api.z.ai/api/paas/v4/chat/completions"
    assert "zai-test-secret" not in captured["url"]
    assert captured["headers"]["Authorization"] == "Bearer zai-test-secret"
    assert result.provider == "zai"
    assert result.model == "glm-5.3"
    assert result.raw_usage == {"prompt_tokens": 2, "completion_tokens": 3}


def test_catalog_and_selection_endpoints_are_founder_only_and_manual_choice_is_verified(
    client,
    monkeypatch,
):
    assert client.get("/api/admin/providers/models").status_code in (401, 403)
    csrf = _login(client)
    monkeypatch.setattr(get_settings(), "openai_api_key", "configured")

    async def _verified_chat(self, messages, model, **kwargs):
        return ChatResult(content="pong", provider="openai", model=model)

    monkeypatch.setattr(OpenAIProvider, "chat", _verified_chat)

    verify = client.post(
        "/api/admin/providers/verify",
        json={"provider": "openai", "role": "chat", "model": "gpt-4o-mini"},
        headers={"X-CSRF-Token": csrf},
    )
    assert verify.status_code == 200
    assert verify.json()["result"] == "ok"

    update = client.put(
        "/api/admin/providers/config",
        json={"role": "chat", "provider": "openai", "model": "gpt-4o-mini"},
        headers={"X-CSRF-Token": csrf},
    )
    assert update.status_code == 200


def test_unregistered_manual_model_is_rejected_before_configuration(client):
    csrf = _login(client)
    response = client.put(
        "/api/admin/providers/config",
        json={"role": "chat", "provider": "openai", "model": "not-registered"},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 409
