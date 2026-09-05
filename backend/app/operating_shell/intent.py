"""Intent Objects + continuity (MainAI V2, Stage V2-I3).

RAW USER EXPRESSION != INTERPRETED TRUTH: create_intent_from_expression() sets ONLY
raw_user_expression; interpreted_goal stays None until record_understanding() is called
separately, and advance_to_active() refuses to leave UNDERSTANDING without a real
interpreted_goal having been set.

FUTURE PLAN != FUTURE AUTHORITY: next_actions holds WorkspaceAction references (pure data,
see types.py) -- nothing here executes them or treats their presence as authorization.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from app.operating_shell.types import (
    AmbiguousResolution,
    IntentHistoryEntry,
    IntentObject,
    IntentState,
    MalformedIntentSnapshotError,
    NON_TERMINAL_INTENT_STATES,
    TERMINAL_INTENT_STATES,
    WorkspaceAction,
    WorkspaceTarget,
)

# CAPTURED -> UNDERSTANDING -> PLANNED -> ACTIVE -> (WAITING/BLOCKED/PAUSED <-> ACTIVE)
#   -> COMPLETED/ABANDONED. SUPERSEDED reachable from any non-terminal state via
# supersede_intent(). No transition skips UNDERSTANDING on the way to ACTIVE.
_VALID_TRANSITIONS: dict[IntentState, frozenset[IntentState]] = {
    IntentState.CAPTURED: frozenset({IntentState.UNDERSTANDING, IntentState.ABANDONED, IntentState.SUPERSEDED}),
    IntentState.UNDERSTANDING: frozenset({IntentState.PLANNED, IntentState.ABANDONED, IntentState.SUPERSEDED}),
    IntentState.PLANNED: frozenset({IntentState.ACTIVE, IntentState.ABANDONED, IntentState.SUPERSEDED}),
    IntentState.ACTIVE: frozenset(
        {IntentState.WAITING, IntentState.BLOCKED, IntentState.PAUSED, IntentState.COMPLETED, IntentState.ABANDONED, IntentState.SUPERSEDED}
    ),
    IntentState.WAITING: frozenset({IntentState.ACTIVE, IntentState.ABANDONED, IntentState.SUPERSEDED}),
    IntentState.BLOCKED: frozenset({IntentState.ACTIVE, IntentState.ABANDONED, IntentState.SUPERSEDED}),
    IntentState.PAUSED: frozenset({IntentState.ACTIVE, IntentState.ABANDONED, IntentState.SUPERSEDED}),
    IntentState.COMPLETED: frozenset(),
    IntentState.ABANDONED: frozenset(),
    IntentState.SUPERSEDED: frozenset(),
}


class IntentTransitionError(ValueError):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _transition(intent: IntentObject, *, to_state: IntentState, note: str) -> IntentObject:
    if to_state not in _VALID_TRANSITIONS.get(intent.state, frozenset()):
        raise IntentTransitionError(f"cannot transition intent {intent.intent_id} from {intent.state.value} to {to_state.value}")
    now = _utcnow()
    intent.history = (*intent.history, IntentHistoryEntry(from_state=intent.state, to_state=to_state, at=now, note=note))
    intent.state = to_state
    intent.updated_at = now
    return intent


def create_intent_from_expression(*, owner_id: uuid.UUID, title: str, raw_user_expression: str) -> IntentObject:
    """Sets ONLY raw_user_expression -- interpreted_goal stays None (RAW USER EXPRESSION !=
    INTERPRETED TRUTH). State starts at CAPTURED."""
    now = _utcnow()
    intent = IntentObject(
        intent_id=uuid.uuid4(), owner_id=owner_id, title=title, raw_user_expression=raw_user_expression,
        state=IntentState.CAPTURED, created_at=now, updated_at=now,
    )
    intent.history = (IntentHistoryEntry(from_state=None, to_state=IntentState.CAPTURED, at=now, note="captured"),)
    return intent


def record_understanding(intent: IntentObject, *, interpreted_goal: str, confidence: float | None = None) -> IntentObject:
    """The ONLY function that sets interpreted_goal. Transitions CAPTURED -> UNDERSTANDING."""
    if not interpreted_goal.strip():
        raise ValueError("interpreted_goal must be a real, non-empty interpretation, not blank")
    _transition(intent, to_state=IntentState.UNDERSTANDING, note="understanding recorded")
    intent.interpreted_goal = interpreted_goal
    intent.confidence = confidence
    return intent


def advance_to_planned(intent: IntentObject, *, next_actions: tuple[WorkspaceAction, ...] = ()) -> IntentObject:
    """Refuses to leave UNDERSTANDING for PLANNED without a real interpreted_goal already
    set -- the state machine's own transition table already prevents CAPTURED -> PLANNED
    directly, but this adds a second, explicit content check (defense in depth against a
    future transition-table edit accidentally widening it)."""
    if not intent.interpreted_goal:
        raise IntentTransitionError(f"intent {intent.intent_id} cannot be PLANNED without a real interpreted_goal set first")
    _transition(intent, to_state=IntentState.PLANNED, note="planned")
    intent.next_actions = next_actions
    return intent


def advance_to_active(intent: IntentObject) -> IntentObject:
    return _transition(intent, to_state=IntentState.ACTIVE, note="activated")


def mark_blocked(intent: IntentObject, *, reason: str) -> IntentObject:
    from app.operating_shell.types import IntentBlocker

    _transition(intent, to_state=IntentState.BLOCKED, note=f"blocked: {reason}")
    intent.blockers = (*intent.blockers, IntentBlocker(description=reason))
    return intent


def unblock(intent: IntentObject) -> IntentObject:
    return _transition(intent, to_state=IntentState.ACTIVE, note="unblocked")


def complete(intent: IntentObject, *, summary: str) -> IntentObject:
    _transition(intent, to_state=IntentState.COMPLETED, note="completed")
    intent.current_summary = summary
    return intent


def abandon(intent: IntentObject, *, reason: str) -> IntentObject:
    _transition(intent, to_state=IntentState.ABANDONED, note=f"abandoned: {reason}")
    return intent


def supersede_intent(old: IntentObject, new: IntentObject) -> IntentObject:
    """Nothing is deleted -- `old` remains queryable by id, but is excluded from
    "what's active" queries once superseded_by is set."""
    if old.state in TERMINAL_INTENT_STATES:
        raise IntentTransitionError(f"intent {old.intent_id} is already terminal ({old.state.value}), cannot be superseded again")
    _transition(old, to_state=IntentState.SUPERSEDED, note=f"superseded by {new.intent_id}")
    old.superseded_by = new.intent_id
    return old


def has_authority_from_next_action(intent: IntentObject) -> bool:
    """FUTURE PLAN != FUTURE AUTHORITY: always False. Recording a next_action never itself
    constitutes authorization to execute it -- a caller must go through
    risk.evaluate_action_authority() from scratch when it's actually time to act."""
    return False


# --- Continuity: query + resolve-by-reference. ----------------------------------------------


def active_intents_for_owner(intents: tuple[IntentObject, ...], *, owner_id: uuid.UUID) -> tuple[IntentObject, ...]:
    """Cross-owner leakage impossible: filters strictly on owner_id, and excludes
    terminal/superseded intents from "what's active right now."""
    return tuple(
        i for i in intents
        if i.owner_id == owner_id and i.state in NON_TERMINAL_INTENT_STATES
    )


def resolve_intent_by_title_fragment(
    intents: tuple[IntentObject, ...], *, owner_id: uuid.UUID, fragment: str
) -> IntentObject | AmbiguousResolution:
    """DO NOT silently guess: if more than one of this owner's non-terminal intents
    contains `fragment` (case-insensitive substring match on title), returns an
    AmbiguousResolution listing every match rather than picking one."""
    fragment_lower = fragment.lower()
    candidates = [
        i for i in intents
        if i.owner_id == owner_id and i.state in NON_TERMINAL_INTENT_STATES and fragment_lower in i.title.lower()
    ]
    if not candidates:
        raise ValueError(f"no active intent matches fragment {fragment!r} for this owner")
    if len(candidates) > 1:
        return AmbiguousResolution(
            reference_kind="CONTINUE",
            candidates=tuple(WorkspaceTarget(target_id=i.intent_id, kind="intent", title=i.title) for i in candidates),
            reason=f"{len(candidates)} active intents match {fragment!r}",
        )
    return candidates[0]


def find_missing_dependencies(intents: tuple[IntentObject, ...]) -> dict[uuid.UUID, tuple[uuid.UUID, ...]]:
    """Detects (does not silently ignore) a dependency reference pointing to a nonexistent
    intent_id. Returns {intent_id: (missing_dependency_ids,)} for every intent with at
    least one dangling dependency."""
    known_ids = {i.intent_id for i in intents}
    missing: dict[uuid.UUID, tuple[uuid.UUID, ...]] = {}
    for intent in intents:
        gaps = tuple(dep for dep in intent.dependencies if dep not in known_ids)
        if gaps:
            missing[intent.intent_id] = gaps
    return missing


def detect_dependency_cycle(intents: tuple[IntentObject, ...]) -> tuple[uuid.UUID, ...] | None:
    """Bounded cycle detection over the dependency graph -- terminates even if a real cycle
    exists (never infinite-loops). Returns the cycle (as a tuple of intent_ids) if found,
    else None. Also raises IntentGraphError via the caller's own missing-dependency check
    (see find_missing_dependencies) -- this function assumes a well-formed node set and
    focuses only on cycle detection."""
    by_id = {i.intent_id: i for i in intents}
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[uuid.UUID, int] = {i.intent_id: WHITE for i in intents}
    path: list[uuid.UUID] = []

    def visit(node_id: uuid.UUID) -> tuple[uuid.UUID, ...] | None:
        color[node_id] = GRAY
        path.append(node_id)
        node = by_id.get(node_id)
        if node is not None:
            for dep in node.dependencies:
                if dep not in color:
                    continue  # unknown dependency -- handled by find_missing_dependencies
                if color[dep] == GRAY:
                    cycle_start = path.index(dep)
                    return tuple(path[cycle_start:]) + (dep,)
                if color[dep] == WHITE:
                    found = visit(dep)
                    if found is not None:
                        return found
        path.pop()
        color[node_id] = BLACK
        return None

    for intent in intents:
        if color[intent.intent_id] == WHITE:
            result = visit(intent.intent_id)
            if result is not None:
                return result
    return None


# --- Snapshot round-trip (a single IntentObject; a caller wraps a collection of these). ----


def _history_to_dict(h: IntentHistoryEntry) -> dict:
    return {"from_state": h.from_state.value if h.from_state else None, "to_state": h.to_state.value, "at": h.at.isoformat(), "note": h.note}


def _history_from_dict(d: dict) -> IntentHistoryEntry:
    return IntentHistoryEntry(
        from_state=IntentState(d["from_state"]) if d["from_state"] else None,
        to_state=IntentState(d["to_state"]), at=datetime.fromisoformat(d["at"]), note=d["note"],
    )


def to_snapshot(intent: IntentObject) -> dict:
    return {
        "intent_id": str(intent.intent_id), "owner_id": str(intent.owner_id), "title": intent.title,
        "raw_user_expression": intent.raw_user_expression, "interpreted_goal": intent.interpreted_goal,
        "state": intent.state.value, "created_at": intent.created_at.isoformat(), "updated_at": intent.updated_at.isoformat(),
        "history": [_history_to_dict(h) for h in intent.history],
        "dependencies": [str(d) for d in intent.dependencies],
        "superseded_by": str(intent.superseded_by) if intent.superseded_by else None,
        "current_summary": intent.current_summary, "confidence": intent.confidence,
    }


_REQUIRED_INTENT_SNAPSHOT_KEYS = {"intent_id", "owner_id", "title", "raw_user_expression", "state", "created_at", "updated_at"}


def from_snapshot(snapshot: dict) -> IntentObject:
    missing = _REQUIRED_INTENT_SNAPSHOT_KEYS - snapshot.keys()
    if missing:
        raise MalformedIntentSnapshotError(f"intent snapshot missing required keys: {sorted(missing)}")
    try:
        return IntentObject(
            intent_id=uuid.UUID(snapshot["intent_id"]), owner_id=uuid.UUID(snapshot["owner_id"]), title=snapshot["title"],
            raw_user_expression=snapshot["raw_user_expression"], interpreted_goal=snapshot.get("interpreted_goal"),
            state=IntentState(snapshot["state"]), created_at=datetime.fromisoformat(snapshot["created_at"]),
            updated_at=datetime.fromisoformat(snapshot["updated_at"]),
            history=tuple(_history_from_dict(h) for h in snapshot.get("history", [])),
            dependencies=tuple(uuid.UUID(d) for d in snapshot.get("dependencies", [])),
            superseded_by=uuid.UUID(snapshot["superseded_by"]) if snapshot.get("superseded_by") else None,
            current_summary=snapshot.get("current_summary", ""), confidence=snapshot.get("confidence"),
        )
    except (KeyError, ValueError, TypeError) as exc:
        raise MalformedIntentSnapshotError(f"intent snapshot is malformed: {exc}") from exc
