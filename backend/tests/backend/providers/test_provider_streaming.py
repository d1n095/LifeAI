"""Pure provider stream parser tests (no live model is claimed)."""

import pytest

from app.providers.streaming import iter_ollama_ndjson, iter_openai_sse


class _Lines:
    def __init__(self, values):
        self._values = iter(values)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._values)
        except StopIteration:
            raise StopAsyncIteration


@pytest.mark.asyncio
async def test_openai_sse_parses_deltas_and_usage():
    chunks = [
        chunk
        async for chunk in iter_openai_sse(
            _Lines(
                [
                    'data: {"choices":[{"delta":{"content":"Hej "}}]}',
                    'data: {"choices":[{"delta":{"content":"värld"}}]}',
                    'data: {"choices":[],"usage":{"prompt_tokens":4,"completion_tokens":2}}',
                    "data: [DONE]",
                ]
            ),
            provider="openai",
            model="gpt-test",
        )
    ]
    assert "".join(chunk.delta for chunk in chunks) == "Hej värld"
    assert chunks[-1].done is True
    assert chunks[-1].raw_usage == {"prompt_tokens": 4, "completion_tokens": 2}


@pytest.mark.asyncio
async def test_openai_sse_empty_stream_fails_closed():
    with pytest.raises(Exception):
        async for _ in iter_openai_sse(
            _Lines(["data: [DONE]"]), provider="openai", model="gpt-test"
        ):
            pass


@pytest.mark.asyncio
async def test_ollama_ndjson_parses_final_usage():
    chunks = [
        chunk
        async for chunk in iter_ollama_ndjson(
            _Lines(
                [
                    '{"message":{"content":"Hej "},"done":false}',
                    '{"message":{"content":"lokal"},"done":false}',
                    '{"done":true,"prompt_eval_count":7,"eval_count":3}',
                ]
            ),
            provider="ollama",
            model="llama-test",
        )
    ]
    assert "".join(chunk.delta for chunk in chunks) == "Hej lokal"
    assert chunks[-1].raw_usage == {"prompt_tokens": 7, "completion_tokens": 3}
