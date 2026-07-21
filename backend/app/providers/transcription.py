"""STEG 12: transcription provider interface for audio/video import, mirroring
app/providers/base.py's LLMProvider pattern so the rest of the platform depends on this
interface, not on any one implementation — a real speech-to-text provider (Whisper, Gemini,
or anything else) can be added later purely as a new TranscriptionProvider subclass, with no
changes anywhere else.

No real transcription provider is wired today (no API keys, no paid calls — an explicit
constraint of this work order). MockTranscriptionProvider below is the ONLY implementation
that exists: it never makes a network call and never does real speech recognition (there is
no ML/ASR library available for that here), but it is not a test-only stub either — it is
what a real deployment gets today, and it is honest about that in the transcript it produces
for a real, unrecognized audio/video file (see its docstring). Tests get deterministic,
meaningful multi-segment transcripts by monkeypatching `.transcribe()` directly — the exact
same pattern already used for `OpenAIProvider.chat`/`.embed` throughout this codebase (see
e.g. tests/backend/test_library_import.py's `_fake_chat`/`_fake_embed`) — not by a separate
"fake provider" class.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

# Nominal bytes-per-second used only to give a real, unrecognized upload a plausible
# duration for the player UI (STEG 13) when no real ASR provider is configured — NOT a
# claim about the file's actual encoding/bitrate. Roughly 128kbps audio / ~4Mbps video.
_NOMINAL_BYTES_PER_SECOND = {
    "audio": 16_000,
    "video": 500_000,
}


@dataclass
class TranscriptSegment:
    start_seconds: float
    end_seconds: float
    text: str


@dataclass
class TranscriptResult:
    segments: list[TranscriptSegment] = field(default_factory=list)
    duration_seconds: float = 0.0
    provider: str = ""
    model: str = ""
    language: str | None = None


class TranscriptionProviderError(RuntimeError):
    pass


class TranscriptionProvider(ABC):
    name: str

    @abstractmethod
    async def transcribe(self, raw: bytes, filename: str, media_kind: str) -> TranscriptResult:
        """media_kind is "audio" or "video" (see app/rag/media_import.py's classification by
        extension) — not a MIME type, since a provider's behavior only needs to distinguish
        those two broad kinds, not every possible container format."""
        ...

    @abstractmethod
    def is_configured(self) -> bool: ...


class MockTranscriptionProvider(TranscriptionProvider):
    """The only TranscriptionProvider wired in this app today. Always "configured" (needs no
    API key — that's the point: STEG 12 ships a working end-to-end pipeline without any real
    paid transcription call). Produces exactly one segment spanning the whole (estimated)
    duration, with placeholder text that says plainly that no real transcription happened —
    it does NOT invent plausible-sounding fake spoken content for a real audio/video file,
    which would be actively misleading in a system whose entire premise (see app/rag/trust.py)
    is never presenting ungrounded content as if it were real.

    A real deployment wanting actual searchable transcripts plugs in a real provider (Whisper,
    Gemini, etc.) implementing this same interface — see resolve_transcription_provider()."""

    name = "mock"

    def is_configured(self) -> bool:
        return True

    async def transcribe(self, raw: bytes, filename: str, media_kind: str) -> TranscriptResult:
        bytes_per_second = _NOMINAL_BYTES_PER_SECOND.get(media_kind, _NOMINAL_BYTES_PER_SECOND["audio"])
        duration = max(1.0, len(raw) / bytes_per_second)
        placeholder = (
            "[Automatisk transkription är inte tillgänglig — ingen riktig "
            "transkriptionsleverantör är konfigurerad (se app/providers/transcription.py). "
            f"Filen \"{filename}\" är importerad och kan spelas upp, men innehållet är inte "
            "sökbart eller källhänvisningsbart förrän en riktig leverantör kopplas in.]"
        )
        return TranscriptResult(
            segments=[TranscriptSegment(start_seconds=0.0, end_seconds=duration, text=placeholder)],
            duration_seconds=duration,
            provider=self.name,
            model="placeholder-v1",
        )


def resolve_transcription_provider() -> TranscriptionProvider:
    """Single resolution point (mirrors app/providers/registry.py's resolve_active for
    chat/embedding) — a future real provider gets registered here, not scattered across
    app/rag/media_import.py's call sites."""
    return MockTranscriptionProvider()
