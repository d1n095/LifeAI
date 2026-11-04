"""Durable Founder-only MainAI boot state.

Boot state records readiness, covenant identity and audit evidence. It is not an execution
authority source; runtime authority remains in the verified lower layers.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, ForeignKeyConstraint, Integer, JSON, String, Text, UUID, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class MainAIFounderCovenant(Base):
    __tablename__ = "mainai_founder_covenants"
    __table_args__ = (UniqueConstraint("id", "owner_id", name="uq_founder_covenants_id_owner"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    version: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), default="ACTIVE")
    covenant_hash: Mapped[str] = mapped_column(String(128))
    clauses: Mapped[list] = mapped_column(JSON, default=list)
    invariants: Mapped[list] = mapped_column(JSON, default=list)
    provenance: Mapped[dict] = mapped_column(JSON, default=dict)
    created_by: Mapped[str] = mapped_column(String(128), default="system")
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class MainAIFounderBoot(Base):
    __tablename__ = "mainai_founder_boots"
    __table_args__ = (
        UniqueConstraint("boot_id", "owner_id", name="uq_founder_boots_boot_owner"),
        ForeignKeyConstraint(["covenant_id", "owner_id"], ["mainai_founder_covenants.id", "mainai_founder_covenants.owner_id"]),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    boot_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), unique=True, default=uuid.uuid4)
    mainai_id: Mapped[str] = mapped_column(String(128))
    founder_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    system_instance_id: Mapped[str] = mapped_column(String(128))
    covenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    covenant_version: Mapped[str] = mapped_column(String(64))
    mode: Mapped[str] = mapped_column(String(32), default="FOUNDER_ONLY")
    status: Mapped[str] = mapped_column(String(32), default="BOOTING")
    recall_status: Mapped[str] = mapped_column(String(48), default="DISABLED_BY_SECURITY_GATE")
    status_reason: Mapped[str] = mapped_column(Text, default="")
    component_manifest: Mapped[dict] = mapped_column(JSON, default=dict)
    readiness_matrix: Mapped[dict] = mapped_column(JSON, default=dict)
    authority_profile: Mapped[dict] = mapped_column(JSON, default=dict)
    presence_state: Mapped[str] = mapped_column(String(32), default="BOOTING")
    active_program_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    audit_summary: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    stop_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class MainAIFounderBootEvent(Base):
    __tablename__ = "mainai_founder_boot_events"
    __table_args__ = (
        UniqueConstraint("boot_id", "sequence", name="uq_founder_boot_event_sequence"),
        ForeignKeyConstraint(["boot_id", "owner_id"], ["mainai_founder_boots.boot_id", "mainai_founder_boots.owner_id"], ondelete="CASCADE"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    boot_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(64))
    metadata_json: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class MainAIFounderBootStatus(Base):
    __tablename__ = "mainai_founder_boot_status"
    __table_args__ = (
        ForeignKeyConstraint(["boot_id", "owner_id"], ["mainai_founder_boots.boot_id", "mainai_founder_boots.owner_id"], ondelete="CASCADE"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    boot_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    presence_state: Mapped[str] = mapped_column(String(32))
    status_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
