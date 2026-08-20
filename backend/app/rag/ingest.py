import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.document import Document, IndexStatus
from app.models.provider_verification import VerificationResult
from app.providers.registry import resolve_active
from app.providers.verification import classify_provider_exception, ensure_verified
from app.rag.chunking import chunk_text
from app.rag.vector_store import upsert_chunks


async def index_document(db: Session, document: Document, text_content: str) -> None:
    """Chunk, embed and store a document's text as pgvector-backed DocumentChunk rows.
    Updates status on the Document row.

    document.uploaded_by becomes the chunks' owner_id (see app/models/document_chunk.py) —
    required, not optional: a document with no uploader has nothing to scope chunk RLS to,
    so indexing is refused rather than silently writing ownerless (and therefore
    RLS-unreachable-by-anyone) rows. In practice this never happens today — the only wired
    ingestion path (POST /api/documents/upload, app/routers/documents.py) always sets it —
    but this is guarded explicitly rather than assumed, since a future ingestion path
    (e.g. the website crawler in docs/ROADMAP.md's Fas 2) could easily forget to.

    This session's `app.current_user_id` must already be set to document.uploaded_by before
    this runs (see app/rls.py's document_chunks_isolation policy) — the caller here
    (app/routers/documents.py's background task) does that explicitly, because a background
    task's fresh SessionLocal() never goes through app/deps.py's get_current_user, which is
    normally what sets it for a real request.
    """
    if document.uploaded_by is None:
        document.status = IndexStatus.failed
        document.error_message = "Dokumentet saknar ägare — kan inte indexeras."
        db.add(document)
        db.commit()
        return

    # Life Library upload consolidation: `embedding` (not the legacy `indexing`) is the
    # granular status for "chunking/embedding is in progress" — see IndexStatus's docstring.
    # The document row itself (and its extracted text, held in `text_content` by the caller)
    # already exists before this point, so a failure below never loses the received material.
    document.status = IndexStatus.embedding
    db.add(document)
    db.commit()

    try:
        chunks = chunk_text(text_content)
        if not chunks:
            # P1: reclassified from the old undifferentiated `failed` — this is an
            # extraction-adjacent problem (the content produced nothing chunkable), not a
            # provider issue, same bucket as app/rag/library_import.py's extract_text()
            # failure (see docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md §4.7).
            document.status = IndexStatus.extraction_failed
            document.error_message = "Inget textinnehåll kunde extraheras."
            db.add(document)
            db.commit()
            return

        provider, model = resolve_active(db, role="embedding")

        # P1: a real pre-flight check — never just is_configured() — BEFORE the actual
        # embed() call is ever attempted. A provider that isn't verified pauses the document
        # on awaiting_provider/blocked_provider instead of attempting (and failing) the call.
        #
        # Auto-resume is ONLY true for ImportJob-backed library imports: app/worker.py's
        # `_requeue_blocked_jobs` / `_reconcile_orphaned_documents` requeue knowledge_import_jobs
        # rows. Workbench save and /api/documents/upload call index_document with request-
        # scoped text and no ImportJob — claiming "bearbetas automatiskt" there was a false
        # capability. Persist content_preview before pausing so at least a durable excerpt
        # survives when the in-memory text_content evaporates with the request.
        verification = await ensure_verified(db, role="embedding")
        if verification.result != VerificationResult.ok:
            document.status = (
                IndexStatus.awaiting_provider
                if verification.result == VerificationResult.not_configured
                else IndexStatus.blocked_provider
            )
            document.content_preview = text_content[:1000]
            resumable = document.import_job_id is not None or bool(document.storage_key)
            if resumable:
                document.error_message = (
                    f"{verification.message} Filen är säkert lagrad och bearbetas automatiskt så "
                    "snart leverantören svarar."
                )
            else:
                document.error_message = (
                    f"{verification.message} Indexering pausad — ingen durable ImportJob/"
                    "storage_key finns för automatisk återstart. Spara eller importera igen "
                    "när leverantören är tillgänglig."
                )
            db.add(document)
            db.commit()
            return

        try:
            vectors = await provider.embed(chunks, model=model)
        except Exception as exc:  # noqa: BLE001 - a genuinely new failure AFTER a passed pre-flight check
            # P1: pre-flight passed but the real call still failed — a distinct, presumably
            # rarer case from a paused awaiting_provider/blocked_provider document, so it is
            # not automatically retried by the worker (see IndexStatus.indexing_failed's
            # docstring). Never str(exc) — see classify_provider_exception's docstring for
            # why (Gemini puts the API key in the request URL).
            document.status = IndexStatus.indexing_failed
            document.error_message = classify_provider_exception(exc).message
            db.add(document)
            db.commit()
            return

        count = upsert_chunks(db, document.id, document.uploaded_by, chunks, vectors)

        document.status = IndexStatus.indexed
        document.chunk_count = count
        document.content_preview = text_content[:1000]
        document.error_message = None
        db.add(document)
        db.commit()
    except Exception as exc:  # noqa: BLE001 - any other, unexpected failure (e.g. upsert_chunks) — an
        # internal-invariant guard, not part of the P1 taxonomy above; still never str(exc)
        # since this branch can in principle wrap a provider-adjacent exception too.
        document.status = IndexStatus.failed
        document.error_message = classify_provider_exception(exc).message
        db.add(document)
        db.commit()


async def reindex_document_id(db: Session, document_id: uuid.UUID, text_content: str) -> None:
    document = db.get(Document, document_id)
    if document is None:
        raise ValueError("Dokumentet finns inte.")
    if document.uploaded_by is not None:
        db.execute(text("SET LOCAL app.current_user_id = :uid"), {"uid": str(document.uploaded_by)})
    await index_document(db, document, text_content)
