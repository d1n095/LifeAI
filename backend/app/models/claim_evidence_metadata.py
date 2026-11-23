import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, ForeignKeyConstraint, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class ClaimEvidenceMetadata(Base):
    """The founder-leg STUDY-METADATA row for one KnowledgeClaim — the deterministic
    producer side of app/rag/evidence_quality.grade_evidence(), which until now had no
    production caller at all (graded metadata existed only in tests). The founder (or an
    import pipeline; never an ungated model leg) DECLARES structured study facts for a
    claim; this table stores exactly what was declared plus the MATERIALIZED verdict the
    pure grader computed from it.

    Why materialize at write time: app/rag/epistemics.py's `epistemic_status_counts_sql()`
    is a single-SQL rollup that must agree with `classify_claim(quality_cap=...)` on the
    per-claim detail path. Re-running a Python grader per claim inside that SQL is not
    possible, and re-deriving the cap in SQL would duplicate the rubric (two
    implementations of one rule = one future disagreement). So the grader runs ONCE, at
    stamp time, and the row carries its outputs: `quality_grade`, `quality_cap`,
    `quality_caveats`, `axis_findings`. The rollup then only needs a LEFT JOIN and a
    three-way substitution over values from the grader's own closed vocabulary.

    Fail-closed by construction: only keys in app/rag/evidence_metadata.DECLARED_KEYS are
    accepted; anything else comes back in `rejected_keys` with a reason and is NOT stored.
    Absent metadata stays absent — the grader's own docstring ("refusing to invent a
    grade") is the contract; this table never fills gaps with guesses.

    Owner anchoring mirrors migration 0056's pattern: the composite FK
    (claim_id, owner_id) -> knowledge_claims(id, owner_id) makes a cross-owner reference a
    foreign-key violation, not merely something RLS filters out afterwards. One row per
    claim (UNIQUE (claim_id, owner_id)); re-stamping UPDATES the row — the founder may
    correct metadata, and the materialized verdict must move with the correction, never
    leave a stale grade behind.
    """

    __tablename__ = "claim_evidence_metadata"
    __table_args__ = (
        ForeignKeyConstraint(
            ["claim_id", "owner_id"],
            ["knowledge_claims.id", "knowledge_claims.owner_id"],
            ondelete="CASCADE",
            name="fk_claim_evidence_metadata_claim_owner",
        ),
        UniqueConstraint("claim_id", "owner_id", name="uq_claim_evidence_metadata_claim_owner"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    claim_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    metadata_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    quality_grade: Mapped[str] = mapped_column(String(20), nullable=False)
    quality_cap: Mapped[str] = mapped_column(String(40), nullable=False)
    quality_caveats: Mapped[list] = mapped_column(JSONB, nullable=False)
    axis_findings: Mapped[list] = mapped_column(JSONB, nullable=False)
    rejected_keys: Mapped[dict] = mapped_column(JSONB, nullable=False)
    declared_via: Mapped[str] = mapped_column(String(60), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
