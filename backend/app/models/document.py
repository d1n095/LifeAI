import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class DocumentSource(str, enum.Enum):
    upload = "upload"
    website = "website"
    code = "code"
    manual = "manual"


class IndexStatus(str, enum.Enum):
    pending = "pending"
    indexing = "indexing"
    indexed = "indexed"
    failed = "failed"


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(String(512))
    source: Mapped[DocumentSource] = mapped_column(Enum(DocumentSource), default=DocumentSource.upload)
    source_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    category: Mapped[str | None] = mapped_column(String(128), nullable=True)
    file_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    content_preview: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[IndexStatus] = mapped_column(Enum(IndexStatus), default=IndexStatus.pending)
    chunk_count: Mapped[int] = mapped_column(default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
