import httpx

from app.config import get_settings
from app.providers.base import ChatResult, LLMProvider, Message, ProviderError

BASE_URL = "https://generativelanguage.googleapis.com/v1beta"


class GeminiProvider(LLMProvider):
    name = "gemini"

    def __init__(self):
        self.settings = get_settings()

    def is_configured(self) -> bool:
        return bool(self.settings.google_api_key)

    async def chat(self, messages: list[Message], model: str, **kwargs) -> ChatResult:
        if not self.is_configured():
            raise ProviderError("Google API-nyckel saknas.")
        system = "\n".join(m.content for m in messages if m.role == "system") or None
        contents = [
            {"role": "user" if m.role == "user" else "model", "parts": [{"text": m.content}]}
            for m in messages
            if m.role != "system"
        ]
        payload: dict = {"contents": contents}
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        url = f"{BASE_URL}/models/{model}:generateContent?key={self.settings.google_api_key}"
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
        content = data["candidates"][0]["content"]["parts"][0]["text"]
        return ChatResult(content=content, provider=self.name, model=model, raw_usage=data.get("usageMetadata", {}))

    async def embed(self, texts: list[str], model: str) -> list[list[float]]:
        if not self.is_configured():
            raise ProviderError("Google API-nyckel saknas.")
        vectors = []
        url = f"{BASE_URL}/models/{model}:embedContent?key={self.settings.google_api_key}"
        async with httpx.AsyncClient(timeout=60) as client:
            for text in texts:
                resp = await client.post(url, json={"content": {"parts": [{"text": text}]}})
                resp.raise_for_status()
                vectors.append(resp.json()["embedding"]["values"])
        return vectors
