import httpx

from app.config import get_settings
from app.providers.base import ChatResult, LLMProvider, Message, ProviderError

BASE_URL = "https://api.deepseek.com/v1"


class DeepSeekProvider(LLMProvider):
    """DeepSeek exposes an OpenAI-compatible chat API."""

    name = "deepseek"

    def __init__(self):
        self.settings = get_settings()

    def is_configured(self) -> bool:
        return bool(self.settings.deepseek_api_key)

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.settings.deepseek_api_key}"}

    async def chat(self, messages: list[Message], model: str, *, timeout: float | None = None, **kwargs) -> ChatResult:
        if not self.is_configured():
            raise ProviderError("DeepSeek API-nyckel saknas.")
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
        raise ProviderError("DeepSeek erbjuder inget publikt embedding-API — använd OpenAI/Gemini/lokal modell.")
