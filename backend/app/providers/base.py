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
