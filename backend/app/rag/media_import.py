"""STEG 12: audio/video import v1. Mirrors app/rag/ingest.py's index_document() but for
timed transcript segments instead of plain extracted text — a chunk built here carries the
[start_seconds, end_seconds) range of the transcript segment(s) it came from
(app/models/document_chunk.py), so a citation can open the source at the exact moment
instead of just the source itself.

Scope of v1 (documented, not accidental): single-file upload only, through the same
/api/library/import endpoint app/rag/library_import.py already dispatches to
non-media files. One audio format (.mp3) and one video format (.mp4) are supported — the two
most common containers, and both have a fixed, checkable binary signature so "MIME/size
check" (STEG 12's own requirement) means something real, not just trusting the extension.
Audio/video bundled inside a ZIP package is explicitly NOT supported yet: zip_import.py's
ALLOWED_EXTENSIONS/MAGIC_BYTES only support simple startswith() signature checks, and mp4's
signature isn't at offset 0 (see MEDIA_MAGIC's mp4 entry below) — extending that module for
one offset-based format was judged not worth the risk of loosening its existing, carefully
scoped security surface for STEG 12 alone. A ZIP containing a .mp3/.mp4 today just skips
those entries as an unsupported extension, same as any other unrecognized file.
"""

import logging

from sqlalchemy.orm import Session

from app.models.document import Document, IndexStatus
from app.models.document_chunk import DocumentChunk
from app.models.provider_verification import VerificationResult
from app.providers.registry import resolve_active
from app.providers.transcription import TranscriptSegment, resolve_transcription_provider
from app.providers.verification import classify_provider_exception, ensure_verified

logger = logging.getLogger("mainai.rag.media_import")

AUDIO_EXTENSIONS = {".mp3"}
VIDEO_EXTENSIONS = {".mp4"}
MEDIA_EXTENSIONS = AUDIO_EXTENSIONS | VIDEO_EXTENSIONS

MEDIA_TYPES: dict[str, str] = {
    ".mp3": "audio/mpeg",
    ".mp4": "video/mp4",
}

# Same order of magnitude as app/routers/library.py's MAX_UPLOAD_BYTES (60 MB) — checked
# again here so this module's own safety guarantee doesn't just depend on the router
# remembering to enforce it upstream.
MAX_MEDIA_FILE_BYTES = 60 * 1024 * 1024

# MP3: either an ID3v2 tag header, or a bare MPEG frame sync (multiple valid header bytes
# depending on version/bitrate — 0xFFFB/0xFFFA/0xFFF3/0xFFF2/0xFFE3 cover the common cases).
# MP4: the ISO base media file format's first box is a 4-byte size followed by a 4-byte type
# — for a valid MP4 that type is "ftyp" at byte offset 4, never offset 0, which is why this
# can't reuse zip_import.py's simple content.startswith(sig) check.
_MP3_PREFIXES = (b"ID3", b"\xff\xfb", b"\xff\xfa", b"\xff\xf3", b"\xff\xf2", b"\xff\xe3")


class MediaImportError(Exception):
    """A single audio/video file's import is rejected — mirrors ZipSecurityError's per-entry
    counterpart, not the whole-import-aborting kind: raised inside _import_one_file's
    existing per-file try/except (app/rag/library_import.py), so one bad media file becomes a
    FileOutcome(status="failed"), never a job-level failure."""


def media_kind_for(filename: str) -> str | None:
    from pathlib import PurePosixPath

    suffix = PurePosixPath(filename).suffix.lower()
    if suffix in AUDIO_EXTENSIONS:
        return "audio"
    if suffix in VIDEO_EXTENSIONS:
        return "video"
    return None


def validate_media_bytes(filename: str, content: bytes, media_kind: str) -> None:
    """MIME/size check (STEG 12's explicit first pipeline step). Raises MediaImportError,
    never returns a bool — the caller's per-file try/except (see module docstring) is what
    turns this into a clean per-file failure rather than a crash."""
    if len(content) > MAX_MEDIA_FILE_BYTES:
        raise MediaImportError(f"Filen är för stor (max {MAX_MEDIA_FILE_BYTES // (1024 * 1024)} MB).")
    if not content:
        raise MediaImportError("Filen är tom.")

    if media_kind == "audio":
        if not content.startswith(_MP3_PREFIXES):
            raise MediaImportError("Filen har inte en giltig MP3-signatur — innehållet matchar inte filändelsen.")
    elif media_kind == "video":
        if len(content) < 8 or content[4:8] != b"ftyp":
            raise MediaImportError("Filen har inte en giltig MP4-signatur — innehållet matchar inte filändelsen.")
    else:
        raise MediaImportError(f"Okänt medietyp: {media_kind}")


class _TimedChunk:
    __slots__ = ("text", "start_seconds", "end_seconds")

    def __init__(self, text: str, start_seconds: float, end_seconds: float):
        self.text = text
        self.start_seconds = start_seconds
        self.end_seconds = end_seconds


def chunk_segments(segments: list[TranscriptSegment], chunk_size: int = 800) -> list[_TimedChunk]:
    """Groups consecutive transcript segments into chunks up to ~chunk_size words each —
    the timed analogue of app/rag/chunking.py's chunk_text(). No sliding-window overlap
    (unlike chunk_text): transcript segments already have natural break points at speech
    boundaries, which word-based prose chunking doesn't have, so overlap would just
    duplicate whole segments rather than smoothing an arbitrary word-count cut. A chunk's
    [start_seconds, end_seconds) is exactly the span of the segments grouped into it —
    never widened or guessed."""
    chunks: list[_TimedChunk] = []
    current_texts: list[str] = []
    current_words = 0
    current_start: float | None = None
    current_end: float | None = None

    def _flush() -> None:
        if current_texts:
            chunks.append(_TimedChunk(" ".join(current_texts), current_start, current_end))

    for seg in segments:
        text = seg.text.strip()
        if not text:
            continue
        word_count = len(text.split())
        if current_texts and current_words + word_count > chunk_size:
            _flush()
            current_texts = []
            current_words = 0
            current_start = None
        if current_start is None:
            current_start = seg.start_seconds
        current_end = seg.end_seconds
        current_texts.append(text)
        current_words += word_count

    _flush()
    return chunks


async def index_media_document(db: Session, document: Document, raw: bytes, filename: str, media_kind: str) -> None:
    """The audio/video analogue of app/rag/ingest.py's index_document(): transcribe -> chunk
    (preserving timestamps) -> embed -> store, updating status/chunk_count/duration on the
    Document row exactly like the text pipeline does. Same ownerless-document guard, same
    "this session's app.current_user_id must already be set" precondition (the caller,
    app/rag/library_import.py's _import_one_file, already establishes that before this
    runs — see app/rag/ingest.py's identical requirement)."""
    if document.uploaded_by is None:
        document.status = IndexStatus.failed
        document.error_message = "Dokumentet saknar ägare — kan inte indexeras."
        db.add(document)
        db.commit()
        return

    # Life Library upload consolidation: transcription is this pipeline's "extraction" step
    # (see app/rag/ingest.py's identical granular-status treatment) — the Document row
    # already exists (IndexStatus.original_stored) before this runs, so a transcription or
    # embedding failure below never loses the received media file.
    document.status = IndexStatus.extracting
    db.add(document)
    db.commit()

    try:
        provider = resolve_transcription_provider()
        transcript = await provider.transcribe(raw, filename, media_kind)
        timed_chunks = chunk_segments(transcript.segments)
        if not timed_chunks:
            document.status = IndexStatus.extraction_failed
            document.error_message = "Ingen transkription kunde skapas."
            db.add(document)
            db.commit()
            return

        document.status = IndexStatus.embedding
        db.add(document)
        db.commit()
        embed_provider, model = resolve_active(db, role="embedding")

        # Same pre-flight gate as app/rag/ingest.py's index_document: never attempt embed()
        # when the provider is unverified, and never persist str(exc) (Gemini embeds keys in URLs).
        verification = await ensure_verified(db, role="embedding")
        if verification.result != VerificationResult.ok:
            document.status = (
                IndexStatus.awaiting_provider
                if verification.result == VerificationResult.not_configured
                else IndexStatus.blocked_provider
            )
            preview = " ".join(c.text for c in timed_chunks)[:1000]
            document.content_preview = preview
            resumable = document.import_job_id is not None or bool(document.storage_key)
            if resumable:
                document.error_message = (
                    f"{verification.message} Filen är säkert lagrad och bearbetas automatiskt så "
                    "snart leverantören svarar."
                )
            else:
                document.error_message = (
                    f"{verification.message} Indexering pausad — ingen durable ImportJob/"
                    "storage_key finns för automatisk återstart. Importera igen när "
                    "leverantören är tillgänglig."
                )
            db.add(document)
            db.commit()
            return

        try:
            vectors = await embed_provider.embed([c.text for c in timed_chunks], model=model)
        except Exception as exc:  # noqa: BLE001 - post-preflight embed failure
            document.status = IndexStatus.indexing_failed
            document.error_message = classify_provider_exception(exc).message
            db.add(document)
            db.commit()
            return

        rows = [
            DocumentChunk(
                document_id=document.id,
                owner_id=document.uploaded_by,
                chunk_index=idx,
                text=chunk.text,
                embedding=vector,
                start_seconds=chunk.start_seconds,
                end_seconds=chunk.end_seconds,
            )
            for idx, (chunk, vector) in enumerate(zip(timed_chunks, vectors))
        ]
        db.add_all(rows)
        db.flush()

        document.status = IndexStatus.indexed
        document.chunk_count = len(rows)
        document.content_preview = " ".join(c.text for c in timed_chunks)[:1000]
        document.media_duration_seconds = transcript.duration_seconds
        document.transcript_provider = transcript.provider
        document.error_message = None
        db.add(document)
        db.commit()
    except Exception as exc:  # noqa: BLE001 - transcription/orchestration failure; never str(exc)
        document.status = IndexStatus.failed
        document.error_message = classify_provider_exception(exc).message
        db.add(document)
        db.commit()
