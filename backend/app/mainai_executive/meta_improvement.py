"""Meta-improvement loop -- "founder correction -> change future REASONING BEHAVIOR", a narrow
extension of `app.mainai_execution.lessons.record_lesson_from_founder_correction()`'s own real
pattern (reconciliation doc real gap #7 / decision item 7), scoped specifically to MainAI's OWN
behavioral defaults (this Part 2 program's own judgment/wip/gravity/compression thresholds and
temperament), not code. Distinct from the existing function's "correction -> engineering lesson
about CODE" the SAME way `record_lesson_from_founder_correction()` is already distinct from the
plain `record_lesson()` it wraps: same mechanism (`EngineeringLesson`, never a new table),
narrower domain -- enforced here by requiring `affected_component` to be one of a small,
explicit, behavior-scoped vocabulary (`BEHAVIORAL_COMPONENTS` below) rather than an arbitrary
code path.

Reuses `record_lesson_from_founder_correction()` completely unchanged (never re-implements its
`note_type == 'correction'` guard, its `problem`/`evidence`/`first_seen_at` derivation from the
note, or its "`root_cause`/`general_rule`/`applies_to` stay the caller's own explicit judgment,
never auto-derived" discipline) -- this module ONLY adds the behavioral-component vocabulary
check on top, then delegates.

Where a genuine contradiction between two behavioral lessons is worth checking (e.g. one
correction says "default to CHALLENGE more aggressively", a later one says "default to CHALLENGE
less aggressively" -- both about `challenge_threshold`), this module composes with
`app.mainai_execution.lesson_conflicts`'s real, already-proven two-stage shape
(`find_conflict_candidate_pairs()` deterministic narrowing -> `detect_conflict()` the one AI
judgment call -> `mark_conflict()` moves BOTH to `disputed`, never picks a winner) completely
unchanged -- it only narrows the candidate set to behavioral lessons first, via
`lookup_behavioral_lessons()`."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.mainai_execution.lessons import record_lesson_from_founder_correction
from app.models.mainai_execution import EngineeringLesson, EngineeringLessonConfidence, EngineeringLessonStatus

# MainAI's own behavioral-default vocabulary this module is scoped to -- NOT a CHECK constraint
# on the `engineering_lessons` table itself (`affected_component` remains a plain,
# unconstrained `String(128)` column -- see `app/models/mainai_execution.py`'s own
# `EngineeringLesson` model), just a documented, ENFORCED-HERE convention distinguishing a
# REASONING-BEHAVIOR lesson from a CODE lesson using the exact same table and the exact same
# column. A future Part 2 module that grows its own tunable default should add its own name
# here, not invent a parallel mechanism.
BEHAVIORAL_COMPONENTS = frozenset(
    {
        "judgment_temperament",
        "wip_default",
        "challenge_threshold",
        "rejected_idea_guard_sensitivity",
        "kill_criteria_threshold",
        "architectural_gravity_threshold",
        "strategic_compression_threshold",
    }
)


class MetaImprovementError(ValueError):
    pass


def record_behavioral_lesson_from_founder_correction(
    db: Session,
    *,
    note,
    root_cause: str,
    affected_component: str,
    general_rule: str,
    applies_to: list[str],
    created_by: str,
    fix: str,
    severity=None,
    regression_test: str | None = None,
    confidence: EngineeringLessonConfidence = EngineeringLessonConfidence.likely,
) -> EngineeringLesson:
    """Same required shape as `record_lesson_from_founder_correction()` -- see that function's
    own docstring for the `note_type == 'correction'` requirement (enforced there, unchanged,
    NOT re-implemented here) and the "problem/evidence/first_seen_at come from the note itself,
    root_cause/general_rule/applies_to remain the caller's own explicit judgment" discipline. The
    ONLY addition: `affected_component` must be one of `BEHAVIORAL_COMPONENTS` -- a lesson about
    CODE should keep calling `record_lesson_from_founder_correction()` directly, never this
    function."""
    if affected_component not in BEHAVIORAL_COMPONENTS:
        raise MetaImprovementError(
            f"affected_component {affected_component!r} is not a recognized MainAI behavioral "
            f"default -- expected one of {sorted(BEHAVIORAL_COMPONENTS)}; a lesson about CODE "
            "belongs to record_lesson_from_founder_correction() directly, not this function"
        )
    return record_lesson_from_founder_correction(
        db,
        note=note,
        root_cause=root_cause,
        affected_component=affected_component,
        general_rule=general_rule,
        applies_to=applies_to,
        created_by=created_by,
        fix=fix,
        severity=severity,
        regression_test=regression_test,
        confidence=confidence,
    )


def lookup_behavioral_lessons(db: Session, *, component: str | None = None) -> list[EngineeringLesson]:
    """Active behavioral-default lessons -- optionally narrowed to one `BEHAVIORAL_COMPONENTS`
    value. Founder-wide, like `EngineeringLesson` itself (not owner-scoped -- see
    `app/mainai_execution/lessons.py`'s own module docstring for why)."""
    if component is not None and component not in BEHAVIORAL_COMPONENTS:
        raise MetaImprovementError(f"{component!r} is not a recognized behavioral component")
    components = (component,) if component else tuple(BEHAVIORAL_COMPONENTS)
    return list(
        db.execute(
            select(EngineeringLesson)
            .where(
                EngineeringLesson.status == EngineeringLessonStatus.active,
                EngineeringLesson.affected_component.in_(components),
            )
            .order_by(EngineeringLesson.severity.desc(), EngineeringLesson.created_at.desc())
        ).scalars()
    )


def find_behavioral_conflict_candidates(db: Session):
    """Thin composition with the real, unchanged
    `app.mainai_execution.lesson_conflicts.find_conflict_candidate_pairs()` -- narrows to
    behavioral lessons first via `lookup_behavioral_lessons()`, then reuses that function's own
    deterministic pairing unchanged. Never re-implements the pairing logic."""
    from app.mainai_execution.lesson_conflicts import find_conflict_candidate_pairs

    return find_conflict_candidate_pairs(db, lessons=lookup_behavioral_lessons(db))


async def resolve_behavioral_conflicts(db: Session):
    """Thin composition with the real, unchanged
    `app.mainai_execution.lesson_conflicts.resolve_conflicts_among()` -- same fail-closed
    AI-judgment shape, narrowed to behavioral lessons only."""
    from app.mainai_execution.lesson_conflicts import resolve_conflicts_among

    return await resolve_conflicts_among(db, lessons=lookup_behavioral_lessons(db))
