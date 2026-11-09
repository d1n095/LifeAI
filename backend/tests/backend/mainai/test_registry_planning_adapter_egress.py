"""Regression coverage for RegistryPlanningAdapter's external-call safety
(app/provider_planning/service.py).

The adapter performs a raw single-provider call and relies on its sole caller,
plan_with_provider(), to have run the egress gate for exactly that (provider, model) first.
Before this fix it also had an `else` branch that fell back to chat_with_fallback() with no
owner_id — an ungated, provider-hopping disclosure path. These tests pin two properties with a
provider spy (no real key, no DB needed on the explicit path):

1. The explicit path sends only the given payload to exactly the authorized provider/model,
   and performs no provider fallback.
2. With no explicit provider/model, the adapter fails closed (raises, never calls a provider)
   instead of silently falling back through an ungated path.
"""

import pytest

from app.providers.base import ChatResult, ProviderError
from app.provider_planning.service import RegistryPlanningAdapter


class _SpyProvider:
    name = "openai"

    def __init__(self):
        self.calls: list[dict] = []

    async def chat(self, messages, model, **kwargs):
        self.calls.append({"messages": messages, "model": model, "kwargs": kwargs})
        return ChatResult(
            content='{"ok": true}',
            provider=self.name,
            model=model,
            raw_usage={"prompt_tokens": 1, "completion_tokens": 1},
        )


@pytest.mark.asyncio
async def test_explicit_path_calls_exactly_the_authorized_provider(monkeypatch):
    spy = _SpyProvider()
    import app.providers.registry as registry

    monkeypatch.setattr(registry, "get_provider", lambda name: spy)

    adapter = RegistryPlanningAdapter(db=None, provider_name="openai", model="gpt-4o-mini")
    response = await adapter.propose({"goal": "x"}, timeout_seconds=5, max_output_bytes=4000)

    assert len(spy.calls) == 1  # exactly one external call, no fallback hop
    assert spy.calls[0]["model"] == "gpt-4o-mini"
    assert response.provider == "openai"
    assert response.model == "gpt-4o-mini"


@pytest.mark.asyncio
async def test_missing_provider_fails_closed_without_any_provider_call(monkeypatch):
    called = {"get_provider": False}
    import app.providers.registry as registry

    def _tracking_get_provider(name):  # pragma: no cover - must never run
        called["get_provider"] = True
        return _SpyProvider()

    monkeypatch.setattr(registry, "get_provider", _tracking_get_provider)

    adapter = RegistryPlanningAdapter(db=None, provider_name=None, model=None)
    with pytest.raises(ProviderError):
        await adapter.propose({"goal": "x"}, timeout_seconds=5, max_output_bytes=4000)
    assert called["get_provider"] is False  # failed closed before touching any provider


@pytest.mark.asyncio
async def test_missing_model_fails_closed(monkeypatch):
    spy = _SpyProvider()
    import app.providers.registry as registry

    monkeypatch.setattr(registry, "get_provider", lambda name: spy)

    adapter = RegistryPlanningAdapter(db=None, provider_name="openai", model=None)
    with pytest.raises(ProviderError):
        await adapter.propose({"goal": "x"}, timeout_seconds=5, max_output_bytes=4000)
    assert spy.calls == []  # never sent anything
