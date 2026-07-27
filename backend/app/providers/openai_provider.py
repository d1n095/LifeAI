import httpx

from app.config import get_settings
from app.providers.base import ChatResult, LLMProvider, Message, ProviderError, looks_like_placeholder_secret

BASE_URL = "https://api.openai.com/v1"


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
        async with httpx.AsyncClient(timeout=timeout or 60) as client:
            resp = await client.post(f"{BASE_URL}/chat/completions", headers=self._headers(), json=payload)
            resp.raise_for_status()
            data = resp.json()
        return ChatResult(
            content=data["choices"][0]["message"]["content"],
            provider=self.name,
            model=model,
            raw_usage=data.get("usage", {}),
        )

    async def embed(self, texts: list[str], model: str, *, timeout: float | None = None) -> list[list[float]]:
        if not self.is_configured():
            raise ProviderError("OpenAI API-nyckel saknas.")
        async with httpx.AsyncClient(timeout=timeout or 60) as client:
            resp = await client.post(
                f"{BASE_URL}/embeddings", headers=self._headers(), json={"model": model, "input": texts}
            )
            resp.raise_for_status()
            data = resp.json()
        return [item["embedding"] for item in data["data"]]
