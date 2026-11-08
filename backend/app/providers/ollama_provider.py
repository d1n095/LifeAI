import httpx

from app.config import get_settings
from app.providers.base import ChatResult, LLMProvider, Message, ProviderError


def _http_error_category(exc: httpx.HTTPError) -> str:
    """Classify an httpx failure for the fail-closed contract.

    A 404 from Ollama means the server is up but the requested model (or endpoint) does not
    exist on it — that is `unsupported` (provisioning gap), not `unreachable` (outage). Every
    other transport/status failure is `unreachable`. Callers use the category to decide
    between "wait for the provider to come back" and "this will never succeed as configured".
    """
    if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None and exc.response.status_code == 404:
        return "unsupported"
    return "unreachable"


def _ollama_error_text(data: object) -> str | None:
    """Ollama can answer HTTP 200 with a JSON error object (e.g. {"error": "model ... not
    found"}). Return that message if present so a 200-but-failed response still fails closed."""
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, str) and err.strip():
            return err.strip()
    return None


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
            raise ProviderError(
                f"Lokal modell (Ollama) inte nåbar: {exc}", category=_http_error_category(exc)
            ) from exc
        # A 2xx response is not proof of success: a malformed body, or Ollama's own 200-OK
        # error object, must fail closed as ProviderError — NOT escape as a raw KeyError/
        # TypeError that bypasses every caller's `except ProviderError` recovery path.
        error_text = _ollama_error_text(data)
        if error_text is not None:
            raise ProviderError(f"Lokal modell (Ollama) svarade med fel: {error_text}", category="unsupported")
        try:
            content = data["message"]["content"]
        except (KeyError, TypeError) as exc:
            raise ProviderError(
                "Lokal modell (Ollama) gav ett svar utan 'message.content'.", category="unsupported"
            ) from exc
        if not isinstance(content, str):
            raise ProviderError(
                "Lokal modell (Ollama) gav ett 'message.content' som inte är text.", category="unsupported"
            )
        return ChatResult(
            content=content,
            provider=self.name,
            model=model,
            # Normalized to prompt_tokens/completion_tokens (Ollama calls them
            # prompt_eval_count/eval_count) — always free (local), but still logged for volume.
            raw_usage={
                "prompt_tokens": data.get("prompt_eval_count", 0) if isinstance(data, dict) else 0,
                "completion_tokens": data.get("eval_count", 0) if isinstance(data, dict) else 0,
            },
        )

    async def embed(self, texts: list[str], model: str, *, timeout: float | None = None) -> list[list[float]]:
        vectors: list[list[float]] = []
        try:
            async with httpx.AsyncClient(timeout=timeout or 120) as client:
                for text in texts:
                    resp = await client.post(
                        f"{self.settings.ollama_base_url}/api/embeddings",
                        json={"model": model, "prompt": text},
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    # Same trust boundary as chat(): a 200-OK error object or a missing/invalid
                    # "embedding" must fail closed, never escape as a raw KeyError/TypeError.
                    error_text = _ollama_error_text(data)
                    if error_text is not None:
                        raise ProviderError(
                            f"Lokal modell (Ollama) svarade med fel: {error_text}", category="unsupported"
                        )
                    try:
                        vector = data["embedding"]
                    except (KeyError, TypeError) as exc:
                        raise ProviderError(
                            "Lokal modell (Ollama) gav ett svar utan 'embedding'.", category="unsupported"
                        ) from exc
                    if not isinstance(vector, list) or not vector or not all(
                        isinstance(v, (int, float)) for v in vector
                    ):
                        raise ProviderError(
                            "Lokal modell (Ollama) gav en 'embedding' som inte är en talvektor.",
                            category="unsupported",
                        )
                    vectors.append(vector)
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"Lokal modell (Ollama) inte nåbar: {exc}", category=_http_error_category(exc)
            ) from exc
        return vectors
