"""Z.ai / GLM adapter using the provider's OpenAI-compatible chat surface."""

import math

import httpx

from app.config import get_settings, validated_zai_base_url
from app.providers.base import (
    ChatResult,
    LLMProvider,
    Message,
    ProviderError,
    looks_like_placeholder_secret,
)
from app.providers.streaming import stream_openai_http



def _category(status_code: int) -> str:
    if status_code in (401, 403):
        return "invalid_key"
    if status_code == 429:
        return "rate_limited"
    return "unreachable"


class ZAIProvider(LLMProvider):
    name = "zai"

    def __init__(self):
        self.settings = get_settings()

    def is_configured(self) -> bool:
        return bool(self.settings.zai_api_key) and not looks_like_placeholder_secret(
            self.settings.zai_api_key
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.settings.zai_api_key}",
            "Content-Type": "application/json",
        }

    async def chat(
        self,
        messages: list[Message],
        model: str,
        *,
        timeout: float | None = None,
        **kwargs,
    ) -> ChatResult:
        if not self.is_configured():
            raise ProviderError(
                "Z.ai API-nyckel saknas.",
                category="not_configured",
                provider_request_may_have_left=False,
            )
        try:
            base_url = validated_zai_base_url(self.settings.zai_base_url)
            if any(key not in {"temperature", "max_tokens"} for key in kwargs):
                raise ValueError("unsupported chat options")
            temperature = kwargs.get("temperature", 0.4)
            if not isinstance(temperature, (int, float)) or not math.isfinite(temperature) or not 0 <= temperature <= 2:
                raise ValueError("invalid temperature")
            max_tokens = kwargs.get("max_tokens")
            if max_tokens is not None and (type(max_tokens) is not int or max_tokens <= 0):
                raise ValueError("invalid token bound")
        except ValueError:
            raise ProviderError("Z.ai konfiguration eller anropsparametrar stöds inte.",
                                category="unsupported", provider_request_may_have_left=False) from None
        payload = {
            "model": model,
            "messages": [{"role": message.role, "content": message.content} for message in messages],
            "temperature": kwargs.get("temperature", 0.4),
        }
        if kwargs.get("max_tokens") is not None:
            payload["max_tokens"] = kwargs["max_tokens"]

        try:
            async with httpx.AsyncClient(timeout=timeout or 60, follow_redirects=False, trust_env=False) as client:
                response = await client.post(
                    f"{base_url}/chat/completions",
                    headers=self._headers(),
                    json=payload,
                )
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPStatusError as exc:
            raise ProviderError(
                f"Z.ai svarade med HTTP {exc.response.status_code}.",
                category=_category(exc.response.status_code),
                provider_request_may_have_left=True,
            ) from exc
        except ValueError:
            raise ProviderError("Z.ai returnerade ett ogiltigt svar.", category="unreachable",
                                provider_request_may_have_left=True) from None
        except httpx.HTTPError as exc:
            raise ProviderError(
                "Z.ai kunde inte nås.",
                category="unreachable",
                provider_request_may_have_left=True,
            ) from exc

        try:
            content = data["choices"][0]["message"]["content"]
            usage = data["usage"]
            if not isinstance(content, str) or not content.strip() or not isinstance(usage, dict):
                raise ValueError("invalid response")
            for key in ("prompt_tokens", "completion_tokens"):
                if type(usage.get(key)) is not int or usage[key] < 0:
                    raise ValueError("invalid usage")
        except (KeyError, IndexError, TypeError, ValueError):
            raise ProviderError("Z.ai returnerade ett ogiltigt svar.", category="unreachable",
                                provider_request_may_have_left=True) from None
        return ChatResult(
            content=content,
            provider=self.name,
            model=model,
            raw_usage={
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
            },
        )

    async def stream_chat(self, messages: list[Message], model: str, *, timeout: float | None = None, **kwargs):
        if not self.is_configured():
            raise ProviderError(
                "Z.ai API-nyckel saknas.",
                category="not_configured",
                provider_request_may_have_left=False,
            )
        try:
            base_url = validated_zai_base_url(self.settings.zai_base_url)
            if any(key not in {"temperature", "max_tokens"} for key in kwargs):
                raise ValueError("unsupported chat options")
            temperature = kwargs.get("temperature", 0.4)
            if not isinstance(temperature, (int, float)) or not math.isfinite(temperature) or not 0 <= temperature <= 2:
                raise ValueError("invalid temperature")
            max_tokens = kwargs.get("max_tokens")
            if max_tokens is not None and (type(max_tokens) is not int or max_tokens <= 0):
                raise ValueError("invalid token bound")
        except ValueError:
            raise ProviderError(
                "Z.ai konfiguration eller anropsparametrar stöds inte.",
                category="unsupported",
                provider_request_may_have_left=False,
            ) from None
        payload = {
            "model": model,
            "messages": [{"role": message.role, "content": message.content} for message in messages],
            "temperature": temperature,
            "stream": True,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        async for chunk in stream_openai_http(
            url=f"{base_url}/chat/completions",
            headers=self._headers(),
            payload=payload,
            provider=self.name,
            model=model,
            timeout=timeout,
        ):
            yield chunk

    async def embed(
        self,
        texts: list[str],
        model: str,
        *,
        timeout: float | None = None,
    ) -> list[list[float]]:
        raise ProviderError(
            "Det registrerade Z.ai/GLM-gränssnittet stöder inte embeddings.",
            category="unsupported",
            provider_request_may_have_left=False,
        )
