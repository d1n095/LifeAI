"""Life Vault / External-AI Egress Control (docs/LIFE_VAULT_EGRESS_CONTROL.md, V2) — proves the
gate wired into app/rag/ingest.py::index_document() actually blocks NEVER_EGRESS-marked
document content before any embedding provider is ever called. Mirrors
test_media_import_provider_pause.py's direct-import-job pattern (`_make_job` + `run_import_job`)
rather than calling index_document() in isolation, so this exercises the real end-to-end
library-import path a founder's own upload actually goes through."""

import pytest
from sqlalchemy import text as sa_text

from app.config import get_settings
from app.models.document import Document, IndexStatus
from app.models.import_job import ImportJob, ImportJobStatus
from app.models.provider_disclosure import ProviderDisclosureEvent
from app.providers.openai_provider import OpenAIProvider
from app.rag.library_import import run_import_job
from app.request_context import current_user_id as current_user_id_var
from app.storage import get_storage

EMBEDDING_DIM = get_settings().embedding_dim


def _set_rls_user(db_session, owner_id) -> None:
    current_user_id_var.set(str(owner_id))
    db_session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


def _read_chunk_for(data: bytes, size: int = 1 << 16):
    pos = 0

    def _read():
        nonlocal pos
        chunk = data[pos : pos + size]
        pos += len(chunk)
        return chunk

    return _read


def _make_job(db_session, owner_id, raw: bytes, filename: str) -> ImportJob:
    _set_rls_user(db_session, owner_id)
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
async def test_never_egress_marked_document_content_is_denied_before_any_embedding_provider_is_ever_called(
    db_session, superuser_db, make_verified_user, monkeypatch
):
    embedded_texts: list[str] = []

    async def _tracking_embed(self, texts, model, **kwargs):
        # app/providers/verification.py's own pre-flight probe legitimately calls .embed()
        # with a fixed, safe constant (VERIFICATION_PROBE_TEXT = "ping") before the gated
        # call below ever runs -- that's expected and fine, since it never carries real
        # document content. What must never happen is the NEVER_EGRESS-marked content itself
        # reaching a real .embed() call.
        embedded_texts.extend(texts)
        return [[0.01] * EMBEDDING_DIM for _ in texts]

    monkeypatch.setattr(OpenAIProvider, "embed", _tracking_embed)

    user, _ = make_verified_user()
    marker_text = "NEVER_EGRESS: detta far aldrig lamnas till en extern leverantor."
    job = _make_job(db_session, user.id, marker_text.encode(), "secret.txt")

    await run_import_job(db_session, job.id, user.id)

    doc = db_session.query(Document).filter_by(uploaded_by=user.id, original_filename="secret.txt").one()
    assert doc.status == IndexStatus.indexing_failed
    assert doc.error_message is not None
    assert "egress denied" in doc.error_message.lower()

    # The marked document content itself never reached a real embedding call -- never a
    # partial embed with the poisoned chunk silently dropped.
    assert all("NEVER_EGRESS" not in t for t in embedded_texts)

    event = (
        superuser_db.query(ProviderDisclosureEvent)
        .filter_by(owner_id=user.id, purpose="rag_ingest")
        .order_by(ProviderDisclosureEvent.created_at.desc(), ProviderDisclosureEvent.id.desc())
        .first()
    )
    assert event is not None
    assert event.requested_by == "rag.ingest.index_document"
    assert event.decision == "denied"
    assert event.sent_content_hash is None
    assert "never_egress_marker" in event.redaction_categories


@pytest.mark.asyncio
async def test_ordinary_document_content_still_indexes_and_records_an_allowed_ledger_entry(
    db_session, superuser_db, make_verified_user, monkeypatch
):
    """Regression guard: the gate must not accidentally break the golden path it was wired
    into -- an ordinary document with no NEVER_EGRESS-marked content indexes exactly as before,
    now with a real allowed disclosure-ledger entry alongside it."""

    async def _fake_embed(self, texts, model, **kwargs):
        return [[0.01 * (i + 1)] * EMBEDDING_DIM for i, _ in enumerate(texts)]

    monkeypatch.setattr(OpenAIProvider, "embed", _fake_embed)

    user, _ = make_verified_user()
    job = _make_job(db_session, user.id, b"Ett helt vanligt dokument utan hemligheter.", "ordinary.txt")

    await run_import_job(db_session, job.id, user.id)

    doc = db_session.query(Document).filter_by(uploaded_by=user.id, original_filename="ordinary.txt").one()
    assert doc.status == IndexStatus.indexed

    event = (
        superuser_db.query(ProviderDisclosureEvent)
        .filter_by(owner_id=user.id, purpose="rag_ingest")
        .order_by(ProviderDisclosureEvent.created_at.desc(), ProviderDisclosureEvent.id.desc())
        .first()
    )
    assert event is not None
    assert event.decision == "allowed"
    assert event.sent_content_hash is not None
