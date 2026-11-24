"""Real end-to-end extraction coverage for every document format app/rag/extract.py claims to
support (library_import.py's MEDIA_TYPES table). test_library_import.py's own fixtures only
ever exercise .txt/.md/.json content directly — real .pdf/.docx/.html bytes appear elsewhere
in the test suite (test_zip_import_security.py) only as header-only fakes used to probe the
ZIP-bomb/content-sniffing security boundary, never as genuinely parseable documents run through
the real pipeline. This file closes that gap: each format is built as real, parseable content
(not just a valid-looking header) and run through the actual run_import_job() -> extract_text()
-> index_document() path, asserting the real extracted text lands in DocumentChunk.text.

Found while auditing: pypdf.PdfWriter can build a minimal valid PDF with a genuinely
extractable text stream using only the built-in Helvetica font (no embedded font needed,
no external dependency like reportlab required) — see _make_real_pdf below."""

import io
from pathlib import Path

import pytest
from docx import Document as DocxDocument
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from app.config import get_settings
from app.db import SessionLocal
from app.models.document import Document, IndexStatus
from app.models.document_chunk import DocumentChunk
from app.models.import_job import ImportJobStatus

from .test_library_import import _fake_embedding_provider, _make_job  # noqa: F401 (fixture reuse)
from app.rag.library_import import run_import_job

EMBEDDING_DIM = get_settings().embedding_dim


def _make_real_pdf(text: str) -> bytes:
    """A minimal but genuinely valid PDF whose single page's content stream actually shows
    `text` with the built-in Helvetica font — pypdf.PdfReader.extract_text() recovers it
    verbatim, unlike the header-only '%PDF-1.4\\nfake...' bytes used elsewhere in this suite
    for security-boundary tests (those are deliberately NOT real documents)."""
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=300)

    font = DictionaryObject()
    font[NameObject("/Type")] = NameObject("/Font")
    font[NameObject("/Subtype")] = NameObject("/Type1")
    font[NameObject("/BaseFont")] = NameObject("/Helvetica")
    font_ref = writer._add_object(font)

    fonts_dict = DictionaryObject()
    fonts_dict[NameObject("/F1")] = font_ref
    resources = DictionaryObject()
    resources[NameObject("/Font")] = fonts_dict
    page[NameObject("/Resources")] = resources

    stream = DecodedStreamObject()
    stream.set_data(f"BT /F1 14 Tf 10 150 Td ({text}) Tj ET".encode("latin-1"))
    page[NameObject("/Contents")] = writer._add_object(stream)

    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _make_real_docx(text: str) -> bytes:
    doc = DocxDocument()
    doc.add_paragraph(text)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _make_real_html(text: str) -> bytes:
    return f"<html><body><h1>Title</h1><p>{text}</p></body></html>".encode("utf-8")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "filename,make_content,marker",
    [
        ("report.pdf", _make_real_pdf, "The quarterly founder report content"),
        ("notes.docx", _make_real_docx, "The quarterly founder notes content"),
        ("page.html", _make_real_html, "The quarterly founder page content"),
    ],
)
async def test_real_document_is_actually_extracted_not_just_accepted(
    db_session, make_verified_user, filename, make_content, marker
):
    """For each non-trivial format: build genuinely parseable content (not a header-only
    fake), run it through the real pipeline, and confirm the SPECIFIC marker text we wrote
    is recoverable from a real DocumentChunk row — proving extract_text() actually parsed
    the format's real structure, not that the importer merely accepted the file."""
    user, _ = make_verified_user()
    raw = make_content(marker)
    job = _make_job(db_session, user.id, raw, filename)

    await run_import_job(db_session, job.id, user.id)

    db_session.refresh(job)
    assert job.status == ImportJobStatus.completed, job.error_message
    assert job.succeeded_count == 1
    assert job.failed_count == 0

    doc = db_session.query(Document).filter_by(uploaded_by=user.id, original_filename=filename).first()
    assert doc is not None
    assert doc.status == IndexStatus.indexed
    assert doc.chunk_count >= 1

    chunks = db_session.query(DocumentChunk).filter_by(document_id=doc.id).all()
    assert len(chunks) >= 1
    full_text = " ".join(c.text for c in chunks)
    assert marker in full_text, f"expected real extracted marker text in chunks for {filename}, got: {full_text!r}"


@pytest.mark.asyncio
async def test_embedding_call_failure_after_preflight_fails_closed_not_stuck_not_silent(
    db_session, make_verified_user, monkeypatch
):
    """app/rag/ingest.py's index_document() has a dedicated except-branch for an embed()
    call that fails AFTER the pre-flight ensure_verified() check already passed (distinct
    from the awaiting_provider/blocked_provider path for a provider that was never reachable
    to begin with) — no existing test in this suite exercises that exact branch with a real
    run_import_job() call. Confirm it fails closed to indexing_failed (not stuck in
    `embedding`, not silently marked `indexed` with zero/partial chunks) and that no
    DocumentChunk rows get written at all for the failed document."""
    from app.providers.openai_provider import OpenAIProvider
    from app.providers.verification import VERIFICATION_PROBE_TEXT

    async def _embed_boom_after_preflight(self, texts, model, **kwargs):
        # ensure_verified()'s own pre-flight probe calls provider.embed([VERIFICATION_PROBE_TEXT], ...)
        # — it must keep succeeding, or this never reaches the post-preflight branch under test
        # at all (it would land on awaiting_provider/blocked_provider instead, a different,
        # already-covered branch). Only the REAL embed call (the document's actual chunks) fails.
        if list(texts) == [VERIFICATION_PROBE_TEXT]:
            return [[0.01] * EMBEDDING_DIM]
        raise RuntimeError("simulated embedding provider failure after preflight")

    # _fake_embedding_provider (imported fixture, autouse) already patched .embed to succeed;
    # this monkeypatch call happens after it runs for this test, so it wins.
    monkeypatch.setattr(OpenAIProvider, "embed", _embed_boom_after_preflight)

    user, _ = make_verified_user()
    job = _make_job(db_session, user.id, b"text that will fail at the embedding step, not extraction", "will-fail.txt")

    await run_import_job(db_session, job.id, user.id)

    db_session.refresh(job)
    # Single-file job, that one file failed at the embed step -> the whole job is `failed`
    # (not `partial`, which is reserved for a mix of successes and failures; not stuck in
    # pending/running, and not misclassified as the unrelated `blocked` status, which is
    # reserved for every file pausing on a pre-flight check that never even attempted a
    # real call — see ImportJobStatus's own docstring).
    assert job.status == ImportJobStatus.failed
    assert job.succeeded_count == 0
    assert job.failed_count == 1

    doc = db_session.query(Document).filter_by(uploaded_by=user.id, original_filename="will-fail.txt").first()
    assert doc is not None
    assert doc.status == IndexStatus.indexing_failed, f"expected indexing_failed, got {doc.status}"
    assert doc.status != IndexStatus.embedding, "must not be stuck mid-embedding"
    assert doc.chunk_count == 0
    assert doc.error_message is not None
    assert "RuntimeError" not in doc.error_message  # classify_provider_exception(), never raw str(exc)

    chunks = db_session.query(DocumentChunk).filter_by(document_id=doc.id).all()
    assert chunks == [], "a failed embed call must leave zero chunk rows, not a partial write"
