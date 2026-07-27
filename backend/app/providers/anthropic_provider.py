import httpx

from app.config import get_settings
from app.providers.base import ChatResult, LLMProvider, Message, ProviderError, looks_like_placeholder_secret

BASE_URL = "https://api.anthropic.com/v1"
ANTHROPIC_VERSION = "2023-06-01"


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self):
        self.settings = get_settings()

    def is_configured(self) -> bool:
        return bool(self.settings.anthropic_api_key) and not looks_like_placeholder_secret(self.settings.anthropic_api_key)

    def _headers(self) -> dict:
        return {
            "x-api-key": self.settings.anthropic_api_key,
            "anthropic-version": ANTHROPIC_VERSION,
        }

    async def chat(self, messages: list[Message], model: str, *, timeout: float | None = None, **kwargs) -> ChatResult:
        if not self.is_configured():
            raise ProviderError("Anthropic API-nyckel saknas.")
        system = "\n".join(m.content for m in messages if m.role == "system") or None
        chat_messages = [{"role": m.role, "content": m.content} for m in messages if m.role != "system"]
        payload = {
            "model": model,
            "max_tokens": kwargs.get("max_tokens", 2048),
            "messages": chat_messages,
        }
        if system:
            payload["system"] = system
        async with httpx.AsyncClient(timeout=timeout or 60) as client:
            resp = await client.post(f"{BASE_URL}/messages", headers=self._headers(), json=payload)
            resp.raise_for_status()
            data = resp.json()
        content = "".join(block.get("text", "") for block in data.get("content", []))
        usage = data.get("usage", {})
        # Normalized to prompt_tokens/completion_tokens like every other provider (Anthropic
        # calls them input_tokens/output_tokens) so usage logging doesn't need per-provider cases.
        normalized_usage = {
            "prompt_tokens": usage.get("input_tokens", 0),
            "completion_tokens": usage.get("output_tokens", 0),
        }
        return ChatResult(content=content, provider=self.name, model=model, raw_usage=normalized_usage)

    async def embed(self, texts: list[str], model: str, *, timeout: float | None = None) -> list[list[float]]:
        raise ProviderError("Anthropic erbjuder inget embedding-API — använd OpenAI/Gemini/lokal modell för embeddings.")
