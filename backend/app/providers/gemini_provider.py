import logging

import httpx

from app.config import get_settings
from app.providers.base import ChatResult, LLMProvider, Message, ProviderError, looks_like_placeholder_secret

API_HOST = "generativelanguage.googleapis.com"
API_VERSION = "v1beta"
BASE_URL = f"https://{API_HOST}/{API_VERSION}"

logger = logging.getLogger(__name__)


def _normalize_model(raw_model: str) -> str:
    """Strips surrounding whitespace and, if the whole string is wrapped in a single matching
    pair of straight quotes, those too — a real, previously-invisible failure mode: Docker
    Compose's `env_file:` directive does NOT strip quote characters the way a shell `source`
    does, so `DEFAULT_LLM_MODEL="gemini-2.5-flash"` in lifeai.env is read by bash tooling as
    the clean value but reaches this app's environment as the LITERAL 8-character-longer
    string `"gemini-2.5-flash"`, quotes included — a value Google's API has never heard of,
    producing a 404 that looks identical to a wrong model name. Logged whenever normalization
    actually changes something, so this stops being invisible if it recurs."""
    normalized = raw_model.strip()
    if len(normalized) >= 2 and normalized[0] == normalized[-1] and normalized[0] in "\"'":
        normalized = normalized[1:-1].strip()
    if normalized != raw_model:
        logger.warning("Gemini model identifier required normalization: %r -> %r", raw_model, normalized)
    return normalized


def _safe_error_detail(resp: httpx.Response) -> str:
    """Google's error responses are a JSON body — {"error": {"code", "message", "status"}} —
    that resp.raise_for_status() discards entirely, leaving only a generic 'HTTP 404' with no
    way to tell a wrong model name apart from a disabled API, a region restriction, or
    anything else Google's own message would have named directly. Safe to surface as-is: this
    is Google's own service-side description of what's wrong with the REQUEST SHAPE, never an
    echo of the request itself — it does not and cannot contain the URL or the header this
    provider sent, so it carries none of the risk classify_provider_exception() guards against
    for a raw httpx exception. Best-effort: falls back to a fixed, still-safe string if the
    body isn't the JSON shape Google normally sends."""
    try:
        error = resp.json().get("error", {})
        status = error.get("status", "")
        message = error.get("message", "")
        if status or message:
            return f"{status}: {message}".strip(": ")
    except (ValueError, AttributeError):
        pass
    return "Inget maskinläsbart felmeddelande i svaret."


def _category_for_status(status_code: int) -> str:
    if status_code in (401, 403):
        return "invalid_key"
    if status_code == 429:
        return "rate_limited"
    return "unreachable"


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

    def _url(self, model: str, action: str) -> str:
        """Single URL builder for every Gemini call (chat AND embed) — a divergence between
        the two was one of the suspects in a 2026-07-27 404 investigation; sharing this one
        function makes that class of bug structurally impossible going forward."""
        return f"{BASE_URL}/models/{model}:{action}"

    async def _post(self, action: str, model: str, payload: dict, timeout: float | None) -> dict:
        model = _normalize_model(model)
        url = self._url(model, action)
        # Diagnostic logging, 2026-07-27 incident: provider name, host, API version, and the
        # normalized model identifier — enough to catch a wrong/duplicated model path, a bad
        # base URL join, or a stray quote/whitespace character (visible via %r) — but never
        # the URL's full path with a real query string, and never headers, so the key can
        # never reach a log line through this call site either.
        logger.info("Gemini request: provider=%s host=%s api_version=%s action=%s model=%r", self.name, API_HOST, API_VERSION, action, model)
        async with httpx.AsyncClient(timeout=timeout or 60) as client:
            resp = await client.post(url, headers=self._headers(), json=payload)
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                detail = _safe_error_detail(resp)
                raise ProviderError(
                    f"Gemini svarade med HTTP {resp.status_code} för modell {model!r}: {detail}",
                    category=_category_for_status(resp.status_code),
                ) from exc
            return resp.json()

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
        data = await self._post("generateContent", model, payload, timeout)
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
        for text in texts:
            data = await self._post("embedContent", model, {"content": {"parts": [{"text": text}]}}, timeout)
            vectors.append(data["embedding"]["values"])
        return vectors
