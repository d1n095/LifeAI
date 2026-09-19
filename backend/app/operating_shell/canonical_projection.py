"""Canonical projection: read-only IntentObject views over real, existing, DB-backed goal/
intent systems (MainAI V2 Intent/Goal architecture reconciliation).

See docs/mainai_v2/MAINAI_V2_INTENT_GOAL_RECONCILIATION.md for the full decision. Summary:
IntentObject is never a fourth independent "goal truth" store. Once a real canonical link
exists, `refresh_from_canonical()` -- backed by `project_from_life_intent()`/
`project_from_mainai_goal()` -- is the ONLY legitimate way that intent's state may change.
The local mutators in intent.py (`advance_to_active`, `mark_blocked`, etc.) all reject direct
calls once linked (and `advance_to_active` rejects unconditionally, even unlinked -- see its
own docstring); only this module ever constructs an ACTIVE/BLOCKED/WAITING/etc. IntentObject,
and only by directly reading a real canonical row's own current state.

DELIBERATE EXCEPTION to this whole V2 lane's package-independence discipline: unlike
`app.guardian`/`app.privacy_boundary`/`app.sentinel`/`app.sovereign_identity`/
`app.life_recovery` (which must never import each other), this module DOES import real,
pre-existing PRODUCTION code -- `app.models.life_intent`, `app.life_intents.service`,
`app.models.mainai_execution`, `app.mainai_execution.planner`. That independence rule only
ever governed the five sibling V2 packages toward EACH OTHER; it never applied to real
production code, and reusing the canonical services' own lookups here (rather than
hand-rolling a second, potentially-diverging owner-scoped query) is the whole point of this
reconciliation. This module is still not imported by app.main/any router/the executive loop
-- read-only consumption from new, unreachable code, never new production wiring.

Both projection functions are READ-ONLY: neither ever calls db.add()/db.flush() with a
mutation, and neither ever assigns to a canonical row's own column.
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.life_intents.service import IntentError
from app.life_intents.service import _intent as _life_intent_row  # noqa: PLC2701 -- see module docstring: reusing the existing owner-scoped lookup on purpose, not hand-rolling a second one that could drift from it.
from app.mainai_execution.planner import GoalNotFoundError, get_goal
from app.models.mainai_execution import MainAIGoalStatus
from app.operating_shell.types import CanonicalKind, IntentObject, IntentState


class CanonicalProjectionError(ValueError):
    """Raised when a canonical row cannot be projected -- missing, wrong owner, or (for
    refresh_from_canonical) not actually canonically linked. Never returns a blank/guessed
    IntentObject in place of raising."""


# LifeIntent.state -> IntentState. Fail-closed: "unknown" (LifeIntent's own column default)
# maps to CAPTURED, never to anything implying real progress has been made -- this table is
# the ONLY place this mapping is decided, so a future new LifeIntent state value needs an
# explicit decision here rather than silently falling through to a guess.
_LIFE_INTENT_STATE_MAP: dict[str, IntentState] = {
    "active": IntentState.ACTIVE,
    "blocked": IntentState.BLOCKED,
    "waiting": IntentState.WAITING,
    "future": IntentState.PLANNED,  # not yet truly active
    "completed": IntentState.COMPLETED,
    "abandoned": IntentState.ABANDONED,
    "superseded": IntentState.SUPERSEDED,
    "unknown": IntentState.CAPTURED,  # fail-closed default; LifeIntent's own column default
}

# MainAIGoalStatus -> IntentState. Same fail-closed discipline: an unrecognized/future status
# value raises rather than silently defaulting to CAPTURED or ACTIVE (see the lookup below) --
# unlike LifeIntent's "unknown" is a real, valid state a fresh row explicitly gets, so the
# missing-key case here is genuinely "this reconciliation has fallen out of date with
# MainAIGoalStatus," not a normal runtime path, and should be loud, not silent.
_MAINAI_GOAL_STATUS_MAP: dict[MainAIGoalStatus, IntentState] = {
    MainAIGoalStatus.pending: IntentState.PLANNED,
    MainAIGoalStatus.planning: IntentState.PLANNED,
    MainAIGoalStatus.running: IntentState.ACTIVE,
    MainAIGoalStatus.waiting: IntentState.WAITING,
    MainAIGoalStatus.blocked: IntentState.BLOCKED,
    MainAIGoalStatus.failed: IntentState.ABANDONED,
    MainAIGoalStatus.completed: IntentState.COMPLETED,
    MainAIGoalStatus.cancelled: IntentState.ABANDONED,
}


def project_from_life_intent(db: Session, *, owner_id: uuid.UUID, life_intent_id: uuid.UUID) -> IntentObject:
    """Read-only. Raises CanonicalProjectionError if the LifeIntent is missing or belongs to
    a different owner (reuses app.life_intents.service's own owner-scoped lookup, never a
    second hand-rolled query that could silently diverge from it).

    LifeIntent has no raw-expression field of its own (it is a STRUCTURED tracking entity,
    not the conversational staging layer -- that role belongs to founder_memory_notes,
    upstream of it). `raw_user_expression` is therefore derived from `title` here -- an
    honest placeholder, not a fabricated claim that this is the founder's own verbatim words;
    a caller that needs the real raw expression must look it up via founder_memory_notes/
    memory_thread_id separately, this projection does not attempt it."""
    try:
        row = _life_intent_row(db, owner_id, life_intent_id, lock=False)
    except IntentError as exc:
        raise CanonicalProjectionError(str(exc)) from exc

    mapped_state = _LIFE_INTENT_STATE_MAP.get(row.state, IntentState.CAPTURED)

    return IntentObject(
        intent_id=uuid.uuid4(),
        owner_id=row.owner_id,
        title=row.title,
        raw_user_expression=row.title,
        state=mapped_state,
        created_at=row.created_at,
        updated_at=row.updated_at,
        canonical_kind=CanonicalKind.LIFE_INTENT,
        canonical_ref=row.id,
    )


def project_from_mainai_goal(db: Session, *, owner_id: uuid.UUID, goal_id: uuid.UUID) -> IntentObject:
    """Read-only. `get_goal()` itself does not check ownership (it is a bare db.get() by
    primary key, relying on RLS at the session layer in real production use) -- this function
    adds its own explicit owner_id check so it is safe even against a superuser/RLS-bypassing
    session, and never trusts RLS alone as its only ownership guarantee.

    `original_instruction` (a real raw-text field on MainAIGoal) populates
    `raw_user_expression`. `interpreted_goal` deliberately stays unset here -- `title` is
    already an interpreted label, not the founder's own raw words, and promoting it into
    `interpreted_goal` without a real understanding step having produced it would blur RAW
    USER EXPRESSION != INTERPRETED TRUTH rather than honor it."""
    try:
        goal = get_goal(db, goal_id)
    except GoalNotFoundError as exc:
        raise CanonicalProjectionError(str(exc)) from exc
    if goal.owner_id != owner_id:
        raise CanonicalProjectionError(f"MainAIGoal {goal_id} belongs to another owner")

    if goal.status not in _MAINAI_GOAL_STATUS_MAP:
        raise CanonicalProjectionError(f"unrecognized MainAIGoalStatus {goal.status!r} -- reconciliation mapping is out of date")
    mapped_state = _MAINAI_GOAL_STATUS_MAP[goal.status]

    return IntentObject(
        intent_id=uuid.uuid4(),
        owner_id=goal.owner_id,
        title=goal.title,
        raw_user_expression=goal.original_instruction,
        state=mapped_state,
        created_at=goal.created_at,
        updated_at=goal.completed_at or goal.started_at or goal.created_at,
        canonical_kind=CanonicalKind.MAINAI_GOAL,
        canonical_ref=goal.id,
    )


def refresh_from_canonical(db: Session, intent: IntentObject) -> IntentObject:
    """The ONLY legitimate way a canonically-linked IntentObject's state may change after
    creation. Re-runs the appropriate projection function and returns a NEW IntentObject
    reflecting the canonical row's CURRENT state. OLD GOAL != CURRENT GOAL: if the underlying
    LifeIntent/MainAIGoal has itself moved to a terminal or superseded state, this reflects
    that too -- a stale IntentObject can never resurrect authority the canonical row no
    longer holds, because this function never trusts the passed-in `intent`'s own state,
    only its `canonical_ref`."""
    if intent.canonical_kind == CanonicalKind.NONE:
        raise CanonicalProjectionError(f"intent {intent.intent_id} has no canonical link to refresh from (canonical_kind is NONE)")
    if intent.canonical_kind == CanonicalKind.LIFE_INTENT:
        return project_from_life_intent(db, owner_id=intent.owner_id, life_intent_id=intent.canonical_ref)
    if intent.canonical_kind == CanonicalKind.MAINAI_GOAL:
        return project_from_mainai_goal(db, owner_id=intent.owner_id, goal_id=intent.canonical_ref)
    raise CanonicalProjectionError(f"unrecognized canonical_kind {intent.canonical_kind!r}")
