from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, LargeBinary, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class PersonalRecallOwnerKey(Base):
    __tablename__ = "personal_recall_owner_keys"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    key_version: Mapped[int] = mapped_column(default=1)
    status: Mapped[str] = mapped_column(String(16), default="active")
    wrap_algorithm: Mapped[str] = mapped_column(String(48))
    system_kek_version: Mapped[str] = mapped_column(String(64))
    wrap_nonce: Mapped[bytes] = mapped_column(LargeBinary)
    wrapped_owner_key: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (UniqueConstraint("owner_id", "key_version", name="uq_recall_owner_key_version"),)


class PersonalRecallSource(Base):
    __tablename__ = "personal_recall_sources"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    source_identity: Mapped[str] = mapped_column(String(256), index=True)
    filename: Mapped[str] = mapped_column(String(512))
    logical_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    source_hash: Mapped[str] = mapped_column(String(64), index=True)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    media_type: Mapped[str] = mapped_column(String(96), default="text/plain")
    parser_version: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(16), default="active", index=True)
    disclosure_class: Mapped[str] = mapped_column(String(16), default="normal")
    encrypted_payload: Mapped[bytes] = mapped_column(LargeBinary)
    payload_algorithm: Mapped[str] = mapped_column(String(48))
    payload_nonce: Mapped[bytes] = mapped_column(LargeBinary)
    owner_key_version: Mapped[int] = mapped_column(default=1)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    supersedes_source_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (UniqueConstraint("owner_id", "source_hash", name="uq_recall_source_exact_duplicate"),)


class PersonalRecallChunk(Base):
    __tablename__ = "personal_recall_chunks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("personal_recall_sources.id", ondelete="CASCADE"), index=True)
    chunk_index: Mapped[int] = mapped_column(default=0)
    chunk_identity: Mapped[str] = mapped_column(String(160), index=True)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    encrypted_text: Mapped[bytes] = mapped_column(LargeBinary)
    payload_algorithm: Mapped[str] = mapped_column(String(48))
    payload_nonce: Mapped[bytes] = mapped_column(LargeBinary)
    owner_key_version: Mapped[int] = mapped_column(default=1)
    classification: Mapped[str] = mapped_column(String(32), default="UNKNOWN")
    state: Mapped[str] = mapped_column(String(16), default="active", index=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    __table_args__ = (UniqueConstraint("owner_id", "source_id", "chunk_index", name="uq_recall_chunk_order"),)


class PersonalRecallGrant(Base):
    __tablename__ = "personal_recall_grants"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    session_id: Mapped[str] = mapped_column(String(160), index=True)
    boot_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    purpose: Mapped[str] = mapped_column(String(128))
    scope: Mapped[dict] = mapped_column(JSON, default=dict)
    resource_classes: Mapped[list] = mapped_column(JSON, default=list)
    disclosure_level: Mapped[str] = mapped_column(String(16), default="metadata")
    can_retrieve: Mapped[bool] = mapped_column(Boolean, default=True)
    can_disclose: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[str] = mapped_column(String(64), default="founder")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PersonalRecallExtraction(Base):
    __tablename__ = "personal_recall_extractions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("personal_recall_sources.id", ondelete="CASCADE"), index=True)
    chunk_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("personal_recall_chunks.id", ondelete="CASCADE"), nullable=True)
    kind: Mapped[str] = mapped_column(String(32), default="UNKNOWN")
    text_hash: Mapped[str] = mapped_column(String(64))
    confidence: Mapped[float | None] = mapped_column(nullable=True)
    state: Mapped[str] = mapped_column(String(24), default="proposed")
    provenance: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
