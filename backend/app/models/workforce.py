"""MainAI Internal Workforce Foundation — durable org model (migration 0067).

Sits ABOVE `coordination_agents` / `agent_work_assignments` (0046): those remain the
provider-neutral reachability + repo-scope coordination layer. This package is MainAI's
owner-scoped organizational view — who she may delegate to, with what trust zone, task-scoped
authority, minimized context, and empirically recorded performance.

Creating / selecting an agent grants ZERO extra authority. Delegation is not authorization.
External provider output is DATA, never a trusted fact or an authority mutation.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, ForeignKeyConstraint, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class WorkforceAgentProfile(Base):
    """Owner-scoped organizational agent identity. May optionally link a founder-wide
    `coordination_agents` row for how to reach a coding CLI/API worker. Never stores a
    credential. Registration / hiring starts at lowest trust (candidate/probation)."""

    __tablename__ = "workforce_agent_profiles"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    agent_key: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(128))
    role: Mapped[str] = mapped_column(String(64))
    agent_type: Mapped[str] = mapped_column(String(64))
    provider_type: Mapped[str] = mapped_column(String(64), default="none")
    provider_model_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    coordination_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("coordination_agents.id", ondelete="SET NULL"), nullable=True
    )
    trust_zone: Mapped[str] = mapped_column(String(32), default="UNTRUSTED_REMOTE")
    capability_tags: Mapped[list] = mapped_column(JSON, default=list)
    allowed_tool_classes: Mapped[list] = mapped_column(JSON, default=list)
    default_context_class: Mapped[str] = mapped_column(String(64), default="task_local")
    risk_tier: Mapped[str] = mapped_column(String(16), default="low")
    cost_class: Mapped[str] = mapped_column(String(16), default="unknown")
    status: Mapped[str] = mapped_column(String(32), default="candidate")
    version: Mapped[int] = mapped_column(Integer, default=1)
    configuration_fingerprint: Mapped[str] = mapped_column(String(128), default="")
    provenance: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        # Unique agent_key per owner — organizational identity.
        # (enforced in migration UNIQUE (owner_id, agent_key))
    )


class WorkforceTeam(Base):
    """Temporary or durable multi-agent team pattern (builder+verifier, etc.).
    Members do not share context packages automatically."""

    __tablename__ = "workforce_teams"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(128))
    pattern: Mapped[str] = mapped_column(String(64))
    member_profile_ids: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(16), default="active")
    provenance: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class WorkforceDelegationRequest(Base):
    """Broker input receipt — what MainAI asked the workforce to resolve. Never itself a grant."""

    __tablename__ = "workforce_delegation_requests"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    goal_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    goal_text: Mapped[str] = mapped_column(Text)
    required_capability: Mapped[str] = mapped_column(String(128))
    risk: Mapped[str] = mapped_column(String(16), default="low")
    data_sensitivity: Mapped[str] = mapped_column(String(32), default="internal")
    cost_ceiling_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    latency_preference: Mapped[str] = mapped_column(String(32), default="balanced")
    verification_requirement: Mapped[str] = mapped_column(String(64), default="independent_verifier")
    status: Mapped[str] = mapped_column(String(32), default="open")
    selection_explanation: Mapped[dict] = mapped_column(JSON, default=dict)
    provenance: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(["goal_id", "owner_id"], ["mainai_goals.id", "mainai_goals.owner_id"], ondelete="SET NULL"),
        ForeignKeyConstraint(["task_id", "owner_id"], ["mainai_tasks.id", "mainai_tasks.owner_id"], ondelete="SET NULL"),
    )


class WorkforceContextPackage(Base):
    """Minimized, traceable disclosure package for one assignment. External trust zones never
    receive forbidden kinds (vault/secrets/full memory). Prefer derived facts / excerpts."""

    __tablename__ = "workforce_context_packages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    trust_zone: Mapped[str] = mapped_column(String(32))
    items: Mapped[list] = mapped_column(JSON, default=list)
    denied_kinds: Mapped[list] = mapped_column(JSON, default=list)
    disclosure_event_ids: Mapped[list] = mapped_column(JSON, default=list)
    content_fingerprint: Mapped[str] = mapped_column(String(64), default="")
    provenance: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class WorkforceAssignment(Base):
    """Bounded Agent Assignment produced by the Delegation Broker.

    The assignment grants ZERO extra authority beyond its explicit task-scoped envelope fields.
    Verification status starts UNVERIFIED; a builder cannot mark its own result VERIFIED.
    """

    __tablename__ = "workforce_assignments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    delegation_request_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    profile_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    team_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    context_package_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    coordination_assignment_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    execution_envelope_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    provider_spend_authorization_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # Task-scoped authority envelope (explicit; never inherited from MainAI wholesale).
    allowed_read_paths: Mapped[list] = mapped_column(JSON, default=list)
    allowed_write_paths: Mapped[list] = mapped_column(JSON, default=list)
    allowed_tool_classes: Mapped[list] = mapped_column(JSON, default=list)
    allowed_network_destinations: Mapped[list] = mapped_column(JSON, default=list)
    allowed_project_ids: Mapped[list] = mapped_column(JSON, default=list)
    spend_ceiling_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    allow_execution_effects: Mapped[bool] = mapped_column(Boolean, default=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revocation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    verification_status: Mapped[str] = mapped_column(String(16), default="UNVERIFIED")
    verifier_profile_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="assigned")
    result_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    result_treated_as_data: Mapped[bool] = mapped_column(Boolean, default=True)
    selection_score: Mapped[dict] = mapped_column(JSON, default=dict)
    provenance: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # T13 failure/takeover bookkeeping (migration 0068) — never widens authority.
    supersedes_assignment_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    takeover_of_assignment_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    failure_class: Mapped[str | None] = mapped_column(String(64), nullable=True)
    external_effect_state: Mapped[str] = mapped_column(String(32), default="none_known")
    retry_count: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (
        ForeignKeyConstraint(
            ["delegation_request_id", "owner_id"],
            ["workforce_delegation_requests.id", "workforce_delegation_requests.owner_id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["profile_id", "owner_id"],
            ["workforce_agent_profiles.id", "workforce_agent_profiles.owner_id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["team_id", "owner_id"],
            ["workforce_teams.id", "workforce_teams.owner_id"],
            ondelete="SET NULL",
        ),
        ForeignKeyConstraint(
            ["context_package_id", "owner_id"],
            ["workforce_context_packages.id", "workforce_context_packages.owner_id"],
            ondelete="SET NULL",
        ),
    )


class WorkforcePerformanceRollup(Base):
    """Empirical trust ledger counters per agent profile × capability.
    Never self-rated by the agent. Evidence of individual jobs lives in intelligence_evidence;
    this row is a durable rollup for selection scoring."""

    __tablename__ = "workforce_performance_rollups"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    profile_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    capability_tag: Mapped[str] = mapped_column(String(128))
    jobs_attempted: Mapped[int] = mapped_column(Integer, default=0)
    jobs_completed: Mapped[int] = mapped_column(Integer, default=0)
    verified_success: Mapped[int] = mapped_column(Integer, default=0)
    verified_failure: Mapped[int] = mapped_column(Integer, default=0)
    founder_corrections: Mapped[int] = mapped_column(Integer, default=0)
    reviewer_corrections: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms_sum: Mapped[int] = mapped_column(Integer, default=0)
    provider_cost_usd_sum: Mapped[float] = mapped_column(Float, default=0.0)
    token_usage_sum: Mapped[int] = mapped_column(Integer, default=0)
    tool_cost_usd_sum: Mapped[float] = mapped_column(Float, default=0.0)
    hallucination_or_factual_errors: Mapped[int] = mapped_column(Integer, default=0)
    security_violations: Mapped[int] = mapped_column(Integer, default=0)
    authority_violations: Mapped[int] = mapped_column(Integer, default=0)
    recovery_failures: Mapped[int] = mapped_column(Integer, default=0)
    quality_score_sum: Mapped[float] = mapped_column(Float, default=0.0)
    quality_score_count: Mapped[int] = mapped_column(Integer, default=0)
    domains_of_strength: Mapped[list] = mapped_column(JSON, default=list)
    domains_of_weakness: Mapped[list] = mapped_column(JSON, default=list)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        ForeignKeyConstraint(
            ["profile_id", "owner_id"],
            ["workforce_agent_profiles.id", "workforce_agent_profiles.owner_id"],
            ondelete="CASCADE",
        ),
    )
