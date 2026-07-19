import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class RelationshipType(str, enum.Enum):
    derived_from = "derived_from"
    supersedes = "supersedes"
    contradicts = "contradicts"
    supports = "supports"
    duplicates = "duplicates"
    belongs_to = "belongs_to"


class SourceRelationship(Base):
    """A directed edge between two knowledge sources (Document rows). Used tonight for two
    concrete things — see docs/FOUNDER_KNOWLEDGE_STUDIO_V1.md:
    1. `supersedes`/`contradicts` edges let app/rag/trust.py flag disputed/conflicting
       material to the founder and to MainAI's chat context instead of silently averaging
       over a contradiction.
    2. The Library UI's source detail page renders every edge touching a source.

    Both `from_source_id` and `to_source_id` must belong to the same `owner_id` — enforced
    in app/routers/library.py at write time (not just relied on implicitly), since in the
    founder-only architecture there is currently exactly one possible owner but the schema
    is written to stay correct once UserAI introduces more than one.
    """

    __tablename__ = "source_relationships"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    from_source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    to_source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    relationship_type: Mapped[RelationshipType] = mapped_column(Enum(RelationshipType))
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
