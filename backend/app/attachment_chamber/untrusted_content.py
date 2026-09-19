"""Attachment Chamber -- prompt-injection boundary (Milestone 6).

DOCUMENT CONTENT != MAINAI AUTHORITY, FILE CONTENT != INSTRUCTION: the defense is
structural, not a keyword denylist. There is no code path in this module that can construct
an UntrustedExtractedContent WITHOUT untrusted_content=True/ingress_source="file"/
instruction_authority="none" -- these are fixed defaults with no parameter to override them,
mirroring app.sentinel.types.SecurityEvent's own structural-safety discipline (details
validated at construction, never trusted as free-form).

Deliberately does NOT attempt prompt-injection keyword pattern-matching ("ignore previous
instructions" etc.) -- this campaign's own repeated "evidence exists != evidence supports
claim" and "substring match instead of exact match" lessons argue against a denylist that
is incomplete by construction. Extracted content is ALWAYS wrapped as inert, marked data,
never given a path to become an instruction, regardless of what it says.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class UntrustedExtractedContent:
    """The ONLY shape extracted file text/preview content may take once it leaves this
    package. No constructor parameter exists for the three safety fields below -- they are
    fixed at their safe values, not merely defaulted (a caller cannot pass
    untrusted_content=False even if it tried, since __post_init__ enforces it)."""

    attachment_id_str: str
    text: str
    extracted_at: datetime
    untrusted_content: bool = True
    ingress_source: str = "file"
    instruction_authority: str = "none"

    def __post_init__(self) -> None:
        # Structural enforcement, not just a default -- object.__setattr__ needed because
        # the dataclass is frozen (immutability is itself part of the safety guarantee: a
        # downstream consumer cannot mutate these markers away after construction either).
        object.__setattr__(self, "untrusted_content", True)
        object.__setattr__(self, "ingress_source", "file")
        object.__setattr__(self, "instruction_authority", "none")


def wrap_extracted_content(*, attachment_id_str: str, text: str) -> UntrustedExtractedContent:
    return UntrustedExtractedContent(attachment_id_str=attachment_id_str, text=text, extracted_at=_utcnow())
