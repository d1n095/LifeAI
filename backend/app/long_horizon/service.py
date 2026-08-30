"""Stage I — long-horizon planning foundation (FUTURE PLAN != FUTURE AUTHORITY)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from sqlalchemy.orm import Session

from app.inspectable_memory import founder_add_memory_note
from app.memory_work_linkage import TimingClass, apply_memory_work_linkage


class HorizonBucket(str, Enum):
    NOW = "now"
    NEAR = "near"
    MID = "mid"
    LONG = "long"


@dataclass
class HorizonItem:
    bucket: HorizonBucket
    title: str
    memory_note_id: uuid.UUID | None = None
    dependencies: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    authority_granted: bool = False  # always false at planning time unless explicit
    needs_reevaluation: bool = True
    provenance: dict = field(default_factory=dict)


@dataclass
class HorizonPlan:
    owner_id: uuid.UUID
    created_at: datetime
    items: list[HorizonItem] = field(default_factory=list)

    def by_bucket(self) -> dict[str, list[HorizonItem]]:
        out: dict[str, list[HorizonItem]] = {b.value: [] for b in HorizonBucket}
        for item in self.items:
            out[item.bucket.value].append(item)
        return out


def classify_horizon(text: str, *, explicit: HorizonBucket | None = None) -> HorizonBucket:
    if explicit is not None:
        return explicit
    t = (text or "").lower()
    if any(tok in t for tok in ("someday", "någon gång", "år", "year", "långsikt", "long-term", "long term")):
        return HorizonBucket.LONG
    if any(tok in t for tok in ("kvartal", "quarter", "månad", "month", "mid")):
        return HorizonBucket.MID
    if any(tok in t for tok in ("snart", "nästa vecka", "near", "soon", "vecka", "week")):
        return HorizonBucket.NEAR
    return HorizonBucket.NOW


def add_horizon_item(
    db: Session,
    *,
    owner_id: uuid.UUID,
    title: str,
    idempotency_key: str,
    bucket: HorizonBucket | None = None,
    dependencies: list[str] | None = None,
    blockers: list[str] | None = None,
) -> HorizonItem:
    """Record a horizon plan item as durable founder memory + optional LATER park.

    FUTURE PLAN != FUTURE AUTHORITY: never authorizes work. LATER/LONG/MID park as
    non-executable candidates via Stage C linkage.
    """
    resolved = classify_horizon(title, explicit=bucket)
    note, _ = founder_add_memory_note(
        db,
        owner_id=owner_id,
        content=title,
        note_type="goal",
        idempotency_key=idempotency_key,
        provenance={
            "stage": "I",
            "horizon": resolved.value,
            "authority_granted": False,
            "needs_reevaluation": True,
            "dependencies": dependencies or [],
            "blockers": blockers or [],
        },
    )
    timing = TimingClass.NOW if resolved == HorizonBucket.NOW else TimingClass.LATER
    apply_memory_work_linkage(db, owner_id=owner_id, note_id=note.id, timing=timing, park_candidate=True)
    return HorizonItem(
        bucket=resolved,
        title=title,
        memory_note_id=note.id,
        dependencies=list(dependencies or []),
        blockers=list(blockers or []),
        authority_granted=False,
        needs_reevaluation=True,
        provenance={"timing": timing.value},
    )


def build_horizon_plan(db: Session, *, owner_id: uuid.UUID, items: list[dict]) -> HorizonPlan:
    """items: [{title, idempotency_key, bucket?, dependencies?, blockers?}, ...]"""
    plan = HorizonPlan(owner_id=owner_id, created_at=datetime.utcnow())
    for raw in items:
        plan.items.append(
            add_horizon_item(
                db,
                owner_id=owner_id,
                title=raw["title"],
                idempotency_key=raw["idempotency_key"],
                bucket=HorizonBucket(raw["bucket"]) if raw.get("bucket") else None,
                dependencies=raw.get("dependencies"),
                blockers=raw.get("blockers"),
            )
        )
    return plan


def reevaluate_horizon_item(item: HorizonItem, *, reality_changed: bool) -> HorizonItem:
    """Plans must re-evaluate as reality changes — never freeze into authority."""
    if reality_changed:
        item.needs_reevaluation = True
        item.authority_granted = False
        item.provenance = {**item.provenance, "reevaluated_at": datetime.utcnow().isoformat()}
    return item
