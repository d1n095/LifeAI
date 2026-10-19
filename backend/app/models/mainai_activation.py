"""Durable authority records for a Founder-supervised activation cycle."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, ForeignKeyConstraint, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class ActivationPreflightVerdict(str, enum.Enum):
    go = "go"
    no_go = "no_go"


class PushGrantStatus(str, enum.Enum):
    active = "active"
    consumed = "consumed"
    expired = "expired"
    cancelled = "cancelled"
    superseded = "superseded"


class MainAIActivationPreflightReceipt(Base):
    __tablename__ = "mainai_activation_preflight_receipts"
    __table_args__ = (
        UniqueConstraint("receipt_digest", name="uq_mainai_activation_preflight_digest"),
        UniqueConstraint("id", "owner_id", name="uq_mainai_activation_preflight_id_owner"),
        ForeignKeyConstraint(
            ["execution_envelope_id", "owner_id"],
            ["execution_authorization_envelopes.id", "execution_authorization_envelopes.owner_id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["provider_spend_authorization_id", "owner_id"],
            ["provider_spend_authorizations.id", "provider_spend_authorizations.owner_id"],
            ondelete="CASCADE",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    goal_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("mainai_goals.id", ondelete="CASCADE"))
    execution_envelope_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    provider_spend_authorization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    repository_identity: Mapped[str] = mapped_column(Text)
    integration_branch: Mapped[str] = mapped_column(Text)
    activation_sha: Mapped[str] = mapped_column(String(40))
    base_branch: Mapped[str] = mapped_column(Text)
    base_sha: Mapped[str] = mapped_column(String(40))
    authority_version: Mapped[int] = mapped_column(Integer)
    required_check_contexts: Mapped[list] = mapped_column(JSONB)
    runtime_config_digest: Mapped[str] = mapped_column(String(64))
    runtime_token_identity: Mapped[str] = mapped_column(Text)
    runtime_token_attestation_digest: Mapped[str] = mapped_column(String(64))
    ruleset_attestation_digest: Mapped[str] = mapped_column(String(64))
    founder_attestation_digest: Mapped[str] = mapped_column(String(64))
    facts: Mapped[dict] = mapped_column(JSONB)
    unknown_required_facts: Mapped[list] = mapped_column(JSONB)
    verdict: Mapped[str] = mapped_column(String(16))
    observed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    receipt_digest: Mapped[str] = mapped_column(String(64))
    provenance: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class MainAIPushGrant(Base):
    __tablename__ = "mainai_push_grants"
    __table_args__ = (
        UniqueConstraint("owner_id", "idempotency_key", name="uq_mainai_push_grants_owner_idem"),
        ForeignKeyConstraint(
            ["execution_envelope_id", "owner_id"],
            ["execution_authorization_envelopes.id", "execution_authorization_envelopes.owner_id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["preflight_receipt_id", "owner_id"],
            ["mainai_activation_preflight_receipts.id", "mainai_activation_preflight_receipts.owner_id"],
            ondelete="CASCADE",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    goal_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("mainai_goals.id", ondelete="CASCADE"))
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("mainai_tasks.id", ondelete="CASCADE"))
    job_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("mainai_jobs.id", ondelete="CASCADE"))
    worktree_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("mainai_task_worktrees.id", ondelete="CASCADE"))
    execution_envelope_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    preflight_receipt_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    repository_identity: Mapped[str] = mapped_column(Text)
    branch: Mapped[str] = mapped_column(Text)
    base_branch: Mapped[str] = mapped_column(Text)
    base_sha: Mapped[str] = mapped_column(String(40))
    local_commit_sha: Mapped[str] = mapped_column(String(40))
    authority_version: Mapped[int] = mapped_column(Integer)
    authorized_by: Mapped[str] = mapped_column(String(64))
    authorized_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(24), default=PushGrantStatus.active.value)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    consumed_by_job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    provenance: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
