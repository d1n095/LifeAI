import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class ClaimRelationshipType(str, enum.Enum):
    """Subset of SourceRelationship's RelationshipType relevant at claim granularity —
    matches STEG 10's explicit list. No `derived_from`/`belongs_to` here: those are
    source-to-source provenance concepts (app/models/source_relationship.py), not something
    one individual claim has to another."""

    supports = "supports"
    contradicts = "contradicts"
    supersedes = "supersedes"
    duplicates = "duplicates"


class ClaimRelationship(Base):
    """A directed edge between two KnowledgeClaim rows — the claim-level analogue of
    SourceRelationship. `contradicts` is what lets app/rag/trust.py's
    assess_claim_confidence() flag a specific claim as disputed (confidence=conflict) rather
    than only ever flagging conflict at the whole-source level; `supports` from an
    INDEPENDENT source (different source_id) is what can raise a claim from `likely` to
    `certain` — a single source repeating itself doesn't count as independent corroboration,
    see app/rag/trust.py.

    Both ends must belong to the same owner_id, enforced at write time in
    app/routers/library.py (not just relied on implicitly) — same reasoning as
    SourceRelationship's docstring.
    """

    __tablename__ = "claim_relationships"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    from_claim_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_claims.id", ondelete="CASCADE"), index=True
    )
    to_claim_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_claims.id", ondelete="CASCADE"), index=True
    )
    relationship_type: Mapped[ClaimRelationshipType] = mapped_column(Enum(ClaimRelationshipType))
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
