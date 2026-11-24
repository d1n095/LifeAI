"""E1/E3 acceptance: durable development state and interruption recovery.

Uses the real coordination tables, real leases, real append-only events and the existing
project-memory checkpoint. The tests prove that persisted evidence outranks recollection, that
a running row without a live lease is stale rather than alive, and that two live claims are
ambiguous instead of silently choosing one.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import pytest
from sqlalchemy import text

from app.agent_coordination.service import (
    acquire_lease,
    create_work_assignment,
    register_agent,
    transition_status,
)
from app.context_atlas.development_state import (
    load_development_state,
    recover_development_state,
)
from app.mainai_execution.planner import create_goal
from app.models.agent_coordination import (
    AgentDispatchExecution,
    ExecutionAdapterState,
)
from app.models.project_memory import NoteKind
from app.project_memory import add_note, create_checkpoint
from app.request_context import current_user_id as current_user_id_var


@pytest.fixture(autouse=True, scope="module")
def _apply_execution_privileges():
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges

    apply_mainai_execution_privileges(migration_engine)


def _scope(db, user) -> None:
    current_user_id_var.set(str(user.id))
    db.execute(text("SET LOCAL app.current_user_id = :uid"), {"uid": str(user.id)})


def _assignment(
    db,
    owner,
    *,
    paths: list[str] | None = None,
    branch: str | None = None,
    ttl_seconds: int | None = 3600,
    running: bool = True,
    head_sha: str | None = None,
    base_sha: str | None = None,
):
    goal = create_goal(
        db,
        owner_id=owner.id,
        title=f"state {uuid.uuid4().hex[:8]}",
        original_instruction="recover this durable objective",
        created_by="founder",
        approval_policy="autonomous_development_work",
    )
    agent = register_agent(
        db,
        agent_key=f"state-agent-{uuid.uuid4().hex[:8]}",
        display_name="state agent",
        adapter_kind="cli",
        supports_read=True,
        supports_write=True,
    )
    assignment = create_work_assignment(
        db,
        owner_id=owner.id,
        goal_id=goal.id,
        task_id=None,
        agent_id=agent.id,
        role="builder",
        read_write_mode="read_write",
        repository_identity="lifeai",
        allowed_paths=paths or [f"backend/app/state_{uuid.uuid4().hex[:8]}/**"],
        requested_by="test",
    )
    if branch is not None:
        result = acquire_lease(
            db,
            assignment=assignment,
            agent_id=agent.id,
            branch=branch,
            worktree_path=f"/tmp/{branch.replace('/', '-')}",
            allowed_paths=assignment.allowed_paths,
            mode="read_write",
            ttl_seconds=ttl_seconds,
        )
        assert result.outcome == "ACQUIRED"
    if running:
        transition_status(db, assignment=assignment, new_status="running")
    if head_sha is not None:
        assignment.head_sha = head_sha
    if base_sha is not None:
        assignment.base_sha = base_sha
    db.commit()
    return assignment, agent


def _dispatch(db, owner, assignment) -> AgentDispatchExecution:
    execution = AgentDispatchExecution(
        owner_id=owner.id,
        assignment_id=assignment.id,
        attempt_id=uuid.uuid4(),
        adapter_key="test",
        adapter_state=ExecutionAdapterState.running,
        last_heartbeat_at=datetime.utcnow(),
    )
    db.add(execution)
    db.commit()
    return execution


def test_live_state_prefers_persisted_lease_and_dispatch(superuser_db, db_session, make_verified_user):
    owner, _ = make_verified_user()
    assignment, _ = _assignment(superuser_db, owner, branch="agent/live")
    _dispatch(superuser_db, owner, assignment)

    _scope(db_session, owner)
    state = load_development_state(
        db_session, owner_id=owner.id, assignment_id=assignment.id
    )

    assert state is not None
    assert state.lease_live is True
    assert state.observed is True
    assert state.branch == "agent/live"
    assert state.latest_event_type == "status_changed"
    assert "resume from the last durable event" in state.next_authorized_action
    assert state.execution_authorized is False
    assert any("assignment" in item for item in state.evidence)


def test_running_without_a_live_lease_is_stale_not_alive(superuser_db, db_session, make_verified_user):
    owner, _ = make_verified_user()
    assignment, _ = _assignment(
        superuser_db, owner, branch="agent/expired", ttl_seconds=-1
    )

    _scope(db_session, owner)
    state = load_development_state(
        db_session, owner_id=owner.id, assignment_id=assignment.id
    )
    recovery = recover_development_state(db_session, owner_id=owner.id)

    assert state is not None
    assert state.lease_live is False
    assert state.observed is False
    assert "acquire a scope lease" in state.next_authorized_action
    assert recovery.status == "stale"
    assert recovery.selected_assignment_id == str(assignment.id)
    assert recovery.candidates[0].stale is True
    assert recovery.execution_authorized is False


def test_recovery_selects_the_only_live_assignment(superuser_db, db_session, make_verified_user):
    owner, _ = make_verified_user()
    assignment, _ = _assignment(superuser_db, owner, branch="agent/recover")

    _scope(db_session, owner)
    report = recover_development_state(db_session, owner_id=owner.id)

    assert report.status == "recovered"
    assert report.selected_assignment_id == str(assignment.id)
    assert report.github_observed is False
    assert report.requires_founder is False
    assert report.execution_authorized is False


def test_two_live_claims_are_ambiguous(superuser_db, db_session, make_verified_user):
    owner, _ = make_verified_user()
    _assignment(
        superuser_db,
        owner,
        paths=["backend/app/state_a/**"],
        branch="agent/a",
    )
    _assignment(
        superuser_db,
        owner,
        paths=["backend/app/state_b/**"],
        branch="agent/b",
    )

    _scope(db_session, owner)
    report = recover_development_state(db_session, owner_id=owner.id)

    assert report.status == "ambiguous"
    assert report.selected_assignment_id is None
    assert report.requires_founder is True
    assert len(report.candidates) == 2


def test_branch_filter_selects_the_matching_candidate(superuser_db, db_session, make_verified_user):
    owner, _ = make_verified_user()
    first, _ = _assignment(
        superuser_db, owner, paths=["backend/app/state_a/**"], branch="agent/a"
    )
    _assignment(superuser_db, owner, paths=["backend/app/state_b/**"], branch="agent/b")

    _scope(db_session, owner)
    report = recover_development_state(
        db_session, owner_id=owner.id, branch="agent/a"
    )

    assert report.status == "recovered"
    assert report.selected_assignment_id == str(first.id)


def test_terminal_work_is_not_recovered(superuser_db, db_session, make_verified_user):
    owner, _ = make_verified_user()
    assignment, _ = _assignment(superuser_db, owner, running=False)
    transition_status(superuser_db, assignment=assignment, new_status="cancelled")
    superuser_db.commit()

    _scope(db_session, owner)
    report = recover_development_state(db_session, owner_id=owner.id)

    assert report.status == "none"
    assert report.selected_assignment_id is None


def test_checkpoint_and_open_blocker_are_part_of_the_state(superuser_db, db_session, make_verified_user):
    owner, _ = make_verified_user()
    assignment, _ = _assignment(superuser_db, owner, branch="agent/checkpoint")
    add_note(
        superuser_db,
        kind=NoteKind.blocker,
        content="waiting for the staging restore verification",
        source_type="test",
        source_ref="test:blocker",
        created_by="test",
    )
    checkpoint = create_checkpoint(
        superuser_db,
        summary="resume the state work",
        branch_name="agent/checkpoint",
        open_pr_refs=["#999"],
        created_by="test",
    )

    _scope(db_session, owner)
    state = load_development_state(
        db_session, owner_id=owner.id, assignment_id=assignment.id
    )

    assert state is not None
    assert state.blockers == ("waiting for the staging restore verification",)
    assert state.checkpoint_summary == "resume the state work"
    assert state.checkpoint_sha == checkpoint.git_commit_sha
    assert state.checkpoint_stale is False


def test_local_commits_are_reported_without_claiming_they_were_pushed(superuser_db, db_session, make_verified_user):
    owner, _ = make_verified_user()
    assignment, _ = _assignment(
        superuser_db,
        owner,
        branch="agent/local",
        base_sha="a" * 40,
        head_sha="b" * 40,
    )

    _scope(db_session, owner)
    report = recover_development_state(db_session, owner_id=owner.id)

    candidate = report.candidates[0]
    assert candidate.head_sha == "b" * 40
    assert "push state is unknown" in candidate.reason
    assert report.github_observed is False


def test_another_owners_state_is_never_visible(superuser_db, db_session, make_verified_user):
    owner_a, _ = make_verified_user()
    owner_b, _ = make_verified_user()
    assignment, _ = _assignment(superuser_db, owner_a, branch="agent/a")

    _scope(db_session, owner_b)
    assert (
        load_development_state(
            db_session, owner_id=owner_b.id, assignment_id=assignment.id
        )
        is None
    )
    assert recover_development_state(db_session, owner_id=owner_b.id).status == "none"
