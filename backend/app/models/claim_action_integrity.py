"""Append-only claim/action integrity evidence and receipt ledgers.

These rows describe facts already observed by trusted control-plane code.  They never grant
permission, dispatch work, merge, deploy, or activate anything.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, JSON, String, Text, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class ClaimState(str, enum.Enum):
    intended = "intended"
    requested = "requested"
    dispatched = "dispatched"
    running = "running"
    completed = "completed"
    externally_observed = "externally_observed"
    independently_verified = "independently_verified"
    certified = "certified"
    merged = "merged"
    deployed = "deployed"
    activated = "activated"
    failed = "failed"
    unknown = "unknown"


class ActionState(str, enum.Enum):
    intended = "intended"
    requested = "requested"
    dispatched = "dispatched"
    running = "running"
    completed = "completed"
    failed = "failed"
    unknown = "unknown"


class VerificationState(str, enum.Enum):
    unverified = "unverified"
    self_reported = "self_reported"
    externally_observed = "externally_observed"
    independently_verified = "independently_verified"
    certified = "certified"
    stale = "stale"
    conflicting = "conflicting"
    invalid = "invalid"


class EvidenceSourceType(str, enum.Enum):
    github = "github"
    database = "database"
    filesystem = "filesystem"
    ci = "ci"
    test_runner = "test_runner"
    deployment_provider = "deployment_provider"
    task_execution_ledger = "task_execution_ledger"
    verification_registry = "verification_registry"
    external_service = "external_service"
    agent_self_report = "agent_self_report"
    mainai_generated_text = "mainai_generated_text"
    user_attestation = "user_attestation"
    unknown = "unknown"


class ClaimActionEvidence(Base):
    __tablename__ = "claim_action_evidence"

    id: Mapped[uuid.UUID] = mapped_column("evidence_id", UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    execution_id: Mapped[str] = mapped_column(String(160), index=True)
    subject_key: Mapped[str] = mapped_column(String(200), index=True)
    action_key: Mapped[str] = mapped_column(String(80), index=True)
    source_type: Mapped[str] = mapped_column(String(40), index=True)
    source_ref: Mapped[str] = mapped_column(String(500))
    artifact_sha: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    authoritative: Mapped[bool] = mapped_column(Boolean, default=False)
    payload: Mapped[dict] = mapped_column(JSON)
    payload_digest: Mapped[str] = mapped_column(String(64), unique=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    recorded_by: Mapped[str] = mapped_column(String(160))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class ClaimActionReceipt(Base):
    __tablename__ = "claim_action_receipts"

    id: Mapped[uuid.UUID] = mapped_column("receipt_id", UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    execution_id: Mapped[str] = mapped_column(String(160), index=True)
    subject_key: Mapped[str] = mapped_column(String(200), index=True)
    action_key: Mapped[str] = mapped_column(String(80), index=True)
    declared_action: Mapped[dict] = mapped_column(JSON)
    permitted_action: Mapped[dict] = mapped_column(JSON)
    executed_action: Mapped[dict] = mapped_column(JSON)
    observed_result: Mapped[dict] = mapped_column(JSON)
    declared_state: Mapped[str] = mapped_column(String(32))
    effective_state: Mapped[str] = mapped_column(String(32), index=True)
    action_state: Mapped[str] = mapped_column(String(24), index=True)
    verification_state: Mapped[str] = mapped_column(String(32), index=True)
    evidence_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("claim_action_evidence.evidence_id", ondelete="RESTRICT"), nullable=True, index=True
    )
    predecessor_receipt_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("claim_action_receipts.receipt_id", ondelete="RESTRICT"), nullable=True
    )
    authority_snapshot: Mapped[dict] = mapped_column(JSON)
    explanation: Mapped[str] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(160))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow, index=True)
