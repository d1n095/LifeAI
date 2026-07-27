import httpx

from app.config import get_settings
from app.providers.base import ChatResult, LLMProvider, Message, ProviderError, looks_like_placeholder_secret

BASE_URL = "https://generativelanguage.googleapis.com/v1beta"


class GeminiProvider(LLMProvider):
    """Security incident, 2026-07-26: the key used to be sent as a `?key=...` URL query
    parameter. An httpx.HTTPStatusError's default message embeds the full request URL, and
    app/providers/registry.py's chat_with_fallback() used to log that raw exception straight
    to the application log on every provider failure — so a Gemini failure leaked the live key
    into Docker logs. Google's current documented contract for this API also accepts (and
    Google now recommends) the credential as the `x-goog-api-key` request header instead —
    switching to it removes the key from the URL entirely, which is what actually keeps it out
    of any exception message built from `request.url` (headers are never included in httpx's
    HTTPStatusError default message). Never revert to the query-string form."""

    name = "gemini"

    def __init__(self):
        self.settings = get_settings()

    def is_configured(self) -> bool:
        return bool(self.settings.google_api_key) and not looks_like_placeholder_secret(self.settings.google_api_key)

    def _headers(self) -> dict:
        return {"x-goog-api-key": self.settings.google_api_key}

    async def chat(self, messages: list[Message], model: str, *, timeout: float | None = None, **kwargs) -> ChatResult:
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
        url = f"{BASE_URL}/models/{model}:generateContent"
        async with httpx.AsyncClient(timeout=timeout or 60) as client:
            resp = await client.post(url, headers=self._headers(), json=payload)
            resp.raise_for_status()
            data = resp.json()
        content = data["candidates"][0]["content"]["parts"][0]["text"]
        usage = data.get("usageMetadata", {})
        # Normalized to prompt_tokens/completion_tokens (Gemini calls them
        # promptTokenCount/candidatesTokenCount) so usage logging is provider-agnostic.
        normalized_usage = {
            "prompt_tokens": usage.get("promptTokenCount", 0),
            "completion_tokens": usage.get("candidatesTokenCount", 0),
        }
        return ChatResult(content=content, provider=self.name, model=model, raw_usage=normalized_usage)

    async def embed(self, texts: list[str], model: str, *, timeout: float | None = None) -> list[list[float]]:
        if not self.is_configured():
            raise ProviderError("Google API-nyckel saknas.")
        vectors = []
        url = f"{BASE_URL}/models/{model}:embedContent"
        async with httpx.AsyncClient(timeout=timeout or 60) as client:
            for text in texts:
                resp = await client.post(url, headers=self._headers(), json={"content": {"parts": [{"text": text}]}})
                resp.raise_for_status()
                vectors.append(resp.json()["embedding"]["values"])
        return vectors
