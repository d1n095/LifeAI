"""Attachment Chamber -- sanitization (Milestone 5).

SANITIZED COPY != ORIGINAL FILE: a sanitized derivative is always a NEW artifact with its
own content_hash, never an in-place mutation of the original -- the original remains
quarantined and untouched.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DerivativeKind(str, Enum):
    PLAIN_TEXT_EXTRACTION = "PLAIN_TEXT_EXTRACTION"
    METADATA_STRIPPED_COPY = "METADATA_STRIPPED_COPY"
    IMAGE_REENCODE = "IMAGE_REENCODE"  # interface-only stub, see sanitize_image_reencode()
    PDF_RENDERED_PAGE = "PDF_RENDERED_PAGE"  # interface-only stub, see sanitize_pdf_render()


class SanitizationNotImplementedError(NotImplementedError):
    """Honest stub -- image re-encoding and PDF-page rendering are NOT implemented this
    foundation stage."""


@dataclass(frozen=True)
class SanitizedDerivative:
    derivative_id: uuid.UUID
    original_attachment_id: uuid.UUID
    derivative_kind: DerivativeKind
    content_hash: str
    created_at: datetime


def sanitize_plain_text_extraction(*, original_attachment_id: uuid.UUID, original_content_hash: str, extracted_text: str) -> SanitizedDerivative:
    """The plain-text extraction IS the sanitized derivative -- structurally cannot equal
    the original's own bytes/hash (it's a different representation entirely: text only,
    stripped of every binary structure, macro, embedded object, and active-content marker
    the original might have carried)."""
    derivative_hash = hashlib.sha256(extracted_text.encode("utf-8")).hexdigest()
    if derivative_hash == original_content_hash:
        # Astronomically unlikely for a real document, but fail loudly rather than silently
        # accept a "sanitized" derivative indistinguishable from the original.
        raise ValueError("sanitized derivative hash collided with the original's hash -- refusing to treat this as sanitized")
    return SanitizedDerivative(
        derivative_id=uuid.uuid4(), original_attachment_id=original_attachment_id,
        derivative_kind=DerivativeKind.PLAIN_TEXT_EXTRACTION, content_hash=derivative_hash, created_at=_utcnow(),
    )


def sanitize_metadata_stripped_copy(*, original_attachment_id: uuid.UUID, stripped_bytes: bytes) -> SanitizedDerivative:
    return SanitizedDerivative(
        derivative_id=uuid.uuid4(), original_attachment_id=original_attachment_id,
        derivative_kind=DerivativeKind.METADATA_STRIPPED_COPY,
        content_hash=hashlib.sha256(stripped_bytes).hexdigest(), created_at=_utcnow(),
    )


def sanitize_image_reencode(*, original_attachment_id: uuid.UUID, raw_bytes: bytes) -> SanitizedDerivative:
    raise SanitizationNotImplementedError("image re-encode sanitization is not implemented in this foundation stage")


def sanitize_pdf_render(*, original_attachment_id: uuid.UUID, raw_bytes: bytes) -> SanitizedDerivative:
    raise SanitizationNotImplementedError("PDF rendered-page sanitization is not implemented in this foundation stage")
