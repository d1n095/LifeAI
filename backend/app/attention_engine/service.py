"""Stage M — attention / priority ranking (horizon ≠ authority)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.founder_memory import list_founder_memory
from app.long_horizon import HorizonBucket, classify_horizon
from app.models.mainai_execution import MainAIGoal, MainAITask
from app.models.work_candidate import WorkCandidate
from app.work_candidates import list_work_candidates


@dataclass
class AttentionItem:
    ref_kind: str
    ref_id: uuid.UUID
    title: str
    score: float
    factors: dict = field(default_factory=dict)
    horizon: str = HorizonBucket.NOW.value
    authority_implied: bool = False  # always False — ranking is not authority


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def rank_attention(
    db: Session,
    *,
    owner_id: uuid.UUID,
    founder_goal_text: str | None = None,
    now: datetime | None = None,
    limit: int = 50,
) -> list[AttentionItem]:
    """Rank competing memory/work items. Horizon informs schedule, never grants authority."""
    now = now or datetime.utcnow()
    items: list[AttentionItem] = []

    for note in list_founder_memory(db, owner_id=owner_id, status="active"):
        age_h = max(0.0, (now - (note.observed_at or note.created_at)).total_seconds() / 3600.0)
        recency = _clamp(1.0 - age_h / (24 * 30))
        horizon = classify_horizon(note.content or "")
        urgency = 0.9 if horizon == HorizonBucket.NOW else 0.6 if horizon == HorizonBucket.NEAR else 0.3 if horizon == HorizonBucket.MID else 0.1
        importance = 0.7 if note.note_type in {"decision", "goal"} else 0.4
        uncertainty = 0.5
        risk = 0.2
        benefit = 0.5
        cost = 0.3
        goal_align = 0.8 if founder_goal_text and founder_goal_text.lower()[:20] in (note.content or "").lower() else 0.4
        score = (
            0.22 * urgency
            + 0.18 * importance
            + 0.12 * risk
            + 0.12 * benefit
            + 0.10 * goal_align
            + 0.08 * uncertainty
            + 0.08 * (1.0 - cost)
            + 0.10 * recency
        )
        items.append(
            AttentionItem(
                ref_kind="founder_memory_note",
                ref_id=note.id,
                title=(note.content or "")[:160],
                score=score,
                factors={
                    "urgency": urgency,
                    "importance": importance,
                    "dependency_criticality": 0.0,
                    "risk": risk,
                    "expected_benefit": benefit,
                    "founder_goal": goal_align,
                    "uncertainty": uncertainty,
                    "cost": cost,
                    "recency": recency,
                },
                horizon=horizon.value,
            )
        )

    for cand in list_work_candidates(db, owner_id=owner_id, status="unreviewed"):
        priority = {"high": 0.9, "medium": 0.6, "low": 0.3}.get(cand.priority or "medium", 0.5)
        score = 0.35 * priority + 0.25 * 0.7 + 0.2 * 0.5 + 0.2 * 0.6
        items.append(
            AttentionItem(
                ref_kind="work_candidate",
                ref_id=cand.id,
                title=cand.title or "",
                score=score,
                factors={"urgency": priority, "importance": 0.7, "dependency_criticality": 0.5},
                horizon=HorizonBucket.NEAR.value if cand.provenance.get("timing") == "later" else HorizonBucket.NOW.value,
            )
        )

    tasks = db.execute(select(MainAITask).where(MainAITask.owner_id == owner_id)).scalars().all()
    for task in tasks:
        status = getattr(task.status, "value", str(task.status))
        if status in {"completed", "cancelled", "failed"}:
            continue
        dep_crit = 0.8 if status in {"blocked", "ready"} else 0.4
        score = 0.3 * 0.8 + 0.25 * dep_crit + 0.2 * 0.6 + 0.25 * 0.7
        items.append(
            AttentionItem(
                ref_kind="mainai_task",
                ref_id=task.id,
                title=(task.description or "")[:160],
                score=score,
                factors={"dependency_criticality": dep_crit, "urgency": 0.8 if status == "ready" else 0.5},
                horizon=HorizonBucket.NOW.value,
            )
        )

    items.sort(key=lambda i: i.score, reverse=True)
    return items[:limit]
