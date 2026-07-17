import httpx

from app.config import get_settings
from app.providers.base import ChatResult, LLMProvider, Message, ProviderError

BASE_URL = "https://openrouter.ai/api/v1"


class OpenRouterProvider(LLMProvider):
    """OpenRouter — one API key, gateway to many hosted models. OpenAI-compatible."""

    name = "openrouter"

    def __init__(self):
        self.settings = get_settings()

    def is_configured(self) -> bool:
        return bool(self.settings.openrouter_api_key)

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.settings.openrouter_api_key}",
            "HTTP-Referer": "https://lifeos.local",
            "X-Title": "LifeOS",
        }

    async def chat(self, messages: list[Message], model: str, **kwargs) -> ChatResult:
        if not self.is_configured():
            raise ProviderError("OpenRouter API-nyckel saknas.")
        payload = {
            "model": model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": kwargs.get("temperature", 0.4),
        }
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(f"{BASE_URL}/chat/completions", headers=self._headers(), json=payload)
            resp.raise_for_status()
            data = resp.json()
        return ChatResult(
            content=data["choices"][0]["message"]["content"],
            provider=self.name,
            model=model,
            raw_usage=data.get("usage", {}),
        )

    async def embed(self, texts: list[str], model: str) -> list[list[float]]:
        raise ProviderError("Använd OpenAI/Gemini/lokal modell för embeddings — OpenRouter fokuserar på chattmodeller.")
