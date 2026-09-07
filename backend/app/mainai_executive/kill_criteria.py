"""Deterministic, read-only kill-criteria / sunk-cost evaluation over real WorkCandidate,
LifeIntent, and EngineeringLesson evidence. SUNK COST != CONTINUE.

`evaluate_kill_criteria()` NEVER mutates state -- there is no `db.add`, no `db.commit`, no
in-place row assignment, and no raw SQL data-modification statement anywhere in this module.
It only recommends. Acting on a `should_kill` recommendation is
entirely the caller's own, separate responsibility, and MUST route through the real,
already-authorized state-change functions this codebase already has:

  - `app.work_candidates.service.dismiss_work_candidate()` / `supersede_work_candidate()`
  - `app.life_intents.service.transition_intent()` (this round's own P0-fixed state machine),
    using its real `LIFE_INTENT_TRANSITIONS` table and `expected_current_state` param for
    optimistic concurrency.

This module never imports either of those mutating functions -- it has no way to mutate even
if a caller misuses its output."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.life_intents.service import TERMINAL_LIFE_INTENT_STATES
from app.models.life_intent import LifeIntent, LifeIntentEvent
from app.models.mainai_execution import EngineeringLesson, EngineeringLessonStatus
from app.models.work_candidate import WorkCandidate

# WorkCandidate.status values (migration 0055 + app.work_candidates.service) that already
# mean "not live" -- evaluate_kill_criteria() surfaces these as a should_kill=True FACT, it
# never infers them.
_WORK_CANDIDATE_DEAD_STATUSES = frozenset({"dismissed", "superseded"})

# app.life_intents.service.TERMINAL_LIFE_INTENT_STATES includes 'completed' -- a SUCCESS, not
# a kill. Only abandoned/superseded are genuinely kill-shaped terminal exits.
_LIFE_INTENT_KILLED_STATES = frozenset({"abandoned", "superseded"})

DEFAULT_STALE_AGE_DAYS = 90

# Confidence contributions per signal -- Numeric(5,4)-scale floats (0..1), matching this
# codebase's real, reused confidence scale (FounderMemoryNote/IntelligenceInterpretation/
# IntelligenceIdea). Never self-reported; each is tied to a concrete, checked fact below.
_CONFIDENCE_ALREADY_DEAD = 0.95
_CONFIDENCE_STALE = 0.55
_CONFIDENCE_DISPUTED_LESSON_OVERLAP = 0.4
_SHOULD_KILL_THRESHOLD = 0.5


def _stale(reference_time: datetime | None, *, stale_age_days: int) -> bool:
    if reference_time is None:
        return False
    return datetime.utcnow() - reference_time > timedelta(days=stale_age_days)


def _candidate_tags(row: WorkCandidate) -> set[str]:
    tags: set[str] = set()
    if isinstance(row.provenance, dict):
        for key in ("tags", "lesson_tags"):
            raw = row.provenance.get(key)
            if isinstance(raw, list):
                tags.update(str(t) for t in raw)
        horizon = row.provenance.get("horizon")
        if horizon:
            tags.add(str(horizon).lower())
    if row.classifier_strategy:
        tags.add(row.classifier_strategy)
    return tags


def _disputed_lessons_touching_tags(db: Session, *, tags: set[str]) -> set[str]:
    """Deterministic tag-overlap narrowing, matching app.mainai_execution.lesson_conflicts'
    own "narrow deterministically" convention -- never an AI judgment call. A candidate whose
    own tags happen to overlap a disputed lesson's `applies_to` is only a SOFT signal (see the
    lower confidence weight above), since the overlap itself proves relatedness, not that the
    dispute is actually ABOUT this candidate."""
    if not tags:
        return set()
    rows = db.execute(
        select(EngineeringLesson).where(EngineeringLesson.status == EngineeringLessonStatus.disputed)
    ).scalars().all()
    return {str(lesson.id) for lesson in rows if set(lesson.applies_to or []) & tags}


def _evaluate_work_candidate(
    db: Session, *, owner_id: uuid.UUID, candidate_id: uuid.UUID, stale_age_days: int
) -> dict[str, Any]:
    row = db.execute(
        select(WorkCandidate).where(WorkCandidate.id == candidate_id, WorkCandidate.owner_id == owner_id)
    ).scalar_one_or_none()
    if row is None:
        return {"found": False, "should_kill": False, "reasons": ["work_candidate not found for this owner"], "confidence": 0.0}

    reasons: list[str] = []
    confidence = 0.0
    if row.status in _WORK_CANDIDATE_DEAD_STATUSES:
        reasons.append(f"work_candidate.status is already {row.status!r}")
        confidence = max(confidence, _CONFIDENCE_ALREADY_DEAD)
    if row.status == "unreviewed" and _stale(row.observed_at, stale_age_days=stale_age_days):
        reasons.append(
            f"work_candidate has been unreviewed for over {stale_age_days} days "
            f"(observed_at={row.observed_at.isoformat()})"
        )
        confidence = max(confidence, _CONFIDENCE_STALE)
    disputed = _disputed_lessons_touching_tags(db, tags=_candidate_tags(row))
    if disputed:
        reasons.append(f"{len(disputed)} disputed EngineeringLesson row(s) share tags with this candidate: {sorted(disputed)}")
        confidence = max(confidence, _CONFIDENCE_DISPUTED_LESSON_OVERLAP)

    return {
        "found": True,
        "should_kill": confidence >= _SHOULD_KILL_THRESHOLD,
        "reasons": reasons or ["no kill signal found"],
        "confidence": round(confidence, 4),
        "status": row.status,
    }


def _evaluate_life_intent(
    db: Session, *, owner_id: uuid.UUID, intent_id: uuid.UUID, stale_age_days: int
) -> dict[str, Any]:
    row = db.execute(
        select(LifeIntent).where(LifeIntent.id == intent_id, LifeIntent.owner_id == owner_id)
    ).scalar_one_or_none()
    if row is None:
        return {"found": False, "should_kill": False, "reasons": ["life_intent not found for this owner"], "confidence": 0.0}

    reasons: list[str] = []
    confidence = 0.0
    if row.state in _LIFE_INTENT_KILLED_STATES:
        reasons.append(f"life_intent.state is already {row.state!r}")
        confidence = max(confidence, _CONFIDENCE_ALREADY_DEAD)

    last_event_at = db.execute(
        select(LifeIntentEvent.created_at)
        .where(LifeIntentEvent.owner_id == owner_id, LifeIntentEvent.intent_id == row.id)
        .order_by(LifeIntentEvent.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    reference_time = last_event_at or row.updated_at
    if row.state not in TERMINAL_LIFE_INTENT_STATES and _stale(reference_time, stale_age_days=stale_age_days):
        reasons.append(f"no life_intent activity for over {stale_age_days} days (last_activity={reference_time.isoformat()})")
        confidence = max(confidence, _CONFIDENCE_STALE)

    return {
        "found": True,
        "should_kill": confidence >= _SHOULD_KILL_THRESHOLD,
        "reasons": reasons or ["no kill signal found"],
        "confidence": round(confidence, 4),
        "state": row.state,
    }


def evaluate_kill_criteria(
    db: Session,
    *,
    owner_id: uuid.UUID,
    work_candidate_id: uuid.UUID | None = None,
    life_intent_id: uuid.UUID | None = None,
    stale_age_days: int = DEFAULT_STALE_AGE_DAYS,
) -> dict[str, Any]:
    """Deterministic checks over real WorkCandidate/LifeIntent/EngineeringLesson fields.
    Returns a recommendation dict -- `should_kill`, `reasons`, `confidence` -- and NEVER
    itself mutates state (see module docstring). At least one of work_candidate_id/
    life_intent_id must be given; both may be given together (e.g. a WorkCandidate whose
    originating LifeIntent is also being evaluated)."""
    if work_candidate_id is None and life_intent_id is None:
        raise ValueError("evaluate_kill_criteria requires work_candidate_id and/or life_intent_id")
    if stale_age_days < 1:
        raise ValueError("stale_age_days must be positive")

    should_kill = False
    reasons: list[str] = []
    confidence = 0.0
    result: dict[str, Any] = {"owner_id": str(owner_id), "authority_impact": "NONE", "mutates": False}

    if work_candidate_id is not None:
        wc = _evaluate_work_candidate(db, owner_id=owner_id, candidate_id=work_candidate_id, stale_age_days=stale_age_days)
        result["work_candidate"] = wc
        should_kill = should_kill or wc["should_kill"]
        reasons.extend(f"[work_candidate] {r}" for r in wc["reasons"])
        confidence = max(confidence, wc["confidence"])

    if life_intent_id is not None:
        li = _evaluate_life_intent(db, owner_id=owner_id, intent_id=life_intent_id, stale_age_days=stale_age_days)
        result["life_intent"] = li
        should_kill = should_kill or li["should_kill"]
        reasons.extend(f"[life_intent] {r}" for r in li["reasons"])
        confidence = max(confidence, li["confidence"])

    result["should_kill"] = should_kill
    result["reasons"] = reasons
    result["confidence"] = round(confidence, 4)
    return result
