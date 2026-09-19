"""Architectural gravity -- the OPPOSITE direction from `missing_piece.detect_missing_pieces()`
(reconciliation doc real gap #5 / decision item 5). `missing_piece` checks "given a request, does
existing coverage already exist" (retrospective, request-anchored). `detect_architectural_
gravity()` checks "given several UNREQUESTED, independent recent items, is the SAME underlying
pattern showing up across them anyway" -- i.e. should MainAI proactively propose formalizing
something nobody explicitly asked to formalize.

Deterministic-first, matching `app.mainai_execution.lesson_conflicts`'s own "narrow
deterministically, judge only the real candidates" two-stage shape -- but this module stops
after the deterministic narrowing stage. Tag/component overlap across independent, real
`app.memory_threads.MemoryThread` rows is itself a concrete, checkable FACT (unlike
`lesson_conflicts.detect_conflict()`'s "do these two rules actually contradict", which genuinely
requires interpretation) -- so no AI-judgment stage is added here. A future caller wanting one
can layer it on top of this function's own bounded `candidates` output without this module
needing to change.

`detect_architectural_gravity()` is read-only: it queries `app.memory_threads` (real,
already-existing `thread_members()`) plus `WorkCandidate`/`IntelligenceIdea` (to read their real
tag-like fields, matching `kill_criteria._candidate_tags()`'s own extraction shape) and NEVER
creates a `WorkCandidate`, `IntelligenceIdea`, or `MemoryThread` itself -- it only PROPOSES that
one might be worth creating (via `strategic_compression.compress_into_program()` or a founder's
own explicit action), mirroring `kill_criteria.evaluate_kill_criteria()`'s own "recommend, never
act" contract exactly."""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.memory_threads.service import thread_members
from app.models.intelligence_governance import IntelligenceIdea
from app.models.memory_thread import MemoryThread
from app.models.work_candidate import WorkCandidate

DEFAULT_LOOKBACK_DAYS = 90
DEFAULT_MIN_INDEPENDENT_THREADS = 3


def _member_tags(db: Session, *, owner_id: uuid.UUID, member) -> set[str]:
    """Deterministic tag extraction over a real `MemoryThreadMember` row. Only `work_candidate`/
    `intelligence_idea` members carry a tag-like signal today -- every other `member_kind`
    contributes no tags (an honest "no signal", never a fabricated one).

    Deliberately narrower than `kill_criteria._candidate_tags()`'s own extraction: this module
    does NOT fold in `WorkCandidate.classifier_strategy` or `IntelligenceIdea.classification_
    basis`. Those are PROCESSING-MECHANISM fields (e.g. "project_entity_promotion_v1",
    "deterministic") that are routinely identical across a whole batch of otherwise-unrelated
    rows produced by the same pipeline -- treating them as a content "pattern" would make
    `detect_architectural_gravity()` fire on "these all went through the same classifier",
    which is not gravity, it's plumbing. Only explicit `provenance` tags (a human/system
    actually asserting "this is about X") and `IntelligenceIdea.idea_kind` (a real content-kind
    field, not a pipeline label) count here."""
    tags: set[str] = set()
    try:
        ref_id = uuid.UUID(str(member.member_ref_id))
    except (TypeError, ValueError):
        return tags
    if member.member_kind == "work_candidate":
        row = db.execute(
            select(WorkCandidate).where(WorkCandidate.id == ref_id, WorkCandidate.owner_id == owner_id)
        ).scalar_one_or_none()
        if row is not None and isinstance(row.provenance, dict):
            for key in ("tags", "lesson_tags"):
                raw = row.provenance.get(key)
                if isinstance(raw, list):
                    tags.update(str(t).lower() for t in raw)
    elif member.member_kind == "intelligence_idea":
        row = db.execute(
            select(IntelligenceIdea).where(IntelligenceIdea.id == ref_id, IntelligenceIdea.owner_id == owner_id)
        ).scalar_one_or_none()
        if row is not None and row.idea_kind:
            tags.add(str(row.idea_kind).lower())
    return tags


def detect_architectural_gravity(
    db: Session,
    *,
    owner_id: uuid.UUID,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    min_independent_threads: int = DEFAULT_MIN_INDEPENDENT_THREADS,
    limit: int = 20,
) -> dict[str, Any]:
    """Bounded, deterministic, read-only. A returned candidate means: one tag shared across
    `>= min_independent_threads` DISTINCT, active `MemoryThread` rows for this owner within
    `lookback_days` -- i.e. the SAME pattern recurring across genuinely separate threads, not
    just multiple items already grouped together in one thread (which is zero new signal --
    see `strategic_compression.py` for the already-grouped case). NEVER mutates."""
    if lookback_days < 1:
        raise ValueError("lookback_days must be positive")
    if min_independent_threads < 2:
        raise ValueError("min_independent_threads must be at least 2 -- one thread is never a pattern")
    if not (1 <= limit <= 200):
        raise ValueError("limit outside supported bounds")

    since = datetime.utcnow() - timedelta(days=lookback_days)
    threads = list(
        db.execute(
            select(MemoryThread).where(
                MemoryThread.owner_id == owner_id,
                MemoryThread.state == "active",
                MemoryThread.last_activity_at >= since,
            )
        ).scalars()
    )

    tag_threads: dict[str, set[uuid.UUID]] = defaultdict(set)
    for thread in threads:
        members = thread_members(db, owner_id=owner_id, thread_id=thread.id, include_inactive=False)
        thread_tags: set[str] = set()
        for member in members:
            thread_tags |= _member_tags(db, owner_id=owner_id, member=member)
        for tag in thread_tags:
            tag_threads[tag].add(thread.id)

    candidates = [
        {
            "tag": tag,
            "thread_ids": sorted(str(t) for t in tids),
            "independent_thread_count": len(tids),
        }
        for tag, tids in tag_threads.items()
        if len(tids) >= min_independent_threads
    ]
    candidates.sort(key=lambda c: (-c["independent_thread_count"], c["tag"]))

    return {
        "owner_id": str(owner_id),
        "gravity_detected": bool(candidates),
        "candidates": candidates[:limit],
        "recommendation": (
            "propose a formalization candidate for founder review -- see "
            "strategic_compression.compress_into_program() to group the underlying items; "
            "never auto-created here"
            if candidates
            else "no recurring unrequested pattern found across independent threads"
        ),
        "auto_created": False,
        "authority_impact": "NONE",
        "lookback_days": lookback_days,
        "min_independent_threads": min_independent_threads,
    }
