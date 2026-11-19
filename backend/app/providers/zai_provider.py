"""Z.ai / GLM adapter using the provider's OpenAI-compatible chat surface."""

import httpx

from app.config import get_settings
from app.providers.base import (
    ChatResult,
    LLMProvider,
    Message,
    ProviderError,
    looks_like_placeholder_secret,
)


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
        payload = {
            "model": model,
            "messages": [{"role": message.role, "content": message.content} for message in messages],
            "temperature": kwargs.get("temperature", 0.4),
        }
        if kwargs.get("max_tokens") is not None:
            payload["max_tokens"] = kwargs["max_tokens"]

        try:
            async with httpx.AsyncClient(timeout=timeout or 60) as client:
                response = await client.post(
                    f"{self.settings.zai_base_url.rstrip('/')}/chat/completions",
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
        except httpx.HTTPError as exc:
            raise ProviderError(
                "Z.ai kunde inte nås.",
                category="unreachable",
                provider_request_may_have_left=True,
            ) from exc

        usage = data.get("usage", {})
        return ChatResult(
            content=data["choices"][0]["message"]["content"],
            provider=self.name,
            model=model,
            raw_usage={
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
            },
        )

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
