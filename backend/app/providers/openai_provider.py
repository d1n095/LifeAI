import httpx

from app.providers.base import chat_token_limit
from app.config import get_settings
from app.providers.base import ChatResult, LLMProvider, Message, ProviderError, looks_like_placeholder_secret
from app.providers.streaming import stream_openai_http

BASE_URL = "https://api.openai.com/v1"


def _http_error_category(exc: httpx.HTTPError) -> str:
    """Classify an OpenAI HTTP failure without exposing its URL or response body."""
    if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
        status = exc.response.status_code
        if status == 429:
            return "rate_limited"
        if status in (401, 403):
            return "invalid_key"
        if status == 404:
            return "unsupported"
        if 400 <= status < 500:
            return "unsupported"
    return "unreachable"


def _request_failed(context: str, exc: httpx.HTTPError) -> ProviderError:
    return ProviderError(
        f"OpenAI-anropet misslyckades ({context}).",
        category=_http_error_category(exc),
        # The HTTP request was sent. Even a connection error is treated conservatively:
        # the caller must not retry a possibly-billable external effect automatically.
        provider_request_may_have_left=True,
    )


def _invalid_response(context: str) -> ProviderError:
    return ProviderError(
        f"OpenAI gav ett ogiltigt svar ({context}).",
        category="unsupported",
        provider_request_may_have_left=True,
    )


class OpenAIProvider(LLMProvider):
    name = "openai"

    def __init__(self):
        self.settings = get_settings()

    def is_configured(self) -> bool:
        return bool(self.settings.openai_api_key) and not looks_like_placeholder_secret(self.settings.openai_api_key)

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.settings.openai_api_key}"}

    async def chat(self, messages: list[Message], model: str, *, timeout: float | None = None, **kwargs) -> ChatResult:
        if not self.is_configured():
            raise ProviderError("OpenAI API-nyckel saknas.")
        payload = {
            "model": model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": kwargs.get("temperature", 0.4),
        }
        token_limit = chat_token_limit(kwargs)
        if token_limit is not None:
            payload["max_tokens"] = token_limit
        try:
            async with httpx.AsyncClient(timeout=timeout or 60) as client:
                resp = await client.post(f"{BASE_URL}/chat/completions", headers=self._headers(), json=payload)
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPError as exc:
            raise _request_failed("chat", exc) from None
        except (ValueError, TypeError):
            raise _invalid_response("chat") from None
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise _invalid_response("chat shape") from None
        if not isinstance(content, str):
            raise _invalid_response("chat content")
        return ChatResult(
            content=content,
            provider=self.name,
            model=model,
            raw_usage=data.get("usage", {}) if isinstance(data, dict) else {},
        )

    async def stream_chat(self, messages: list[Message], model: str, *, timeout: float | None = None, **kwargs):
        if not self.is_configured():
            raise ProviderError("OpenAI API-nyckel saknas.", category="not_configured", provider_request_may_have_left=False)
        payload = {
            "model": model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": kwargs.get("temperature", 0.4),
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        token_limit = chat_token_limit(kwargs)
        if token_limit is not None:
            payload["max_tokens"] = token_limit
        async for chunk in stream_openai_http(
            url=f"{BASE_URL}/chat/completions",
            headers=self._headers(),
            payload=payload,
            provider=self.name,
            model=model,
            timeout=timeout,
        ):
            yield chunk

    async def embed(self, texts: list[str], model: str, *, timeout: float | None = None) -> list[list[float]]:
        if not self.is_configured():
            raise ProviderError("OpenAI API-nyckel saknas.")
        try:
            async with httpx.AsyncClient(timeout=timeout or 60) as client:
                resp = await client.post(
                    f"{BASE_URL}/embeddings", headers=self._headers(), json={"model": model, "input": texts}
                )
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPError as exc:
            raise _request_failed("embedding", exc) from None
        except (ValueError, TypeError):
            raise _invalid_response("embedding") from None
        try:
            items = data["data"]
            if not isinstance(items, list):
                raise TypeError("data is not a list")
            vectors = [item["embedding"] for item in items]
        except (KeyError, IndexError, TypeError):
            raise _invalid_response("embedding shape") from None
        if not all(isinstance(vector, list) and vector for vector in vectors):
            raise _invalid_response("embedding vector")
        return vectors
