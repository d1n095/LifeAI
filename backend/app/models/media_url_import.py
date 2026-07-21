import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class MediaUrlImportStatus(str, enum.Enum):
    """v1 only ever creates rows in pending_review and never advances them — see
    app/routers/library.py's create_media_url_import. The other members exist so the schema
    doesn't need a migration the day a human reviewer (or, later, an actual fetcher with a
    verified rights/consent gate) starts acting on these requests."""

    pending_review = "pending_review"
    approved = "approved"
    rejected = "rejected"


class MediaUrlImport(Base):
    """STEG 12's "secure URL-import model for future YouTube/web video" — deliberately a
    record of INTENT only. Nothing in this codebase reads `url` and fetches it: there is no
    downloader, no yt-dlp/ffmpeg dependency, no outbound request tied to this table anywhere
    in app/. Creating a row here is indistinguishable, from a safety standpoint, from writing
    a note — it exists so the founder can register "I want to import this" and so a future,
    explicitly-reviewed fetcher has a place to read verified consent/rights from before ever
    making a network request, not so importing already works.

    consent_confirmed and rights_note exist because "don't auto-download copyrighted
    material" is a real constraint, not a checkbox for its own sake: any future fetcher must
    refuse to run against a row where consent_confirmed is False, and rights_note is where
    the founder documents WHY they're allowed to import this specific URL (their own
    content, explicit permission, a permissive license, etc.) — see
    docs/FOUNDER_KNOWLEDGE_STUDIO_V1.md's STEG 12 section.
    """

    __tablename__ = "media_url_imports"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id"), nullable=True)
    url: Mapped[str] = mapped_column(Text)
    platform: Mapped[str] = mapped_column(String(64))
    consent_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    rights_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[MediaUrlImportStatus] = mapped_column(Enum(MediaUrlImportStatus), default=MediaUrlImportStatus.pending_review)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
