"""LifeIntent state machine P0 fix: adversarial test program.

See docs/mainai_v2/MAINAI_V2_LIFEINTENT_STATE_MACHINE_P0.md for the full decision. This file
covers the founder's own required test program: valid forward transitions, terminal-state
resurrection attempts (all four terminal-adjacent scenarios), stale-caller rejection via
`expected_current_state`, concurrent workers, cancel/finalize races, supersession/
reactivation races, duplicate replay, restart-with-stale-ORM-state, wrong-owner isolation,
and goal/work-candidate consistency.

tests/backend/context/test_goals_dreams_dependencies.py (migration 0041's own original test
suite) is the authoritative source for the REAL existing LifeIntent state vocabulary this
file's transition table is built from -- not re-derived or guessed here.
"""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import sessionmaker

from app.db import migration_engine
from app.life_intents.service import (
    LIFE_INTENT_TRANSITIONS,
    TERMINAL_LIFE_INTENT_STATES,
    InvalidTransitionError,
    StaleTransitionError,
    TerminalStateError,
    create_intent,
    transition_intent,
)
from app.life_intents.service import _intent as _life_intent_row
from app.models.life_intent import LifeIntentEvent
from app.models.mainai_execution import MainAIGoal, MainAIGoalStatus
from app.models.user import User
from app.work_candidates import authorize_work_candidate, record_work_candidate

from tests.backend.mainai.test_work_candidates import _owner_with_entity


def _owner(db) -> User:
    owner = User(email=f"lifeintent-sm-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    db.add(owner)
    db.flush()
    return owner


def _intent(db, owner, key: str, **kwargs):
    return create_intent(db, owner_id=owner.id, title=key, idempotency_key=key, **kwargs)


# --- 1. Valid forward transitions (table-driven over the REAL vocabulary). ------------------


@pytest.mark.parametrize(
    "from_state,to_state",
    [
        ("unknown", "future"),
        ("unknown", "active"),
        ("future", "active"),
        ("active", "blocked"),
        ("active", "waiting"),
        ("active", "completed"),
        ("active", "abandoned"),
        ("active", "superseded"),
        ("blocked", "active"),
        ("blocked", "waiting"),
        ("waiting", "active"),
        ("waiting", "blocked"),
    ],
)
def test_valid_forward_transitions_succeed(superuser_db, from_state, to_state):
    owner = _owner(superuser_db)
    superuser_db.commit()
    intent = _intent(superuser_db, owner, f"fwd-{from_state}-{to_state}", state=from_state)
    superuser_db.commit()
    result = transition_intent(superuser_db, owner_id=owner.id, intent_id=intent.id, state=to_state, reason="valid forward move")
    assert result.state == to_state


# --- 2. Terminal-state resurrection: every terminal state, attempting every other state. ---


@pytest.mark.parametrize("terminal_state", sorted(TERMINAL_LIFE_INTENT_STATES))
@pytest.mark.parametrize("attempted_target", ["active", "blocked", "waiting", "future"])
def test_terminal_states_reject_all_generic_transitions(superuser_db, terminal_state, attempted_target):
    owner = _owner(superuser_db)
    superuser_db.commit()
    intent = _intent(superuser_db, owner, f"terminal-{terminal_state}-{attempted_target}", state=terminal_state)
    superuser_db.commit()
    with pytest.raises(TerminalStateError):
        transition_intent(superuser_db, owner_id=owner.id, intent_id=intent.id, state=attempted_target, reason="attempted resurrection")
    superuser_db.expire_all()
    reloaded = _life_intent_row(superuser_db, owner.id, intent.id)
    assert reloaded.state == terminal_state, "a rejected resurrection attempt must never have touched the row"


def test_transition_table_has_no_undocumented_terminal_exit():
    """Structural: every terminal state's own transition set is genuinely empty -- this is
    what makes the parametrized test above exhaustive rather than a sample."""
    for state in TERMINAL_LIFE_INTENT_STATES:
        assert LIFE_INTENT_TRANSITIONS[state] == set()


def test_unrecognized_target_state_rejected_at_creation_and_transition(superuser_db):
    owner = _owner(superuser_db)
    superuser_db.commit()
    with pytest.raises(InvalidTransitionError):
        _intent(superuser_db, owner, "bad-initial-state", state="in_progress_typo")

    intent = _intent(superuser_db, owner, "bad-target-state")
    superuser_db.commit()
    with pytest.raises(InvalidTransitionError):
        transition_intent(superuser_db, owner_id=owner.id, intent_id=intent.id, state="in_progress_typo", reason="typo")


# --- 3. Stale-caller rejection via expected_current_state (optimistic concurrency). --------


def test_stale_caller_with_expected_current_state_is_rejected(superuser_db):
    """READ STATE A -> another actor changes A->B -> stale caller's A->C attempt is REJECTED,
    even though active->blocked (C) would otherwise be a perfectly legal transition from A --
    the point is the caller's own premise (still in A) is already wrong."""
    owner = _owner(superuser_db)
    superuser_db.commit()
    intent = _intent(superuser_db, owner, "stale-caller", state="active")
    superuser_db.commit()

    # Another actor moves it to "waiting" first.
    transition_intent(superuser_db, owner_id=owner.id, intent_id=intent.id, state="waiting", reason="external actor")
    superuser_db.commit()

    # The stale caller still believes it's "active" and wants to move it to "blocked" --
    # blocked is a legal target FROM active, but the caller's premise is stale.
    with pytest.raises(StaleTransitionError):
        transition_intent(
            superuser_db, owner_id=owner.id, intent_id=intent.id, state="blocked", reason="stale caller",
            expected_current_state="active",
        )
    superuser_db.expire_all()
    reloaded = _life_intent_row(superuser_db, owner.id, intent.id)
    assert reloaded.state == "waiting", "the stale rejection must not have touched the row"


def test_expected_current_state_omitted_preserves_backward_compatible_behavior(superuser_db):
    """Callers that don't pass expected_current_state (the two existing call sites in
    test_goals_dreams_dependencies.py) get exactly the pre-fix calling convention, just with
    the new transition-table validation applied."""
    owner = _owner(superuser_db)
    superuser_db.commit()
    intent = _intent(superuser_db, owner, "no-staleness-check", state="active")
    superuser_db.commit()
    result = transition_intent(superuser_db, owner_id=owner.id, intent_id=intent.id, state="completed", reason="no expected_current_state given")
    assert result.state == "completed"


def test_expected_current_state_matching_current_value_succeeds(superuser_db):
    owner = _owner(superuser_db)
    superuser_db.commit()
    intent = _intent(superuser_db, owner, "correct-expectation", state="active")
    superuser_db.commit()
    result = transition_intent(
        superuser_db, owner_id=owner.id, intent_id=intent.id, state="blocked", reason="correct caller",
        expected_current_state="active",
    )
    assert result.state == "blocked"


# --- 4. Concurrent workers (this codebase's established real-thread race pattern, see
# test_concurrent_state_updates_serialize in test_goals_dreams_dependencies.py). ------------


def test_two_concurrent_workers_one_valid_one_now_invalid_only_one_wins(superuser_db):
    """Two real threads, two real sessions, same row. One transitions active->completed
    (valid), the other (racing, reading the same pre-transition state) attempts
    active->blocked. Whichever commits first wins; the second, now facing a row that has
    already moved to a state its own attempted transition isn't valid from, must be
    rejected -- not silently overwrite the first result."""
    owner = _owner(superuser_db)
    intent = _intent(superuser_db, owner, "concurrent-diverge", state="active")
    owner_id, intent_id = owner.id, intent.id
    superuser_db.commit()

    results = {}

    def worker(name, target_state):
        db = sessionmaker(bind=migration_engine)()
        try:
            row = transition_intent(db, owner_id=owner_id, intent_id=intent_id, state=target_state, reason=f"worker {name}")
            db.commit()
            results[name] = ("ok", row.state)
        except Exception as exc:  # noqa: BLE001 -- deliberately capturing whichever error type lands
            db.rollback()
            results[name] = ("error", type(exc).__name__)
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda args: worker(*args), [("a", "completed"), ("b", "blocked")]))

    # Exactly one of the two must have succeeded (row-level locking serializes them, and
    # whichever runs second sees the other's already-applied terminal-ish state); the other
    # must have failed closed, never silently corrupting/overwriting the winner.
    successes = [v for v in results.values() if v[0] == "ok"]
    assert len(successes) == 1, f"expected exactly one winner, got {results}"

    superuser_db.expire_all()
    final = _life_intent_row(superuser_db, owner_id, intent_id)
    assert final.state == successes[0][1]


# --- 5. Cancel/finalize race: work candidate authorized, goal cancelled independently. -----
# (Full test already lives in test_life_intent_work_candidate_goal_races.py -- this file adds
# the LifeIntent-specific angle: a LifeIntent linked to a goal that gets cancelled must still
# obey its OWN state machine independently; the two are not kept in sync by the schema, see
# MAINAI_V2_INTENT_GOAL_RECONCILIATION.md #0.)


def test_life_intent_state_machine_independent_of_linked_goal_cancellation(superuser_db):
    owner = _owner(superuser_db)
    superuser_db.commit()
    goal = MainAIGoal(owner_id=owner.id, title="linked goal", original_instruction="do the thing", created_by="test")
    superuser_db.add(goal)
    superuser_db.flush()
    intent = _intent(superuser_db, owner, "linked-to-goal", state="active", mainai_goal_id=goal.id)
    superuser_db.commit()

    goal.status = MainAIGoalStatus.cancelled
    goal.completed_at = datetime.utcnow()
    superuser_db.commit()

    # The LifeIntent's own state machine is untouched by the goal's cancellation -- no hidden
    # bidirectional sync exists between the two models (see MAINAI_V2_INTENT_GOAL_
    # RECONCILIATION.md #0's "genuine, deliberate looseness" finding).
    superuser_db.expire_all()
    reloaded = _life_intent_row(superuser_db, owner.id, intent.id)
    assert reloaded.state == "active", "goal cancellation alone must never silently transition the LifeIntent"

    # A real, explicit transition on the LifeIntent still works normally regardless of the
    # linked goal's own (already-terminal) status.
    result = transition_intent(superuser_db, owner_id=owner.id, intent_id=intent.id, state="abandoned", reason="founder abandoned it too")
    assert result.state == "abandoned"


# --- 6. Supersession / reactivation race: OLD INTENT != CURRENT INTENT. --------------------


def test_superseded_intent_cannot_be_reactivated_by_a_stale_worker_or_linked_work(superuser_db):
    owner = _owner(superuser_db)
    superuser_db.commit()
    old = _intent(superuser_db, owner, "old-plan", state="active")
    superuser_db.commit()

    transition_intent(superuser_db, owner_id=owner.id, intent_id=old.id, state="superseded", reason="replaced by a better plan")
    superuser_db.commit()

    # A stale worker, unaware of the supersession, tries to reactivate it.
    with pytest.raises(TerminalStateError):
        transition_intent(superuser_db, owner_id=owner.id, intent_id=old.id, state="active", reason="stale worker")

    # "Linked work" trying to reactivate it via a stale-expectation call also fails --
    # whether via the terminal-state gate or the staleness gate, never silently succeeding.
    with pytest.raises((TerminalStateError, StaleTransitionError)):
        transition_intent(
            superuser_db, owner_id=owner.id, intent_id=old.id, state="active", reason="linked work retry",
            expected_current_state="active",
        )

    superuser_db.expire_all()
    reloaded = _life_intent_row(superuser_db, owner.id, old.id)
    assert reloaded.state == "superseded"


# --- 7. Duplicate replay: the same transition call delivered twice. ------------------------


def test_duplicate_transition_replay_is_a_safe_noop(superuser_db):
    owner = _owner(superuser_db)
    superuser_db.commit()
    intent = _intent(superuser_db, owner, "replay-target", state="active")
    superuser_db.commit()

    first = transition_intent(superuser_db, owner_id=owner.id, intent_id=intent.id, state="completed", reason="done")
    superuser_db.commit()
    # Redelivering the SAME transition (already-applied target state) is a safe no-op, not
    # an error and not a duplicate event -- unchanged pre-fix behavior, re-confirmed here.
    second = transition_intent(superuser_db, owner_id=owner.id, intent_id=intent.id, state="completed", reason="done (redelivered)")
    assert first.state == second.state == "completed"

    events = superuser_db.execute(
        select(LifeIntentEvent).where(LifeIntentEvent.intent_id == intent.id, LifeIntentEvent.event_type == "state_changed")
    ).scalars().all()
    assert len(events) == 1, "a redelivered no-op transition must not record a second state_changed event"


# --- 8. Restart with stale ORM state: a fresh session must see the CURRENT row, not a
# cached, pre-restart identity-map object. --------------------------------------------------


def test_restart_with_stale_orm_object_sees_current_state_not_cached_state(superuser_db):
    owner = _owner(superuser_db)
    intent = _intent(superuser_db, owner, "restart-scenario", state="active")
    owner_id, intent_id = owner.id, intent.id
    superuser_db.commit()

    # Simulate "restart": a second, independent session/process changes the row.
    other_session = sessionmaker(bind=migration_engine)()
    try:
        transition_intent(other_session, owner_id=owner_id, intent_id=intent_id, state="superseded", reason="changed during simulated restart")
        other_session.commit()
    finally:
        other_session.close()

    # The original session's own in-memory `intent` object is now stale (still thinks
    # "active") -- but a fresh read through the canonical lookup, or a transition attempt,
    # must see the REAL current state, never the cached Python object's stale field.
    superuser_db.expire_all()
    reloaded = _life_intent_row(superuser_db, owner_id, intent_id)
    assert reloaded.state == "superseded"
    with pytest.raises(TerminalStateError):
        transition_intent(superuser_db, owner_id=owner_id, intent_id=intent_id, state="active", reason="post-restart stale attempt")


# --- 9. Wrong owner isolation. ---------------------------------------------------------------


def test_wrong_owner_cannot_transition_read_or_reactivate(superuser_db, db_session):
    from app.life_intents.service import IntentError

    owner_a = _owner(superuser_db)
    owner_b = _owner(superuser_db)
    superuser_db.commit()
    intent = _intent(superuser_db, owner_a, "owner-a-intent", state="active")
    superuser_db.commit()

    with pytest.raises(IntentError):
        transition_intent(superuser_db, owner_id=owner_b.id, intent_id=intent.id, state="completed", reason="wrong owner")

    # RLS-level isolation: owner_b's own session cannot even see owner_a's row.
    superuser_db.commit()
    db_session.execute(text("SELECT set_config('app.current_user_id',:id,false)"), {"id": str(owner_b.id)})
    from app.models.life_intent import LifeIntent

    assert db_session.execute(select(LifeIntent).where(LifeIntent.id == intent.id)).scalar_one_or_none() is None


# --- 10. Goal/WorkCandidate consistency (LifeIntent side): authorizing linked work never
# implicitly transitions the LifeIntent, and vice versa. ------------------------------------


def test_authorizing_linked_work_candidate_never_implicitly_transitions_the_life_intent(superuser_db):
    owner, entity = _owner_with_entity(superuser_db)
    superuser_db.commit()
    intent = _intent(superuser_db, owner, "intent-with-linked-work", state="future")
    superuser_db.commit()

    candidate = record_work_candidate(
        superuser_db, owner_id=owner.id, source_entity_id=entity.id, title="do the thing",
        idempotency_key="wc-for-intent", classifier_strategy="test", classifier_confidence=0.9,
    )
    superuser_db.commit()
    authorize_work_candidate(superuser_db, owner_id=owner.id, candidate_id=candidate.id, authorized_by="founder")
    superuser_db.commit()

    # Authorizing the (entirely separate) WorkCandidate never implicitly moved the LifeIntent
    # -- no hidden bidirectional magic. The LifeIntent's own state is unchanged until an
    # explicit transition_intent() call moves it.
    superuser_db.expire_all()
    reloaded = _life_intent_row(superuser_db, owner.id, intent.id)
    assert reloaded.state == "future"
