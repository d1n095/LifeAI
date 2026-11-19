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


def _is_numeric_vector(value: object) -> bool:
    """A non-empty list of real numbers — the only shape acceptable as an embedding."""
    return (
        isinstance(value, list)
        and bool(value)
        and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value)
    )


def _is_missing_embed_route(resp: httpx.Response) -> bool:
    """True only for a 404 that means "this server has no /api/embed" — a plain-text 404.

    A model-not-found 404 is JSON with an `error` field and must NOT be mistaken for an old
    server: silently retrying that against the legacy endpoint would turn a provisioning gap
    ("pull the model") into a confusing double failure."""
    if resp.status_code != 404:
        return False
    try:
        data = resp.json()
    except ValueError:
        return True
    return not (isinstance(data, dict) and data.get("error"))


def _validated_batch_embeddings(data: object, *, expected_count: int) -> list[list[float]]:
    """Validate an /api/embed body, failing closed on every malformed shape.

    All vectors in a batch must share one width: app/rag/embedding_space.py's fit_vectors()
    fails closed on a mixed batch anyway, and catching it here names the actual cause
    (a misbehaving runtime) instead of surfacing as a storage-layer error."""
    error_text = _ollama_error_text(data)
    if error_text is not None:
        raise ProviderError(
            f"Lokal modell (Ollama) svarade med fel: {error_text}", category="unsupported"
        )
    if not isinstance(data, dict) or "embeddings" not in data:
        raise ProviderError(
            "Lokal modell (Ollama) gav ett svar utan 'embeddings'.", category="unsupported"
        )
    raw = data["embeddings"]
    if not isinstance(raw, list) or not all(_is_numeric_vector(vector) for vector in raw):
        raise ProviderError(
            "Lokal modell (Ollama) gav 'embeddings' som inte är en lista av talvektorer.",
            category="unsupported",
        )
    if len(raw) != expected_count:
        raise ProviderError(
            f"Lokal modell (Ollama) gav {len(raw)} vektorer för {expected_count} texter.",
            category="unsupported",
        )
    if len({len(vector) for vector in raw}) > 1:
        raise ProviderError(
            "Lokal modell (Ollama) gav vektorer med olika dimension i samma batch.",
            category="unsupported",
        )
    return [[float(v) for v in vector] for vector in raw]


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
        # `max_tokens` is the cross-provider convention every other adapter already honours
        # (anthropic_provider.py, and provider_verification's 1-token probe). Ollama previously
        # ignored it entirely, so a local generation could run until the model's full context
        # and exceed the client's 120s timeout — observed as a corpus_review job failing after
        # exactly 2m0s with an empty "unreachable" error. Mapping it to Ollama's own
        # `options.num_predict` makes a bounded-output request actually bounded on this provider.
        max_tokens = kwargs.get("max_tokens")
        if max_tokens is not None:
            payload["options"] = {"num_predict": int(max_tokens)}
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
        """Embed a whole batch in ONE `/api/embed` call, at the model's NATIVE width.

        Deliberately does not truncate or pad to settings.embedding_dim. Fitting a native
        vector into the fixed pgvector column is app/rag/embedding_space.py's job, which
        zero-pads (cosine-preserving, so rankings are identical) and stamps each chunk with
        the model + native dimension so retrieval never mixes embedding spaces. Truncating
        here instead would have to discard signal and would break that contract for exactly
        the local models the padding path exists to support.

        Batching matters on CPU-local runtimes: one request per document batch instead of one
        per chunk is the difference between a usable ingestion and a ~13 s-per-chunk crawl.
        """
        if not texts:
            return []
        try:
            async with httpx.AsyncClient(timeout=timeout or 120) as client:
                resp = await client.post(
                    f"{self.settings.ollama_base_url}/api/embed",
                    json={"model": model, "input": texts},
                )
                if _is_missing_embed_route(resp):
                    # An Ollama older than 0.1.36 has no /api/embed; its 404 is a plain-text
                    # "404 page not found", which is NOT the same failure as a JSON
                    # model-not-found (that one stays a real `unsupported`, below). Falling back
                    # to the per-text endpoint keeps an older-but-working local runtime usable
                    # instead of rejecting it outright.
                    return await self._embed_legacy(client, texts, model)
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"Lokal modell (Ollama) inte nåbar: {exc}", category=_http_error_category(exc)
            ) from exc
        return _validated_batch_embeddings(data, expected_count=len(texts))

    async def _embed_legacy(
        self, client: httpx.AsyncClient, texts: list[str], model: str
    ) -> list[list[float]]:
        """Pre-`/api/embed` Ollama: the same fail-closed contract, one text per request."""
        vectors: list[list[float]] = []
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
            if not _is_numeric_vector(vector):
                raise ProviderError(
                    "Lokal modell (Ollama) gav en 'embedding' som inte är en talvektor.",
                    category="unsupported",
                )
            vectors.append([float(v) for v in vector])
        return vectors
