"""Durable Founder Communication Ledger. See
docs/mainai_v2/MAINAI_COGNITIVE_OPS_RECONCILIATION.md for the architecture decision.

Backs migration 0074's `mainai_ops_founder_communications` table -- append-only (enforced by
the reused `intelligence_governance_deny_mutation()` trigger from migration 0038, which denies
ANY update/delete on this table, full stop). Recording a decision-received update means writing
a NEW row whose OWN `supersedes_id` points back at the prior one -- the prior row is never
touched. This mirrors the exact fix already applied in
`app.mainai_research.knowledge_ingestion` (a self-referential UPDATE of the OLD row was wrong
there for the same reason it would be wrong here: the trigger blocks it, and semantically
supersession belongs on the new record, not a mutation of history).

Does NOT store hidden chain-of-thought -- only topic, previously-communicated status, material
facts, decision requested/received, and timestamps, per the founder's own explicit instruction."""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.mainai_cognitive_ops.types import CognitiveOpsError

__all__ = ["record_communication", "list_communications_for_topic", "latest_communication_for_topic"]


def record_communication(
    db: Session,
    *,
    owner_id: uuid.UUID,
    topic: str,
    status_communicated: str,
    idempotency_key: str,
    material_facts: dict[str, Any] | None = None,
    decision_requested: str | None = None,
    decision_received: str | None = None,
    supersedes_id: uuid.UUID | None = None,
) -> dict:
    if not topic.strip():
        raise CognitiveOpsError("topic must not be empty")
    if not status_communicated.strip():
        raise CognitiveOpsError("status_communicated must not be empty")

    existing = db.execute(
        text("SELECT * FROM mainai_ops_founder_communications WHERE owner_id = :owner_id AND idempotency_key = :key"),
        {"owner_id": owner_id, "key": idempotency_key},
    ).mappings().first()
    if existing:
        return dict(existing)

    row = db.execute(
        text(
            """INSERT INTO mainai_ops_founder_communications
            (owner_id, topic, status_communicated, material_facts, decision_requested, decision_received, supersedes_id, idempotency_key)
            VALUES (:owner_id, :topic, :status, CAST(:facts AS jsonb), :decision_requested, :decision_received, :supersedes_id, :key)
            RETURNING *"""
        ),
        {
            "owner_id": owner_id, "topic": topic, "status": status_communicated,
            "facts": json.dumps(material_facts or {}), "decision_requested": decision_requested,
            "decision_received": decision_received, "supersedes_id": supersedes_id, "key": idempotency_key,
        },
    ).mappings().first()

    return dict(row)


def list_communications_for_topic(db: Session, *, owner_id: uuid.UUID, topic: str) -> list[dict]:
    rows = db.execute(
        text("SELECT * FROM mainai_ops_founder_communications WHERE owner_id = :owner_id AND topic = :topic ORDER BY communicated_at ASC"),
        {"owner_id": owner_id, "topic": topic},
    ).mappings().all()
    return [dict(r) for r in rows]


def latest_communication_for_topic(db: Session, *, owner_id: uuid.UUID, topic: str) -> dict | None:
    row = db.execute(
        text("SELECT * FROM mainai_ops_founder_communications WHERE owner_id = :owner_id AND topic = :topic ORDER BY communicated_at DESC LIMIT 1"),
        {"owner_id": owner_id, "topic": topic},
    ).mappings().first()
    return dict(row) if row else None
