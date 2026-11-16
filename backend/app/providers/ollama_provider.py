from urllib.parse import urlparse

import httpx

from app.config import get_settings
from app.providers.base import ChatResult, LLMProvider, Message, ProviderError

# Hosts that mean "this Ollama runs on the same host/deployment as the backend, so a call to
# it is NOT an external-AI disclosure": the loopback addresses, and the in-compose service
# name `ollama` (the default ollama_base_url, resolved on the deployment's own internal
# network, never over the public internet). Anything else is a real remote endpoint and is
# treated as external egress -- see is_external_egress below.
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "ollama"})


class OllamaProvider(LLMProvider):
    """Local model runner (Llama / Qwen / Mistral) — no API key, no external cost."""

    name = "ollama"

    def __init__(self):
        self.settings = get_settings()

    def is_configured(self) -> bool:
        # Local provider is always "configured" — availability is checked at call time.
        return True

    @property
    def is_external_egress(self) -> bool:
        """False (on-host, no external-AI disclosure) only when the CONFIGURED base URL points
        at the loopback interface or the in-deployment `ollama` container; True otherwise.

        Deliberately derived from the live `ollama_base_url`, never hardcoded to False for the
        provider class: `ollama_base_url` is operator-configurable and could legitimately point
        at a remote Ollama host over the network, which IS external egress. Classifying by the
        provider name alone would let a misconfigured remote endpoint be ledgered as "nothing
        left the machine" -- exactly the false-negative this fail-safe exists to prevent. A
        URL we cannot parse a host from counts as external (the safe side)."""
        host = (urlparse(self.settings.ollama_base_url).hostname or "").lower().strip("[]")
        return host not in _LOCAL_HOSTS

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
