"""Attachment Chamber -- MIME/format detection (Milestone 3).

Hand-rolled magic-byte signature checks for well-documented, public file-format headers --
matching app.rag.zip_import.MAGIC_BYTES's own naming/shape convention (that table only
covers .pdf/.docx, scoped to its own narrower purpose; this one is the general-purpose
version for the ingress boundary). Not "inventing crypto" -- these are public format
signatures, the exact same technique libmagic itself uses. No python-magic/filetype
dependency added; this codebase has neither installed, and a small, auditable, dependency-
free table is the right foundation-stage choice.

FILE PARSED != FILE SAFE, MODEL INTERPRETATION != SECURITY VERDICT: this module only ever
produces DetectedType/ActiveContentSignal *data* -- nothing here decides what happens next
(see risk.py for how detection results feed a QuarantineState decision).
"""

from __future__ import annotations

from app.attachment_chamber.path_safety import detect_double_extension

# ZIP/DOCX/XLSX/PPTX are ALL zip containers (same caveat app.rag.zip_import already
# documents for .docx specifically) -- "zip" is the detected_kind for any of them; the
# declared extension is what distinguishes which specific office format was CLAIMED.
ATTACHMENT_MAGIC_BYTES: dict[str, list[bytes]] = {
    "pdf": [b"%PDF-"],
    "zip": [b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"],  # local file header / empty archive / spanned
    "png": [b"\x89PNG\r\n\x1a\n"],
    "jpeg": [b"\xff\xd8\xff"],
    "gif": [b"GIF8"],
    "gzip": [b"\x1f\x8b"],
    "pe_executable": [b"MZ"],
    "elf_executable": [b"\x7fELF"],
}

# Which detected_kind a given declared extension is expected to match, for conflict
# detection. Extensions not listed here have no fixed-signature expectation (e.g. .txt/.md/
# .csv -- their "detection" is just UTF-8/ASCII decodability, handled separately below).
_EXPECTED_KIND_FOR_EXTENSION: dict[str, str] = {
    ".pdf": "pdf",
    ".docx": "zip",
    ".xlsx": "zip",
    ".pptx": "zip",
    ".zip": "zip",
    ".png": "png",
    ".jpg": "jpeg",
    ".jpeg": "jpeg",
    ".gif": "gif",
    ".gz": "gzip",
    ".exe": "pe_executable",
    ".msi": "pe_executable",
}


def detect_magic_kind(content_prefix: bytes) -> str | None:
    """Returns the FIRST matching detected_kind, or None if the prefix matches no known
    signature (including the plain-text fallback case, which this function deliberately
    does NOT claim -- text is a lack-of-binary-signature inference, made by the caller, not
    a positive magic-byte match)."""
    for kind, signatures in ATTACHMENT_MAGIC_BYTES.items():
        if any(content_prefix.startswith(sig) for sig in signatures):
            return kind
    return None


def looks_like_text(content_prefix: bytes) -> bool:
    if not content_prefix:
        return True  # zero-byte file: vacuously "text", nothing to contradict it
    try:
        content_prefix.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def detect_mime(filename: str, content_prefix: bytes):
    """CONFLICTS MUST BE SURFACED, NEVER SILENTLY RESOLVED ONE WAY. Returns a
    types.DetectedType. A conflict is any of: (a) the declared extension has a known
    expected magic-byte kind, and the actual content doesn't match it, or (b) a double
    extension was found (the hidden segment claims one thing, the last extension claims
    another -- surfaced as a signal regardless of whether the LAST extension's magic bytes
    happen to check out, since the hidden segment is itself the red flag)."""
    from app.attachment_chamber.types import DetectedType

    last_ext, hidden_ext = detect_double_extension(filename)
    detected_kind = detect_magic_kind(content_prefix)
    if detected_kind is None and looks_like_text(content_prefix):
        detected_kind = "text"

    conflict = False
    expected = _EXPECTED_KIND_FOR_EXTENSION.get(last_ext)
    if expected is not None and detected_kind is not None and detected_kind != expected:
        conflict = True
    double_extension_detected = hidden_ext is not None
    if double_extension_detected:
        conflict = True  # a hidden extension segment is itself a red flag, independent of magic bytes

    return DetectedType(
        declared_extension=last_ext, detected_kind=detected_kind, conflict=conflict,
        double_extension_detected=double_extension_detected, hidden_extension_segment=hidden_ext,
    )
