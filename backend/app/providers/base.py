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
    pass
