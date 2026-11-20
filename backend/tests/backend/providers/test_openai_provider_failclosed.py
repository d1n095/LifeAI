"""Fail-closed contract for the external OpenAI adapter.

Founder Alpha uses a local Ollama model by default, but an admin can activate OpenAI and the
same embedding seam is reachable through ``retrieve_context()``.  A 429, a 5xx, or a 2xx
response with an invalid JSON body must therefore be a classified ``ProviderError`` — never a
raw ``httpx``/``JSONDecodeError`` that bypasses chat's degraded-grounding handling or becomes
an unhandled 500 in knowledge search.

No network: ``httpx.MockTransport`` intercepts every request in-process.
"""

from __future__ import annotations

import httpx
import pytest

from app.config import get_settings
from app.providers.base import ProviderError
from app.providers.openai_provider import OpenAIProvider


@pytest.fixture
def mock_openai(monkeypatch):
    monkeypatch.setattr(get_settings(), "openai_api_key", "sk-test-only")
    real_async_client = httpx.AsyncClient

    def install(handler):
        def factory(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return real_async_client(*args, **kwargs)

        monkeypatch.setattr("app.providers.openai_provider.httpx.AsyncClient", factory)

    return install


def _raw(status: int, content: bytes):
    return lambda request: httpx.Response(status, content=content)


@pytest.mark.asyncio
async def test_chat_invalid_json_fails_closed(mock_openai):
    mock_openai(_raw(200, b"not-json"))
    with pytest.raises(ProviderError) as error:
        await OpenAIProvider().chat([], model="gpt-4o-mini")
    assert error.value.category == "unsupported"
    assert error.value.provider_request_may_have_left is True


@pytest.mark.asyncio
async def test_embed_invalid_json_fails_closed(mock_openai):
    mock_openai(_raw(200, b"not-json"))
    with pytest.raises(ProviderError) as error:
        await OpenAIProvider().embed(["hello"], model="text-embedding-3-small")
    assert error.value.category == "unsupported"
    assert error.value.provider_request_may_have_left is True


@pytest.mark.asyncio
async def test_chat_429_is_rate_limited(mock_openai):
    mock_openai(_raw(429, b'{"error": "slow down"}'))
    with pytest.raises(ProviderError) as error:
        await OpenAIProvider().chat([], model="gpt-4o-mini")
    assert error.value.category == "rate_limited"
    assert error.value.provider_request_may_have_left is True


@pytest.mark.asyncio
async def test_chat_500_is_unreachable(mock_openai):
    mock_openai(_raw(500, b'{"error": "internal"}'))
    with pytest.raises(ProviderError) as error:
        await OpenAIProvider().chat([], model="gpt-4o-mini")
    assert error.value.category == "unreachable"
    assert error.value.provider_request_may_have_left is True


@pytest.mark.asyncio
async def test_embed_500_is_unreachable(mock_openai):
    mock_openai(_raw(500, b'{"error": "internal"}'))
    with pytest.raises(ProviderError) as error:
        await OpenAIProvider().embed(["hello"], model="text-embedding-3-small")
    assert error.value.category == "unreachable"
    assert error.value.provider_request_may_have_left is True
