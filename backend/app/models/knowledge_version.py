import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class KnowledgeVersion(Base):
    """Immutable snapshot of a Document's extracted content at one point in time. A new
    version is created whenever the same checksum-distinct content is (re)imported for an
    existing source, or a source is deliberately re-extracted (e.g. after an extraction-logic
    upgrade) — see docs/FOUNDER_KNOWLEDGE_STUDIO_V1.md. Rows are never updated after
    creation, only inserted, so `created_at` alone is a reliable ordering key.

    RLS-protected the same way as document_chunks (owner_id, see app/rls.py) — a version
    row's content (raw_metadata) can include extraction details a non-owner must never read.
    """

    __tablename__ = "knowledge_versions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    version_number: Mapped[int] = mapped_column(Integer)
    checksum: Mapped[str] = mapped_column(String(64))
    extraction_version: Mapped[str] = mapped_column(String(32))
    raw_metadata: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
