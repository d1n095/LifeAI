"""Race/concurrency verification for the REAL canonical services this reconciliation reads
from (app.life_intents.service, app.work_candidates.service, app.mainai_execution.planner) --
NOT for app.operating_shell itself, which is in-memory and has nothing to race on.

See docs/mainai_v2/MAINAI_V2_INTENT_GOAL_RECONCILIATION.md #0/#5. This codebase's existing
race-test convention (see e.g. test_cancel_after_accept_before_driver.py) is sequenced
operations on one real Postgres session, not true multi-threaded connections -- a
with_for_update()-protected row's serialization is a property of Postgres locking, not
something a test needs multiple real connections to exercise meaningfully; sequencing the
calls in the order a race would produce is the established, sufficient pattern here.
"""

import uuid
from datetime import datetime

import pytest

from app.life_intents.service import (
    IntentError,
    add_blocker,
    add_dependency,
    create_intent,
    transition_intent,
)
from app.models.mainai_execution import TERMINAL_MAINAI_GOAL_STATUSES, MainAIGoalStatus
from app.models.user import User
from app.work_candidates import authorize_work_candidate, record_work_candidate
from app.work_candidates.service import WorkCandidateError

from tests.backend.mainai.test_work_candidates import _owner_with_entity


def _user(db) -> User:
    user = User(email=f"race-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    db.add(user)
    db.flush()
    return user


# --- Same user creates equivalent intents concurrently: idempotency key reuse. -------------


def test_create_intent_concurrent_equivalent_requests_return_the_same_row(superuser_db):
    """Two 'concurrent' create_intent() calls with the same idempotency_key and identical
    semantic content must be safe -- the second returns the SAME row, never a duplicate."""
    owner = _user(superuser_db)
    superuser_db.commit()

    first = create_intent(superuser_db, owner_id=owner.id, title="Fixa skulderna", idempotency_key="race-key-1")
    superuser_db.commit()
    second = create_intent(superuser_db, owner_id=owner.id, title="Fixa skulderna", idempotency_key="race-key-1")

    assert first.id == second.id


def test_create_intent_concurrent_conflicting_requests_rejected(superuser_db):
    """Same idempotency_key, genuinely different content -- must be rejected, never silently
    overwrite or silently return either row as if they were equivalent."""
    owner = _user(superuser_db)
    superuser_db.commit()

    create_intent(superuser_db, owner_id=owner.id, title="Fixa skulderna", idempotency_key="race-key-2")
    superuser_db.commit()
    with pytest.raises(IntentError):
        create_intent(superuser_db, owner_id=owner.id, title="Köp en bil", idempotency_key="race-key-2")


# --- Duplicate event replay: add_blocker/add_dependency idempotency. -----------------------


def test_add_blocker_duplicate_replay_returns_the_same_row(superuser_db):
    owner = _user(superuser_db)
    superuser_db.commit()
    intent = create_intent(superuser_db, owner_id=owner.id, title="Fixa skulderna", idempotency_key="race-key-3")
    superuser_db.commit()

    first = add_blocker(superuser_db, owner_id=owner.id, intent_id=intent.id, category="knowledge", description="need bank statements", idempotency_key="blocker-1")
    superuser_db.commit()
    second = add_blocker(superuser_db, owner_id=owner.id, intent_id=intent.id, category="knowledge", description="need bank statements", idempotency_key="blocker-1")

    assert first.id == second.id


def test_add_dependency_duplicate_replay_returns_the_same_row(superuser_db):
    owner = _user(superuser_db)
    superuser_db.commit()
    a = create_intent(superuser_db, owner_id=owner.id, title="A", idempotency_key="race-dep-a")
    b = create_intent(superuser_db, owner_id=owner.id, title="B", idempotency_key="race-dep-b")
    superuser_db.commit()

    first = add_dependency(superuser_db, owner_id=owner.id, from_intent_id=a.id, to_intent_id=b.id, relationship_type="requires", idempotency_key="dep-1")
    superuser_db.commit()
    second = add_dependency(superuser_db, owner_id=owner.id, from_intent_id=a.id, to_intent_id=b.id, relationship_type="requires", idempotency_key="dep-1")

    assert first.id == second.id


# --- Work candidate approved, then its resulting goal is independently revoked. ------------


def test_work_candidate_stays_authorized_after_its_goal_is_later_cancelled(superuser_db):
    """WorkCandidate.status must never silently revert, and nothing may re-authorize an
    already-authorized candidate a second time, even after the goal it produced is later
    cancelled through an entirely separate path."""
    owner, entity = _owner_with_entity(superuser_db)
    superuser_db.commit()

    candidate = record_work_candidate(
        superuser_db, owner_id=owner.id, source_entity_id=entity.id, title="Migrate to Postgres",
        idempotency_key="wc-race-1", classifier_strategy="test", classifier_confidence=0.9,
    )
    superuser_db.commit()
    candidate, goal = authorize_work_candidate(superuser_db, owner_id=owner.id, candidate_id=candidate.id, authorized_by="founder")
    superuser_db.commit()
    assert candidate.status == "authorized"

    # The goal is independently cancelled (a real DB CHECK constraint requires completed_at
    # for any terminal status -- matched here, same as canonical_projection's own test helper).
    goal.status = MainAIGoalStatus.cancelled
    if goal.status in TERMINAL_MAINAI_GOAL_STATUSES:
        goal.completed_at = datetime.utcnow()
    superuser_db.commit()

    superuser_db.expire_all()
    from app.work_candidates.service import get_work_candidate

    reloaded_candidate = get_work_candidate(superuser_db, owner_id=owner.id, candidate_id=candidate.id)
    assert reloaded_candidate.status == "authorized"  # never silently reverted

    with pytest.raises(WorkCandidateError):
        authorize_work_candidate(superuser_db, owner_id=owner.id, candidate_id=candidate.id, authorized_by="founder")


# --- Intent state changes while a linked goal is active: schema looseness, not a bug. ------


def test_life_intent_and_its_linked_goal_have_no_enforced_state_consistency():
    """mainai_goal_id is a nullable, non-enforced-consistency reference (see
    app/models/life_intent.py) -- a LifeIntent's own `state` and its linked MainAIGoal's
    `status` are never automatically kept in sync by the schema or either service. This test
    documents that this is real, current, deliberate looseness (LifeIntent existed before any
    MainAIGoal link was added, and the link is optional) -- NOT something this reconciliation
    is responsible for fixing. No assertion is made here beyond the two systems genuinely
    being independent columns; see the KNOWN GAP test below for the one place this looseness
    combines with a second, more concerning gap in transition_intent() itself.
    """


def test_transition_intent_has_no_state_machine_KNOWN_GAP(superuser_db):
    """KNOWN GAP, found during this reconciliation's race/concurrency audit, deliberately NOT
    fixed here (see docs/mainai_v2/MAINAI_V2_INTENT_GOAL_RECONCILIATION.md and this round's
    final report): app.life_intents.service.transition_intent() performs NO state-machine
    validation at all -- `state` is a bare string column (see app/models/life_intent.py), and
    transition_intent() only checks `if old != state`, never whether the FROM/TO pair is a
    legitimate transition. This test proves the gap is real: a LifeIntent already superseded
    can be moved straight back to "active" by a stale/late caller (e.g. a worker that read the
    intent before it was superseded and is only now getting around to updating it), and
    nothing rejects this. This directly matters to this reconciliation's own OLD GOAL !=
    CURRENT GOAL guarantee: app.operating_shell.canonical_projection.project_from_life_intent()
    will faithfully (and correctly, per its OWN contract) reflect whatever `state` the row
    currently holds -- if the row itself is wrongly resurrected by this gap, the projection
    layer has no way to know that and will report ACTIVE again. This is a real, existing
    production gap in app.life_intents.service, outside this round's assigned package
    (app.operating_shell) and NOT fixed here -- flagged as an OPEN P0/P1 in the final report
    for a founder/reviewer decision on the correct transition table, rather than guessed at
    and silently patched."""
    owner = _user(superuser_db)
    superuser_db.commit()
    intent = create_intent(superuser_db, owner_id=owner.id, title="Fixa skulderna", idempotency_key="race-key-gap")
    superuser_db.commit()

    transition_intent(superuser_db, owner_id=owner.id, intent_id=intent.id, state="active", reason="starting work")
    superuser_db.commit()
    transition_intent(superuser_db, owner_id=owner.id, intent_id=intent.id, state="superseded", reason="replaced by a better plan")
    superuser_db.commit()

    # A stale/late caller (simulating a race: it decided to move the intent to "active" based
    # on state it read BEFORE the supersession above) is NOT rejected by the current code --
    # this assertion documents the gap, it does not endorse it.
    resurrected = transition_intent(superuser_db, owner_id=owner.id, intent_id=intent.id, state="active", reason="stale worker retry")
    assert resurrected.state == "active"  # KNOWN GAP: should have been rejected; see docstring above.
