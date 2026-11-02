"""Basic startup is model-independent: upload → durable original + sha256 → extraction →
restart survival, with NO embedding/chat model available.

This proves the Founder's "first supervised startup" milestone can be demonstrated *before*
any local-AI-model migration (docs/LIFE_FIRST_STARTUP_READINESS.md §1/§4): the only thing a
missing provider blocks is embedding/retrieval, and its absence is a graceful PAUSE
(`awaiting_provider`), never a lost upload and never an error.

Design notes (why this is a faithful "no model" test, not a mock of the happy path):
  * `verify_provider` is patched to report `not_configured` — exactly what a fresh machine
    with no API key / no local model returns (app/providers/verification.py returns
    `Outcome(VerificationResult.not_configured, "Ingen API-nyckel ...")` in that case). No
    network call, provider-agnostic.
  * `embed()` is patched to RAISE. If the pause logic were wrong and indexing ever reached a
    real embed, the document would land on `indexing_failed`, which the assertions below
    reject — so "the model is never called on the basic path" is enforced, not assumed.
  * The worker is driven through the real owner-blind claim clock + `process_claimed_job`
    (same as test_documents_upload_durable_delivery.py), with the API request session closed
    first — i.e. the extraction/pause happens across a simulated process restart, and a second
    fresh session re-reads to prove the durable state survives a further restart.
"""

from __future__ import annotations

import hashlib
import io

import pytest
from fastapi import UploadFile
from sqlalchemy import text as sa_text
from sqlalchemy.orm import sessionmaker

from app.models.document import Document, IndexStatus
from app.models.document_chunk import DocumentChunk
from app.models.import_job import ImportJob, ImportJobStatus
from app.providers.verification import Outcome, VerificationResult
from app.request_context import current_user_id as current_user_id_var
from app.routers.documents import upload_document
from app.storage import get_storage

# A small synthetic text file with Swedish characters — also shows extraction is UTF-8 clean
# with no model involved.
RAW = (
    "Livs första uppstart utan AI-modell.\n"
    "Grundaren loggar in, skickar ett meddelande och laddar upp en liten fil.\n"
    "Originalet bevaras byte-exakt, kontrollsumman beräknas, texten extraheras — "
    "allt detta kräver ingen inbäddnings- eller chattmodell.\n"
).encode("utf-8")
SHA256 = hashlib.sha256(RAW).hexdigest()


@pytest.fixture(autouse=True)
def _no_model_available(monkeypatch):
    """Force 'no embedding/chat model available', and make any embed() call a hard failure."""

    async def _unverified(provider, model, role, timeout):
        return Outcome(VerificationResult.not_configured, "Ingen API-nyckel är konfigurerad.")

    # Patched where ensure_verified looks it up (same module).
    monkeypatch.setattr("app.providers.verification.verify_provider", _unverified)

    from app.providers.openai_provider import OpenAIProvider

    async def _embed_must_not_run(self, texts, model, **kwargs):  # pragma: no cover - guard
        raise AssertionError("embed() must never be called when no model is available")

    monkeypatch.setattr(OpenAIProvider, "embed", _embed_must_not_run)


def _upload_file(name: str, raw: bytes) -> UploadFile:
    return UploadFile(filename=name, file=io.BytesIO(raw))


@pytest.mark.asyncio
async def test_basic_startup_upload_extract_persist_with_no_model(
    db_session, superuser_db, make_verified_user
):
    user, _ = make_verified_user()
    current_user_id_var.set(str(user.id))
    db_session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(user.id)})

    # --- HTTP upload: original + hash committed durably before any indexing ---
    doc = await upload_document(
        request=None,  # type: ignore[arg-type]
        file=_upload_file("forsta_uppstart.txt", RAW),
        category="notes",
        db=db_session,
        user=user,
    )
    doc_id = doc.id
    job_id = doc.import_job_id
    assert job_id is not None
    assert doc.status == IndexStatus.original_stored
    assert doc.storage_key is not None
    assert doc.checksum == SHA256  # sha256 integrity, computed with no model
    assert get_storage().exists(doc.storage_key)
    assert db_session.query(DocumentChunk).filter_by(document_id=doc_id).count() == 0

    # --- Simulated API process death: no request session, no owner context, no BackgroundTasks ---
    db_session.close()
    current_user_id_var.set(None)

    from app.db import SessionLocal, migration_engine
    from app.jobs.lease import claim_next_job
    from app.worker import process_claimed_job

    claim_db = sessionmaker(bind=migration_engine)()
    try:
        claimed = claim_next_job(claim_db, "worker-no-model", 120)
    finally:
        claim_db.close()
    assert claimed == (job_id, user.id)  # owner-blind claim clock found it unaided

    # --- Worker runs in a fresh process/session: extracts, then PAUSES (no model) ---
    worker_db = SessionLocal()
    try:
        await process_claimed_job(worker_db, job_id, user.id)
    finally:
        worker_db.close()

    # --- Re-read in yet another fresh session (second restart): durable state survived ---
    superuser_db.expire_all()
    parked = superuser_db.get(Document, doc_id)
    job = superuser_db.get(ImportJob, job_id)

    # Extraction ran and is cached, but the document is PAUSED on the provider, not failed.
    assert parked.status == IndexStatus.awaiting_provider
    assert parked.content_preview, "extracted text should be cached even with no model"
    assert parked.content_preview[:40] in RAW.decode("utf-8")  # it really is the file's text
    # Original + integrity intact and unchanged.
    assert parked.checksum == SHA256
    assert get_storage().exists(parked.storage_key)
    # No embedding happened: zero chunks, and the job is a resumable PAUSE, not a failure.
    assert superuser_db.query(DocumentChunk).filter_by(document_id=doc_id).count() == 0
    assert job.status in (ImportJobStatus.blocked, ImportJobStatus.partial)
    assert job.status not in (ImportJobStatus.failed, ImportJobStatus.completed)


@pytest.mark.asyncio
async def test_basic_startup_survives_a_further_restart(
    db_session, superuser_db, make_verified_user
):
    """A second, independent fresh session proves the paused-but-preserved state is durable,
    not an artifact of one session's identity map."""
    user, _ = make_verified_user()
    current_user_id_var.set(str(user.id))
    db_session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(user.id)})

    doc = await upload_document(
        request=None,  # type: ignore[arg-type]
        file=_upload_file("restart.txt", RAW),
        category=None,
        db=db_session,
        user=user,
    )
    doc_id = doc.id
    job_id = doc.import_job_id
    db_session.close()
    current_user_id_var.set(None)

    from app.db import SessionLocal, migration_engine
    from app.jobs.lease import claim_next_job
    from app.worker import process_claimed_job

    claim_db = sessionmaker(bind=migration_engine)()
    try:
        claim_next_job(claim_db, "worker-no-model-2", 120)
    finally:
        claim_db.close()
    worker_db = SessionLocal()
    try:
        await process_claimed_job(worker_db, job_id, user.id)
    finally:
        worker_db.close()

    # Brand-new session = another restart. The original and its hash are still there.
    reread = SessionLocal()
    try:
        reread.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(user.id)})
        again = reread.get(Document, doc_id)
        assert again is not None
        assert again.status == IndexStatus.awaiting_provider
        assert again.checksum == SHA256
        assert again.content_preview
        assert get_storage().exists(again.storage_key)
    finally:
        reread.close()
