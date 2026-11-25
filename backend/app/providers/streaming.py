"""Shared provider streaming transports.

Only the provider boundary knows how a vendor frames tokens. The chat route consumes the
single ``ChatStreamChunk`` contract from ``app.providers.base`` and never parses vendor JSON.
The parsers are deliberately separate from the HTTP call so the SSE/NDJSON contract can be
tested without a live model.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx

from app.providers.base import ChatStreamChunk, ProviderError


def _status_category(status_code: int) -> str:
    if status_code in (401, 403):
        return "invalid_key"
    if status_code == 429:
        return "rate_limited"
    if status_code == 404 or 400 <= status_code < 500:
        return "unsupported"
    return "unreachable"


def _safe_stream_error(provider: str, *, category: str = "unreachable") -> ProviderError:
    # Never include a response body, URL, or provider error payload: those can contain keys
    # or customer text. The category is the only provider detail the founder needs.
    return ProviderError(
        f"{provider} stream avbröts eller returnerade ett ogiltigt svar.",
        category=category,
        provider_request_may_have_left=True,
    )


async def iter_openai_sse(
    lines: AsyncIterator[str], *, provider: str, model: str
) -> AsyncIterator[ChatStreamChunk]:
    """Parse OpenAI-compatible ``data: {...}`` SSE lines.

    Handles the final usage-only chunk (``choices: []``) emitted when
    ``stream_options.include_usage`` is supported. A stream that ends without any text is an
    error, not an empty successful answer.
    """

    usage: dict = {}
    saw_text = False
    async for raw_line in lines:
        line = (raw_line or "").strip()
        if not line or line.startswith(":"):
            continue
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            event = json.loads(data)
        except (TypeError, ValueError):
            raise _safe_stream_error(provider, category="unreachable") from None
        if not isinstance(event, dict):
            raise _safe_stream_error(provider, category="unreachable")
        if event.get("error"):
            raise _safe_stream_error(provider, category="unreachable")
        raw_usage = event.get("usage")
        if isinstance(raw_usage, dict):
            usage = raw_usage
        choices = event.get("choices")
        if not isinstance(choices, list) or not choices:
            continue
        first = choices[0] if isinstance(choices[0], dict) else {}
        delta = first.get("delta") if isinstance(first.get("delta"), dict) else {}
        text = delta.get("content")
        if text is None:
            text = first.get("text")
        if isinstance(text, str) and text:
            saw_text = True
            yield ChatStreamChunk(delta=text, provider=provider, model=model)
    if not saw_text:
        raise _safe_stream_error(provider, category="unreachable")
    yield ChatStreamChunk(
        provider=provider,
        model=model,
        raw_usage=usage if isinstance(usage, dict) else {},
        done=True,
    )


async def iter_ollama_ndjson(
    lines: AsyncIterator[str], *, provider: str, model: str
) -> AsyncIterator[ChatStreamChunk]:
    """Parse Ollama's newline-delimited ``/api/chat`` stream."""

    usage: dict = {}
    saw_text = False
    async for raw_line in lines:
        line = (raw_line or "").strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except (TypeError, ValueError):
            raise _safe_stream_error(provider, category="unreachable") from None
        if not isinstance(event, dict):
            raise _safe_stream_error(provider, category="unreachable")
        if event.get("error"):
            raise _safe_stream_error(provider, category="unsupported")
        message = event.get("message")
        if isinstance(message, dict):
            text = message.get("content")
            if isinstance(text, str) and text:
                saw_text = True
                yield ChatStreamChunk(delta=text, provider=provider, model=model)
        if event.get("done") is True:
            usage = {
                "prompt_tokens": event.get("prompt_eval_count", 0),
                "completion_tokens": event.get("eval_count", 0),
            }
    if not saw_text:
        raise _safe_stream_error(provider, category="unreachable")
    yield ChatStreamChunk(provider=provider, model=model, raw_usage=usage, done=True)


async def stream_openai_http(
    *,
    url: str,
    headers: dict[str, str],
    payload: dict,
    provider: str,
    model: str,
    timeout: float | None = None,
) -> AsyncIterator[ChatStreamChunk]:
    """Open one OpenAI-compatible streaming HTTP request and yield parsed chunks."""

    try:
        async with httpx.AsyncClient(
            timeout=timeout or 60,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            async with client.stream("POST", url, headers=headers, json=payload) as response:
                if response.status_code >= 400:
                    raise ProviderError(
                        f"{provider} svarade med HTTP {response.status_code}.",
                        category=_status_category(response.status_code),
                        provider_request_may_have_left=True,
                    )
                async for chunk in iter_openai_sse(
                    response.aiter_lines(), provider=provider, model=model
                ):
                    yield chunk
    except httpx.HTTPError:
        raise _safe_stream_error(provider, category="unreachable") from None
