"""Why-graph / decision-debt — founder-inspectable linkage without CoT.

Reuses memory_threads + work_candidates + truth claims. No new tables.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.founder_memory import FounderMemoryNote
from app.models.mainai_execution import MainAITask, MainAITaskEvent, MainAITaskEventType
from app.models.work_candidate import WorkCandidate


def why_feature_exists(
    db: Session,
    *,
    owner_id: uuid.UUID,
    work_candidate_id: uuid.UUID | None = None,
    note_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Evidence/provenance chain for a candidate or memory note — not narration."""
    chain: list[dict[str, Any]] = []
    if note_id is not None:
        note = db.execute(
            select(FounderMemoryNote).where(
                FounderMemoryNote.id == note_id, FounderMemoryNote.owner_id == owner_id
            )
        ).scalar_one_or_none()
        if note is not None:
            chain.append(
                {
                    "kind": "founder_memory_note",
                    "id": str(note.id),
                    "note_type": note.note_type,
                    "status": note.status,
                    "authority": note.authority,
                    "supersedes": str(note.supersedes_note_id) if note.supersedes_note_id else None,
                    "content_preview": (note.content or "")[:160],
                }
            )
    if work_candidate_id is not None:
        wc = db.execute(
            select(WorkCandidate).where(
                WorkCandidate.id == work_candidate_id, WorkCandidate.owner_id == owner_id
            )
        ).scalar_one_or_none()
        if wc is not None:
            chain.append(
                {
                    "kind": "work_candidate",
                    "id": str(wc.id),
                    "title": wc.title,
                    "status": wc.status,
                    "priority": wc.priority,
                    "provenance": wc.provenance or {},
                    "authorized": False,
                }
            )
    return {
        "chain": chain,
        "implemented": False,  # candidate alone is never implemented
        "verified": False,
        "chain_of_thought_exposed": False,
        "evidence_only": True,
    }


def list_decision_debt(
    db: Session,
    *,
    owner_id: uuid.UUID,
    limit: int = 20,
) -> dict[str, Any]:
    """Bounded queue of temporary/outdated/unverified decision notes — no spam."""
    notes = list(
        db.execute(
            select(FounderMemoryNote)
            .where(
                FounderMemoryNote.owner_id == owner_id,
                FounderMemoryNote.note_type.in_(("decision", "correction")),
                FounderMemoryNote.status.in_(("active", "disputed")),
            )
            .order_by(FounderMemoryNote.observed_at.desc())
            .limit(limit)
        ).scalars()
    )
    debt = []
    for n in notes:
        impact = "medium"
        if n.status == "disputed":
            impact = "high"
        if n.supersedes_note_id:
            impact = "high"
        debt.append(
            {
                "note_id": str(n.id),
                "note_type": n.note_type,
                "status": n.status,
                "impact": impact,
                "preview": (n.content or "")[:120],
                "needs_founder": n.status == "disputed",
            }
        )
    # High-impact first
    debt.sort(key=lambda d: {"high": 0, "medium": 1, "low": 2}.get(d["impact"], 9))
    return {
        "items": debt[:limit],
        "bounded": True,
        "spam": False,
        "authority_impact": "NONE",
    }


def list_triggered_decision_debt(
    db: Session,
    *,
    owner_id: uuid.UUID,
    limit: int = 20,
    recency_days: int = 14,
) -> dict[str, Any]:
    """Additive extension of list_decision_debt() above: same bounded-queue shape, but adds
    real TRIGGERING logic instead of a static list -- reconciliation doc's real gap #11
    ("surfacing on new-evidence/deadline/dependency-ready, not just a static bounded list").
    Does NOT modify list_decision_debt()/why_feature_exists() above -- purely additive.

    Three independent, deterministic trigger kinds, each grounded in an existing durable fact
    this codebase already records (never inferred, never an LLM judgment call of its own):

      - 'dependency_ready': an unreviewed WorkCandidate (app.work_candidates) lists another
        WorkCandidate id in its own `dependencies` column (the same real UUID-parsing
        app.inspectable_memory.service._from_work_candidate() already does for that column)
        that has since become `authorized` -- the blocking work this candidate was waiting on
        just became real. Surfacing this creates nothing and authorizes nothing.
      - 'lesson_conflict_recent': app.mainai_execution.lesson_conflicts.mark_conflict()'s own
        real `MainAITaskEvent(event_type=lesson_conflict_detected)` rows, within
        `recency_days`, joined back (task -> goal -> WorkCandidate.authorized_goal_id) to the
        WorkCandidate whose already-authorized work is affected -- new contradicting evidence
        against work already in flight, read-only.
      - 'note_disputed' / 'note_recent': the same FounderMemoryNote pool list_decision_debt()
        already surfaces, split out by whether it is `disputed` (needs_founder) or was
        `observed_at` within `recency_days` (freshly added, not stale carryover).

    No deadline field exists on WorkCandidate or LifeIntent today (confirmed by direct
    reading of both models) -- deadline-based triggering is intentionally not implemented
    here; a future migration adding one should extend this function, not invent a parallel
    one."""
    if not (1 <= limit <= 200):
        raise ValueError("limit outside supported bounds")
    if recency_days < 1:
        raise ValueError("recency_days must be positive")
    since = datetime.utcnow() - timedelta(days=recency_days)
    items: list[dict[str, Any]] = []

    # --- dependency_ready ----------------------------------------------------------------
    unreviewed = list(
        db.execute(
            select(WorkCandidate).where(
                WorkCandidate.owner_id == owner_id, WorkCandidate.status == "unreviewed"
            )
        ).scalars()
    )

    def _dep_ids(candidate: WorkCandidate) -> set[uuid.UUID]:
        parsed: set[uuid.UUID] = set()
        for raw in candidate.dependencies or []:
            try:
                parsed.add(uuid.UUID(str(raw)))
            except (TypeError, ValueError):
                continue
        return parsed

    all_dep_ids: set[uuid.UUID] = set()
    for candidate in unreviewed:
        all_dep_ids |= _dep_ids(candidate)
    authorized_dep_ids: set[uuid.UUID] = set()
    if all_dep_ids:
        authorized_dep_ids = set(
            db.execute(
                select(WorkCandidate.id).where(
                    WorkCandidate.owner_id == owner_id,
                    WorkCandidate.id.in_(all_dep_ids),
                    WorkCandidate.status == "authorized",
                )
            ).scalars()
        )
    for candidate in unreviewed:
        ready = _dep_ids(candidate) & authorized_dep_ids
        if ready:
            items.append(
                {
                    "kind": "work_candidate",
                    "id": str(candidate.id),
                    "trigger": "dependency_ready",
                    "impact": "high",
                    "preview": candidate.title[:120],
                    "ready_dependency_ids": sorted(str(d) for d in ready),
                    "needs_founder": False,
                }
            )

    # --- lesson_conflict_recent ------------------------------------------------------------
    recent_conflict_events = list(
        db.execute(
            select(MainAITaskEvent)
            .where(
                MainAITaskEvent.owner_id == owner_id,
                MainAITaskEvent.event_type == MainAITaskEventType.lesson_conflict_detected,
                MainAITaskEvent.created_at >= since,
            )
            .order_by(MainAITaskEvent.created_at.desc())
            .limit(limit)
        ).scalars()
    )
    if recent_conflict_events:
        task_ids = {ev.task_id for ev in recent_conflict_events}
        goal_ids = set(
            db.execute(select(MainAITask.goal_id).where(MainAITask.id.in_(task_ids))).scalars()
        )
        affected_candidates = (
            list(
                db.execute(
                    select(WorkCandidate).where(
                        WorkCandidate.owner_id == owner_id,
                        WorkCandidate.authorized_goal_id.in_(goal_ids),
                    )
                ).scalars()
            )
            if goal_ids
            else []
        )
        for candidate in affected_candidates:
            items.append(
                {
                    "kind": "work_candidate",
                    "id": str(candidate.id),
                    "trigger": "lesson_conflict_recent",
                    "impact": "high",
                    "preview": candidate.title[:120],
                    "conflict_event_count": len(recent_conflict_events),
                    "needs_founder": True,
                }
            )

    # --- note_disputed / note_recent --------------------------------------------------------
    notes = list(
        db.execute(
            select(FounderMemoryNote)
            .where(
                FounderMemoryNote.owner_id == owner_id,
                FounderMemoryNote.note_type.in_(("decision", "correction")),
                FounderMemoryNote.status.in_(("active", "disputed")),
            )
            .order_by(FounderMemoryNote.observed_at.desc())
            .limit(limit)
        ).scalars()
    )
    for n in notes:
        if n.status == "disputed":
            items.append(
                {
                    "kind": "founder_memory_note",
                    "id": str(n.id),
                    "trigger": "note_disputed",
                    "impact": "high",
                    "preview": (n.content or "")[:120],
                    "needs_founder": True,
                }
            )
        elif n.observed_at and n.observed_at >= since:
            items.append(
                {
                    "kind": "founder_memory_note",
                    "id": str(n.id),
                    "trigger": "note_recent",
                    "impact": "medium",
                    "preview": (n.content or "")[:120],
                    "needs_founder": False,
                }
            )

    items.sort(key=lambda d: {"high": 0, "medium": 1, "low": 2}.get(d["impact"], 9))
    return {
        "items": items[:limit],
        "bounded": True,
        "spam": False,
        "authority_impact": "NONE",
        "recency_days": recency_days,
    }
