from abc import ABC, abstractmethod
from dataclasses import dataclass, field

# Real incident, 2026-07-26: a founder-facing config walkthrough left a literal placeholder
# string (a bracketed instruction like "<your key here>", "changeme", ...) as the ACTIVE
# value of an API key env var (compounded by a duplicate env-var line — see
# scripts/vps/lib.sh's check_no_duplicate_env_keys() for that half of the fix) — is_configured()
# reported True (a non-empty string), so the provider was attempted and failed confusingly
# instead of being cleanly skipped like a genuinely absent key. Matched case-insensitively,
# and also matches a value that's ENTIRELY wrapped in <angle brackets> or {curly braces} —
# the two most common "fill this in" placeholder conventions — without needing every
# possible example token spelled out here.
_PLACEHOLDER_SECRET_VALUES = {
    "changeme",
    "change-me",
    "change_me",
    "change-me-in-production",
    "your-api-key-here",
    "your_api_key_here",
    "your-api-key",
    "insert-key-here",
    "insert_key_here",
    "replace-me",
    "replace_me",
    "replaceme",
    "din_riktiga_nyckel",
    "din-riktiga-nyckel",
    "todo",
    "fixme",
    "placeholder",
    "example",
    "not-a-real-secret",
    "not_a_real_secret",
    "xxx",
    "xxxx",
    "xxxxxxxx",
}


def looks_like_placeholder_secret(value: str | None) -> bool:
    """True if `value` is non-empty but obviously a leftover template placeholder rather than
    a real credential — never true for an empty/None value, which already means "not
    configured" through the normal `bool(...)` check every provider's is_configured() uses.
    Deliberately a fixed denylist plus two bracket conventions, not a cleverness heuristic
    (e.g. "looks too short") that could reject a real, unusually-shaped key."""
    if not value:
        return False
    normalized = value.strip().lower()
    if normalized in _PLACEHOLDER_SECRET_VALUES:
        return True
    if (normalized.startswith("<") and normalized.endswith(">")) or (normalized.startswith("{") and normalized.endswith("}")):
        return True
    return False


@dataclass
class Message:
    role: str  # "user" | "assistant" | "system"
    content: str


@dataclass
class ChatResult:
    content: str
    provider: str
    model: str
    raw_usage: dict = field(default_factory=dict)


@dataclass
class StreamChunk:
    """One increment of a streamed chat response.

    `delta` is the incremental text to append (empty on the final chunk). `done=True` marks
    the last chunk — it, and only it, carries `usage`/`provider`/`model`, mirroring
    `ChatResult`'s fields so a caller can build one from the other without a second shape to
    track. A provider that cannot stream natively still yields this same shape (see
    `LLMProvider.chat_stream`'s default below) so callers never need a provider-specific
    branch."""

    delta: str
    done: bool = False
    usage: dict | None = None
    provider: str | None = None
    model: str | None = None


class LLMProvider(ABC):
    """Common interface every AI provider must implement.

    The rest of the platform only ever talks to this interface, so swapping
    OpenAI for Anthropic, Gemini, DeepSeek, OpenRouter or a local Ollama model
    requires no changes outside app/providers/.
    """

    name: str

    @abstractmethod
    async def chat(self, messages: list[Message], model: str, *, timeout: float | None = None, **kwargs) -> ChatResult: ...

    @abstractmethod
    async def embed(self, texts: list[str], model: str, *, timeout: float | None = None) -> list[list[float]]: ...

    @abstractmethod
    def is_configured(self) -> bool: ...

    async def chat_stream(self, messages: list[Message], model: str, *, timeout: float | None = None, **kwargs):
        """Default: no real incremental streaming — await the whole reply, then yield it as
        one chunk followed by the `done` sentinel. Every provider is therefore usable through
        the streaming endpoint even before it grows a real streaming implementation (today
        only `OllamaProvider` overrides this with genuine token-by-token output); callers must
        not assume a non-final chunk means real incremental progress was made on the wire —
        only that content is now available to show.

        `self.chat()` is wrapped here, not left to raise a raw exception into the caller's
        generator: `app/routers/chat.py`'s `_generate_stream_events()` only catches
        `ProviderError`/`_ClientDisconnected` around `provider.chat_stream()` (by design — see
        that function's docstring) the same way `chat_with_fallback()`'s own per-provider loop
        only expects `ProviderError`-shaped failures, never a raw `httpx` exception. Real,
        reproduced case: an `OpenAIProvider.chat()` call blocked by the network proxy raised
        `httpx.ProxyError` straight through this default implementation, which the router did
        not catch — the stream closed with zero bytes and no error event, and the claimed
        reply row was left stuck on its `inflight:<token>` marker forever (`_claim_reply_attempt`
        permanently blocks any further attempt on that marker, so the founder could not even
        retry). `provider_request_may_have_left=True` is the conservative default here, same as
        `chat_with_fallback()`'s own `getattr(exc, "provider_request_may_have_left", None) is
        not False` check — an exception this generic does not prove the request never reached
        the provider."""
        try:
            result = await self.chat(messages, model, timeout=timeout, **kwargs)
        except ProviderError:
            raise
        except Exception as exc:
            from app.providers.verification import classify_provider_exception

            safe = classify_provider_exception(exc)
            raise ProviderError(safe.message, category=safe.result.value, provider_request_may_have_left=True) from exc
        if result.content:
            yield StreamChunk(delta=result.content)
        yield StreamChunk(delta="", done=True, usage=result.raw_usage, provider=result.provider, model=result.model)


class ProviderError(RuntimeError):
    """`category` is an optional, already-safe classification (see
    app/providers/verification.py's classify_provider_exception — the same VerificationResult
    values: "unreachable", "rate_limited", "invalid_key", "unsupported", "not_configured") a
    caller can expose to a client without risking leaking request details (e.g. an API key
    embedded in a provider's URL, see that module's docstring). None when the raiser hasn't
    classified the failure — callers must treat that as "unknown", never assume a category."""

    def __init__(
        self,
        message: str,
        *,
        category: str | None = None,
        provider_request_may_have_left: bool | None = None,
    ):
        super().__init__(message)
        self.category = category
        # After adapter.propose() is entered: None/True → treat as ambiguous (do not
        # release spend). False → proven pre-external-effect; release is allowed.
        self.provider_request_may_have_left = provider_request_may_have_left


def chat_token_limit(kwargs, default=None):
    """Explicit output bounds are never silently ignored by a transport."""
    value = kwargs.get("max_tokens", default)
    if value is not None and (type(value) is not int or value <= 0):
        raise ProviderError("Invalid output token bound.", category="unsupported",
                            provider_request_may_have_left=False)
    return value
