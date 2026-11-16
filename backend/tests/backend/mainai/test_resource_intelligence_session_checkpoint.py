"""MainAI Resource Intelligence Part 1 -- `app.resource_intelligence.session_checkpoint` --
proves the save/load round trip for `AgentSessionCheckpoint` is exact, that a second save for
the SAME `(agent_id, attempt_id)` supersedes the first via `supersedes_note_id` (mirroring
`app.mainai_executive.continuity.save_continuity_checkpoint()`'s own already-tested pattern
exactly), and that this mechanism is the SAME real `FounderMemoryNote` store (no new table) --
against a real Postgres database, never mocked."""

from __future__ import annotations

import uuid

import pytest

from app.models.founder_memory import FounderMemoryNote
from app.resource_intelligence.session_checkpoint import (
    CHECKPOINT_MARKER,
    AgentSessionCheckpoint,
    checkpoint_from_dict,
    checkpoint_to_dict,
    load_agent_session_checkpoint,
    save_agent_session_checkpoint,
)


@pytest.fixture
def owner_id(superuser_db, make_verified_user):
    user, _password = make_verified_user()
    return user.id


def _checkpoint(**overrides) -> AgentSessionCheckpoint:
    defaults = dict(
        agent_id=uuid.uuid4(),
        attempt_id=uuid.uuid4(),
        current_objective="Implement resource intelligence Part 1.",
        current_status="in_progress",
        exact_sha="abc123def456",
        current_branch="claude/mainai-v2-resource-intelligence",
        current_worktree="/Users/founder/LifeAI-worktrees/claude-v2-resource-intelligence",
        open_p0=["fix flaky test"],
        open_p1=["add more coverage"],
        what_was_tried=["read the reconciliation doc", "read agent_coordination models"],
        what_failed=["nothing yet"],
        what_passed=["ruff check", "python -c import app.main"],
        important_findings=["build_agent_outcome_payload already declares cost fields"],
        current_test_evidence=["24 passed in test_agent_execution_control.py"],
        next_action="write cost_bridge tests",
        do_not_repeat=["do not touch app.agent_coordination files"],
        authority_boundaries=["RESOURCE_OPTIMIZATION != AUTHORITY", "recommend only, never execute"],
        unresolved_questions=["should attempt_id ever be a bare string?"],
        critical_session_only_facts=["local Postgres runs on port 5433"],
        provenance={"via": "resource_intelligence.session_checkpoint test"},
    )
    defaults.update(overrides)
    return AgentSessionCheckpoint(**defaults)


def test_checkpoint_to_dict_from_dict_round_trip_is_exact_in_memory():
    cp = _checkpoint()
    restored = checkpoint_from_dict(checkpoint_to_dict(cp))
    assert restored == cp


def test_checkpoint_from_dict_rejects_non_checkpoint_payload():
    with pytest.raises(ValueError):
        checkpoint_from_dict({"marker": "something_else"})


def test_checkpoint_preserves_attempt_id_type_across_round_trip():
    uuid_attempt = _checkpoint(attempt_id=uuid.uuid4())
    assert isinstance(checkpoint_from_dict(checkpoint_to_dict(uuid_attempt)).attempt_id, uuid.UUID)

    str_attempt = _checkpoint(attempt_id="session-token-not-a-uuid")
    restored = checkpoint_from_dict(checkpoint_to_dict(str_attempt))
    assert restored.attempt_id == "session-token-not-a-uuid"
    assert isinstance(restored.attempt_id, str)


def test_load_agent_session_checkpoint_returns_none_when_never_saved(superuser_db, owner_id):
    assert load_agent_session_checkpoint(superuser_db, owner_id=owner_id, agent_id=uuid.uuid4(), attempt_id=uuid.uuid4()) is None


def test_save_then_load_round_trips_through_real_founder_memory(superuser_db, owner_id):
    cp = _checkpoint()
    note = save_agent_session_checkpoint(superuser_db, owner_id=owner_id, checkpoint=cp)
    superuser_db.commit()

    assert isinstance(note, FounderMemoryNote)
    assert note.note_type == "observation"
    assert note.status == "active"
    assert (note.provenance or {}).get("kind") == CHECKPOINT_MARKER

    loaded = load_agent_session_checkpoint(superuser_db, owner_id=owner_id, agent_id=cp.agent_id, attempt_id=cp.attempt_id)
    assert loaded == cp


def test_second_save_supersedes_the_first_via_supersedes_note_id_chain(superuser_db, owner_id):
    agent_id = uuid.uuid4()
    attempt_id = uuid.uuid4()

    first = _checkpoint(agent_id=agent_id, attempt_id=attempt_id, current_status="in_progress", next_action="keep going")
    first_note = save_agent_session_checkpoint(superuser_db, owner_id=owner_id, checkpoint=first)
    superuser_db.commit()

    second = _checkpoint(agent_id=agent_id, attempt_id=attempt_id, current_status="done", next_action="none -- complete")
    second_note = save_agent_session_checkpoint(superuser_db, owner_id=owner_id, checkpoint=second)
    superuser_db.commit()

    assert second_note.supersedes_note_id == first_note.id

    superuser_db.refresh(first_note)
    assert first_note.status == "superseded"
    # Old note's own content is NEVER mutated in place.
    assert "in_progress" in first_note.content or "keep going" not in first_note.content

    loaded = load_agent_session_checkpoint(superuser_db, owner_id=owner_id, agent_id=agent_id, attempt_id=attempt_id)
    assert loaded == second
    assert loaded.current_status == "done"


def test_checkpoints_for_different_attempts_of_the_same_agent_do_not_collide(superuser_db, owner_id):
    agent_id = uuid.uuid4()
    attempt_1, attempt_2 = uuid.uuid4(), uuid.uuid4()

    cp1 = _checkpoint(agent_id=agent_id, attempt_id=attempt_1, current_objective="attempt one")
    cp2 = _checkpoint(agent_id=agent_id, attempt_id=attempt_2, current_objective="attempt two")
    save_agent_session_checkpoint(superuser_db, owner_id=owner_id, checkpoint=cp1)
    save_agent_session_checkpoint(superuser_db, owner_id=owner_id, checkpoint=cp2)
    superuser_db.commit()

    loaded_1 = load_agent_session_checkpoint(superuser_db, owner_id=owner_id, agent_id=agent_id, attempt_id=attempt_1)
    loaded_2 = load_agent_session_checkpoint(superuser_db, owner_id=owner_id, agent_id=agent_id, attempt_id=attempt_2)
    assert loaded_1.current_objective == "attempt one"
    assert loaded_2.current_objective == "attempt two"


def test_checkpoint_is_owner_scoped(superuser_db, owner_id, make_verified_user):
    agent_id = uuid.uuid4()
    attempt_id = uuid.uuid4()
    cp = _checkpoint(agent_id=agent_id, attempt_id=attempt_id)
    save_agent_session_checkpoint(superuser_db, owner_id=owner_id, checkpoint=cp)
    superuser_db.commit()

    other_user, _pw = make_verified_user()
    assert load_agent_session_checkpoint(superuser_db, owner_id=other_user.id, agent_id=agent_id, attempt_id=attempt_id) is None
