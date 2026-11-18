"""P4 production-readiness: local-first routing without an OpenAI key.

Proves that with NO OpenAI API key configured:
1. The chat chain head is a LOCAL provider (ollama) — a keyless deployment still has a
   usable local model, and OpenAI never appears in the chain.
2. The consultation config gate refuses (fail-closed) — disabled flags plus the missing
   key mean no consultation code path can reach api.openai.com.
3. Admin/status surfaces still report the configured settings default (resolve_active is
   deliberately NOT changed — only the executable chain demotes unconfigured providers).
"""

import warnings

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.openai_consult import service
from app.providers import registry
from app.providers.ollama_provider import OllamaProvider
from app.providers.openai_provider import OpenAIProvider


@compiles(UUID, "sqlite")
def _uuid_sqlite(t, c, **kw):
    return "CHAR(32)"


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(t, c, **kw):
    return "JSON"


@pytest.fixture
def no_openai_env(monkeypatch):
    from app.db import Base
    import app.models.provider_config  # noqa: F401
    import app.models.user  # noqa: F401
    import app.models.provider_spend  # noqa: F401
    import app.models.execution_envelope  # noqa: F401

    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    settings = Settings(
        environment="development",
        openai_api_key=None,
        openai_consult_enabled=False,
        openai_consult_mainai_enabled=False,
    )
    monkeypatch.setattr(registry, "get_settings", lambda: settings)
    # The provider instances read settings through THEIR OWN module binding (get_provider
    # constructs cls() whose __init__ calls the openai provider module's get_settings) —
    # patching only registry.get_settings left the conftest's fake OPENAI_API_KEY visible
    # to is_configured(), so the keyless fixture actually resolved a configured OpenAI and
    # test_chat_chain_head_is_local_without_openai_key failed spuriously. Test 2 already
    # re-patches this binding for the keyed case; the fixture must do the same for the
    # keyless case.
    import app.providers.openai_provider as op_mod

    monkeypatch.setattr(op_mod, "get_settings", lambda: settings)
    yield db, settings
    db.close()
    engine.dispose()


def test_chat_chain_head_is_local_without_openai_key(no_openai_env):
    db, settings = no_openai_env
    chain = registry.resolve_chat_chain(db)
    assert chain, "a provider chain must always resolve"
    names = [p.name for p, _ in chain]
    assert "openai" not in names, "OpenAI must not appear in the chain without a configured key"
    assert names[0] == "ollama", f"the chain head must be the local model, got {names[0]}"
    assert any(isinstance(p, OllamaProvider) for p, _ in chain)


def test_chain_with_openai_key_keeps_openai_head(no_openai_env, monkeypatch):
    """The demotion must not change routing for deployments that DO have a key."""
    db, _settings = no_openai_env
    keyed = Settings(environment="development", openai_api_key="sk-Zk9pQ3vR7sT4uW1yX0aB5cD8eF2gH6jL")
    monkeypatch.setattr(registry, "get_settings", lambda: keyed)
    # The provider instance reads settings through ITS OWN module binding.
    import app.providers.openai_provider as op_mod
    monkeypatch.setattr(op_mod, "get_settings", lambda: keyed)
    chain = registry.resolve_chat_chain(db)
    assert isinstance(chain[0][0], OpenAIProvider), "configured OpenAI stays primary"


def test_consultation_gate_fails_closed_without_key_or_flags(no_openai_env):
    db, settings = no_openai_env
    with pytest.raises(service.ConsultationConfigError, match="avstängd"):
        service.resolve_gate(settings)
    enabled_only = Settings(environment="development", openai_consult_enabled=True, openai_api_key=None)
    with pytest.raises(service.ConsultationConfigError, match="vägras"):
        service.resolve_gate(enabled_only)


def test_resolve_active_still_reports_settings_default(no_openai_env):
    """Transparency: the configured default is still what admin/status surfaces report —
    only the executable chain demotes it."""
    db, _settings = no_openai_env
    provider, model = registry.resolve_active(db, role="chat")
    assert provider.name == "openai"  # settings default unchanged
    assert isinstance(provider, OpenAIProvider)
