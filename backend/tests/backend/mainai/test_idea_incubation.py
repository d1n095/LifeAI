"""app.mainai_executive.idea_incubation -- migration 0069's widened IntelligenceIdea.
disposition vocabulary (incubating/planned/ready/later) plus explicit transition functions,
mirroring app.life_intents.service's LIFE_INTENT_TRANSITIONS test program (see
tests/backend/context/test_life_intent_state_machine.py). GOOD IDEA != ACTIVE JOB: the
adversarial guard test proves promote_ready_idea_to_work_candidate() cannot skip straight to
authorized work, and that authorize_work_candidate() is never reachable from this module.

CRITICAL: intelligence_ideas is DB-enforced append-only (migration 0038's own
trg_intelligence_ideas_deny_mutation trigger) -- confirmed directly by
test_transitioning_never_issues_an_update_the_old_row_is_immutable below, which is the
adversarial proof for THIS module's own append-only-respecting design (transitions insert a
new row + link, never UPDATE)."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.exc import InternalError
import pytest

from app.intelligence_governance.service import record_execution, record_idea
from app.mainai_executive.idea_incubation import (
    IDEA_DISPOSITION_TRANSITIONS,
    TERMINAL_IDEA_DISPOSITIONS,
    IdeaIncubationError,
    IdeaNotReadyError,
    InvalidIdeaTransitionError,
    StaleIdeaTransitionError,
    TerminalIdeaStateError,
    promote_ready_idea_to_work_candidate,
    resolve_current_idea,
    transition_idea_disposition,
)
from app.models.intelligence_governance import IntelligenceIdea
from app.models.mainai_execution import MainAIGoal, MainAIPlan, MainAITask
from app.models.user import User
from app.models.work_candidate import WorkCandidate
from app.work_candidates.service import authorize_work_candidate

from tests.backend.mainai.test_work_candidates import _owner_with_entity


def _owner(db) -> User:
    user = User(email=f"idea-inc-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    db.add(user)
    db.flush()
    return user


def _task(db, owner_id):
    goal = MainAIGoal(owner_id=owner_id, title="p", original_instruction="s", created_by="test")
    db.add(goal)
    db.flush()
    plan = MainAIPlan(owner_id=owner_id, goal_id=goal.id, version=1, rationale="test", created_by="test")
    db.add(plan)
    db.flush()
    task = MainAITask(owner_id=owner_id, goal_id=goal.id, plan_id=plan.id, description="d", task_type="repo_edit")
    db.add(task)
    db.flush()
    return task


def _idea(db, owner, *, disposition="unknown", content="an idea", idempotency_key=None):
    task = _task(db, owner.id)
    execution = record_execution(db, owner_id=owner.id, task_id=task.id, idempotency_key=f"exec-{uuid.uuid4()}", role="builder")
    return record_idea(
        db, owner_id=owner.id, execution_id=execution.id, idea_kind="idea", content=content,
        disposition=disposition,
        disposition_reason="seed" if disposition in {"accepted", "rejected"} else None,
        idempotency_key=idempotency_key or f"idea-{uuid.uuid4()}",
    )


# --- 1. Valid forward transitions (table-driven). -------------------------------------------


@pytest.mark.parametrize(
    "from_state,to_state",
    [
        ("unknown", "incubating"),
        ("unknown", "planned"),
        ("unknown", "later"),
        ("incubating", "planned"),
        ("incubating", "later"),
        ("planned", "ready"),
        ("planned", "incubating"),
        ("ready", "accepted"),
        ("ready", "planned"),
        ("later", "incubating"),
        ("later", "planned"),
    ],
)
def test_valid_forward_transitions_succeed(superuser_db, from_state, to_state):
    owner = _owner(superuser_db)
    superuser_db.commit()
    idea = _idea(superuser_db, owner, disposition=from_state)
    superuser_db.commit()
    reason = "moving forward" if to_state in {"accepted", "rejected"} else None
    result = transition_idea_disposition(
        superuser_db, owner_id=owner.id, idea_id=idea.id, disposition=to_state,
        idempotency_key=f"trans-{uuid.uuid4()}", reason=reason,
    )
    assert result.disposition == to_state
    assert result.id != idea.id  # a NEW row, never an UPDATE of the old one
    assert result.execution_id == idea.execution_id


def test_transition_appends_new_row_and_leaves_the_old_one_byte_for_byte_unchanged(superuser_db):
    owner = _owner(superuser_db)
    superuser_db.commit()
    idea = _idea(superuser_db, owner, disposition="unknown")
    superuser_db.commit()
    transition_idea_disposition(
        superuser_db, owner_id=owner.id, idea_id=idea.id, disposition="incubating",
        idempotency_key=f"trans-{uuid.uuid4()}",
    )
    superuser_db.commit()
    superuser_db.expire_all()
    original = superuser_db.execute(select(IntelligenceIdea).where(IntelligenceIdea.id == idea.id)).scalar_one()
    assert original.disposition == "unknown"  # the ORIGINAL row is untouched
    all_rows = superuser_db.execute(
        select(IntelligenceIdea).where(IntelligenceIdea.execution_id == idea.execution_id)
    ).scalars().all()
    assert len(all_rows) == 2


def test_resolve_current_idea_follows_the_chain(superuser_db):
    owner = _owner(superuser_db)
    superuser_db.commit()
    idea = _idea(superuser_db, owner, disposition="unknown")
    superuser_db.commit()
    step1 = transition_idea_disposition(
        superuser_db, owner_id=owner.id, idea_id=idea.id, disposition="incubating",
        idempotency_key=f"trans-{uuid.uuid4()}",
    )
    superuser_db.commit()
    step2 = transition_idea_disposition(
        superuser_db, owner_id=owner.id, idea_id=step1.id, disposition="planned",
        idempotency_key=f"trans-{uuid.uuid4()}",
    )
    superuser_db.commit()

    assert resolve_current_idea(superuser_db, owner_id=owner.id, idea_id=idea.id).id == step2.id
    assert resolve_current_idea(superuser_db, owner_id=owner.id, idea_id=step1.id).id == step2.id
    assert resolve_current_idea(superuser_db, owner_id=owner.id, idea_id=step2.id).id == step2.id


def test_transition_table_has_no_undocumented_terminal_exit():
    for state in TERMINAL_IDEA_DISPOSITIONS:
        assert IDEA_DISPOSITION_TRANSITIONS[state] == set()


@pytest.mark.parametrize("terminal_state", sorted(TERMINAL_IDEA_DISPOSITIONS))
def test_terminal_dispositions_reject_all_generic_transitions(superuser_db, terminal_state):
    owner = _owner(superuser_db)
    superuser_db.commit()
    idea = _idea(superuser_db, owner, disposition=terminal_state)
    superuser_db.commit()
    with pytest.raises(TerminalIdeaStateError):
        transition_idea_disposition(
            superuser_db, owner_id=owner.id, idea_id=idea.id, disposition="incubating",
            idempotency_key=f"trans-{uuid.uuid4()}",
        )
    superuser_db.expire_all()
    reloaded = superuser_db.execute(select(IntelligenceIdea).where(IntelligenceIdea.id == idea.id)).scalar_one()
    assert reloaded.disposition == terminal_state, "a rejected transition must never have touched the row"
    no_new_rows = superuser_db.execute(
        select(IntelligenceIdea).where(IntelligenceIdea.execution_id == idea.execution_id)
    ).scalars().all()
    assert len(no_new_rows) == 1, "a rejected transition must never have inserted a new row either"


def test_unrecognized_disposition_rejected(superuser_db):
    owner = _owner(superuser_db)
    superuser_db.commit()
    idea = _idea(superuser_db, owner, disposition="unknown")
    superuser_db.commit()
    with pytest.raises(InvalidIdeaTransitionError):
        transition_idea_disposition(
            superuser_db, owner_id=owner.id, idea_id=idea.id, disposition="in_progress_typo",
            idempotency_key=f"trans-{uuid.uuid4()}",
        )


def test_illegal_edge_rejected(superuser_db):
    """incubating -> accepted is not in the transition table -- must go through ready."""
    owner = _owner(superuser_db)
    superuser_db.commit()
    idea = _idea(superuser_db, owner, disposition="incubating")
    superuser_db.commit()
    with pytest.raises(InvalidIdeaTransitionError):
        transition_idea_disposition(
            superuser_db, owner_id=owner.id, idea_id=idea.id, disposition="accepted",
            idempotency_key=f"trans-{uuid.uuid4()}", reason="skip",
        )


def test_accepted_rejected_require_reason(superuser_db):
    owner = _owner(superuser_db)
    superuser_db.commit()
    idea = _idea(superuser_db, owner, disposition="ready")
    superuser_db.commit()
    with pytest.raises(IdeaIncubationError):
        transition_idea_disposition(
            superuser_db, owner_id=owner.id, idea_id=idea.id, disposition="accepted",
            idempotency_key=f"trans-{uuid.uuid4()}",
        )


def test_same_disposition_call_is_a_harmless_noop_no_new_row(superuser_db):
    owner = _owner(superuser_db)
    superuser_db.commit()
    idea = _idea(superuser_db, owner, disposition="incubating")
    superuser_db.commit()
    result = transition_idea_disposition(
        superuser_db, owner_id=owner.id, idea_id=idea.id, disposition="incubating",
        idempotency_key=f"trans-{uuid.uuid4()}",
    )
    assert result.id == idea.id
    all_rows = superuser_db.execute(
        select(IntelligenceIdea).where(IntelligenceIdea.execution_id == idea.execution_id)
    ).scalars().all()
    assert len(all_rows) == 1


def test_stale_caller_rejected_via_expected_current_state(superuser_db):
    owner = _owner(superuser_db)
    superuser_db.commit()
    idea = _idea(superuser_db, owner, disposition="incubating")
    superuser_db.commit()
    transition_idea_disposition(
        superuser_db, owner_id=owner.id, idea_id=idea.id, disposition="planned",
        idempotency_key=f"trans-{uuid.uuid4()}",
    )
    superuser_db.commit()
    with pytest.raises(StaleIdeaTransitionError):
        transition_idea_disposition(
            superuser_db, owner_id=owner.id, idea_id=idea.id, disposition="ready",
            idempotency_key=f"trans-{uuid.uuid4()}", expected_current_state="incubating",
        )


def test_transitioning_never_issues_an_update_the_old_row_is_immutable(superuser_db):
    """Adversarial proof of the module's own foundational design constraint: attempting a raw
    UPDATE on intelligence_ideas.disposition (what a naive, LifeIntent-style implementation
    WOULD have done) is rejected by the database itself. If this test ever starts failing
    because Postgres allows the UPDATE, it means the append-only trigger was removed --
    transition_idea_disposition()'s whole design would then need re-review, not just this
    test."""
    owner = _owner(superuser_db)
    superuser_db.commit()
    idea = _idea(superuser_db, owner, disposition="unknown")
    superuser_db.commit()
    idea.disposition = "incubating"
    with pytest.raises(InternalError, match="append-only"):
        superuser_db.flush()
    superuser_db.rollback()


# --- 2. GOOD IDEA != ACTIVE JOB: the structural guard. ---------------------------------------


def test_promote_ready_idea_to_work_candidate_requires_ready(superuser_db):
    """FAILS if the guard is removed/bypassed: an incubating idea must be rejected outright,
    never silently staged as a WorkCandidate."""
    owner, entity = _owner_with_entity(superuser_db)
    superuser_db.commit()
    idea = _idea(superuser_db, owner, disposition="incubating")
    superuser_db.commit()
    with pytest.raises(IdeaNotReadyError):
        promote_ready_idea_to_work_candidate(
            superuser_db, owner_id=owner.id, idea_id=idea.id, source_entity_id=entity.id,
            idempotency_key=f"promo-{uuid.uuid4()}",
        )
    # No WorkCandidate must have been created by the rejected attempt.
    count = superuser_db.execute(select(WorkCandidate).where(WorkCandidate.owner_id == owner.id)).scalars().all()
    assert count == []


def test_promote_ready_idea_creates_only_unreviewed_staged_candidate_never_authorized(superuser_db):
    """The produced WorkCandidate must be 'unreviewed' -- record_work_candidate() (staging)
    only, never authorize_work_candidate() (the only function that can create a MainAIGoal).
    A separate, explicit authorize_work_candidate() call is required to actually authorize it
    -- proving this module cannot itself jump an idea straight into authorized work."""
    owner, entity = _owner_with_entity(superuser_db)
    superuser_db.commit()
    idea = _idea(superuser_db, owner, disposition="ready")
    superuser_db.commit()
    candidate = promote_ready_idea_to_work_candidate(
        superuser_db, owner_id=owner.id, idea_id=idea.id, source_entity_id=entity.id,
        idempotency_key=f"promo-{uuid.uuid4()}",
    )
    superuser_db.commit()
    assert candidate.status == "unreviewed"
    assert candidate.authorized_goal_id is None
    assert candidate.provenance["idea_id"] == str(idea.id)
    assert candidate.provenance["authorized"] is False

    # The SEPARATE, real, existing gate is still the only way to actually authorize it.
    authorized, goal = authorize_work_candidate(
        superuser_db, owner_id=owner.id, candidate_id=candidate.id, authorized_by="founder",
    )
    superuser_db.commit()
    assert authorized.status == "authorized"
    assert authorized.authorized_goal_id == goal.id


def test_idea_incubation_module_never_imports_authorize_work_candidate():
    """Structural: this module has no bound reference to the real authorization gate at all
    (not merely 'doesn't call it this run') -- protects against a future edit silently
    importing the mutating gate. Checks the module's actual namespace, not source text, since
    the module docstring legitimately NAMES authorize_work_candidate() in prose explaining why
    it must never be imported here."""
    import app.mainai_executive.idea_incubation as module
    import app.work_candidates.service as wc_service

    assert not hasattr(module, "authorize_work_candidate")
    assert getattr(module, "record_work_candidate", None) is wc_service.record_work_candidate
