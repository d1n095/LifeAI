"""Canonical projection tests (MainAI V2 Intent/Goal architecture reconciliation).

Real Postgres, via `superuser_db` -- unlike every other app.operating_shell test file (which
is pure in-memory), this one genuinely reads real LifeIntent/MainAIGoal rows, because
project_from_life_intent()/project_from_mainai_goal() are real DB-touching code by design.
See docs/mainai_v2/MAINAI_V2_INTENT_GOAL_RECONCILIATION.md.
"""

import uuid

import pytest

from app.life_intents.service import create_intent, transition_intent
from app.mainai_execution.planner import create_goal
from app.models.mainai_execution import MainAIGoalStatus
from app.models.user import User
from app.operating_shell import CanonicalKind, CanonicalProjectionError, IntentState
from app.operating_shell.canonical_projection import project_from_life_intent, project_from_mainai_goal


def _user(db) -> User:
    user = User(email=f"reconcile-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    db.add(user)
    db.flush()
    return user


def _life_intent(db, owner, *, state="unknown", title="Fixa skulderna"):
    return create_intent(db, owner_id=owner.id, title=title, state=state, idempotency_key=f"li-{uuid.uuid4()}")


def _goal(db, owner, *, status=MainAIGoalStatus.pending, title="Migrate to Postgres"):
    from datetime import datetime

    from app.models.mainai_execution import TERMINAL_MAINAI_GOAL_STATUSES

    goal = create_goal(db, owner_id=owner.id, title=title, original_instruction="Migrera databasen till Postgres.", created_by="test")
    if status != MainAIGoalStatus.pending:
        goal.status = status
        # A real DB CHECK constraint (ck_mainai_goals_completed_at_matches_terminal_status)
        # requires completed_at to be set for any terminal status -- match real production
        # behavior rather than fighting the schema in a test.
        if status in TERMINAL_MAINAI_GOAL_STATUSES:
            goal.completed_at = datetime.utcnow()
        db.flush()
    return goal


# --- LifeIntent state mapping, table-driven, all 7 real states. ---------------------------

_LIFE_INTENT_STATE_CASES = [
    ("active", IntentState.ACTIVE),
    ("blocked", IntentState.BLOCKED),
    ("waiting", IntentState.WAITING),
    ("future", IntentState.PLANNED),
    ("completed", IntentState.COMPLETED),
    ("abandoned", IntentState.ABANDONED),
    ("superseded", IntentState.SUPERSEDED),
    ("unknown", IntentState.CAPTURED),
]


@pytest.mark.parametrize("life_intent_state,expected", _LIFE_INTENT_STATE_CASES)
def test_project_from_life_intent_maps_every_real_state(superuser_db, life_intent_state, expected):
    owner = _user(superuser_db)
    life_intent = _life_intent(superuser_db, owner, state=life_intent_state)
    superuser_db.commit()

    projected = project_from_life_intent(superuser_db, owner_id=owner.id, life_intent_id=life_intent.id)

    assert projected.state == expected
    assert projected.canonical_kind == CanonicalKind.LIFE_INTENT
    assert projected.canonical_ref == life_intent.id
    assert projected.owner_id == owner.id


# --- MainAIGoal status mapping, table-driven, all 8 real statuses. -------------------------

_MAINAI_GOAL_STATUS_CASES = [
    (MainAIGoalStatus.pending, IntentState.PLANNED),
    (MainAIGoalStatus.planning, IntentState.PLANNED),
    (MainAIGoalStatus.running, IntentState.ACTIVE),
    (MainAIGoalStatus.waiting, IntentState.WAITING),
    (MainAIGoalStatus.blocked, IntentState.BLOCKED),
    (MainAIGoalStatus.failed, IntentState.ABANDONED),
    (MainAIGoalStatus.completed, IntentState.COMPLETED),
    (MainAIGoalStatus.cancelled, IntentState.ABANDONED),
]


@pytest.mark.parametrize("goal_status,expected", _MAINAI_GOAL_STATUS_CASES)
def test_project_from_mainai_goal_maps_every_real_status(superuser_db, goal_status, expected):
    owner = _user(superuser_db)
    goal = _goal(superuser_db, owner, status=goal_status)
    superuser_db.commit()

    projected = project_from_mainai_goal(superuser_db, owner_id=owner.id, goal_id=goal.id)

    assert projected.state == expected
    assert projected.canonical_kind == CanonicalKind.MAINAI_GOAL
    assert projected.canonical_ref == goal.id
    assert projected.raw_user_expression == goal.original_instruction


# --- Missing / cross-owner. -----------------------------------------------------------------


def test_project_from_life_intent_raises_for_nonexistent_id(superuser_db):
    owner = _user(superuser_db)
    superuser_db.commit()
    with pytest.raises(CanonicalProjectionError):
        project_from_life_intent(superuser_db, owner_id=owner.id, life_intent_id=uuid.uuid4())


def test_project_from_mainai_goal_raises_for_nonexistent_id(superuser_db):
    owner = _user(superuser_db)
    superuser_db.commit()
    with pytest.raises(CanonicalProjectionError):
        project_from_mainai_goal(superuser_db, owner_id=owner.id, goal_id=uuid.uuid4())


def test_project_from_life_intent_cross_owner_leakage_impossible(superuser_db):
    owner_a = _user(superuser_db)
    owner_b = _user(superuser_db)
    life_intent = _life_intent(superuser_db, owner_a, state="active")
    superuser_db.commit()

    with pytest.raises(CanonicalProjectionError):
        project_from_life_intent(superuser_db, owner_id=owner_b.id, life_intent_id=life_intent.id)


def test_project_from_mainai_goal_cross_owner_leakage_impossible(superuser_db):
    """get_goal() itself performs no ownership check (a bare db.get() by primary key) -- this
    proves project_from_mainai_goal()'s OWN explicit owner_id check is real and load-bearing,
    not merely inherited for free from the thing it wraps."""
    owner_a = _user(superuser_db)
    owner_b = _user(superuser_db)
    goal = _goal(superuser_db, owner_a)
    superuser_db.commit()

    with pytest.raises(CanonicalProjectionError):
        project_from_mainai_goal(superuser_db, owner_id=owner_b.id, goal_id=goal.id)


def test_project_from_mainai_goal_cross_owner_three_check(superuser_db):
    """Three-check: confirm that if project_from_mainai_goal()'s own owner_id check were
    removed, the cross-owner call above WOULD succeed (proving get_goal() really doesn't
    check ownership on its own, so the guard in canonical_projection.py is doing real work)."""
    from app.mainai_execution.planner import get_goal

    owner_a = _user(superuser_db)
    owner_b = _user(superuser_db)
    goal = _goal(superuser_db, owner_a)
    superuser_db.commit()

    # get_goal() alone, with no owner check, DOES return owner_a's goal when asked by owner_b
    # -- this is exactly the gap project_from_mainai_goal()'s own check exists to close.
    unchecked = get_goal(superuser_db, goal.id)
    assert unchecked.owner_id == owner_a.id != owner_b.id


# --- Read-only: neither projection function ever writes to the source table. ---------------


def test_project_from_life_intent_never_writes_to_the_source_row(superuser_db):
    owner = _user(superuser_db)
    life_intent = _life_intent(superuser_db, owner, state="active")
    superuser_db.commit()
    updated_before = life_intent.updated_at

    project_from_life_intent(superuser_db, owner_id=owner.id, life_intent_id=life_intent.id)
    superuser_db.commit()
    superuser_db.expire_all()

    from app.models.life_intent import LifeIntent

    reloaded_row = superuser_db.get(LifeIntent, life_intent.id)
    assert reloaded_row.updated_at == updated_before
    assert reloaded_row.state == "active"


def test_project_from_mainai_goal_never_writes_to_the_source_row(superuser_db):
    owner = _user(superuser_db)
    goal = _goal(superuser_db, owner, status=MainAIGoalStatus.running)
    superuser_db.commit()
    status_before = goal.status

    project_from_mainai_goal(superuser_db, owner_id=owner.id, goal_id=goal.id)
    superuser_db.commit()
    superuser_db.expire_all()

    from app.models.mainai_execution import MainAIGoal

    reloaded_row = superuser_db.get(MainAIGoal, goal.id)
    assert reloaded_row.status == status_before


# --- refresh_from_canonical() + OLD GOAL != CURRENT GOAL. -----------------------------------


def test_refresh_from_canonical_reflects_superseded_life_intent(superuser_db):
    from app.operating_shell import active_intents_for_owner
    from app.operating_shell.canonical_projection import refresh_from_canonical

    owner = _user(superuser_db)
    life_intent = _life_intent(superuser_db, owner, state="active")
    superuser_db.commit()

    projected = project_from_life_intent(superuser_db, owner_id=owner.id, life_intent_id=life_intent.id)
    assert projected.state == IntentState.ACTIVE
    assert projected in active_intents_for_owner((projected,), owner_id=owner.id)

    # The real LifeIntent is independently superseded (a real service call, nothing to do
    # with the projected IntentObject above).
    transition_intent(superuser_db, owner_id=owner.id, intent_id=life_intent.id, state="superseded", reason="replaced by a better plan")
    superuser_db.commit()

    refreshed = refresh_from_canonical(superuser_db, projected)
    assert refreshed.state == IntentState.SUPERSEDED
    # OLD GOAL != CURRENT GOAL: the OLD (pre-refresh) local object is never trusted again --
    # only the refreshed one is queried from here on.
    assert refreshed not in active_intents_for_owner((refreshed,), owner_id=owner.id)


def test_refresh_from_canonical_requires_a_real_canonical_link(superuser_db):
    from app.operating_shell.canonical_projection import refresh_from_canonical
    from app.operating_shell.intent import create_intent_from_expression

    owner = _user(superuser_db)
    local_only = create_intent_from_expression(owner_id=owner.id, title="scratch", raw_user_expression="something")
    assert local_only.canonical_kind == CanonicalKind.NONE
    with pytest.raises(CanonicalProjectionError):
        refresh_from_canonical(superuser_db, local_only)
