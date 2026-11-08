"""Fail-closed contract for the local Ollama provider (runtime-resilience scenarios #2/#5/#7).

Ollama is the local-first, no-egress provider behind Founder Alpha, and it is a subprocess
trust boundary: the backend must treat its responses as untrusted. Every caller recovers from
a provider failure through `except ProviderError` (park/retry/fallback). Before this hardening,
`chat()` did `data["message"]["content"]` and `embed()` did `resp.json()["embedding"]` with no
guard, so a 200-OK response carrying Ollama's own error object, or any malformed body, escaped
as a raw `KeyError`/`TypeError` that bypassed that recovery path. These tests pin that every
failure mode — transport timeout, model-unavailable (404), Ollama 200-error body, and malformed
shapes — surfaces as a classified `ProviderError`, and that the happy path still returns a
normal result.

No network and no Docker: `httpx.MockTransport` intercepts every request in-process.
"""

from __future__ import annotations

import httpx
import pytest

from app.providers.base import ProviderError
from app.providers.ollama_provider import OllamaProvider


@pytest.fixture
def mock_ollama(monkeypatch):
    """Route the provider's httpx.AsyncClient through a MockTransport driven by `handler`.

    The provider constructs `httpx.AsyncClient(timeout=...)` with no transport, so we wrap the
    class to inject a MockTransport while leaving its other arguments untouched.
    """
    real_async_client = httpx.AsyncClient

    def install(handler):
        def factory(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return real_async_client(*args, **kwargs)

        monkeypatch.setattr("app.providers.ollama_provider.httpx.AsyncClient", factory)

    return install


def _json(status, body):
    return lambda request: httpx.Response(status, json=body)


def _raise(exc):
    def handler(request):
        raise exc

    return handler


# --------------------------------------------------------------------------------------- chat

@pytest.mark.asyncio
async def test_chat_happy_path_returns_content_and_usage(mock_ollama):
    mock_ollama(_json(200, {"message": {"content": "hej"}, "prompt_eval_count": 3, "eval_count": 7}))
    result = await OllamaProvider().chat([], model="qwen2.5:0.5b")
    assert result.content == "hej"
    assert result.provider == "ollama"
    assert result.raw_usage == {"prompt_tokens": 3, "completion_tokens": 7}


@pytest.mark.asyncio
async def test_chat_timeout_fails_closed_as_unreachable(mock_ollama):
    # Scenario #5 — provider timeout.
    mock_ollama(_raise(httpx.ReadTimeout("timed out")))
    with pytest.raises(ProviderError) as ei:
        await OllamaProvider().chat([], model="qwen2.5:0.5b")
    assert ei.value.category == "unreachable"


@pytest.mark.asyncio
async def test_chat_model_not_found_404_is_unsupported(mock_ollama):
    # Scenario #2 — model unavailable / missing (server up, model absent → HTTP 404).
    mock_ollama(_json(404, {"error": "model 'ghost' not found, try pulling it first"}))
    with pytest.raises(ProviderError) as ei:
        await OllamaProvider().chat([], model="ghost")
    assert ei.value.category == "unsupported"


@pytest.mark.asyncio
async def test_chat_200_error_body_fails_closed_not_keyerror(mock_ollama):
    # Scenario #7 — malformed/error response: 200 OK but an Ollama error object.
    mock_ollama(_json(200, {"error": "model runner crashed"}))
    with pytest.raises(ProviderError) as ei:
        await OllamaProvider().chat([], model="qwen2.5:0.5b")
    assert ei.value.category == "unsupported"
    assert "fel" in str(ei.value)


@pytest.mark.asyncio
async def test_chat_200_missing_message_content_fails_closed(mock_ollama):
    mock_ollama(_json(200, {"done": True}))
    with pytest.raises(ProviderError):
        await OllamaProvider().chat([], model="qwen2.5:0.5b")


@pytest.mark.asyncio
async def test_chat_200_nonstring_content_fails_closed(mock_ollama):
    mock_ollama(_json(200, {"message": {"content": 12345}}))
    with pytest.raises(ProviderError):
        await OllamaProvider().chat([], model="qwen2.5:0.5b")


@pytest.mark.asyncio
async def test_chat_server_error_500_is_unreachable(mock_ollama):
    mock_ollama(_json(500, {"error": "internal"}))
    with pytest.raises(ProviderError) as ei:
        await OllamaProvider().chat([], model="qwen2.5:0.5b")
    assert ei.value.category == "unreachable"


# -------------------------------------------------------------------------------------- embed

@pytest.mark.asyncio
async def test_embed_happy_path_returns_vectors(mock_ollama):
    mock_ollama(_json(200, {"embedding": [0.1, 0.2, 0.3]}))
    vectors = await OllamaProvider().embed(["hello"], model="all-minilm")
    assert vectors == [[0.1, 0.2, 0.3]]


@pytest.mark.asyncio
async def test_embed_model_not_found_404_is_unsupported(mock_ollama):
    # Scenario #2 — embedding model unavailable.
    mock_ollama(_json(404, {"error": "model 'all-minilm' not found"}))
    with pytest.raises(ProviderError) as ei:
        await OllamaProvider().embed(["hello"], model="all-minilm")
    assert ei.value.category == "unsupported"


@pytest.mark.asyncio
async def test_embed_200_error_body_fails_closed(mock_ollama):
    mock_ollama(_json(200, {"error": "no embedding for you"}))
    with pytest.raises(ProviderError):
        await OllamaProvider().embed(["hello"], model="all-minilm")


@pytest.mark.asyncio
async def test_embed_200_missing_embedding_fails_closed(mock_ollama):
    mock_ollama(_json(200, {"done": True}))
    with pytest.raises(ProviderError):
        await OllamaProvider().embed(["hello"], model="all-minilm")


@pytest.mark.asyncio
async def test_embed_200_nonvector_embedding_fails_closed(mock_ollama):
    mock_ollama(_json(200, {"embedding": "not-a-vector"}))
    with pytest.raises(ProviderError):
        await OllamaProvider().embed(["hello"], model="all-minilm")


@pytest.mark.asyncio
async def test_embed_timeout_fails_closed_as_unreachable(mock_ollama):
    # Scenario #5 — provider timeout on the embedding path.
    mock_ollama(_raise(httpx.ConnectTimeout("timed out")))
    with pytest.raises(ProviderError) as ei:
        await OllamaProvider().embed(["hello"], model="all-minilm")
    assert ei.value.category == "unreachable"
