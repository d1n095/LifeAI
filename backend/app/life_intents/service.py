"""Bounded, explainable and provider-independent life-intent feasibility."""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.active_context.service import InvalidContextReference, _require_ref
from app.models.life_intent import (
    LifeIntent,
    LifeIntentBlocker,
    LifeIntentDependency,
    LifeIntentEvent,
)
from app.models.user import User


class IntentError(ValueError):
    pass


class InvalidTransitionError(IntentError):
    """The requested FROM/TO state pair is not a legitimate transition per
    LIFE_INTENT_TRANSITIONS -- raised instead of silently applying it."""


class TerminalStateError(InvalidTransitionError):
    """The intent's current state has no outbound transitions at all (`completed`/
    `abandoned`/`superseded`). STALE CALLER != CURRENT AUTHORITY: no generic call to
    transition_intent() may resurrect a terminal intent -- this codebase defines no separate
    reopen operation for LifeIntent (unlike e.g. app.strategy_evaluation's `invalidated`
    exit from `completed`/`failed`); if one is ever needed it must be its own, explicitly
    named, explicitly authorized function, never a side effect of this generic gate."""


class StaleTransitionError(IntentError):
    """The caller's `expected_current_state` no longer matches the row's real current state --
    another actor already changed it since the caller last read it. READ STATE A -> another
    actor changes A to B -> a stale caller's own A->C attempt is REJECTED here, even when
    A->C would otherwise be a legitimate transition, because the caller's premise (the
    intent is still in state A) is itself already wrong."""


# Real, existing LifeIntent state vocabulary (confirmed by direct reading of
# tests/backend/context/test_goals_dreams_dependencies.py, the original migration-0041 test
# suite -- not invented for this fix): unknown (the column default, pre-classification),
# future (a dream/goal not yet started), active, blocked, waiting, and the three terminal
# states completed/abandoned/superseded. This is the SAME state vocabulary
# app.life_intents.service.evaluate_feasibility() already treats
# completed/abandoned/superseded/waiting/future/blocked/unknown as non-actionable -- this
# table does not introduce a new model, it makes the existing one's legal transitions
# explicit and enforced, following the exact same module-level-dict-of-allowed-targets
# pattern already established by app.strategy_evaluation.service's
# EXPERIMENT_TRANSITIONS/CANDIDATE_TRANSITIONS.
#
# STALE CALLER != CURRENT AUTHORITY: completed/abandoned/superseded map to an empty set --
# nothing may transition OUT of a terminal LifeIntent via this function. No separate reopen
# operation exists anywhere in this codebase today; if the product ever needs one, it must be
# a new, explicitly-named, explicitly-authorized function, never a widening of this table.
LIFE_INTENT_TRANSITIONS: dict[str, set[str]] = {
    "unknown": {"future", "active", "blocked", "waiting", "completed", "abandoned", "superseded"},
    "future": {"active", "blocked", "waiting", "completed", "abandoned", "superseded"},
    "active": {"blocked", "waiting", "completed", "abandoned", "superseded"},
    "blocked": {"active", "waiting", "abandoned", "superseded"},
    "waiting": {"active", "blocked", "abandoned", "superseded"},
    "completed": set(),
    "abandoned": set(),
    "superseded": set(),
}

TERMINAL_LIFE_INTENT_STATES = frozenset({"completed", "abandoned", "superseded"})


@dataclass(frozen=True)
class FeasibilityResult:
    intent_id: uuid.UUID
    actionable: bool
    state: str
    reasons: tuple[dict, ...]
    cycles: tuple[tuple[str, ...], ...]


def _intent(db: Session, owner_id, intent_id, lock=False):
    q = select(LifeIntent).where(
        LifeIntent.id == intent_id, LifeIntent.owner_id == owner_id
    )
    row = db.execute(q.with_for_update() if lock else q).scalar_one_or_none()
    if row is None:
        raise IntentError("intent is missing or belongs to another owner")
    return row


def _event(db, intent, event_type, *, blocker_id=None, actor="unknown", detail=None):
    db.add(
        LifeIntentEvent(
            owner_id=intent.owner_id,
            intent_id=intent.id,
            blocker_id=blocker_id,
            event_type=event_type,
            actor_type=actor,
            detail=detail or {},
        )
    )


def create_intent(
    db: Session,
    *,
    owner_id,
    title,
    intent_kind="unknown",
    state="unknown",
    classification_basis="unknown",
    authority="unknown",
    provenance=None,
    mainai_goal_id=None,
    memory_thread_id=None,
    idempotency_key,
):
    if state not in LIFE_INTENT_TRANSITIONS:
        raise InvalidTransitionError(f"{state!r} is not a recognized LifeIntent state")
    if (
        db.execute(
            select(User.id).where(User.id == owner_id).with_for_update()
        ).scalar_one_or_none()
        is None
    ):
        raise IntentError("owner does not exist")
    existing = db.execute(
        select(LifeIntent).where(
            LifeIntent.owner_id == owner_id,
            LifeIntent.idempotency_key == idempotency_key,
        )
    ).scalar_one_or_none()
    semantic = (
        title,
        intent_kind,
        state,
        classification_basis,
        authority,
        provenance or {},
        mainai_goal_id,
        memory_thread_id,
    )
    if existing:
        if (
            existing.title,
            existing.intent_kind,
            existing.state,
            existing.classification_basis,
            existing.authority,
            existing.provenance,
            existing.mainai_goal_id,
            existing.memory_thread_id,
        ) != semantic:
            raise IntentError("idempotency key reused for a different intent")
        return existing
    if mainai_goal_id:
        try:
            _require_ref(db, owner_id, "mainai_goal", mainai_goal_id)
        except InvalidContextReference as exc:
            raise IntentError(str(exc)) from exc
    if memory_thread_id:
        try:
            _require_ref(db, owner_id, "memory_thread", memory_thread_id)
        except InvalidContextReference as exc:
            raise IntentError(str(exc)) from exc
    row = LifeIntent(
        owner_id=owner_id,
        title=title,
        intent_kind=intent_kind,
        state=state,
        classification_basis=classification_basis,
        authority=authority,
        provenance=provenance or {},
        mainai_goal_id=mainai_goal_id,
        memory_thread_id=memory_thread_id,
        idempotency_key=idempotency_key,
    )
    db.add(row)
    db.flush()
    _event(
        db,
        row,
        "intent_created",
        actor="founder" if classification_basis == "manual" else "system",
    )
    return row


def transition_intent(
    db: Session, *, owner_id, intent_id, state, reason, actor="founder", expected_current_state=None
):
    """Fail-closed LifeIntent state machine (see LIFE_INTENT_TRANSITIONS above).

    `expected_current_state` is an optional optimistic-concurrency/currentness check: if
    given, and the row's ACTUAL current state (read just now, under the row lock) no longer
    matches it, raises StaleTransitionError immediately -- before even considering whether
    the requested transition would otherwise be legal. This is deliberately separate from
    transition-table validation: a caller can be stale (acting on out-of-date information)
    even when the transition it's requesting would, in isolation, be a legal edge in the
    table. Callers that don't need this guarantee may omit it, preserving the existing
    unconditional-transition-table-only behavior other call sites already rely on.

    A same-state call (old == state) remains a harmless no-op that records no new event --
    unchanged from before this fix, and required by
    tests/backend/context/test_goals_dreams_dependencies.py's own
    test_concurrent_state_updates_serialize, which relies on exactly this to assert only ONE
    state_changed event when two concurrent, both-correct actors race to set the same target
    state.
    """
    if not reason.strip():
        raise IntentError("state transition requires a reason")
    if state not in LIFE_INTENT_TRANSITIONS:
        raise InvalidTransitionError(f"{state!r} is not a recognized LifeIntent state")
    row = _intent(db, owner_id, intent_id, lock=True)
    old = row.state
    if expected_current_state is not None and old != expected_current_state:
        raise StaleTransitionError(
            f"caller expected intent {intent_id} to currently be {expected_current_state!r}, but it is "
            f"{old!r} -- another actor already changed it since this caller last read it"
        )
    if old != state:
        if old in TERMINAL_LIFE_INTENT_STATES:
            raise TerminalStateError(f"intent {intent_id} is already terminal ({old!r}); cannot transition to {state!r}")
        if state not in LIFE_INTENT_TRANSITIONS.get(old, set()):
            raise InvalidTransitionError(f"cannot transition intent {intent_id} from {old!r} to {state!r}")
        row.state = state
        row.updated_at = datetime.utcnow()
        if state == "completed":
            row.completed_at = row.updated_at
        _event(
            db,
            row,
            "state_changed",
            actor=actor,
            detail={"old": old, "new": state, "reason": reason},
        )
    return row


def add_blocker(
    db: Session,
    *,
    owner_id,
    intent_id,
    category,
    description,
    idempotency_key,
    basis="unknown",
    reference_kind=None,
    reference_id=None,
    provenance=None,
):
    intent = _intent(db, owner_id, intent_id, lock=True)
    if reference_kind:
        try:
            _require_ref(db, owner_id, reference_kind, reference_id)
        except InvalidContextReference as exc:
            raise IntentError(str(exc)) from exc
    existing = db.execute(
        select(LifeIntentBlocker).where(
            LifeIntentBlocker.owner_id == owner_id,
            LifeIntentBlocker.intent_id == intent.id,
            LifeIntentBlocker.idempotency_key == idempotency_key,
        )
    ).scalar_one_or_none()
    semantic = (
        category,
        description,
        basis,
        reference_kind,
        str(reference_id) if reference_id else None,
        provenance or {},
    )
    if existing:
        if (
            existing.category,
            existing.description,
            existing.basis,
            existing.reference_kind,
            existing.reference_id,
            existing.provenance,
        ) != semantic:
            raise IntentError("idempotency key reused for a different blocker")
        return existing
    blocker = LifeIntentBlocker(
        owner_id=owner_id,
        intent_id=intent.id,
        category=category,
        description=description,
        basis=basis,
        reference_kind=reference_kind,
        reference_id=str(reference_id) if reference_id else None,
        provenance=provenance or {},
        idempotency_key=idempotency_key,
    )
    db.add(blocker)
    db.flush()
    _event(
        db,
        intent,
        "blocker_added",
        blocker_id=blocker.id,
        actor="founder" if basis == "manual" else "system",
        detail={"category": category},
    )
    return blocker


def resolve_blocker(
    db: Session, *, owner_id, blocker_id, status="resolved", reason, actor="founder"
):
    blocker = db.execute(
        select(LifeIntentBlocker)
        .where(
            LifeIntentBlocker.id == blocker_id, LifeIntentBlocker.owner_id == owner_id
        )
        .with_for_update()
    ).scalar_one_or_none()
    if (
        blocker is None
        or status not in {"resolved", "superseded", "invalidated"}
        or not reason.strip()
    ):
        raise IntentError("valid blocker resolution and reason required")
    if blocker.status == "active":
        blocker.status = status
        blocker.resolved_at = datetime.utcnow()
        blocker.resolution_reason = reason
        intent = _intent(db, owner_id, blocker.intent_id)
        _event(
            db,
            intent,
            f"blocker_{status}",
            blocker_id=blocker.id,
            actor=actor,
            detail={"reason": reason},
        )
    return blocker


def add_dependency(
    db: Session,
    *,
    owner_id,
    from_intent_id,
    to_intent_id,
    relationship_type,
    idempotency_key,
    basis="unknown",
    provenance=None,
):
    if from_intent_id == to_intent_id:
        raise IntentError("self dependency is invalid")
    ids = sorted((from_intent_id, to_intent_id), key=str)
    rows = list(
        db.execute(
            select(LifeIntent)
            .where(LifeIntent.owner_id == owner_id, LifeIntent.id.in_(ids))
            .order_by(LifeIntent.id)
            .with_for_update()
        ).scalars()
    )
    if len(rows) != 2:
        raise IntentError("dependency endpoint is missing or belongs to another owner")
    existing = db.execute(
        select(LifeIntentDependency).where(
            LifeIntentDependency.owner_id == owner_id,
            LifeIntentDependency.idempotency_key == idempotency_key,
        )
    ).scalar_one_or_none()
    semantic = (
        from_intent_id,
        to_intent_id,
        relationship_type,
        basis,
        provenance or {},
    )
    if existing:
        if (
            existing.from_intent_id,
            existing.to_intent_id,
            existing.relationship_type,
            existing.basis,
            existing.provenance,
        ) != semantic:
            raise IntentError("idempotency key reused for a different dependency")
        return existing
    dep = LifeIntentDependency(
        owner_id=owner_id,
        from_intent_id=from_intent_id,
        to_intent_id=to_intent_id,
        relationship_type=relationship_type,
        basis=basis,
        provenance=provenance or {},
        idempotency_key=idempotency_key,
    )
    db.add(dep)
    db.flush()
    source = next(r for r in rows if r.id == from_intent_id)
    _event(
        db,
        source,
        "dependency_added",
        actor="founder" if basis == "manual" else "system",
        detail={"to": str(to_intent_id), "relationship_type": relationship_type},
    )
    return dep


def evaluate_feasibility(
    db: Session, *, owner_id, intent_id, max_depth=20, max_nodes=200
):
    if not (0 <= max_depth <= 50 and 1 <= max_nodes <= 1000):
        raise IntentError("feasibility bounds outside supported limits")
    root = _intent(db, owner_id, intent_id)
    reasons = []
    cycles = []
    seen = set()
    non_actionable = {
        "blocked",
        "waiting",
        "future",
        "completed",
        "abandoned",
        "superseded",
        "unknown",
    }

    def walk(node, path, depth):
        if len(seen) >= max_nodes or depth > max_depth:
            reasons.append({"path": path, "reason": "bound_reached"})
            return
        key = str(node.id)
        if key in path:
            cycle = tuple([*path, key])
            cycles.append(cycle)
            reasons.append({"path": cycle, "reason": "dependency_cycle"})
            return
        seen.add(key)
        new_path = (*path, key)
        if node.state in non_actionable:
            reasons.append({"path": new_path, "reason": f"state:{node.state}"})
        blockers = db.execute(
            select(LifeIntentBlocker).where(
                LifeIntentBlocker.owner_id == owner_id,
                LifeIntentBlocker.intent_id == node.id,
                LifeIntentBlocker.status == "active",
            )
        ).scalars()
        for b in blockers:
            reasons.append(
                {
                    "path": new_path,
                    "reason": "active_blocker",
                    "blocker_id": str(b.id),
                    "category": b.category,
                }
            )
        deps = db.execute(
            select(LifeIntentDependency).where(
                LifeIntentDependency.owner_id == owner_id,
                LifeIntentDependency.from_intent_id == node.id,
                LifeIntentDependency.relationship_type == "requires",
            )
        ).scalars()
        for dep in deps:
            walk(_intent(db, owner_id, dep.to_intent_id), new_path, depth + 1)

    walk(root, tuple(), 0)
    return FeasibilityResult(
        root.id, not reasons, root.state, tuple(reasons), tuple(cycles)
    )


def list_actionable(db: Session, *, owner_id):
    rows = db.execute(
        select(LifeIntent).where(
            LifeIntent.owner_id == owner_id, LifeIntent.state == "active"
        )
    ).scalars()
    return [
        row
        for row in rows
        if evaluate_feasibility(db, owner_id=owner_id, intent_id=row.id).actionable
    ]
