"""Durable independent verification registry.

Records here are evidence for readiness, not execution authority. They are append-only and bind
an examiner result to one exact candidate SHA.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, JSON, String, Text, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class MainAIVerificationRecord(Base):
    __tablename__ = "mainai_verification_records"

    id: Mapped[uuid.UUID] = mapped_column("verification_id", UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    component_id: Mapped[str] = mapped_column(String(128), index=True)
    candidate_id: Mapped[str] = mapped_column(String(160), index=True)
    candidate_sha: Mapped[str] = mapped_column(String(40), index=True)
    candidate_tree_identity: Mapped[str | None] = mapped_column(String(160), nullable=True)
    builder_identity: Mapped[str] = mapped_column(String(128), index=True)
    examiner_identity: Mapped[str] = mapped_column(String(128), index=True)
    examiner_class: Mapped[str] = mapped_column(String(64), default="external_agent")
    independence_relationship: Mapped[str] = mapped_column(String(64), default="different_agent")
    identity_assurance: Mapped[str] = mapped_column(String(32), default="asserted")
    review_type: Mapped[str] = mapped_column(String(64), default="independent_review")
    review_result: Mapped[str] = mapped_column(String(16), index=True)
    reviewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    verification_scope: Mapped[dict] = mapped_column(JSON, default=dict)
    evidence_summary: Mapped[str] = mapped_column(Text)
    test_evidence_refs: Mapped[list] = mapped_column(JSON, default=list)
    source_provenance: Mapped[dict] = mapped_column(JSON, default=dict)
    supersedes_verification_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    invalidates_verification_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
