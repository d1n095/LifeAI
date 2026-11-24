"""Phase 2 acceptance: path-scoped ownership over the real coordination tables.

These tests use the real Postgres test database and the real `agent_work_assignments` /
`agent_scope_leases` tables. They create assignments through the existing coordination
service (never a parallel fixture model) and prove:

- a path overlap is reported with the assignment, agent, role and live lease,
- the directory-boundary rule is the existing one (`backend/app/dev` is not
  `backend/app/development_x`),
- terminal assignments and expired leases are not owners,
- an unscoped assignment is `unclear`, never silently "owned",
- another owner's assignment is never visible through this owner's query.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from app.agent_coordination.service import (
    acquire_lease,
    create_work_assignment,
    register_agent,
    transition_status,
)
from app.context_atlas.ownership import CLEAR, OWNED, UNCLEAR, UNKNOWN, find_work_owners
from app.mainai_execution.planner import create_goal
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
    paths: list[str],
    agent_key: str | None = None,
    role: str = "builder",
    mode: str = "read_write",
):
    goal = create_goal(
        db,
        owner_id=owner.id,
        title=f"ownership {uuid.uuid4().hex[:8]}",
        original_instruction="ownership test",
        created_by="founder",
        approval_policy="autonomous_development_work",
    )
    agent = register_agent(
        db,
        agent_key=agent_key or f"agent-{uuid.uuid4().hex[:8]}",
        display_name="test agent",
        adapter_kind="cli",
        supports_read=True,
        supports_write=mode == "read_write",
    )
    assignment = create_work_assignment(
        db,
        owner_id=owner.id,
        goal_id=goal.id,
        task_id=None,
        agent_id=agent.id,
        role=role,
        read_write_mode=mode,
        repository_identity="lifeai",
        allowed_paths=paths,
        requested_by="test",
    )
    db.commit()
    return assignment, agent


def test_path_overlap_reports_the_real_assignment_and_live_lease(
    superuser_db, db_session, make_verified_user
):
    owner, _ = make_verified_user()
    assignment, agent = _assignment(
        superuser_db, owner, paths=["backend/app/context_atlas/**"]
    )
    acquire_lease(
        superuser_db,
        assignment=assignment,
        agent_id=agent.id,
        branch="agent/atlas",
        worktree_path="/tmp/wt-atlas",
        allowed_paths=["backend/app/context_atlas/**"],
        mode="read_write",
        ttl_seconds=3600,
    )
    transition_status(superuser_db, assignment=assignment, new_status="running")
    superuser_db.commit()

    _scope(db_session, owner)
    report = find_work_owners(
        db_session,
        owner_id=owner.id,
        paths=["backend/app/context_atlas/ownership.py"],
    )

    assert report.status == OWNED
    assert len(report.owners) == 1
    found = report.owners[0]
    assert found.assignment_id == str(assignment.id)
    assert found.agent_key == agent.agent_key
    assert found.status == "running"
    assert found.branch == "agent/atlas"
    assert found.lease_generation == 1
    assert found.conflicting_paths == ("backend/app/context_atlas/ownership.py",)
    assert report.execution_authorized is False
    assert any(agent.agent_key in ref for ref in report.evidence_refs())


def test_directory_boundary_is_not_a_raw_string_prefix(
    superuser_db, db_session, make_verified_user
):
    owner, _ = make_verified_user()
    _assignment(superuser_db, owner, paths=["backend/app/dev"])

    _scope(db_session, owner)
    report = find_work_owners(
        db_session, owner_id=owner.id, paths=["backend/app/development_x/module.py"]
    )

    assert report.status == CLEAR
    assert report.owners == ()


def test_terminal_assignment_is_not_an_owner(
    superuser_db, db_session, make_verified_user
):
    owner, _ = make_verified_user()
    assignment, _ = _assignment(superuser_db, owner, paths=["backend/app/rag/**"])
    transition_status(superuser_db, assignment=assignment, new_status="cancelled")
    superuser_db.commit()

    _scope(db_session, owner)
    report = find_work_owners(
        db_session, owner_id=owner.id, paths=["backend/app/rag/retrieve.py"]
    )

    assert report.status == CLEAR
    assert report.owners == ()


def test_expired_lease_is_reported_without_a_live_generation(
    superuser_db, db_session, make_verified_user
):
    owner, _ = make_verified_user()
    assignment, agent = _assignment(superuser_db, owner, paths=["backend/app/memory/**"])
    acquire_lease(
        superuser_db,
        assignment=assignment,
        agent_id=agent.id,
        branch="agent/memory",
        worktree_path="/tmp/wt-memory",
        allowed_paths=["backend/app/memory/**"],
        mode="read_write",
        ttl_seconds=-1,
    )
    superuser_db.commit()

    _scope(db_session, owner)
    report = find_work_owners(
        db_session, owner_id=owner.id, paths=["backend/app/memory/service.py"]
    )

    assert report.status == OWNED  # the assignment still exists
    assert report.owners[0].lease_generation is None
    assert "no live lease" in report.owners[0].evidence_ref()


def test_unscoped_assignment_is_unclear_not_owned(
    superuser_db, db_session, make_verified_user
):
    owner, _ = make_verified_user()
    _assignment(superuser_db, owner, paths=[])

    _scope(db_session, owner)
    report = find_work_owners(
        db_session, owner_id=owner.id, paths=["backend/app/routers/chat.py"]
    )

    assert report.status == UNCLEAR
    assert report.owners[0].unscoped is True
    assert report.owners[0].conflicting_paths == ()


def test_empty_request_is_unknown_not_clear(
    superuser_db, db_session, make_verified_user
):
    owner, _ = make_verified_user()
    _scope(db_session, owner)
    report = find_work_owners(db_session, owner_id=owner.id, paths=[])
    assert report.status == UNKNOWN
    assert report.execution_authorized is False


def test_another_owners_assignment_is_never_visible(
    superuser_db, db_session, make_verified_user
):
    owner_a, _ = make_verified_user()
    owner_b, _ = make_verified_user()
    _assignment(superuser_db, owner_a, paths=["backend/app/rag/**"])

    _scope(db_session, owner_b)
    report_b = find_work_owners(
        db_session, owner_id=owner_b.id, paths=["backend/app/rag/retrieve.py"]
    )
    assert report_b.status == CLEAR

    _scope(db_session, owner_a)
    report_a = find_work_owners(
        db_session, owner_id=owner_a.id, paths=["backend/app/rag/retrieve.py"]
    )
    assert report_a.status == OWNED


def test_read_only_assignment_is_still_an_owner(
    superuser_db, db_session, make_verified_user
):
    owner, _ = make_verified_user()
    _assignment(
        superuser_db,
        owner,
        paths=["backend/app/context_atlas/**"],
        role="reviewer",
        mode="read_only",
    )

    _scope(db_session, owner)
    report = find_work_owners(
        db_session, owner_id=owner.id, paths=["backend/app/context_atlas/identity.py"]
    )

    assert report.status == OWNED
    assert report.owners[0].read_write_mode == "read_only"
