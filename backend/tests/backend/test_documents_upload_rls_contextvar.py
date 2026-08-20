"""Legacy /api/documents/upload background indexer must bind RLS via contextvar."""

import pytest
from sqlalchemy import text as sa_text

from app.config import get_settings
from app.models.document import Document, DocumentSource, IndexStatus
from app.models.document_chunk import DocumentChunk
from app.request_context import current_user_id as current_user_id_var
from app.routers.documents import _index_in_background

EMBEDDING_DIM = get_settings().embedding_dim


@pytest.fixture(autouse=True)
def _fake_embedding_provider(monkeypatch):
    from app.providers.base import ChatResult
    from app.providers.openai_provider import OpenAIProvider

    async def _fake_embed(self, texts, model, **kwargs):
        return [[0.01 * (i + 1)] * EMBEDDING_DIM for i, _ in enumerate(texts)]

    async def _fake_chat(self, messages, model, **kwargs):
        return ChatResult(content="[]", provider="openai", model=model, raw_usage={"prompt_tokens": 1, "completion_tokens": 1})

    monkeypatch.setattr(OpenAIProvider, "embed", _fake_embed)
    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat)


def test_documents_upload_background_indexer_survives_mid_flight_commits(db_session, make_verified_user):
    """index_document commits while embedding; after_begin rebinds only from contextvar.
    SET LOCAL alone used to leave chunk upserts without owner scope after the first commit."""
    user, _ = make_verified_user()
    current_user_id_var.set(str(user.id))
    db_session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(user.id)})

    doc = Document(
        uploaded_by=user.id,
        title="legacy.txt",
        original_filename="legacy.txt",
        checksum="a" * 64,
        source=DocumentSource.upload,
        status=IndexStatus.pending,
    )
    db_session.add(doc)
    db_session.commit()
    doc_id = doc.id

    # Match a fresh background task: no request-scoped owner left in this process.
    current_user_id_var.set(None)

    _index_in_background(doc_id, "Enough text content for the legacy upload indexer to chunk and embed.", user.id)

    current_user_id_var.set(str(user.id))
    db_session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(user.id)})
    db_session.expire_all()
    doc = db_session.get(Document, doc_id)
    assert doc is not None
    assert doc.status == IndexStatus.indexed
    assert db_session.query(DocumentChunk).filter_by(document_id=doc_id).count() >= 1
