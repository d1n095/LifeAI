"""Canonical durable Level-2 program contract and append-only journal."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, Text, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class MainAILevel2Program(Base):
    __tablename__ = "mainai_level2_programs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    objective: Mapped[str] = mapped_column(Text)
    scope: Mapped[list] = mapped_column(JSON, default=list)
    acceptance_criteria: Mapped[list] = mapped_column(JSON, default=list)
    verification_criteria: Mapped[list] = mapped_column(JSON, default=list)
    authority_boundary: Mapped[list] = mapped_column(JSON, default=list)
    dependency_graph: Mapped[dict] = mapped_column(JSON, default=dict)
    state: Mapped[str] = mapped_column(String(32), default="RUNNING")
    remaining_work: Mapped[list] = mapped_column(JSON, default=list)
    current_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    builder_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    examiner_required: Mapped[bool] = mapped_column(default=True)
    budget_state: Mapped[dict] = mapped_column(JSON, default=dict)
    stop_conditions: Mapped[list] = mapped_column(JSON, default=list)
    founder_only_decisions: Mapped[list] = mapped_column(JSON, default=list)
    handoff_state: Mapped[dict] = mapped_column(JSON, default=dict)
    currentness_token: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)


class MainAILevel2Event(Base):
    __tablename__ = "mainai_level2_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    program_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("mainai_level2_programs.id", ondelete="CASCADE"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(48))
    metadata_json: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
