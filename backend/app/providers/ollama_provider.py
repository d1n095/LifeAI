import httpx

from app.config import get_settings
from app.providers.base import ChatResult, LLMProvider, Message, ProviderError


class OllamaProvider(LLMProvider):
    """Local model runner (Llama / Qwen / Mistral) — no API key, no external cost."""

    name = "ollama"

    def __init__(self):
        self.settings = get_settings()

    def is_configured(self) -> bool:
        # Local provider is always "configured" — availability is checked at call time.
        return True

    async def chat(self, messages: list[Message], model: str, *, timeout: float | None = None, **kwargs) -> ChatResult:
        payload = {
            "model": model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "stream": False,
        }
        try:
            async with httpx.AsyncClient(timeout=timeout or 120) as client:
                resp = await client.post(f"{self.settings.ollama_base_url}/api/chat", json=payload)
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPError as exc:
            raise ProviderError(f"Lokal modell (Ollama) inte nåbar: {exc}") from exc
        return ChatResult(
            content=data["message"]["content"],
            provider=self.name,
            model=model,
            # Normalized to prompt_tokens/completion_tokens (Ollama calls them
            # prompt_eval_count/eval_count) — always free (local), but still logged for volume.
            raw_usage={
                "prompt_tokens": data.get("prompt_eval_count", 0),
                "completion_tokens": data.get("eval_count", 0),
            },
        )

    async def embed(self, texts: list[str], model: str, *, timeout: float | None = None) -> list[list[float]]:
        vectors = []
        try:
            async with httpx.AsyncClient(timeout=timeout or 120) as client:
                for text in texts:
                    resp = await client.post(
                        f"{self.settings.ollama_base_url}/api/embeddings",
                        json={"model": model, "prompt": text},
                    )
                    resp.raise_for_status()
                    vectors.append(resp.json()["embedding"])
        except httpx.HTTPError as exc:
            raise ProviderError(f"Lokal modell (Ollama) inte nåbar: {exc}") from exc
        return vectors
