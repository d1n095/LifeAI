"""Stage N — HOT / WARM / COLD memory tiers.

Tiering never rewrites truth or destroys provenance. Cold remains searchable.
Promotion/demotion based on actual retrieval/use.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from enum import Enum

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.founder_memory import list_founder_memory
from app.models.memory_tier import MemoryTierState


class MemoryTier(str, Enum):
    HOT = "hot"
    WARM = "warm"
    COLD = "cold"


class MemoryTierError(ValueError):
    pass


def _get_or_create(
    db: Session,
    *,
    owner_id: uuid.UUID,
    target_kind: str,
    target_id: uuid.UUID,
    default_tier: MemoryTier = MemoryTier.WARM,
) -> MemoryTierState:
    row = db.execute(
        select(MemoryTierState).where(
            MemoryTierState.owner_id == owner_id,
            MemoryTierState.target_kind == target_kind,
            MemoryTierState.target_id == target_id,
        )
    ).scalar_one_or_none()
    if row is not None:
        return row
    row = MemoryTierState(
        owner_id=owner_id,
        target_kind=target_kind,
        target_id=target_id,
        tier=default_tier.value,
        provenance={"stage": "N", "truth_unchanged": True},
    )
    db.add(row)
    db.flush()
    return row


def record_retrieval(
    db: Session,
    *,
    owner_id: uuid.UUID,
    target_kind: str,
    target_id: uuid.UUID,
) -> MemoryTierState:
    """Record actual use — may promote cold/warm → hot. Never mutates target content."""
    row = _get_or_create(db, owner_id=owner_id, target_kind=target_kind, target_id=target_id)
    row.retrieval_count = int(row.retrieval_count or 0) + 1
    row.last_retrieved_at = datetime.utcnow()
    if row.tier == MemoryTier.COLD.value and row.retrieval_count >= 1:
        row.tier = MemoryTier.WARM.value
    if row.retrieval_count >= 3:
        row.tier = MemoryTier.HOT.value
    row.updated_at = datetime.utcnow()
    db.flush()
    return row


def demote_stale(
    db: Session,
    *,
    owner_id: uuid.UUID,
    older_than_hours: float = 24 * 14,
    now: datetime | None = None,
) -> int:
    """Demote unused hot/warm items toward cold. Content/provenance of targets untouched."""
    now = now or datetime.utcnow()
    cutoff = now - timedelta(hours=older_than_hours)
    rows = list(
        db.execute(
            select(MemoryTierState).where(
                MemoryTierState.owner_id == owner_id,
                MemoryTierState.tier.in_((MemoryTier.HOT.value, MemoryTier.WARM.value)),
            )
        ).scalars().all()
    )
    changed = 0
    for row in rows:
        last = row.last_retrieved_at or row.created_at
        if last < cutoff:
            row.tier = MemoryTier.COLD.value if row.tier == MemoryTier.WARM.value else MemoryTier.WARM.value
            row.updated_at = now
            changed += 1
    db.flush()
    return changed


def list_by_tier(
    db: Session,
    *,
    owner_id: uuid.UUID,
    tier: MemoryTier | str,
) -> list[MemoryTierState]:
    tier_v = tier.value if isinstance(tier, MemoryTier) else tier
    return list(
        db.execute(
            select(MemoryTierState).where(
                MemoryTierState.owner_id == owner_id,
                MemoryTierState.tier == tier_v,
            )
        ).scalars().all()
    )


def search_including_cold(
    db: Session,
    *,
    owner_id: uuid.UUID,
    text: str,
) -> list[dict]:
    """Cold remains searchable — returns active notes regardless of tier."""
    needle = (text or "").lower()
    hits = []
    for note in list_founder_memory(db, owner_id=owner_id):
        if needle and needle not in (note.content or "").lower():
            continue
        state = _get_or_create(db, owner_id=owner_id, target_kind="founder_memory_note", target_id=note.id)
        hits.append(
            {
                "note_id": str(note.id),
                "content": note.content,
                "status": note.status,
                "tier": state.tier,
                "truth_preserved": True,
            }
        )
    return hits
