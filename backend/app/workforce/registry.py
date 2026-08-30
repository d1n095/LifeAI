"""Owner-scoped Agent Registry (T1). Creating an agent grants ZERO authority."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.workforce import WorkforceAgentProfile
from app.workforce.types import AGENT_LIFECYCLE_STATUSES


class WorkforceRegistryError(Exception):
    pass


class AgentNotSelectableError(WorkforceRegistryError):
    pass


def _fingerprint(payload: dict) -> str:
    raw = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def register_workforce_agent(
    db: Session,
    *,
    owner_id: uuid.UUID,
    agent_key: str,
    name: str,
    role: str,
    agent_type: str,
    provider_type: str = "none",
    provider_model_id: str | None = None,
    coordination_agent_id: uuid.UUID | None = None,
    trust_zone: str = "UNTRUSTED_REMOTE",
    capability_tags: list[str] | tuple[str, ...] = (),
    allowed_tool_classes: list[str] | tuple[str, ...] = (),
    default_context_class: str = "task_local",
    risk_tier: str = "low",
    cost_class: str = "unknown",
    status: str = "candidate",
    provenance: dict | None = None,
) -> WorkforceAgentProfile:
    """Idempotent upsert by (owner_id, agent_key). New agents begin at lowest trust
    (`candidate` by default). Registration is NOT authorization."""

    if status not in AGENT_LIFECYCLE_STATUSES:
        raise WorkforceRegistryError(f"invalid lifecycle status: {status}")

    existing = db.execute(
        select(WorkforceAgentProfile).where(
            WorkforceAgentProfile.owner_id == owner_id,
            WorkforceAgentProfile.agent_key == agent_key,
        )
    ).scalar_one_or_none()

    config_body = {
        "role": role,
        "agent_type": agent_type,
        "provider_type": provider_type,
        "provider_model_id": provider_model_id,
        "trust_zone": trust_zone,
        "capability_tags": list(capability_tags),
        "allowed_tool_classes": list(allowed_tool_classes),
        "default_context_class": default_context_class,
        "risk_tier": risk_tier,
        "cost_class": cost_class,
    }
    values = {
        "name": name,
        **config_body,
        "coordination_agent_id": coordination_agent_id,
        "status": status,
        "configuration_fingerprint": _fingerprint(config_body),
        "provenance": dict(provenance or {}),
    }
    if existing is not None:
        for key, value in values.items():
            setattr(existing, key, value)
        if existing.configuration_fingerprint != values["configuration_fingerprint"]:
            existing.version = int(existing.version) + 1
        existing.updated_at = datetime.utcnow()
        db.flush()
        return existing

    row = WorkforceAgentProfile(owner_id=owner_id, agent_key=agent_key, version=1, **values)
    db.add(row)
    db.flush()
    return row


def get_workforce_agent(db: Session, *, owner_id: uuid.UUID, agent_id: uuid.UUID) -> WorkforceAgentProfile:
    row = db.get(WorkforceAgentProfile, agent_id)
    if row is None or row.owner_id != owner_id:
        raise WorkforceRegistryError(f"workforce agent {agent_id} not found for owner")
    return row


def get_workforce_agent_by_key(db: Session, *, owner_id: uuid.UUID, agent_key: str) -> WorkforceAgentProfile:
    row = db.execute(
        select(WorkforceAgentProfile).where(
            WorkforceAgentProfile.owner_id == owner_id,
            WorkforceAgentProfile.agent_key == agent_key,
        )
    ).scalar_one_or_none()
    if row is None:
        raise WorkforceRegistryError(f"workforce agent '{agent_key}' not found for owner")
    return row


def list_workforce_agents(
    db: Session,
    *,
    owner_id: uuid.UUID,
    include_retired: bool = False,
) -> list[WorkforceAgentProfile]:
    q = select(WorkforceAgentProfile).where(WorkforceAgentProfile.owner_id == owner_id)
    if not include_retired:
        q = q.where(WorkforceAgentProfile.status.notin_(("retired",)))
    return list(db.execute(q.order_by(WorkforceAgentProfile.created_at)).scalars())


def disable_workforce_agent(db: Session, *, owner_id: uuid.UUID, agent_id: uuid.UUID) -> WorkforceAgentProfile:
    row = get_workforce_agent(db, owner_id=owner_id, agent_id=agent_id)
    row.status = "disabled"
    row.updated_at = datetime.utcnow()
    db.flush()
    return row


def retire_workforce_agent(db: Session, *, owner_id: uuid.UUID, agent_id: uuid.UUID) -> WorkforceAgentProfile:
    row = get_workforce_agent(db, owner_id=owner_id, agent_id=agent_id)
    row.status = "retired"
    row.retired_at = datetime.utcnow()
    row.updated_at = datetime.utcnow()
    db.flush()
    return row


def assert_agent_selectable(profile: WorkforceAgentProfile) -> None:
    if profile.status in ("retired", "disabled", "need_detected"):
        raise AgentNotSelectableError(
            f"agent '{profile.agent_key}' status={profile.status} is not selectable"
        )
    if profile.retired_at is not None:
        raise AgentNotSelectableError(f"agent '{profile.agent_key}' is retired")
