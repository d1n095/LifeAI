import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, ForeignKeyConstraint, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class ExecutionScopeProposal(Base):
    """A claim that a MainAIGoal MIGHT need a particular execution scope (paths/capabilities/
    risk) to be autonomously runnable via app.development_supervisor.service.run_supervisor()
    -- never a claim that this scope is authorized. See migration 0057's own module docstring
    for the full architecture: PROPOSED_SCOPE != AUTHORIZED_SCOPE, structurally, not just
    documented. The only path to a real ExecutionAuthorizationEnvelope is
    app.execution_envelopes.service.authorize_execution_scope()."""

    __tablename__ = "execution_scope_proposals"
    __table_args__ = (
        ForeignKeyConstraint(
            ["goal_id", "owner_id"], ["mainai_goals.id", "mainai_goals.owner_id"], ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["authorized_envelope_id", "owner_id"], ["execution_authorization_envelopes.id", "execution_authorization_envelopes.owner_id"], ondelete="SET NULL",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    goal_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    repository_identity: Mapped[str | None] = mapped_column(Text, nullable=True)
    proposed_paths: Mapped[list] = mapped_column(JSONB, default=list)
    proposed_capabilities: Mapped[list] = mapped_column(JSONB, default=list)
    proposed_risk: Mapped[str] = mapped_column(String(16), default="low")
    proposal_reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    proposal_strategy: Mapped[str] = mapped_column(String(64), default="unknown")
    provenance: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(24), default="unreviewed")
    authorized_envelope_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    rejected_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    observed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ExecutionAuthorizationEnvelope(Base):
    """Real, founder-granted execution authority for one MainAIGoal -- the ceiling every
    later, narrower task-level WorkBinding must fit inside (see migration 0057's own module
    docstring). Never created directly; the only path here is
    app.execution_envelopes.service.authorize_execution_scope(), which always requires the
    caller's own explicit authorized_by/authorized_paths/authorized_capabilities/
    authorized_risk -- the source proposal's own suggested values are never silently copied
    in. Superseded (never mutated) if the founder re-authorizes a goal's scope later."""

    __tablename__ = "execution_authorization_envelopes"
    __table_args__ = (
        ForeignKeyConstraint(
            ["goal_id", "owner_id"], ["mainai_goals.id", "mainai_goals.owner_id"], ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["supersedes_envelope_id", "owner_id"], ["execution_authorization_envelopes.id", "execution_authorization_envelopes.owner_id"],
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    goal_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    source_proposal_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    repository_identity: Mapped[str | None] = mapped_column(Text, nullable=True)
    authorized_paths: Mapped[list] = mapped_column(JSONB, default=list)
    authorized_capabilities: Mapped[list] = mapped_column(JSONB, default=list)
    authorized_risk: Mapped[str] = mapped_column(String(16))
    authorized_by: Mapped[str] = mapped_column(String(64))
    authorized_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    status: Mapped[str] = mapped_column(String(24), default="active")
    supersedes_envelope_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    provenance: Mapped[dict] = mapped_column(JSONB, default=dict)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
