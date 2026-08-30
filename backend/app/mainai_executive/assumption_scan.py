"""Assumption / contradiction scan for the executive cycle.

Read-only: surfaces unverified assumptions and lesson conflict *candidates*
into observability. Never calls AI conflict judgment. Never silently re-authorizes.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.mainai_execution.lessons import lookup_lessons
from app.mainai_execution.lesson_conflicts import find_conflict_candidate_pairs
from app.models.mainai_execution import EngineeringLesson


def scan_assumptions_and_conflicts(
    db: Session,
    *,
    owner_id: uuid.UUID,
    lesson_tags: list[str] | None = None,
    lessons: list[EngineeringLesson] | None = None,
) -> dict[str, Any]:
    unverified: list[dict[str, Any]] = []
    conflict_candidates: list[dict[str, Any]] = []

    try:
        from app.models.problem_learning import LifeProblemAssumption

        rows = db.execute(
            select(LifeProblemAssumption)
            .where(
                LifeProblemAssumption.owner_id == owner_id,
                LifeProblemAssumption.status.in_(("untested", "unknown")),
            )
            .limit(20)
        ).scalars()
        for row in rows:
            unverified.append(
                {
                    "id": str(row.id),
                    "problem_id": str(row.problem_id),
                    "status": row.status,
                    "text": (row.statement or "")[:200],
                }
            )
    except Exception:
        unverified = []

    active_lessons = list(lessons or [])
    if not active_lessons and lesson_tags:
        active_lessons = list(lookup_lessons(db, applies_to_any=list(lesson_tags)))

    try:
        pairs = find_conflict_candidate_pairs(db, lessons=active_lessons)
        for a, b in pairs[:10]:
            conflict_candidates.append(
                {
                    "lesson_a": str(a.id),
                    "lesson_b": str(b.id),
                    "affected_component": a.affected_component,
                    # Deterministic candidate only — AI judgment NOT invoked here.
                    "judged_conflict": False,
                    "judgment_deferred": True,
                }
            )
    except Exception:
        conflict_candidates = []

    return {
        "unverified_assumptions": unverified,
        "lesson_conflict_candidates": conflict_candidates,
        "contradiction_refs": _to_contradiction_refs(unverified, conflict_candidates),
        "silently_reauthorized": False,
        "assumption_invalidation_requires_replan": bool(unverified or conflict_candidates),
        "authority_impact": "NONE — scan only; founder/policy must re-authorize separately",
        "ai_conflict_judgment_invoked": False,
    }


def _to_contradiction_refs(
    unverified: list[dict[str, Any]],
    conflict_candidates: list[dict[str, Any]],
) -> list[str]:
    """Stable string refs for safe_planner.FounderPlanningRequest.contradiction_refs.

    Does not authorize, resolve, or judge conflicts — feed-only.
    """
    refs: list[str] = []
    for row in unverified:
        refs.append(f"assumption:{row['id']}")
    for pair in conflict_candidates:
        refs.append(f"lesson_conflict:{pair['lesson_a']}:{pair['lesson_b']}")
    return refs
