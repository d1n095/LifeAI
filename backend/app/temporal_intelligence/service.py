"""Stage D — Temporal historical intelligence service.

Evidence-backed recap from durable tables only. Never invents narrative from model context.
"""

from __future__ import annotations

import re
import uuid
from collections import Counter
from datetime import datetime

from sqlalchemy.orm import Session

from app.temporal_intelligence.sources import (
    collect_engineering_lessons,
    collect_founder_memory,
    collect_goals_plans_tasks,
    collect_memory_thread_events,
    collect_project_entities,
    collect_project_memory_refs,
    collect_recovery,
    collect_work_candidates,
)
from app.temporal_intelligence.types import RecapEvidenceItem, RecapReport, RecapWindow
from app.temporal_intelligence.windows import TemporalWindowError, resolve_window

_NORMALIZE_RE = re.compile(r"[^a-z0-9åäö]+", re.IGNORECASE)


class TemporalIntelligenceError(ValueError):
    pass


def _norm_title(title: str) -> str:
    return _NORMALIZE_RE.sub(" ", (title or "").lower()).strip()


def build_recap(
    db: Session,
    *,
    owner_id: uuid.UUID,
    window: RecapWindow | str = RecapWindow.DAY,
    start: datetime | None = None,
    end: datetime | None = None,
    now: datetime | None = None,
    kinds: set[str] | None = None,
    limit: int = 500,
    include_project_wide: bool = True,
) -> RecapReport:
    """Assemble a durable evidence recap for the founder.

    Answers shapes like "vad gjorde vi idag?" by querying stored rows — never by asking a model
    to recall chat context.
    """
    try:
        rng = resolve_window(window, now=now, start=start, end=end)
    except TemporalWindowError as exc:
        raise TemporalIntelligenceError(str(exc)) from exc

    items: list[RecapEvidenceItem] = []
    items.extend(collect_founder_memory(db, owner_id=owner_id, rng=rng))
    items.extend(collect_goals_plans_tasks(db, owner_id=owner_id, rng=rng))
    items.extend(collect_work_candidates(db, owner_id=owner_id, rng=rng))
    items.extend(collect_project_entities(db, owner_id=owner_id, rng=rng))
    items.extend(collect_recovery(db, owner_id=owner_id, rng=rng))
    items.extend(collect_memory_thread_events(db, owner_id=owner_id, rng=rng))
    if include_project_wide:
        items.extend(collect_engineering_lessons(db, rng=rng))
        items.extend(collect_project_memory_refs(db, rng=rng))

    if kinds is not None:
        items = [i for i in items if i.kind in kinds]

    items.sort(key=lambda i: i.occurred_at, reverse=True)
    if limit and len(items) > limit:
        items = items[:limit]

    counts = Counter(i.kind for i in items)
    title_counts = Counter(_norm_title(i.title) for i in items if _norm_title(i.title))
    repeated = [
        {"normalized_title": title, "count": count}
        for title, count in title_counts.most_common()
        if count >= 2 and title
    ]

    return RecapReport(
        range=rng,
        owner_id=owner_id,
        items=items,
        counts_by_kind=dict(counts),
        repeated_titles=repeated,
        evidence_only=True,
    )


def answer_founder_recap_question(
    db: Session,
    *,
    owner_id: uuid.UUID,
    question: str,
    now: datetime | None = None,
) -> RecapReport:
    """Map common Swedish/English recap questions to a window, then build evidence."""
    q = (question or "").strip().lower()
    if any(tok in q for tok in ("idag", "today", "i dag")):
        window: RecapWindow | str = RecapWindow.DAY
    elif any(tok in q for tok in ("veckan", "week", "senaste veckan")):
        window = RecapWindow.WEEK
    elif any(tok in q for tok in ("månaden", "manaden", "month", "förra månaden", "forra manaden")):
        window = RecapWindow.MONTH
    elif any(tok in q for tok in ("kvartal", "quarter")):
        window = RecapWindow.QUARTER
    elif any(tok in q for tok in ("året", "aret", "year", "i år", "i ar")):
        window = RecapWindow.YEAR
    elif any(tok in q for tok in ("timmen", "hour", "senaste timmen")):
        window = RecapWindow.HOUR
    elif any(tok in q for tok in ("hela projektet", "entire project", "all time", "någonsin")):
        window = RecapWindow.ENTIRE_PROJECT
    else:
        window = RecapWindow.DAY
    return build_recap(db, owner_id=owner_id, window=window, now=now)
