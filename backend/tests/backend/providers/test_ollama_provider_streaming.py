"""Real token-by-token streaming for the local Ollama provider (Founder Alpha streaming effort,
Item 1). Mirrors test_ollama_provider_failclosed.py's fail-closed discipline for the streaming
path: a malformed line, a 200-with-error-object, a stream that ends without a "done": true
sentinel, and a transport failure must all surface as a classified ProviderError — never a raw
exception that bypasses a caller's `except ProviderError` recovery path. No network, no Docker:
httpx.MockTransport intercepts every request in-process.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.providers.base import Message, ProviderError, StreamChunk
from app.providers.ollama_provider import OllamaProvider


@pytest.fixture
def mock_ollama_stream(monkeypatch):
    real_async_client = httpx.AsyncClient

    def install(lines: list[str] | None = None, *, raise_exc: Exception | None = None, status: int = 200):
        def handler(request):
            if raise_exc is not None:
                raise raise_exc
            body = "\n".join(lines or [])
            return httpx.Response(status, content=body.encode(), headers={"content-type": "application/x-ndjson"})

        def factory(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return real_async_client(*args, **kwargs)

        monkeypatch.setattr("app.providers.ollama_provider.httpx.AsyncClient", factory)

    return install


async def _collect(provider, **kwargs):
    chunks: list[StreamChunk] = []
    async for chunk in provider.chat_stream([Message(role="user", content="hej")], model="qwen2.5:0.5b", **kwargs):
        chunks.append(chunk)
    return chunks


@pytest.mark.asyncio
async def test_happy_path_yields_incremental_deltas_then_done(mock_ollama_stream):
    mock_ollama_stream([
        json.dumps({"message": {"content": "Hej"}, "done": False}),
        json.dumps({"message": {"content": "san"}, "done": False}),
        json.dumps({"message": {"content": "!"}, "done": False}),
        json.dumps({"message": {"content": ""}, "done": True, "prompt_eval_count": 4, "eval_count": 9}),
    ])
    chunks = await _collect(OllamaProvider())
    assert [c.delta for c in chunks] == ["Hej", "san", "!", ""]
    assert [c.done for c in chunks] == [False, False, False, True]
    final = chunks[-1]
    assert final.usage == {"prompt_tokens": 4, "completion_tokens": 9}
    assert final.provider == "ollama"
    assert final.model == "qwen2.5:0.5b"
    # Reassembling the deltas must reproduce the full reply exactly.
    assert "".join(c.delta for c in chunks) == "Hejsan!"


@pytest.mark.asyncio
async def test_empty_content_lines_are_skipped_not_yielded_as_empty_deltas(mock_ollama_stream):
    mock_ollama_stream([
        "",  # a blank keep-alive line some HTTP intermediaries can introduce
        json.dumps({"message": {"content": "ok"}, "done": False}),
        json.dumps({"message": {}, "done": False}),  # content omitted entirely
        json.dumps({"done": True, "prompt_eval_count": 1, "eval_count": 1}),
    ])
    chunks = await _collect(OllamaProvider())
    assert [c.delta for c in chunks if not c.done] == ["ok"]


@pytest.mark.asyncio
async def test_malformed_json_line_fails_closed(mock_ollama_stream):
    mock_ollama_stream(["not json at all"])
    with pytest.raises(ProviderError) as exc_info:
        await _collect(OllamaProvider())
    assert exc_info.value.category == "unsupported"


@pytest.mark.asyncio
async def test_200_ok_error_object_mid_stream_fails_closed(mock_ollama_stream):
    """Ollama can answer HTTP 200 with a JSON error object even mid-stream (e.g. the model was
    unloaded between the first token and the next) — this must fail closed exactly like the
    non-streaming chat()'s equivalent case, not surface as a silent truncation."""
    mock_ollama_stream([
        json.dumps({"message": {"content": "star"}, "done": False}),
        json.dumps({"error": "model 'qwen2.5:0.5b' not found, try pulling it first"}),
    ])
    with pytest.raises(ProviderError) as exc_info:
        await _collect(OllamaProvider())
    assert exc_info.value.category == "unsupported"
    assert "not found" in str(exc_info.value)


@pytest.mark.asyncio
async def test_stream_ending_without_done_sentinel_fails_closed(mock_ollama_stream):
    """A connection that just stops (truncated response, server crash) must not be mistaken
    for a successful completion merely because no exception was raised by the transport."""
    mock_ollama_stream([
        json.dumps({"message": {"content": "partial"}, "done": False}),
    ])
    with pytest.raises(ProviderError) as exc_info:
        await _collect(OllamaProvider())
    assert exc_info.value.category == "unsupported"
    assert exc_info.value.provider_request_may_have_left is True


@pytest.mark.asyncio
async def test_non_string_content_fails_closed(mock_ollama_stream):
    mock_ollama_stream([
        json.dumps({"message": {"content": 12345}, "done": False}),
    ])
    with pytest.raises(ProviderError) as exc_info:
        await _collect(OllamaProvider())
    assert exc_info.value.category == "unsupported"


@pytest.mark.asyncio
async def test_transport_failure_fails_closed_as_unreachable(mock_ollama_stream):
    mock_ollama_stream(raise_exc=httpx.ConnectError("connection refused"))
    with pytest.raises(ProviderError) as exc_info:
        await _collect(OllamaProvider())
    assert exc_info.value.category == "unreachable"


@pytest.mark.asyncio
async def test_max_tokens_maps_to_num_predict_for_streaming(monkeypatch):
    """Same bounded-output contract chat() already has — a streaming request must not be able
    to run unbounded just because it takes a different code path."""
    captured = {}
    real_async_client = httpx.AsyncClient

    def handler(request):
        captured["payload"] = json.loads(request.content)
        return httpx.Response(200, content=(json.dumps({"done": True, "prompt_eval_count": 0, "eval_count": 0}) + "\n").encode())

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("app.providers.ollama_provider.httpx.AsyncClient", factory)
    await _collect(OllamaProvider(), max_tokens=64)
    assert captured["payload"]["options"] == {"num_predict": 64}
    assert captured["payload"]["stream"] is True


class _DropsMidStream(httpx.AsyncByteStream):
    """Yields real bytes, then raises as if the TCP connection died — the actual shape of a
    local Ollama process restarting mid-generation (not a clean HTTP error response, since the
    server is gone, not merely unhappy)."""

    def __init__(self, lines: list[str]):
        self._lines = lines

    async def __aiter__(self):
        for line in self._lines:
            yield (line + "\n").encode()
        raise httpx.ReadError("connection reset by peer")


@pytest.mark.asyncio
async def test_connection_drops_mid_stream_fails_closed_as_unreachable_after_partial_deltas(monkeypatch):
    """Restart-recovery scenario: the local Ollama process dies mid-generation. Already-yielded
    deltas must not be lost or retracted, and the drop itself must classify as `unreachable`
    (not `unsupported`) so the app's existing retry path treats this as "try again", matching
    `_http_error_category()`'s contract for every other transport failure — the local-restart
    case must not require a second, parallel classification scheme."""
    real_async_client = httpx.AsyncClient

    def handler(request):
        return httpx.Response(
            200,
            stream=_DropsMidStream([
                json.dumps({"message": {"content": "Hej"}, "done": False}),
                json.dumps({"message": {"content": "san"}, "done": False}),
            ]),
            headers={"content-type": "application/x-ndjson"},
        )

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("app.providers.ollama_provider.httpx.AsyncClient", factory)

    received: list[StreamChunk] = []
    with pytest.raises(ProviderError) as exc_info:
        async for chunk in OllamaProvider().chat_stream([Message(role="user", content="hej")], model="qwen2.5:0.5b"):
            received.append(chunk)

    assert [c.delta for c in received] == ["Hej", "san"]
    assert exc_info.value.category == "unreachable"


@pytest.mark.asyncio
async def test_early_generator_close_does_not_raise(mock_ollama_stream):
    """The SSE endpoint stops iterating early on client disconnect — closing the async
    generator mid-stream (via aclose()) must not raise or leak; this is what lets the
    `async with client.stream(...)` block exit cleanly and close the connection to Ollama."""
    mock_ollama_stream([
        json.dumps({"message": {"content": "a"}, "done": False}),
        json.dumps({"message": {"content": "b"}, "done": False}),
        json.dumps({"message": {"content": "c"}, "done": False}),
        json.dumps({"done": True, "prompt_eval_count": 1, "eval_count": 1}),
    ])
    gen = OllamaProvider().chat_stream([Message(role="user", content="hej")], model="qwen2.5:0.5b")
    first = await gen.__anext__()
    assert first.delta == "a"
    await gen.aclose()  # must not raise
