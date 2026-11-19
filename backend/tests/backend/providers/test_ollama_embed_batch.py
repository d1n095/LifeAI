"""Batched local embedding (`/api/embed`) and the native-width contract.

Why these tests exist: the local-first deployment embeds with Ollama, and two properties are
easy to regress without noticing —

  1. BATCHING. One request per document batch, not one per chunk. On a CPU runtime the
     per-chunk request overhead dominates (measured ~13 s per chunk before), so a regression
     here is the difference between usable ingestion and a crawl.
  2. NATIVE WIDTH. The provider must return the model's own vector width and NOT truncate or
     pad to settings.embedding_dim. app/rag/embedding_space.py zero-pads on write and stamps
     each chunk with (model, native_dim), which is what lets a non-1536-dimension local model
     work with no destructive migration. Truncating here would silently break that.

Also covered: an old Ollama without /api/embed (plain-text 404) falls back to the legacy
per-text endpoint, while a JSON model-not-found 404 stays a real `unsupported` failure instead
of being mistaken for an old server.

No network and no Docker: httpx.MockTransport intercepts every request in-process.
"""

from __future__ import annotations

import httpx
import pytest

from app.providers.base import ProviderError
from app.providers.ollama_provider import OllamaProvider


@pytest.fixture
def mock_ollama(monkeypatch):
    """Install a MockTransport and record every request URL the provider makes."""
    real_async_client = httpx.AsyncClient
    calls: list[str] = []

    def install(handler):
        def factory(*args, **kwargs):
            def run(request):
                calls.append(f"{request.method} {request.url}")

            def transport_handler(request):
                run(request)
                return handler(request)

            kwargs["transport"] = httpx.MockTransport(transport_handler)
            return real_async_client(*args, **kwargs)

        monkeypatch.setattr("app.providers.ollama_provider.httpx.AsyncClient", factory)

    install.calls = calls
    return install


def _by_path(batch_handler, legacy_handler):
    def handler(request: httpx.Request):
        if str(request.url).endswith("/api/embed"):
            return batch_handler(request)
        return legacy_handler(request)

    return handler


@pytest.mark.asyncio
async def test_batch_is_one_http_call_and_keeps_the_native_width(mock_ollama):
    mock_ollama(lambda request: httpx.Response(200, json={"embeddings": [[0.1] * 768, [0.2] * 768]}))
    vectors = await OllamaProvider().embed(["a", "b"], model="nomic-embed-text")
    assert len(mock_ollama.calls) == 1
    assert "/api/embed" in mock_ollama.calls[0]
    assert [len(vector) for vector in vectors] == [768, 768]
    # Not padded to the 1536 storage column and not truncated: padding is the storage layer's
    # job (app/rag/embedding_space.py), which also records the native width on each chunk.
    assert vectors[0][0] == 0.1


@pytest.mark.asyncio
async def test_int_values_are_returned_as_floats(mock_ollama):
    mock_ollama(lambda request: httpx.Response(200, json={"embeddings": [[0, 1]]}))
    assert await OllamaProvider().embed(["a"], model="all-minilm") == [[0.0, 1.0]]


@pytest.mark.asyncio
async def test_old_server_without_embed_route_falls_back_to_the_legacy_endpoint(mock_ollama):
    # An Ollama older than 0.1.36 answers /api/embed with a PLAIN-TEXT 404 (unknown route).
    mock_ollama(
        _by_path(
            lambda request: httpx.Response(404, text="404 page not found"),
            lambda request: httpx.Response(200, json={"embedding": [0.3, 0.4]}),
        )
    )
    vectors = await OllamaProvider().embed(["a", "b"], model="all-minilm")
    assert vectors == [[0.3, 0.4], [0.3, 0.4]]
    assert len(mock_ollama.calls) == 3  # one batch attempt + one legacy call per text
    assert sum("/api/embeddings" in call for call in mock_ollama.calls) == 2


@pytest.mark.asyncio
async def test_model_not_found_404_is_not_mistaken_for_an_old_server(mock_ollama):
    # A JSON error means the route EXISTS and the model does not — never fall back silently.
    mock_ollama(
        _by_path(
            lambda request: httpx.Response(404, json={"error": "model 'ghost' not found"}),
            lambda request: httpx.Response(200, json={"embedding": [0.1]}),
        )
    )
    with pytest.raises(ProviderError) as excinfo:
        await OllamaProvider().embed(["a"], model="ghost")
    assert excinfo.value.category == "unsupported"
    assert len(mock_ollama.calls) == 1


@pytest.mark.asyncio
async def test_batch_error_object_fails_closed(mock_ollama):
    mock_ollama(lambda request: httpx.Response(200, json={"error": "no embedding for you"}))
    with pytest.raises(ProviderError) as excinfo:
        await OllamaProvider().embed(["a"], model="all-minilm")
    assert excinfo.value.category == "unsupported"


@pytest.mark.asyncio
async def test_vector_count_mismatch_fails_closed(mock_ollama):
    mock_ollama(lambda request: httpx.Response(200, json={"embeddings": [[0.1, 0.2]]}))
    with pytest.raises(ProviderError) as excinfo:
        await OllamaProvider().embed(["a", "b"], model="all-minilm")
    assert "1 vektorer" in str(excinfo.value)


@pytest.mark.asyncio
async def test_mixed_native_widths_in_one_batch_fail_closed(mock_ollama):
    # fit_vectors() would reject this too, but failing here names the real cause (a
    # misbehaving runtime) instead of surfacing as a storage-layer error.
    mock_ollama(lambda request: httpx.Response(200, json={"embeddings": [[0.1] * 768, [0.2] * 384]}))
    with pytest.raises(ProviderError) as excinfo:
        await OllamaProvider().embed(["a", "b"], model="all-minilm")
    assert "olika dimension" in str(excinfo.value)


@pytest.mark.asyncio
async def test_empty_input_makes_no_request(mock_ollama):
    mock_ollama(lambda request: httpx.Response(500))
    assert await OllamaProvider().embed([], model="all-minilm") == []
    assert mock_ollama.calls == []
