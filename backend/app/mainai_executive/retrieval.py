"""Pure, deterministic re-ranking pass over `app.active_context`'s real output.

`app.active_context.service`'s own traversal (`refresh_context()`/`current_members()`) ranks
strictly by BFS discovery order (`rank`) -- the reconciliation doc's own confirmed gap:
"Ranking today is traversal-order only." `rank_by_strength()` never queries the database and
never touches `active_context/service.py` itself -- it is a strict post-processing layer over
whatever that module already returned, so it is trivially safe to call from anywhere without
adding new I/O or a parallel retrieval index (reconciliation doc §1.9: "extends... never a
parallel retrieval index")."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Sequence

# ActiveContextMember.state vocabulary (app.models.active_context.ContextMemberState) --
# duck-typed here (not imported) so rank_by_strength() works over ORM rows, lightweight
# dicts, or a caller's own copy, without adding a hard SQLAlchemy-model dependency. A
# founder's own explicit pin (state == "pinned") is real authority
# (active_context.service.ContextAuthority.founder) -- always sorted first, never buried by a
# heuristic score. stale/suppressed/superseded members (only present when a caller passed
# include_noncurrent=True to current_members()) are penalized, never boosted.
_STATE_BOOST = {"pinned": 1.0, "active": 0.0, "stale": -0.5, "suppressed": -1.0, "superseded": -1.0}


@dataclass(frozen=True)
class StrengthWeights:
    confidence: float = 0.5
    recency: float = 0.3
    contradiction: float = 0.2

    def __post_init__(self) -> None:
        if self.confidence + self.recency + self.contradiction <= 0:
            raise ValueError("StrengthWeights must sum to a positive value")


@dataclass(frozen=True)
class RankedItem:
    item: Any
    object_type: str
    object_ref: str
    strength_score: float
    components: dict[str, float]
    pinned: bool


def _get(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        return item.get(name, default)
    return getattr(item, name, default)


def _recency_score(last_activated_at: datetime | None, *, now: datetime, half_life_days: float) -> float:
    """Exponential decay -- 1.0 at age 0, 0.5 at exactly one half-life. Unknown timestamp
    (e.g. a plain dict without one) gets a neutral 0.5, never a fabricated high or low."""
    if last_activated_at is None:
        return 0.5
    age_days = max(0.0, (now - last_activated_at).total_seconds() / 86400.0)
    if half_life_days <= 0:
        return 0.0
    return 0.5 ** (age_days / half_life_days)


def rank_by_strength(
    items: Sequence[Any],
    *,
    confidence_by_key: Mapping[tuple[str, str], float] | None = None,
    contradiction_count_by_key: Mapping[tuple[str, str], int] | None = None,
    default_confidence: float = 0.5,
    max_contradiction_for_full_penalty: int = 3,
    weights: StrengthWeights | None = None,
    now: datetime | None = None,
    recency_half_life_days: float = 14.0,
) -> list[RankedItem]:
    """Re-sorts `app.active_context`'s real member output (`ActiveContextMember` rows, or any
    duck-typed equivalent exposing `object_type`/`object_ref`/`state`/`last_activated_at`/
    `added_at`) by a weighted blend of confidence, recency, and INVERSE contradiction/dispute
    count.

    Confidence and contradiction counts are NOT stored on `ActiveContextMember` itself (its
    own ranking is traversal-order only) -- pass them via `confidence_by_key`/
    `contradiction_count_by_key`, keyed by `(object_type, object_ref)`, computed by the caller
    from whatever real source actually carries that fact for a given object_type (e.g.
    `IntelligenceIdea.confidence`, an `EngineeringLesson.status == disputed` count, an
    `IntelligenceInterpretation`'s own confidence). Items with no entry fall back to
    `default_confidence` / zero contradictions -- an honest "unknown", never a fabricated
    score.

    Pinned members (`state == "pinned"`) always sort first, before any score-based ordering.
    Ties within the same pinned/unpinned group break by the ORIGINAL input order (stable
    sort), preserving `active_context`'s own traversal-order signal as the final tie-break
    rather than discarding it."""
    weights = weights or StrengthWeights()
    now = now or datetime.utcnow()
    confidence_by_key = confidence_by_key or {}
    contradiction_count_by_key = contradiction_count_by_key or {}

    ranked: list[RankedItem] = []
    for item in items:
        object_type = str(_get(item, "object_type", ""))
        object_ref = str(_get(item, "object_ref", ""))
        state = str(_get(item, "state", "active"))
        key = (object_type, object_ref)

        confidence = min(1.0, max(0.0, float(confidence_by_key.get(key, default_confidence))))

        last_activated = _get(item, "last_activated_at") or _get(item, "added_at")
        recency = _recency_score(last_activated, now=now, half_life_days=recency_half_life_days)

        contradictions = max(0, int(contradiction_count_by_key.get(key, 0)))
        contradiction_penalty = min(1.0, contradictions / max(1, max_contradiction_for_full_penalty))
        contradiction_strength = 1.0 - contradiction_penalty  # inverse -- fewer contradictions is stronger

        state_boost = _STATE_BOOST.get(state, 0.0)

        # state_boost is a small nudge (0.05 scale), never blended into the weighted sum
        # itself, so `weights` stays interpretable as a real confidence/recency/contradiction
        # blend that sums to <= 1.0, not something state can silently dominate.
        score = (
            weights.confidence * confidence
            + weights.recency * recency
            + weights.contradiction * contradiction_strength
            + 0.05 * state_boost
        )

        ranked.append(
            RankedItem(
                item=item,
                object_type=object_type,
                object_ref=object_ref,
                strength_score=round(score, 6),
                components={
                    "confidence": round(confidence, 4),
                    "recency": round(recency, 4),
                    "contradiction_strength": round(contradiction_strength, 4),
                    "contradictions": float(contradictions),
                    "state_boost": state_boost,
                },
                pinned=(state == "pinned"),
            )
        )

    # Stable sort: python's sort is stable, so ties (same pinned-group, same score) keep the
    # original active_context traversal order.
    ranked.sort(key=lambda r: (not r.pinned, -r.strength_score))
    return ranked
