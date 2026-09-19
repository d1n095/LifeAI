"""Attachment Chamber -- safe preview (Milestone 5).

PREVIEW != NATIVE APPLICATION OPEN, PREVIEW != OPEN, OPEN != EXECUTE: nothing in this module
launches an external application, renders active content, or executes anything derived from
file bytes. Text preview is bounded, size-capped plain-text extraction only.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from app.attachment_chamber.types import AttachmentIdentity, QuarantineState

MAX_TEXT_PREVIEW_CHARS = 20_000


class PreviewKind(str, Enum):
    TEXT = "TEXT"
    IMAGE = "IMAGE"
    DOCUMENT = "DOCUMENT"


class PreviewNotReadyError(ValueError):
    pass


class PreviewNotImplementedError(NotImplementedError):
    """Honest stub -- image decoding and document rendering are NOT implemented this
    foundation stage. Never silently returns a fake/empty preview claiming success."""


@dataclass(frozen=True)
class PreviewResult:
    attachment_id_str: str
    kind: PreviewKind
    content: str
    truncated: bool
    # Structural prompt-injection boundary markers -- see untrusted_content.py for the
    # general-purpose version; duplicated here as non-optional fields with fixed safe
    # values so a PreviewResult can never be constructed without them.
    untrusted_content: bool = True
    ingress_source: str = "file"
    instruction_authority: str = "none"


def preview_text(identity: AttachmentIdentity, raw_text: str) -> PreviewResult:
    """Requires the identity to already be at SAFE_FOR_PREVIEW -- PREVIEW is not something
    any RECEIVED/QUARANTINED/INSPECTING file may skip ahead to."""
    if identity.lifecycle_state != QuarantineState.SAFE_FOR_PREVIEW:
        raise PreviewNotReadyError(f"attachment {identity.attachment_id} is {identity.lifecycle_state.value}, not SAFE_FOR_PREVIEW")
    truncated = len(raw_text) > MAX_TEXT_PREVIEW_CHARS
    content = raw_text[:MAX_TEXT_PREVIEW_CHARS]
    return PreviewResult(attachment_id_str=str(identity.attachment_id), kind=PreviewKind.TEXT, content=content, truncated=truncated)


def preview_image(identity: AttachmentIdentity, raw_bytes: bytes) -> PreviewResult:
    raise PreviewNotImplementedError("image preview (decode + bounded re-render) is not implemented in this foundation stage")


def preview_document(identity: AttachmentIdentity, raw_bytes: bytes) -> PreviewResult:
    raise PreviewNotImplementedError("document preview (sanitized/rendered representation) is not implemented in this foundation stage")
