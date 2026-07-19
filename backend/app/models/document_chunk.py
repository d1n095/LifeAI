import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.config import get_settings
from app.db import Base

settings = get_settings()


class DocumentChunk(Base):
    """A chunked, embedded slice of a Document's text — the pgvector replacement for what
    used to live in Qdrant (see app/rag/vector_store.py). Unlike Document itself (shared
    company knowledge, see app/rls.py's docstring), chunks are owner-scoped and
    RLS-protected: see the document_chunks_isolation policy in app/rls.py. This is a
    deliberate narrowing for the pgvector migration, not a preexisting property of the old
    Qdrant-backed search — search results (and writes) are now strictly per-uploader.

    embedding's fixed dimension (settings.embedding_dim) is a real limitation pgvector
    imposes that Qdrant's lazily-created collection didn't: switching to an embedding model
    with a different vector size requires a new migration (ALTER COLUMN ... TYPE
    vector(N)), not just a runtime config change.
    """

    __tablename__ = "document_chunks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    # NOT NULL and RLS-bearing — see app/rag/ingest.py for why the background indexing task
    # must set app.current_user_id explicitly to this same value before writing, since it
    # doesn't run through app/deps.py's get_current_user the way a normal request does.
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    chunk_index: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float]] = mapped_column(Vector(settings.embedding_dim))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    document = relationship("Document", viewonly=True)
