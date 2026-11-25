"""Module → path → owner, over the real coordination tables.

MainAI asks "which agent owns this module?"; the answer must come from the static system map
plus the existing path-scoped ownership, not from a second ownership model.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from app.agent_coordination.service import (
    acquire_lease,
    create_work_assignment,
    register_agent,
    transition_status,
)
from app.context_atlas.ownership import CLEAR, OWNED
from app.context_atlas.runtime import SystemIntelligence
from app.mainai_execution.planner import create_goal
from app.request_context import current_user_id as current_user_id_var

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True, scope="module")
def _apply_execution_privileges():
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges

    apply_mainai_execution_privileges(migration_engine)


@pytest.fixture(scope="module")
def intelligence() -> SystemIntelligence:
    return SystemIntelligence(REPO_ROOT)


def _scope(db, user) -> None:
    current_user_id_var.set(str(user.id))
    db.execute(text("SET LOCAL app.current_user_id = :uid"), {"uid": str(user.id)})


def _assignment(db, owner, *, paths: list[str]):
    goal = create_goal(
        db,
        owner_id=owner.id,
        title=f"module ownership {uuid.uuid4().hex[:8]}",
        original_instruction="module ownership test",
        created_by="founder",
        approval_policy="autonomous_development_work",
    )
    agent = register_agent(
        db,
        agent_key=f"agent-{uuid.uuid4().hex[:8]}",
        display_name="test agent",
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
        allowed_paths=paths,
        requested_by="test",
    )
    db.commit()
    return assignment, agent


def test_module_ownership_resolves_path_and_real_assignment(
    superuser_db, db_session, make_verified_user, intelligence
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
    report = intelligence.module_ownership(
        db_session,
        owner_id=owner.id,
        module="app.context_atlas.recommendation",
    )

    assert report.status == OWNED
    assert report.path == "backend/app/context_atlas/recommendation.py"
    assert report.ownership is not None
    assert report.ownership.owners[0].assignment_id == str(assignment.id)
    assert report.ownership.owners[0].agent_key == agent.agent_key
    assert any(str(assignment.id) in ref for ref in report.evidence)
    assert report.execution_authorized is False


def test_module_outside_the_assignment_scope_is_clear(
    superuser_db, db_session, make_verified_user, intelligence
):
    owner, _ = make_verified_user()
    _assignment(superuser_db, owner, paths=["backend/app/context_atlas/**"])

    _scope(db_session, owner)
    report = intelligence.module_ownership(
        db_session,
        owner_id=owner.id,
        module="app.mainai_executive.investigation",
    )

    assert report.status == CLEAR
    assert report.path == "backend/app/mainai_executive/investigation.py"
    assert report.ownership is not None and report.ownership.owners == ()
    assert report.execution_authorized is False


def test_unknown_module_is_not_found_not_owned(
    superuser_db, db_session, make_verified_user, intelligence
):
    owner, _ = make_verified_user()
    _scope(db_session, owner)

    report = intelligence.module_ownership(
        db_session,
        owner_id=owner.id,
        module="app.context_atlas.does_not_exist",
    )

    assert report.status == "not_found"
    assert report.path is None
    assert report.ownership is None
    assert report.execution_authorized is False
