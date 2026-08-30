"""Stage Q — interruption recovery from durable state only."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.mainai_execution import MainAIGoal, MainAITask
from app.temporal_intelligence import RecapWindow, build_recap
from app.work_candidates import list_work_candidates


@dataclass
class InterruptionRecoveryBrief:
    what_we_were_doing: list[str] = field(default_factory=list)
    why: list[str] = field(default_factory=list)
    what_changed: list[str] = field(default_factory=list)
    what_remains: list[str] = field(default_factory=list)
    what_became_obsolete: list[str] = field(default_factory=list)
    best_next_action: str | None = None
    durable_only: bool = True
    gap: str = "unknown"


def recover_after_interruption(
    db: Session,
    *,
    owner_id: uuid.UUID,
    gap: str = "days",
) -> InterruptionRecoveryBrief:
    """Reconstruct context from durable tables only — no hidden session memory."""
    window = {
        "hours": RecapWindow.DAY,
        "days": RecapWindow.WEEK,
        "weeks": RecapWindow.MONTH,
        "months": RecapWindow.QUARTER,
    }.get(gap, RecapWindow.WEEK)
    recap = build_recap(db, owner_id=owner_id, window=window, include_project_wide=False, limit=100)

    goals = db.execute(select(MainAIGoal).where(MainAIGoal.owner_id == owner_id)).scalars().all()
    active_goals = [g for g in goals if getattr(g.status, "value", str(g.status)) not in {"completed", "cancelled"}]
    tasks = db.execute(select(MainAITask).where(MainAITask.owner_id == owner_id)).scalars().all()
    open_tasks = [t for t in tasks if getattr(t.status, "value", str(t.status)) not in {"completed", "cancelled", "failed"}]
    done_tasks = [t for t in tasks if getattr(t.status, "value", str(t.status)) == "completed"]
    candidates = list_work_candidates(db, owner_id=owner_id, status="unreviewed")
    superseded = list_work_candidates(db, owner_id=owner_id, status="superseded")

    brief = InterruptionRecoveryBrief(
        what_we_were_doing=[g.title for g in active_goals] + [(t.description or "")[:120] for t in open_tasks[:10]],
        why=[f"goal:{g.id}" for g in active_goals],
        what_changed=[f"{i.kind}:{i.title[:80]}" for i in recap.items[:20]],
        what_remains=[(c.title or "")[:120] for c in candidates[:10]] + [(t.description or "")[:120] for t in open_tasks[:5]],
        what_became_obsolete=[(c.title or "")[:120] for c in superseded[:10]] + [(t.description or "")[:80] for t in done_tasks[:5]],
        gap=gap,
        durable_only=True,
    )
    if open_tasks:
        brief.best_next_action = f"Resume task: {(open_tasks[0].description or '')[:160]}"
    elif candidates:
        brief.best_next_action = f"Review work candidate: {(candidates[0].title or '')[:160]}"
    elif active_goals:
        brief.best_next_action = f"Continue goal: {active_goals[0].title}"
    else:
        brief.best_next_action = "No active work — ask founder for next priority."
    return brief
