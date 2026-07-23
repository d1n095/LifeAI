"""DEL 14 (prestanda/kostnad): local, real measurements against the Founder Knowledge Studio
pipeline — real Postgres/pgvector, a deterministic fake embedding provider (never a real AI
key, same pattern as test_library_import.py). Not a strict pass/fail benchmark suite: the
actual safety mechanism is the hard limits already enforced and tested in
app/rag/zip_import.py (MAX_FILES, MAX_TOTAL_UNCOMPRESSED_BYTES, ...) — this file's job is to
put real numbers next to those limits and act as a light regression guard (a large
unexplained slowdown fails the test) rather than to define the limits themselves.

Run with `-s` to see the printed numbers: pytest tests/backend/test_performance_measurement.py -s
"""

import io
import time
import uuid
import zipfile

import pytest

from app.config import get_settings
from app.models.import_job import ImportJob, ImportJobStatus
from app.rag.library_import import run_import_job
from app.rag.retrieve import retrieve_context
from app.rag.vector_store import hybrid_search
from app.storage import get_storage

EMBEDDING_DIM = get_settings().embedding_dim
FILE_COUNT = 10
PARAGRAPHS_PER_FILE = 20


@pytest.fixture(autouse=True)
def _fake_embedding_provider(monkeypatch):
    from app.providers.base import ChatResult
    from app.providers.openai_provider import OpenAIProvider

    async def _fake_embed(self, texts, model, **kwargs):
        return [[0.01 * ((i % 97) + 1)] * EMBEDDING_DIM for i, _ in enumerate(texts)]

    # Import now also runs claim extraction (app/rag/claims.py, STEG 10), which calls the
    # chat provider — without this mock, OpenAIProvider.is_configured() would see the truthy
    # "fake-key-for-tests" value and attempt a REAL outbound call during this measurement.
    async def _fake_chat(self, messages, model, **kwargs):
        return ChatResult(content="[]", provider="openai", model=model, raw_usage={"prompt_tokens": 5, "completion_tokens": 2})

    monkeypatch.setattr(OpenAIProvider, "embed", _fake_embed)
    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat)


def _make_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for i in range(FILE_COUNT):
            text = "\n\n".join(
                f"Stycke {p} i fil {i}. Detta ar syntetiskt testinnehall for prestandamatning, "
                f"unikt sokord matningsterm{i}." for p in range(PARAGRAPHS_PER_FILE)
            )
            zf.writestr(f"dokument-{i}.txt", text.encode())
    return buf.getvalue()


def _read_chunk_for(data: bytes, size: int = 1 << 16):
    pos = 0

    def _read():
        nonlocal pos
        chunk = data[pos : pos + size]
        pos += len(chunk)
        return chunk

    return _read


def _make_job(db_session, owner_id, raw: bytes, filename: str) -> ImportJob:
    """Durable-worker package: run_import_job() reads the original from app/storage/ via
    ImportJob.source_storage_key rather than taking raw bytes directly — this helper does the
    storage write a real POST /api/library/import would already have done (see
    test_library_import.py's identical helper)."""
    from sqlalchemy import text

    db_session.execute(text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})
    blob = get_storage().write_stream(_read_chunk_for(raw), max_bytes=max(len(raw), 1))
    job = ImportJob(
        owner_id=owner_id,
        status=ImportJobStatus.pending,
        source_filename=filename,
        source_checksum=blob.sha256,
        source_storage_key=blob.storage_key,
        source_size_bytes=blob.size_bytes,
    )
    db_session.add(job)
    db_session.commit()
    return job


@pytest.mark.asyncio
async def test_measure_import_and_search_performance(db_session, make_verified_user, capsys):
    user, _ = make_verified_user()
    zip_bytes = _make_zip()
    job = _make_job(db_session, user.id, zip_bytes, "prestandapaket.zip")

    import_start = time.monotonic()
    await run_import_job(db_session, job.id, user.id)
    import_seconds = time.monotonic() - import_start

    db_session.refresh(job)
    assert job.status.value == "completed"
    assert job.succeeded_count == FILE_COUNT

    from sqlalchemy import text as sqltext

    from app.models.document_chunk import DocumentChunk

    db_session.execute(sqltext("SET LOCAL app.current_user_id = :uid"), {"uid": str(user.id)})
    total_chunks = db_session.query(DocumentChunk).filter_by(owner_id=user.id).count()

    search_vector = [0.01] * EMBEDDING_DIM
    search_start = time.monotonic()
    hits = hybrid_search(db_session, user.id, search_vector, "matningsterm3", top_k=10)
    search_seconds = time.monotonic() - search_start

    retrieve_start = time.monotonic()
    context_hits = await retrieve_context(db_session, user.id, "vad star i dokumenten om matning?", top_k=5)
    retrieve_seconds = time.monotonic() - retrieve_start
    prompt_chars = sum(len(h["text"]) for h in context_hits)

    report = (
        f"\n--- Founder Knowledge Studio v1 — lokal prestandamätning ---\n"
        f"Import: {FILE_COUNT} filer, {PARAGRAPHS_PER_FILE} stycken/fil -> {import_seconds:.3f}s "
        f"({import_seconds / FILE_COUNT:.3f}s/fil)\n"
        f"Chunkar totalt: {total_chunks} ({total_chunks / FILE_COUNT:.1f} chunkar/fil i snitt)\n"
        f"Embedding-batchstorlek: en batch per fil (samtliga chunkar i en fil embeddas i ett anrop, se app/rag/ingest.py)\n"
        f"Hybrid-sök (top_k=10): {search_seconds * 1000:.1f} ms, {len(hits)} träffar\n"
        f"Chatt-hämtning (top_k=5): {retrieve_seconds * 1000:.1f} ms, {len(context_hits)} chunkar, "
        f"{prompt_chars} tecken kontext till prompten\n"
        f"--------------------------------------------------------------\n"
    )
    print(report)

    # Light regression guard, not the safety mechanism itself (that's zip_import.py's hard
    # limits — see module docstring): a local import/search taking orders of magnitude longer
    # than this against a tiny synthetic package would indicate a real performance
    # regression worth investigating, not normal variance.
    assert import_seconds < 30
    assert search_seconds < 5
    assert retrieve_seconds < 5
    assert total_chunks > 0
