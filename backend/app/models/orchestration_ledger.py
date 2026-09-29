"""Orchestration truth ledger tables (migration 0089).

Occupancy and GitHub-backed software truth for coordinating external coding agents.
These rows are advisory project-manager state. They do not grant merge, deploy, Recall,
provider, or RLS authority.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class OrchestrationAgent(Base):
    __tablename__ = "orchestration_agents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    agent_key: Mapped[str] = mapped_column(String(64))
    display_name: Mapped[str] = mapped_column(String(128))
    occupancy_status: Mapped[str] = mapped_column(String(32), default="idle")
    supports_parallel_workers: Mapped[bool] = mapped_column(Boolean, default=False)
    max_slots: Mapped[int] = mapped_column(Integer, default=1)
    current_task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    provenance: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    __table_args__ = (UniqueConstraint("owner_id", "agent_key", name="uq_orchestration_agents_owner_key"),)


class OrchestrationSlot(Base):
    __tablename__ = "orchestration_slots"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("orchestration_agents.id", ondelete="CASCADE"))
    slot_key: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="free")
    task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    __table_args__ = (UniqueConstraint("owner_id", "agent_id", "slot_key", name="uq_orchestration_slots_owner_agent_key"),)


class OrchestrationTask(Base):
    __tablename__ = "orchestration_tasks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("orchestration_agents.id", ondelete="SET NULL"), nullable=True)
    assigned_slot_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("orchestration_slots.id", ondelete="SET NULL"), nullable=True)
    title: Mapped[str] = mapped_column(String(256))
    role: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), default="queued")
    exact_input_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    working_branch: Mapped[str | None] = mapped_column(String(255), nullable=True)
    output_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    remote_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    remote_pushed: Mapped[bool] = mapped_column(Boolean, default=False)
    frozen: Mapped[bool] = mapped_column(Boolean, default=False)
    protects_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    protects_branch: Mapped[str | None] = mapped_column(String(255), nullable=True)
    artifacts: Mapped[list] = mapped_column(JSON, default=list)
    test_runs: Mapped[list] = mapped_column(JSON, default=list)
    blockers: Mapped[list] = mapped_column(JSON, default=list)
    next_action: Mapped[dict] = mapped_column(JSON, default=dict)
    authority_required: Mapped[str] = mapped_column(String(64), default="none")
    last_verified_source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class OrchestrationTaskDependency(Base):
    __tablename__ = "orchestration_task_dependencies"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("orchestration_tasks.id", ondelete="CASCADE"))
    depends_on_task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("orchestration_tasks.id", ondelete="CASCADE"))
    dependency_kind: Mapped[str] = mapped_column(String(32), default="blocks")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("task_id", "depends_on_task_id", "dependency_kind", name="uq_orchestration_task_dependencies"),
    )


class OrchestrationClaim(Base):
    """Append-only agent text. Never authoritative for GitHub-backed fields."""

    __tablename__ = "orchestration_claims"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("orchestration_tasks.id", ondelete="CASCADE"), nullable=True)
    agent_key: Mapped[str] = mapped_column(String(64))
    claim_kind: Mapped[str] = mapped_column(String(64))
    claimed_value: Mapped[dict] = mapped_column(JSON, default=dict)
    raw_text: Mapped[str] = mapped_column(Text)
    bound_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    authoritative: Mapped[bool] = mapped_column(Boolean, default=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class OrchestrationGitHubSnapshot(Base):
    """Append-only GitHub observations. Software truth for remote/CI/PR/deploy questions."""

    __tablename__ = "orchestration_github_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("orchestration_tasks.id", ondelete="CASCADE"), nullable=True)
    branch: Mapped[str] = mapped_column(String(255))
    exists_remotely: Mapped[bool] = mapped_column(Boolean)
    commit_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    tree_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    local_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    local_matches_remote: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    ci_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    pr_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    default_branch: Mapped[str | None] = mapped_column(String(255), nullable=True)
    default_branch_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    deployments_payload: Mapped[list] = mapped_column(JSON, default=list)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    source: Mapped[str] = mapped_column(String(32), default="github")
