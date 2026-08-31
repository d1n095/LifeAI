"""Why-graph / decision-debt — founder-inspectable linkage without CoT.

Reuses memory_threads + work_candidates + truth claims. No new tables.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.founder_memory import FounderMemoryNote
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
