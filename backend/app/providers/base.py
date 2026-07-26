from abc import ABC, abstractmethod
from dataclasses import dataclass, field


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

    def __init__(self, message: str, *, category: str | None = None):
        super().__init__(message)
        self.category = category
